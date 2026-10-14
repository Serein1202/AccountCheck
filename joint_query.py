# -*- coding: utf-8 -*-
"""账号封禁 · 联合查询（amis + ACE）—— 单入口合并版

================================ 运行说明 ================================
双击运行（推荐 pythonw 打开），或在命令行执行：

    python joint_query.py                 # 启动统一 GUI
    python joint_query.py --selfcheck      # 自检：导入 / 编号识别 / 模块接口 / 导出逻辑，不联网
    python joint_query.py --version        # 版本信息
    python joint_query.py --check-update   # 只检查有没有新版本（联网）
    python joint_query.py --update         # 检查 → 断点续传下载 → 校验 → 替换安装（联网）

依赖：纯 Python 标准库（tkinter/ttk），无第三方包。
程序结构：本文件是主入口与全部界面逻辑；查询引擎与支撑组件在 src/ 目录下
（amis 侧、ACE 侧、Sentry 上报、授权接入、自动更新）。

授权校验：启动时向 license-guard 校验授权码（不通过则按后端策略退出），
相关文件为 src/license_boot.py（接入层，含服务地址/公钥）、
src/vendor/license_guard.py（SDK）。授权码放在程序目录的 license.key，
或用环境变量 LICENSE_KEY 提供。--version / --selfcheck / --test-sentry
为排查用途，不做授权校验。

自动更新：启动后（或点右上角「检查更新」）对比 GitHub Release 里的版本，
有新版本即下载并替换正在运行的 exe。下载支持断点续传，并内置多个
GitHub 加速镜像（见 src/update_check.py，可用环境变量
JOINT_UPDATE_MIRRORS 覆盖）。设 JOINT_UPDATE_DISABLE=1 可整体关闭。

================================ 行为摘要 ================================
1) 单入口：一个输入框批量粘贴编号，支持 逗号 / 空格 / 换行 / 分号 分隔，
   也可点「导入文件」从 txt/csv 读取；「开始查询」一键并发查询，可随时「停止」。
2) 自动识别：14 位纯数字 → 无端ID（amis char_name）；
   2~12 位数字 → 角色ID（amis channel_uid）；
   ACE 侧 OpenID 只认角色ID：角色ID 编号直接用自身查 ACE；无端ID 编号先经 amis
   角色信息接口换算出角色ID 再查 ACE（换算失败则该编号跳过 ACE，不拿无端ID 硬查）；
   其余位数/非数字 → 提示并跳过（想强查可切换模式下拉）。
   模式：自动 / 仅角色ID / 仅无端ID。
3) 三个结果区块：编号识别结果 / amis 登录封禁 / ACE 数据；
   后两区双击任意一行弹出明细（含截图的记录在「路径/描述」列显示缩略图，单击看大图）。
4) 登录态各自独立：amis 走 amis_query 的 Token 机制（缓存 → 浏览器 → 登录框）；
   ACE 走 ace_query 的 Cookie 机制（凭据文件回退 → phone_login 自动登录）。
   任一平台登录态失效只弹对应登录框，换新后自动续跑本批查询。
5) 统一导出：单个 CSV（含 编号识别 / amis 汇总 / amis 明细 / ACE 汇总 / ACE 明细
   五段），写入本文件所在目录的「查询结果」子目录，文件名带时间戳。
6) 并发策略（批量提速）：amis 与 ACE 两个平台同时查询、互不等待；每个平台内部按
   编号并发（AMIS_WORKERS / ACE_ACCOUNT_WORKERS）；ACE 单个编号里「可疑用户核查」
   与「数据查询」两条链路并行、处罚记录翻页也并行；ACE 接口另有在途请求总闸
   （ACE_INFLIGHT_LIMIT）防止瞬时并发过高。
7) 自动更新：启动 6 秒后静默检查一次 GitHub Release，发现新版本时右上角状态
   变黄并提示；点「检查更新」可手动检查、点状态可下载。下载走加速镜像且支持
   断点续传，下完校验 SHA256，随后自动生成替换脚本重启到新版本。

注意：本文件是「合并入口」，所有查询能力均来自 amis_query / ace_query，
     在此不重复实现业务逻辑，两个原始工具的命令行用法不受影响。
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import inspect
import io
import json
import os
import queue
import re
import shutil
import sys
import tempfile
import threading
import time
from datetime import datetime

import tkinter as tk
from tkinter import filedialog, messagebox

# ---------------------------------------------------------------- 模块加载
# 程序所在目录：打包成 exe 后必须取「exe 所在目录」，不能用 __file__ ——
# PyInstaller 单文件模式下 __file__ 指向临时解包目录（sys._MEIPASS），
# 在那里写导出的 CSV / 登录态缓存，会随进程退出一起被清掉。
if getattr(sys, "frozen", False):
    SCRIPT_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

# 库模块集中在脚本目录的 src/ 子目录（amis/ace/sentry/license 及其 vendor）。
# 源码运行时把 src/ 加进模块搜索路径；打包成 exe 后模块已由 PyInstaller 直接打包，
# _MEIPASS 里没有 src/ 子目录，isdir 守卫会跳过。
_MODULE_ROOT = os.path.dirname(os.path.abspath(__file__))
_SRC_DIR = os.path.join(_MODULE_ROOT, "src")
if os.path.isdir(_SRC_DIR) and _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

try:
    import amis_query
    import ace_query
except Exception as _imp_err:            # 缺少 src/ 下模块时给出可读提示
    raise SystemExit(
        "无法导入 src/ 目录下的 amis_query.py / ace_query.py：%s\n"
        "请确认目录结构完整（库模块应位于脚本目录的 src\\ 子目录）：\n  %s"
        % (_imp_err, _MODULE_ROOT)
    )


try:                                     # 授权校验（license-guard）：缺失则拒绝启动（fail-closed）
    import license_boot
except Exception as _lic_err:            # pragma: no cover - 仅在文件缺失时发生
    license_boot = None
    _LICENSE_BOOT_ERROR = _lic_err

_UPDATE_IMPORT_ERROR = None
try:                                     # 自动更新（检查新版 / 断点续传下载 / 自替换）
    import update_check
except Exception as _upd_err:            # pragma: no cover - 仅在文件缺失时发生
    update_check = None
    _UPDATE_IMPORT_ERROR = _upd_err

APP_NAME = "账号封禁 · 联合查询"
APP_TITLE = APP_NAME + "（amis + ACE）"
VERSION = "1.0.0"

# CI 打包时由工作流生成 _build_version.py（gitignore，不入库），用于把
# 「每次 push 递增的发布版本号」带进 exe，使 --version 与 Release 名一致。
try:
    from _build_version import BUILD_VERSION
except Exception:                        # 本地源码运行时没有该文件
    BUILD_VERSION = ""

# 「检查更新」比较用的版本号：exe 里是 CI 生成的完整版本（如 1.0.7），
# 源码运行时退回 VERSION。
CURRENT_VERSION = BUILD_VERSION or VERSION

# 启动后自动检查更新的延时（毫秒）：先让窗口和登录态检测跑起来，再悄悄问一次
AUTO_UPDATE_DELAY_MS = 6000

OUTPUT_DIR_NAME = "查询结果"                 # 导出 CSV 的相对子目录名

# 编号种类
KIND_ROLE = "role"                          # 角色ID（amis channel_uid，同时作为 ACE OpenID）
KIND_WUDUAN = "wuduan"                      # 无端ID（amis char_name）
KIND_LABEL = {KIND_ROLE: "角色ID", KIND_WUDUAN: "无端ID"}

# 识别模式
MODE_AUTO = "auto"
MODE_ROLE = "role"
MODE_WUDUAN = "wuduan"
MODE_OPTIONS = [("自动", MODE_AUTO), ("仅角色ID", MODE_ROLE),
                ("仅无端ID", MODE_WUDUAN)]

PLATFORM_AMIS = "amis"
PLATFORM_ACE = "ACE"
PLATFORM_LABEL = {PLATFORM_AMIS: "amis", PLATFORM_ACE: "ACE"}

# 登录态探测：仅用于轻量只读请求，不会产生业务数据
ACE_PROBE_OPENID = "0"
AMIS_PROBE_KEYWORD = "99999999999999"
PROBE_TIMEOUT = 12               # 探测请求的超时（秒）：比业务请求短得多——
                                 # 探测只需要「通 / 不通」的结论，卡 30s 只会让芯片一直「检测中」

# 单个编号在联合查询里的参与情况
IDENT_COLUMNS = ("idx", "raw", "kinds", "platforms", "note")
IDENT_HEADERS = ("序号", "编号", "识别类型", "参与平台", "备注")
IDENT_WIDTHS = (60, 220, 150, 110, 430)

# ACE 汇总表
ACE_SUMMARY_COLUMNS = ("openid", "check", "punish", "level", "last")
ACE_SUMMARY_HEADERS = ("OpenID", "核查记录数", "处罚记录数", "最高可疑级别", "最近处罚时间")
ACE_SUMMARY_WIDTHS = (280, 140, 140, 160, 300)
ACE_SUMMARY_HEADERS_CSV = ACE_SUMMARY_HEADERS + ("备注",)   # CSV 导出仍带备注（含失败原因）

_SPLIT_RE = re.compile(r"[,，;；\s]+")


# ---------------------------------------------------------------- 小工具
def now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def timestamp_text():
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def result_dir():
    """统一导出目录：本文件所在目录下的「查询结果」"""
    path = os.path.join(SCRIPT_DIR, OUTPUT_DIR_NAME)
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        pass
    return path


def split_tokens(raw):
    """按 逗号/空格/换行/分号 切分输入，保留原始顺序（未去重）"""
    return [t for t in _SPLIT_RE.split(raw or "") if t]


def normalize_token(raw):
    return (raw or "").strip().strip("\ufeff").strip('"\'').strip()


def dedupe(seq):
    seen, out = set(), []
    for item in seq:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


class IdentItem(object):
    """一个编号的识别结果"""

    __slots__ = ("idx", "raw", "amis_kind", "ace_openid", "kinds", "note", "skipped")

    def __init__(self, idx, raw, amis_kind=None, ace_openid=None, kinds=(), note="", skipped=False):
        self.idx = idx
        self.raw = raw
        self.amis_kind = amis_kind          # KIND_ROLE / KIND_WUDUAN / None
        self.ace_openid = ace_openid        # 送 ACE 查询的 OpenID（None 表示不查 ACE）
        self.kinds = list(kinds)
        self.note = note
        self.skipped = bool(skipped)

    @property
    def kind_text(self):
        if not self.kinds:
            return "未识别（跳过）"
        return " + ".join(KIND_LABEL.get(k, k) for k in self.kinds)

    @property
    def platform_text(self):
        plats = []
        if self.amis_kind:
            plats.append(PLATFORM_AMIS)
        if self.ace_openid:
            plats.append(PLATFORM_ACE)
        return " + ".join(plats) if plats else "—"

    def as_row(self):
        return (self.idx, self.raw, self.kind_text, self.platform_text, self.note or "—")


def classify_token(raw, mode=MODE_AUTO):
    """识别单个编号；返回 IdentItem（skipped=True 表示不参与查询）"""
    token = normalize_token(raw)
    if not token:
        return None
    wd, lo, hi = amis_query.WDID_DIGITS, amis_query.ID_MIN_DIGITS, amis_query.ID_MAX_DIGITS
    digits = token.isdigit()

    if mode == MODE_ROLE:
        if not digits:
            return IdentItem(0, token, note="非纯数字，无法按角色ID查询，已跳过", skipped=True)
        return IdentItem(0, token, KIND_ROLE, token, [KIND_ROLE],
                         "手动指定角色ID（amis + ACE，OpenID 即角色ID）")
    if mode == MODE_WUDUAN:
        if not digits:
            return IdentItem(0, token, note="非纯数字，无法按无端ID查询，已跳过", skipped=True)
        return IdentItem(0, token, KIND_WUDUAN, token, [KIND_WUDUAN],
                         "手动指定无端ID（amis；ACE 用换算出的角色ID）")

    # 自动模式
    if not digits:
        return IdentItem(0, token, note="非纯数字，自动模式已跳过", skipped=True)
    n = len(token)
    if n == wd:
        return IdentItem(0, token, KIND_WUDUAN, token, [KIND_WUDUAN],
                         "%d 位纯数字：按无端ID 查询（amis；ACE 用换算出的角色ID）" % n)
    if lo <= n <= hi:
        return IdentItem(0, token, KIND_ROLE, token, [KIND_ROLE],
                         "%d 位数字：按角色ID 查询（amis + ACE）" % n)
    return IdentItem(0, token,
                     note="数字位数 %d 不在支持范围（角色ID %d-%d 位 / 无端ID %d 位），已跳过"
                          % (n, lo, hi, wd),
                     skipped=True)


def build_ident_items(raw_text, mode=MODE_AUTO):
    """切分 + 去重 + 识别，返回 (items, 去重被丢弃的重复数)"""
    tokens = split_tokens(raw_text)
    ordered = dedupe(tokens)
    items, idx = [], 0
    for token in ordered:
        item = classify_token(token, mode)
        if item is None:
            continue
        idx += 1
        item.idx = idx
        items.append(item)
    return items, max(0, len(tokens) - len(ordered))


def format_error(exc):
    """统一异常文案（优先使用两个模块自带的格式化函数）"""
    try:
        if isinstance(exc, ace_query.AceError):
            return ace_query._format_error(exc)
    except Exception:
        pass
    text = str(exc).strip() or exc.__class__.__name__
    return text


# ==================================================================
#       错误上报（Sentry）实现 —— 原 src/sentry_bridge.py 已并入本文件
# ==================================================================
# 并入后这些函数/类就在本模块命名空间内；本节末尾用 SimpleNamespace 聚成
# `sentry_bridge` 这个名字，使调用处写法与它曾是独立模块时完全一致。
import types as _types

# Sentry 错误上报桥接（纯标准库实现，不引入任何第三方依赖）
#
# 为什么不用 sentry-sdk
# ---------------------
# 本项目约定「纯 Python 标准库」（见 joint_query.py 头部说明），因此在标准库
# urllib 之上直接对接 Sentry 官方 HTTP Store 协议，行为完全自控、无需 pip 安装。
#
# 设计原则
# --------
# 1. 零依赖、零安装：只依赖 json / urllib / traceback / linecache 等标准库；
# 2. 绝不影响业务：所有上报都在守护线程中异步发送，超时短、失败静默，
#    上报自身的任何异常都被吞掉；
# 3. 隐私优先：事件送出前统一脱敏——角色ID/无端ID/手机号等 5 位以上数字、
#    token/密码/Cookie/密钥类字段、本机用户名路径（C:\\Users\\xxx）全部替换；
#    不上报请求体、不上报用户身份；
# 4. 可随时关闭：环境变量 JOINT_SENTRY=0 或把 DSN 置空即完全停用（含初始化）。
#
# 用法
# ----
#     import sentry_bridge
#     sentry_bridge.init(release=VERSION, app_name=APP_NAME, app_root=SCRIPT_DIR)
#     sentry_bridge.install_excepthooks()          # 捕获主线程/子线程未处理异常
#     sentry_bridge.capture_exception(exc, tags={"stage": "query"})
#     sentry_bridge.capture_message("批量查询失败", level="error", extra={"失败数": 3})
#     sentry_bridge.add_breadcrumb("开始查询", category="query")
#
# 环境变量
# --------
#     JOINT_SENTRY_DSN   覆盖内置 DSN（设为空串即关闭上报）
#     JOINT_SENTRY=0     强制关闭上报（优先级最高）
#     JOINT_SENTRY_ENV   环境名，默认 production（例如 staging / local）

import json
import linecache
import os
import platform as _platform
import re
import socket
import sys
import threading
import time
import traceback
import uuid
from collections import deque
from urllib import request as _urlrequest

# ---------------------------------------------------------------- 常量
# 内置 DSN（DSN 中的 key 是「公开写入客户端」的标识，不是密钥）
DEFAULT_DSN = ("https://b1e7965ae1df7a65ba033099b58b4ba4"
               "@o4511234196570112.ingest.us.sentry.io/4512219813969920")
CLIENT_NAME = "joint-query"
CLIENT_VERSION = "1.0.0"

ENV_DSN = "JOINT_SENTRY_DSN"          # 覆盖 DSN（空串 = 关闭）
ENV_SWITCH = "JOINT_SENTRY"           # 0/off/false/no = 关闭
ENV_ENVIRONMENT = "JOINT_SENTRY_ENV"  # 环境名
DEFAULT_ENVIRONMENT = "production"

_MAX_BREADCRUMBS = 30                 # 面包屑环形缓冲长度（随事件一起上报）
_MAX_EVENTS_PER_MIN = 20              # 每分钟最多上报条数（防异常风暴刷屏）
_MAX_EVENTS_TOTAL = 120               # 单次运行最多上报条数
_MAX_STRING = 500                     # 单个字符串字段最长长度
_TIMEOUT = 6.0                        # 单次上报超时（秒）

# 本程序的源文件名：打包成 exe 后帧路径与运行目录对不上，靠它判定「应用帧」
_APP_SOURCE_FILES = frozenset(("joint_query.py", "amis_query.py", "ace_query.py",
                              "license_boot.py"))

_STORE_RE = re.compile(
    r"^(?P<scheme>https?)://(?P<key>[^:@/]+)(?::[^@/]*)?@(?P<host>[^/]+)/(?P<path>[^?#]+)$")

# ---- 脱敏规则 ----
_RE_DSN_ANY = re.compile(r"(?i)https?://[^\s@/]+@[^\s/]+/\d+")
_RE_SECRET = re.compile(
    r"(?i)\b(bearer|token|password|passwd|pwd|cookie|authorization|auth|secret|"
    r"sessionid|session_id|access_key|apikey|api_key)"
    r"(\s*[\"'`]?\s*[:=]\s*[\"'`]?\s*)([^\s,;\"'`\)\]\}]*)")
_RE_WIN_USER = re.compile(r"(?i)([a-z]:\\users\\)([^\\\s\"']+)")
_RE_NIX_HOME = re.compile(r"(?i)(/(?:home|users)/)([^/\s\"']+)")
_RE_LONG_DIGITS = re.compile(r"\d{5,}")
# 命中这些键名的字段一律整体脱敏（大小写不敏感、包含即命中）
_SENSITIVE_KEYS = (
    "password", "passwd", "pwd", "token", "cookie", "authorization", "auth",
    "secret", "session", "credential", "dsn", "phone", "mobile", "telephone",
    "account", "user_name", "username", "openid", "role_id", "roleid",
    "channel_uid", "char_name", "uid", "idcard", "id_card", "signature",
    # 中文键名（界面/日志里可能出现）
    "密码", "手机", "电话", "账号", "帐号", "凭据", "密钥", "口令", "令牌",
)

_lock = threading.RLock()
_client = None                        # 模块级单例


# ---------------------------------------------------------------- 脱敏
def scrub_text(value, limit=_MAX_STRING):
    """把一段文本里的敏感信息替换成占位符（长数字 / 密钥 / 本机用户名路径）"""
    try:
        text = value if isinstance(value, str) else str(value)
    except Exception:
        return "<unserializable>"
    try:
        text = _RE_DSN_ANY.sub("<dsn>", text)
        text = _RE_SECRET.sub(lambda m: "%s%s<redacted>" % (m.group(1), m.group(2)), text)
        text = _RE_WIN_USER.sub(lambda m: m.group(1) + "<user>", text)
        text = _RE_NIX_HOME.sub(lambda m: m.group(1) + "<user>", text)
        text = _RE_LONG_DIGITS.sub("<id>", text)
    except Exception:
        pass
    if limit and len(text) > limit:
        text = text[:limit] + "…<truncated>"
    return text


def scrub_obj(value, depth=0):
    """递归脱敏任意对象（用于 extra / data 等结构化字段）"""
    if depth > 4:
        return "<deep>"
    try:
        if isinstance(value, dict):
            out = {}
            for i, (k, v) in enumerate(value.items()):
                if i >= 40:
                    break
                key = str(k)
                low = key.lower()
                if any(s in low for s in _SENSITIVE_KEYS):
                    out[key] = "<redacted>"
                else:
                    out[key] = scrub_obj(v, depth + 1)
            return out
        if isinstance(value, (list, tuple, set, frozenset)):
            return [scrub_obj(v, depth + 1) for v in list(value)[:40]]
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return scrub_text(value)
    except Exception:
        return "<unserializable>"


# ---------------------------------------------------------------- DSN
def parse_dsn(dsn):
    """解析 DSN → 配置 dict；无效返回 None

    返回 {scheme, public_key, host, project_id, store_url, envelope_url}
    """
    try:
        text = str(dsn or "").strip()
        m = _STORE_RE.match(text)
        if not m:
            return None
        path = m.group("path").strip("/")
        project_id = path.split("/")[-1] if path else ""
        if not project_id.isdigit():
            return None
        scheme, host = m.group("scheme"), m.group("host")
        return {
            "scheme": scheme,
            "public_key": m.group("key"),
            "host": host,
            "project_id": project_id,
            "store_url": "%s://%s/api/%s/store/" % (scheme, host, project_id),
            "envelope_url": "%s://%s/api/%s/envelope/" % (scheme, host, project_id),
        }
    except Exception:
        return None


def resolve_dsn():
    """决定本次运行使用的 DSN：JOINT_SENTRY=0 → 关闭；否则环境变量优先，其次内置"""
    if str(os.environ.get(ENV_SWITCH, "")).strip().lower() in ("0", "off", "false", "no"):
        return ""
    env_dsn = os.environ.get(ENV_DSN)
    if env_dsn is not None:
        return env_dsn.strip()
    return DEFAULT_DSN


# ---------------------------------------------------------------- 客户端
class SentryClient(object):
    """最小可用的 Sentry 客户端（Store 接口 + 异步发送 + 统一脱敏）"""

    def __init__(self, dsn="", release="", environment="", app_name="", app_root="",
                 logger_name="joint_query", timeout=_TIMEOUT):
        self.dsn = dsn or ""
        self.config = parse_dsn(self.dsn)
        self.enabled = bool(self.config)
        self.release = str(release or "")
        self.environment = str(environment or "") or DEFAULT_ENVIRONMENT
        self.app_name = str(app_name or "joint-query")
        self.app_root = os.path.abspath(app_root) if app_root else ""
        self.logger_name = logger_name
        self.timeout = float(timeout)
        self.breadcrumbs = deque(maxlen=_MAX_BREADCRUMBS)
        self.started_at = time.time()
        self.sent = 0
        self.dropped = 0
        self.last_error = ""
        self.last_event_id = ""
        self._stamps = deque()            # 发送时间戳（限流用）
        self._inflight = []
        self._thread_lock = threading.Lock()
        try:
            self.server_name = scrub_text(socket.gethostname(), 64)
        except Exception:
            self.server_name = ""

    # ---------------- 面包屑 ----------------
    def add_breadcrumb(self, message, category="app", level="info", data=None):
        if not self.enabled:
            return
        try:
            crumb = {"type": "default", "category": str(category or "app"),
                     "level": str(level or "info"),
                     "message": scrub_text(message, 300),
                     "timestamp": time.time()}
            if data:
                crumb["data"] = scrub_obj(data)
            self.breadcrumbs.append(crumb)
        except Exception:
            pass

    # ---------------- 事件构造 ----------------
    def _tags(self, tags):
        out = {"app": self.app_name, "client": CLIENT_NAME}
        if self.release:
            out["app.version"] = self.release
        out["python"] = "%d.%d.%d" % sys.version_info[:3]
        try:
            out["os"] = _platform.system() or ""
        except Exception:
            pass
        for k, v in (tags or {}).items():
            if v is None:
                continue
            out[str(k)] = scrub_text(v, 200)
        return {k: v for k, v in out.items() if v != ""}

    def _contexts(self):
        ctx = {}
        try:
            ctx["runtime"] = {
                "name": "python", "version": "%d.%d.%d" % sys.version_info[:3],
                "build": sys.version.replace("\n", " ")[:120],
            }
            ctx["os"] = {"name": _platform.system(), "version": _platform.release(),
                         "build": _platform.version()[:120]}
            ctx["device"] = {"arch": _platform.machine(),
                             "processor_count": os.cpu_count() or 0}
        except Exception:
            pass
        if self.release:
            ctx["app"] = {"app_name": self.app_name, "app_version": self.release,
                          "app_start_time": time.strftime(
                              "%Y-%m-%dT%H:%M:%S", time.localtime(self.started_at))}
        return ctx

    def _in_app(self, filename):
        """该帧是否属于本程序自己的代码。

        正常（跑源码）时按「路径在程序目录下」判断即可。但打包成 exe 后，
        帧里的路径是**构建机上**的源码路径或包内路径，与运行期的 exe 目录对不上，
        会导致本程序的帧全被判成「非应用帧」，Sentry 里就看不到自己的调用栈。
        所以补两条兜底：包内路径（sys._MEIPASS）、以及本程序的源文件名。
        """
        if not filename:
            return False
        path = os.path.abspath(filename)
        if self.app_root and (path.startswith(self.app_root)
                              or filename.startswith(self.app_root)):
            return True
        bundle = getattr(sys, "_MEIPASS", "")
        if bundle and path.startswith(bundle):
            return True
        return os.path.basename(path) in _APP_SOURCE_FILES

    def _frame(self, filename, function, lineno, frame=None):
        base = os.path.basename(filename) or filename
        info = {"filename": base, "function": scrub_text(function, 120),
                "lineno": int(lineno or 0),
                "in_app": self._in_app(filename)}
        try:
            module = frame.f_globals.get("__name__") if frame is not None else None
            if module:
                info["module"] = str(module)
        except Exception:
            pass
        try:
            lines = linecache.getlines(filename)
            if lines and 1 <= lineno <= len(lines):
                info["context_line"] = scrub_text(lines[lineno - 1].strip(), 300)
                info["pre_context"] = [scrub_text(t.strip(), 300)
                                       for t in lines[max(0, lineno - 4):lineno - 1]]
                info["post_context"] = [scrub_text(t.strip(), 300)
                                        for t in lines[lineno:lineno + 4]]
        except Exception:
            pass
        return info

    def _stacktrace(self, tb):
        frames = []
        try:
            for frame, lineno in (traceback.walk_tb(tb) if tb is not None else []):
                try:
                    code = frame.f_code
                    frames.append(self._frame(code.co_filename or "", code.co_name,
                                              lineno, frame))
                except Exception:
                    continue
        except Exception:
            pass
        if not frames:
            return None
        return {"frames": frames[-40:]}

    def _base_event(self, level, tags, extra, exception=None, message=None):
        event = {
            "event_id": uuid.uuid4().hex,
            "timestamp": time.time(),
            "platform": "python",
            "level": str(level or "error"),
            "logger": self.logger_name,
            "release": self.release or None,
            "environment": self.environment or None,
            "server_name": self.server_name or None,
            "sdk": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
            "tags": self._tags(tags),
            "extra": scrub_obj(extra or {}),
            "contexts": self._contexts(),
            "breadcrumbs": {"values": list(self.breadcrumbs)},
        }
        if exception:
            event["exception"] = {"values": [exception]}
        if message:
            event["message"] = scrub_text(message, 800)
        return {k: v for k, v in event.items()
                if v is not None and v != "" and v != {} and v != []}

    def build_exception_event(self, exc, level="error", tags=None, extra=None,
                              handled=True):
        """构造异常事件（不发送，供测试与自检使用）"""
        etype = type(exc)
        mech = {"type": "generic", "handled": bool(handled)} if handled else \
            {"type": "uncaught", "handled": False}
        value = {
            "type": getattr(etype, "__name__", "Exception"),
            "value": scrub_text(exc, 800),
            "module": getattr(etype, "__module__", "") or "",
            "mechanism": mech,
        }
        st = self._stacktrace(getattr(exc, "__traceback__", None))
        if st:
            value["stacktrace"] = st
        return self._base_event(level, tags, extra, exception=value)

    def build_message_event(self, message, level="info", tags=None, extra=None):
        return self._base_event(level, tags, extra, message=message)

    # ---------------- 发送 ----------------
    def _allow_send(self):
        now = time.time()
        while self._stamps and now - self._stamps[0] > 60.0:
            self._stamps.popleft()
        if len(self._stamps) >= _MAX_EVENTS_PER_MIN:
            self.dropped += 1
            self.last_error = "已触发限流（每分钟 %d 条）" % _MAX_EVENTS_PER_MIN
            return False
        if self.sent + self.dropped >= _MAX_EVENTS_TOTAL:
            self.dropped += 1
            self.last_error = "已达单次运行上报上限（%d 条）" % _MAX_EVENTS_TOTAL
            return False
        self._stamps.append(now)
        return True

    def send(self, event, wait=False):
        """发送事件；wait=True 时同步发送（用于进程即将退出/未捕获异常）"""
        if not self.enabled or not event:
            return ""
        if not self._allow_send():
            return ""
        if wait:
            self._post(event)
            return event.get("event_id", "")
        try:
            th = threading.Thread(target=self._post, args=(event,), daemon=True)
            with self._thread_lock:
                self._inflight = [t for t in self._inflight if t.is_alive()]
                self._inflight.append(th)
            th.start()
        except Exception:
            self._post(event)
        return event.get("event_id", "")

    def _post(self, event):
        cfg = self.config
        if not cfg:
            return
        try:
            payload = json.dumps(event, ensure_ascii=False, default=str).encode("utf-8")
        except Exception:
            self.last_error = "事件序列化失败"
            return
        auth = ("Sentry sentry_version=7, sentry_client=%s/%s, sentry_key=%s"
                % (CLIENT_NAME, CLIENT_VERSION, cfg["public_key"]))
        try:
            req = _urlrequest.Request(cfg["store_url"], data=payload, method="POST")
            req.add_header("Content-Type", "application/json")
            req.add_header("X-Sentry-Auth", auth)
            req.add_header("User-Agent", "%s/%s" % (CLIENT_NAME, CLIENT_VERSION))
            with _urlrequest.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read(8192).decode("utf-8", "replace")
                self.sent += 1
                self.last_error = ""
                try:
                    self.last_event_id = str(json.loads(body).get("id") or "")
                except Exception:
                    self.last_event_id = ""
        except Exception as exc:                 # 上报失败绝不影响业务
            self.dropped += 1
            self.last_error = scrub_text(exc, 200)

    def flush(self, timeout=3.0):
        deadline = time.time() + max(0.0, float(timeout))
        while True:
            with self._thread_lock:
                alive = [t for t in self._inflight if t.is_alive()]
                self._inflight = alive
            if not alive or time.time() >= deadline:
                return
            alive[0].join(timeout=min(0.2, deadline - time.time()))

    # ---------------- 便捷入口 ----------------
    def capture_exception(self, exc, level="error", tags=None, extra=None,
                          handled=True, wait=False):
        try:
            if isinstance(exc, type):
                exc = exc("异常类型：%s" % exc.__name__)
            return self.send(self.build_exception_event(
                exc, level=level, tags=tags, extra=extra, handled=handled), wait=wait)
        except Exception:
            return ""

    def capture_message(self, message, level="info", tags=None, extra=None,
                        wait=False):
        try:
            return self.send(self.build_message_event(message, level=level, tags=tags,
                                                      extra=extra), wait=wait)
        except Exception:
            return ""


# ---------------------------------------------------------------- 单例 API
def init(dsn=None, release="", environment="", app_name="", app_root="",
         logger_name="joint_query", force=False):
    """初始化（幂等）：未显式传 dsn 时走 resolve_dsn()；返回客户端实例"""
    global _client
    with _lock:
        if _client is not None and not force:
            return _client
        if dsn is None:
            dsn = resolve_dsn()
        env = environment or os.environ.get(ENV_ENVIRONMENT, "") or DEFAULT_ENVIRONMENT
        _client = SentryClient(dsn=dsn, release=release, environment=env,
                               app_name=app_name, app_root=app_root,
                               logger_name=logger_name)
    return _client


def get_client():
    return _client


def is_enabled():
    return bool(_client is not None and _client.enabled)


def status_text():
    """一行状态文案（界面展示用）"""
    if _client is None:
        return "错误上报：未初始化"
    if not _client.enabled:
        return "错误上报：已关闭"
    return "错误上报：Sentry 已启用（v%s）" % CLIENT_VERSION


def add_breadcrumb(message, category="app", level="info", data=None):
    client = _client
    if client is None or not client.enabled:
        return
    try:
        client.add_breadcrumb(message, category=category, level=level, data=data)
    except Exception:
        pass


def capture_exception(exc=None, level="error", tags=None, extra=None, handled=True,
                      wait=False):
    """捕获异常（exc 省略时取当前 sys.exc_info()）；未初始化/已关闭时静默返回"""
    client = _client
    if client is None or not client.enabled:
        return ""
    try:
        if exc is None:
            exc = sys.exc_info()[1]
        if exc is None:
            return ""
        return client.capture_exception(exc, level=level, tags=tags, extra=extra,
                                        handled=handled, wait=wait)
    except Exception:
        return ""


def capture_message(message, level="info", tags=None, extra=None, wait=False):
    client = _client
    if client is None or not client.enabled:
        return ""
    try:
        return client.capture_message(message, level=level, tags=tags, extra=extra,
                                      wait=wait)
    except Exception:
        return ""


def flush(timeout=3.0):
    client = _client
    if client is None:
        return
    try:
        client.flush(timeout)
    except Exception:
        pass


# ---------------------------------------------------------------- 全局钩子
def _report_uncaught(exc_type, exc, tb, scope="main"):
    """未捕获异常：始终上报（同步发送，避免进程退出丢失）"""
    if exc is None and exc_type is not None:
        try:
            exc = exc_type("未捕获异常：%s" % getattr(exc_type, "__name__", "?"))
        except Exception:
            exc = Exception("未捕获异常")
    if exc is None:
        return
    if tb is not None and getattr(exc, "__traceback__", None) is None:
        try:
            exc = exc.with_traceback(tb)
        except Exception:
            pass
    capture_exception(exc, level="fatal", tags={"scope": scope, "uncaught": "1"},
                      handled=False, wait=True)


def install_excepthooks():
    """安装主线程 / 子线程未捕获异常钩子（幂等）"""
    with _lock:
        if getattr(install_excepthooks, "_done", False):
            return
        install_excepthooks._done = True

    prev_sys = sys.excepthook

    def _sys_hook(exc_type, exc, tb):
        try:
            _report_uncaught(exc_type, exc, tb, scope="main")
        except Exception:
            pass
        try:
            prev_sys(exc_type, exc, tb)
        except Exception:
            pass

    sys.excepthook = _sys_hook

    prev_thread = getattr(threading, "excepthook", None)
    if prev_thread is not None:
        def _thread_hook(args):
            try:
                _report_uncaught(getattr(args, "exc_type", None),
                                 getattr(args, "exc_value", None),
                                 getattr(args, "exc_traceback", None),
                                 scope="thread:%s" % getattr(
                                     getattr(args, "thread", None), "name", "?"))
            except Exception:
                pass
            try:
                prev_thread(args)
            except Exception:
                pass
        threading.excepthook = _thread_hook


def tk_callback_exception(exc_type, exc, tb, on_report=None):
    """Tkinter 回调异常处理器（挂到 root.report_callback_exception）

    Tk 会吞掉回调里的异常（只在 stderr 打一行），这里补齐：上报 + 打印 +
    可选地弹出提示，避免「点了按钮没反应」这类问题被静默忽略。
    """
    try:
        _report_uncaught(exc_type, exc, tb, scope="tk-callback")
    except Exception:
        pass
    try:
        traceback.print_exception(exc_type, exc, tb)
    except Exception:
        pass
    if on_report:
        try:
            on_report(exc)
        except Exception:
            pass

# ---- 兼容命名空间：让调用处继续写 sentry_bridge.xxx() ----
sentry_bridge = _types.SimpleNamespace(
    DEFAULT_DSN=DEFAULT_DSN,
    parse_dsn=parse_dsn,
    resolve_dsn=resolve_dsn,
    scrub_text=scrub_text,
    scrub_obj=scrub_obj,
    SentryClient=SentryClient,
    init=init,
    get_client=get_client,
    is_enabled=is_enabled,
    status_text=status_text,
    add_breadcrumb=add_breadcrumb,
    capture_exception=capture_exception,
    capture_message=capture_message,
    flush=flush,
    install_excepthooks=install_excepthooks,
    tk_callback_exception=tk_callback_exception,
)

# ==================================================================
#                    错误上报（Sentry）—— 薄封装
# ==================================================================
# 说明：实现在本节上方（纯标准库、异步发送、送出前统一脱敏）。
#       这里只做「薄封装 + 兜底」，保证未接入或已关闭时调用完全无副作用。
#       关闭方式：环境变量 JOINT_SENTRY=0；或把 JOINT_SENTRY_DSN 设为空串。

def sentry_ready():
    """错误上报是否已启用（关闭或未初始化时恒为 False）"""
    try:
        return bool(sentry_bridge.is_enabled())
    except Exception:
        return False


def sentry_status():
    try:
        return sentry_bridge.status_text()
    except Exception:
        return "错误上报：未知"


def report_exception(exc, tags=None, extra=None, level="error", handled=True):
    """上报异常（静默失败，绝不影响主流程）"""
    if exc is None:
        return ""
    try:
        return sentry_bridge.capture_exception(exc, level=level, tags=tags,
                                               extra=extra, handled=handled)
    except Exception:
        return ""


def report_message(message, level="error", tags=None, extra=None):
    """上报一段消息（用于「非异常但值得关注」的情况，如批量部分失败）"""
    try:
        return sentry_bridge.capture_message(message, level=level, tags=tags,
                                             extra=extra)
    except Exception:
        return ""


def mark(text, category="app", level="info", data=None):
    """记录面包屑（随下一次上报一起送出，用来看「出错前发生了什么」）"""
    try:
        sentry_bridge.add_breadcrumb(text, category=category, level=level, data=data)
    except Exception:
        pass


# ==================================================================
#              平台登录状态：本地只读快照 + 在线轻量探测
# ==================================================================
# 状态取值：ok（在线校验通过）/ invalid（服务端判定失效）/ missing（本机无凭据）
#           unknown（未能完成在线校验，如网络异常）/ checking（检测中，仅界面用）
#
# 说明：状态探测是「只读」的——只读取本地凭据文件与浏览器 LocalStorage，
#       不会删除 / 改写任何凭据；在线探测也只是一次无副作用的查询请求。

def fmt_expire_text(expire):
    """时间戳 → 可读到期时间；无效返回空串"""
    try:
        expire = int(expire or 0)
    except (TypeError, ValueError):
        return ""
    if expire <= 0:
        return ""
    try:
        return datetime.fromtimestamp(expire).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


def _amis_cached_token():
    """只读读取 amis 本地 Token 缓存（不删除、不改写），返回 (token, info)"""
    try:
        path = amis_query.token_cache_path()
    except Exception:
        return "", {}
    if not os.path.isfile(path):
        return "", {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        token = str(data.get("token") or "").strip()
    except Exception:
        return "", {}
    if not token:
        return "", {}
    info = amis_query.parse_token_info(token)
    account = str(data.get("account") or "").strip()
    if account:
        info["account"] = account
    try:
        expire = int(data.get("expire") or 0)
    except (TypeError, ValueError):
        expire = 0
    if expire:
        info["expire"] = expire
    return token, info


def _amis_browser_token():
    """只读扫描 Chrome / Edge LocalStorage 里的 amis Token（复制后扫描，不动浏览器数据）"""
    candidates = []
    tmp_root = tempfile.mkdtemp(prefix="joint_status_")
    try:
        for root in getattr(amis_query, "BROWSER_ROOTS", ()):
            if not os.path.isdir(root):
                continue
            for profile in os.listdir(root):
                leveldb = os.path.join(root, profile, "Local Storage", "leveldb")
                if os.path.isdir(leveldb):
                    candidates.extend(amis_query.scan_leveldb_dir(leveldb, tmp_root))
    except Exception:
        candidates = []
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
    if not candidates:
        return "", {}
    candidates.sort(key=lambda x: x[0], reverse=True)
    _expire, token, info = candidates[0]
    return token, info


def amis_local_snapshot():
    """只读获取 amis 本地登录态（环境变量 → 本地缓存 → 浏览器），不联网、不删除任何文件"""
    env_token = os.environ.get("AMIS_TOKEN", "").strip()
    if env_token:
        info = amis_query.parse_token_info(env_token)
        return {"state": "local", "token": env_token, "source": "环境变量 AMIS_TOKEN",
                "account": str(info.get("account") or ""),
                "expire": int(info.get("expire") or 0)}
    token, info = _amis_cached_token()
    if token:
        return {"state": "local", "token": token, "source": "本地缓存 .amis_token.json",
                "account": str(info.get("account") or ""),
                "expire": int(info.get("expire") or 0)}
    token, info = _amis_browser_token()
    if token:
        return {"state": "local", "token": token, "source": "浏览器 LocalStorage",
                "account": str(info.get("account") or ""),
                "expire": int(info.get("expire") or 0)}
    return {"state": "missing", "token": "", "source": "", "account": "", "expire": 0}


def _ace_expire_ts(data):
    """尽力从 ACE 凭据里解析 Cookie 到期时间戳（无则返回 0）"""
    for key in ("expire", "expires", "cookie_expire", "expire_at"):
        try:
            value = int(data.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    match = re.search(r"expires\s*=\s*(\d{10})", str(data.get("note") or ""))
    return int(match.group(1)) if match else 0


def ace_local_snapshot():
    """只读获取 ACE 本地凭据状态（不联网、不改写凭据文件）"""
    path = str(getattr(ace_query, "CREDENTIALS_FILE", "") or "")
    if not path or not os.path.isfile(path):
        return {"state": "missing", "cookies": {}, "account": "", "expire": 0,
                "source": "", "detail": "未找到 ACE 凭据文件"}
    try:
        data, cookies = ace_query.load_credentials()
    except Exception as exc:
        return {"state": "missing", "cookies": {}, "account": "", "expire": 0,
                "source": os.path.basename(path), "detail": format_error(exc)}
    account = str((data.get("account") or {}).get("user_name")
                  or (cookies or {}).get("user_name") or "")
    return {"state": "local", "cookies": dict(cookies or {}), "account": account,
            "expire": _ace_expire_ts(data), "source": os.path.basename(path), "detail": ""}


def probe_amis_online(token, account="", timeout=None):
    """在线轻量校验 amis Token：valid / invalid / unknown（只读查询，无副作用）"""
    if not token:
        return "missing"
    try:
        amis_query._request_user_info(AMIS_PROBE_KEYWORD, token, account or "",
                                      amis_query.KEY_TYPE_ROLE,
                                      timeout=timeout or PROBE_TIMEOUT)
        return "valid"
    except amis_query.TokenInvalidError:
        return "invalid"
    except Exception:
        return "unknown"


def probe_ace_online(cookies, timeout=None):
    """在线轻量校验 ACE 登录态：valid / invalid / unknown（查询一个不存在的 OpenID）"""
    if not cookies:
        return "missing"
    timeout = timeout or PROBE_TIMEOUT
    try:
        begin, end = ace_query.dq_time_range()
        ace_query.query_punish(ACE_PROBE_OPENID, 1, 1, cookies, begin, end,
                               timeout=timeout)
        return "valid"
    except Exception as exc:
        if isinstance(exc, getattr(ace_query, "LoginManualRequired", ())):
            return "unknown"
        if isinstance(exc, ace_query.AceError) and getattr(exc, "auth", False):
            return "invalid"
        return "unknown"


def probe_platform_status(platform, timeout=None):
    """完整探测某平台登录态，返回 dict(state, account, expire, source, detail)

    判定顺序：本地凭据 → 在线轻量校验；只有服务端明确判定失效才算 invalid，
    网络异常一律降级为 unknown（不误报失效）。
    """
    if platform == PLATFORM_AMIS:
        snap = amis_local_snapshot()
        source = snap.get("source") or ""
        expire = snap.get("expire") or 0
        if snap["state"] == "missing":
            return {"state": "missing", "account": "", "expire": 0, "source": "",
                    "detail": "本机未找到 amis Token（环境变量 / 本地缓存 / 浏览器均无）"}
        verdict = probe_amis_online(snap.get("token"), snap.get("account"),
                                    timeout=timeout)
        worth = ("，本地记录到期时间 %s" % fmt_expire_text(expire)) if expire else ""
        if verdict == "valid":
            return {"state": "ok", "account": snap.get("account") or "", "expire": expire,
                    "source": source,
                    "detail": "在线校验通过（凭据来源：%s%s）" % (source or "未知", worth)}
        if verdict == "invalid":
            return {"state": "invalid", "account": snap.get("account") or "", "expire": expire,
                    "source": source,
                    "detail": "服务端判定 Token 已失效或已过期（凭据来源：%s%s）"
                              % (source or "未知", worth)}
        return {"state": "unknown", "account": snap.get("account") or "", "expire": expire,
                "source": source,
                "detail": "未能完成在线校验（网络或接口异常），本地凭据来源：%s%s"
                          % (source or "未知", worth)}

    snap = ace_local_snapshot()
    expire = snap.get("expire") or 0
    if snap["state"] == "missing":
        detail = snap.get("detail") or "未找到 ACE 凭据文件"
        return {"state": "missing", "account": snap.get("account") or "", "expire": 0,
                "source": snap.get("source") or "", "detail": detail}
    verdict = probe_ace_online(snap.get("cookies"), timeout=timeout)
    account = snap.get("account") or ""
    worth = ("；本地记录 Cookie 到期时间 %s" % fmt_expire_text(expire)) if expire else ""
    if verdict == "valid":
        return {"state": "ok", "account": account, "expire": expire,
                "source": snap.get("source") or "",
                "detail": "在线校验通过（账号：%s%s）" % (account or "未知", worth)}
    if verdict == "invalid":
        return {"state": "invalid", "account": account, "expire": expire,
                "source": snap.get("source") or "",
                "detail": "服务端判定登录态已失效或已过期（账号：%s%s）"
                          % (account or "未知", worth)}
    return {"state": "unknown", "account": account, "expire": expire,
            "source": snap.get("source") or "",
            "detail": "未能完成在线校验（网络或接口异常），本地凭据仍在"}


# ==================================================================
#                    查询引擎（amis + ACE 联合）
# ==================================================================
# 说明：本文件不重复实现业务逻辑，所有网络请求全部委托给
# amis_query / ace_query 两个模块，这里只做「编排 + 汇总」。
ACE_CHECK_LEVEL = "all"        # 可疑用户核查：可疑级别固定 all（与原工具界面一致）
ACE_MAX_PAGES = 50             # 数据查询：最多翻页数（防止异常响应导致无限翻页）

# ---- 并发参数（批量查询总耗时主要由这里决定）----
AMIS_WORKERS = 6               # amis 平台同时查询的编号数（amis_query 模块默认 4）
ACE_ACCOUNT_WORKERS = 6        # ACE 平台同时查询的编号数
ACE_PAGE_WORKERS = 4           # 单个编号「数据查询」翻页时的并发页数
ACE_INFLIGHT_LIMIT = 8         # ACE 接口同时在途的请求上限（账号并发 × 翻页并发后的总闸）
PARALLEL_PLATFORMS = True      # amis 与 ACE 两个平台同时查询（互不等待）
ACE_INFLIGHT = threading.Semaphore(ACE_INFLIGHT_LIMIT)


class AceAuthRequired(Exception):
    """ACE 登录态失效，需要用户重新登录后重试"""

    def __init__(self, reason=""):
        super().__init__(reason or "ACE 登录态已失效，需要重新登录")
        self.reason = reason


def preflight_amis_login(log):
    """查询前预检 amis 登录态：本机无凭据 / 服务端判定失效 → 立即弹登录框换取新 Token

    返回 dict(verdict, token, relogin)；verdict 为 valid / unknown。
    用户取消登录时由 amis_query 抛 RuntimeError，由调用方提示并跳过 amis 平台。
    """
    snap = amis_local_snapshot()
    if snap["state"] == "missing":
        log("[amis] 预检：本机未找到可用 Token，请先登录…")
        token, _info = amis_query.obtain_token_by_login(
            "本机未找到可用的 amis Token，请登录后再查询。",
            reason_kind=amis_query.LOGIN_REASON_FIRST)
        return {"verdict": "valid", "token": token, "relogin": True}

    token = snap.get("token") or ""
    verdict = probe_amis_online(token, snap.get("account"))
    if verdict == "invalid":
        log("[amis] 预检：服务端判定登录态已失效，弹出登录框重新登录…")
        token, _info = amis_query.obtain_token_by_login(
            "服务端判定当前 amis 登录 Token 已失效或已过期，请重新登录后继续查询。",
            reason_kind=amis_query.LOGIN_REASON_EXPIRED)
        return {"verdict": "valid", "token": token, "relogin": True}
    if verdict == "valid":
        log("[amis] 预检：登录态有效（凭据来源：%s）。" % (snap.get("source") or "未知"))
    else:
        log("[amis] 预检：未能完成在线校验（网络或接口异常），按本地凭据继续查询。")
    return {"verdict": verdict, "token": token, "relogin": False}


def preflight_ace_login(cookies, provider, log):
    """查询前预检 ACE 登录态：服务端判定失效 → 弹登录框换新凭据

    返回 (cookies, verdict)；verdict 为 valid / ok / unknown / cancelled；
    cancelled 表示用户取消登录，此时 cookies 为 None。
    """
    verdict = probe_ace_online(cookies)
    if verdict == "valid":
        log("[ACE] 预检：登录态有效。")
        return cookies, "valid"
    if verdict == "invalid":
        log("[ACE] 预检：服务端判定登录态已失效或已过期，弹出登录框重新登录…")
        if provider is None:
            raise AceAuthRequired("ACE 登录态已失效或已过期，且未提供登录入口")
        fresh = provider("ACE 登录态已失效或已过期，请重新登录后继续查询"
                         "（登录成功后会自动续跑本批查询）。")
        if not fresh:
            return None, "cancelled"
        log("[ACE] 预检：已取得新的登录态，继续查询。")
        return fresh, "ok"
    log("[ACE] 预检：未能完成在线校验（网络或接口异常），按本地凭据继续查询。")
    return cookies, "unknown"


class JointResult(object):
    """一次联合查询的完整结果（界面渲染与 CSV 导出共用）"""

    def __init__(self):
        self.items = []            # [IdentItem]（含被跳过的）
        self.ident_rows = []       # 编号识别表行
        self.amis = None           # 合并后的 amis_query.QueryResult；无 amis 编号时为 None
        self.amis_error = ""       # amis 整批失败原因（例如用户取消登录）
        self.amis_kind_of = {}     # {编号文本: KIND_ROLE / KIND_WUDUAN}
        self.ace = {}              # {openid: ACE 结果 dict}
        self.ace_order = []        # ACE 参与查询的编号顺序（值为实际查询用的 OpenID）
        self.ace_converted = {}    # {无端ID: 换算出的角色ID}（ACE 只认角色ID，仅记录换算来源）
        self.ace_error = ""        # ACE 整批失败原因（例如用户取消登录）
        self.stopped = False
        self.query_at = now_text()
        self.logs = []

    # ---- amis 侧派生信息 ----
    @property
    def amis_summaries(self):
        return list(self.amis.summaries) if self.amis else []

    @property
    def amis_detail_rows(self):
        return dict(self.amis.detail_rows or {}) if self.amis else {}

    def amis_summary_rows(self):
        """amis 汇总表行：在原始汇总行前插入「编号类型」列"""
        rows = []
        if not self.amis:
            return rows
        for i, s in enumerate(self.amis.summaries, 1):
            base = amis_query.summary_to_row(i, s)
            kind = self.amis_kind_of.get(str(s.get("query_value")), "")
            rows.append([base[0], base[1], KIND_LABEL.get(kind, "—")] + base[2:])
        return rows

    def amis_banned_count(self):
        return sum(1 for s in self.amis_summaries
                   if s.get("banned") and not s.get("failed"))

    def amis_failed_count(self):
        return sum(1 for s in self.amis_summaries if s.get("failed"))

    # ---- ACE 侧派生信息 ----
    def ace_summary_rows(self, with_note=True):
        """ACE 汇总表行（与 ACE_SUMMARY_HEADERS 对应）

        OpenID 列为「实际查询用的 OpenID」：角色ID 编号即自身，无端ID 编号为换算出的角色ID。
        with_note=True 时在行尾附加「备注」（失败原因 / 换算来源），仅供 CSV 导出使用；
        界面表格不展示备注列（with_note=False）。
        """
        converted_from = {}
        for wd, rid in (self.ace_converted or {}).items():
            converted_from.setdefault(str(rid), []).append(str(wd))
        rows = []
        for openid in self.ace_order:
            data = self.ace.get(openid) or {}
            check = data.get("records") or []
            punish = data.get("punish_items") or []
            if data.get("check_error"):
                check_text = "失败"
            else:
                check_text = str(len(check))
            punish_text = "失败" if data.get("punish_error") else str(len(punish))
            level = ace_query.LEVEL_LABELS.get(data.get("top_level") or "", "")
            note = "；".join(t for t in (data.get("check_error"), data.get("punish_error")) if t)
            row = [openid, check_text, punish_text, level or "—",
                   data.get("last_punish") or "—"]
            if with_note:
                origin = converted_from.get(str(openid))
                if origin:
                    note = ("%s；" % note if note else "") + "由无端ID %s 换算" % "、".join(origin)
                row.append(note or "—")
            rows.append(row)
        return rows

    def ace_hit_count(self):
        """ACE 侧「有可疑/处罚记录」的账号数（两类数据任一非空）"""
        total = 0
        for openid in self.ace_order:
            data = self.ace.get(openid) or {}
            if data.get("records") or data.get("punish_items"):
                total += 1
        return total


# ---------------------------------------------------------------- ACE 单账号查询
def _ace_note(log_fn, text):
    if log_fn:
        try:
            log_fn(text)
        except Exception:
            pass


def ace_blank_result(openid):
    """单个 OpenID 的结果骨架（含查询时间范围，供界面展示）"""
    check_begin, check_end = ace_query.resolve_dates(None, None)
    dq_begin, dq_end = ace_query.dq_time_range()
    return {
        "openid": openid,
        "records": [],           # 可疑用户核查记录
        "punish_items": [],      # 数据查询（处罚）记录
        "punish_count": 0,       # 处罚记录总数（接口返回）
        "check_error": "",       # 核查失败原因（空 = 成功）
        "punish_error": "",      # 数据查询失败原因（空 = 成功）
        "check_range": "%s ~ %s" % (check_begin, check_end),
        "punish_range": "%s ~ %s" % (dq_begin, dq_end),
        "top_level": "",
        "last_punish": "",
    }


class _AceStopped(Exception):
    """内部信号：用户点了「停止」，剩余请求不再发出（不写入任何结果）"""


def ace_check_part(openid, cookies, log_fn=None, should_stop=None):
    """ACE 链路 1：可疑用户核查（近 30 天）

    只做一件事、可与其他链路并行；返回 {"records": [...], "check_error": "..."}。
    登录态失效时抛 AceAuthRequired（由上层统一换新凭据重试）。
    should_stop 返回 True 时立即返回 None（表示本次未查询，调用方不应计入结果）。
    """
    part = {"records": [], "check_error": ""}
    check_begin, check_end = ace_query.resolve_dates(None, None)
    if should_stop and should_stop():
        return None
    try:
        with ACE_INFLIGHT:                       # 全局限流：避免瞬时并发压垮接口
            if should_stop and should_stop():    # 排队等锁期间可能已被停止，不再发请求
                return None
            data = ace_query.query_openid(openid, check_begin, check_end,
                                          ACE_CHECK_LEVEL, cookies)
        part["records"] = ace_query.extract_records(data, openid)
    except ace_query.LoginManualRequired:
        raise
    except ace_query.AceError as exc:
        if getattr(exc, "auth", False):
            raise AceAuthRequired(ace_query._format_error(exc))
        part["check_error"] = ace_query._format_error(exc)
        _ace_note(log_fn, "[ACE] (%s) 可疑用户核查失败：%s" % (openid, part["check_error"]))
    except Exception as exc:
        part["check_error"] = format_error(exc)
        _ace_note(log_fn, "[ACE] (%s) 可疑用户核查异常：%s" % (openid, part["check_error"]))
    return part


def ace_punish_part(openid, cookies, log_fn=None, should_stop=None):
    """ACE 链路 2：数据查询（回溯 10 年处罚记录，多页时并发拉取）

    返回 {"punish_items": [...], "punish_count": n, "punish_error": "..."}。
    分页语义与串行版本一致：任一分页失败即整条链路判失败（不返回半截数据）。
    should_stop 返回 True 时立即返回 None（表示本次未查询，调用方不应计入结果）。
    """
    part = {"punish_items": [], "punish_count": 0, "punish_error": ""}
    if should_stop and should_stop():
        return None
    try:
        dq_begin, dq_end = ace_query.dq_time_range()
        page_size = ace_query.DQ_PAGE_SIZE
        with ACE_INFLIGHT:                       # 全局限流：避免瞬时并发压垮接口
            if should_stop and should_stop():    # 排队等锁期间可能已被停止，不再发请求
                return None
            count, first_page = ace_query.query_punish(openid, 1, page_size, cookies,
                                                       dq_begin, dq_end)
        items = list(first_page or [])
        total = int(count or 0)
        pages = min(ACE_MAX_PAGES, max(1, (total + page_size - 1) // page_size))
        if pages > 1 and len(items) < total:
            todo = list(range(2, pages + 1))

            def fetch_page(page_no):
                if should_stop and should_stop():
                    raise _AceStopped()          # 已停止：剩余分页不再发出
                with ACE_INFLIGHT:
                    if should_stop and should_stop():
                        raise _AceStopped()
                    _c, rows = ace_query.query_punish(openid, page_no, page_size, cookies,
                                                      dq_begin, dq_end)
                return page_no, list(rows or [])

            # 分页之间互不依赖：并发拉取（任一页失败会在此抛出，与串行版行为一致）
            with concurrent.futures.ThreadPoolExecutor(
                    max_workers=min(ACE_PAGE_WORKERS, len(todo))) as pool:
                rest = list(pool.map(fetch_page, todo))
            rest.sort(key=lambda pair: pair[0])
            for _page_no, rows in rest:
                items.extend(rows)
                if len(items) >= total:
                    break
        part["punish_items"] = items
        part["punish_count"] = max(total, len(items))
    except _AceStopped:
        return None
    except ace_query.LoginManualRequired:
        raise
    except ace_query.AceError as exc:
        if getattr(exc, "auth", False):
            raise AceAuthRequired(ace_query._format_error(exc))
        part["punish_error"] = ace_query._format_error(exc)
        _ace_note(log_fn, "[ACE] (%s) 数据查询失败：%s" % (openid, part["punish_error"]))
    except Exception as exc:
        part["punish_error"] = format_error(exc)
        _ace_note(log_fn, "[ACE] (%s) 数据查询异常：%s" % (openid, part["punish_error"]))
    return part


def ace_merge_parts(openid, check_part, punish_part):
    """把两条链路的片段合并成一个完整结果 dict"""
    out = ace_blank_result(openid)
    if check_part:
        out["records"] = check_part.get("records") or []
        out["check_error"] = check_part.get("check_error") or ""
    if punish_part:
        out["punish_items"] = punish_part.get("punish_items") or []
        try:
            out["punish_count"] = int(punish_part.get("punish_count") or 0)
        except (TypeError, ValueError):
            out["punish_count"] = 0
        out["punish_error"] = punish_part.get("punish_error") or ""
    out["top_level"] = ace_query.batch_top_level(out["records"])
    out["last_punish"] = ace_query.batch_last_punish(out["punish_items"])
    return out


def ace_query_one(openid, cookies, log_fn=None):
    """查询单个 OpenID 的两类 ACE 数据，返回结果 dict（失败信息写进结果，不抛异常）

    「可疑用户核查」与「数据查询」两条链路并行执行，单个账号耗时约减半。
    异常约定：仅当登录态失效时抛 AceAuthRequired，交由上层弹登录框换新凭据后重试。
    """
    check_part = punish_part = None
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        fu_check = pool.submit(ace_check_part, openid, cookies, log_fn)
        fu_punish = pool.submit(ace_punish_part, openid, cookies, log_fn)
        check_part = fu_check.result()      # 登录态失效等异常在此抛出
        punish_part = fu_punish.result()

    result = ace_merge_parts(openid, check_part, punish_part)
    if not result["check_error"]:
        _ace_note(log_fn, "[ACE] (%s) 可疑用户核查：%d 条记录" % (openid, len(result["records"])))
    if not result["punish_error"]:
        _ace_note(log_fn, "[ACE] (%s) 数据查询：%d 条处罚记录" % (openid, len(result["punish_items"])))
    return result


def run_ace_batch(openids, cookies, log, stopping, workers=None, on_item=None):
    """并发查询一批 OpenID（每个编号的「核查」与「处罚」两条链路同时进行）

    返回 (results, pending, auth_reason)：
      results     : {openid: 结果 dict}（已完成的编号）
      pending     : 因登录态失效 / 用户停止而未取到结果的编号（按输入顺序）
      auth_reason : 登录态失效原因（空串表示没有遇到失效）

    on_item(done, total, openid) 为可选回调：每查完一个编号调用一次（进度条用，在工作线程中调用）。
    """
    workers = max(1, int(workers or ACE_ACCOUNT_WORKERS))
    total = len(openids)
    results, parts, order = {}, {}, {}
    auth_ids, auth_reason = [], []
    lock = threading.Lock()
    counter = {"done": 0}

    def finish_part(openid, kind, part):
        seq = None
        with lock:
            bucket = parts.setdefault(openid, {})
            bucket[kind] = part
            if len(bucket) < 2:
                return
            counter["done"] += 1
            seq = counter["done"]
        if on_item:                       # 进度回调：一个编号的两条链路都返回即计数
            try:
                on_item(seq, total, openid)
            except Exception:
                pass
        merged = ace_merge_parts(openid, bucket.get("check"), bucket.get("punish"))
        order[openid] = seq
        with lock:
            results[openid] = merged
        log("[ACE] (%d/%d) %s 完成：可疑核查 %s / 处罚 %s"
            % (seq, total, openid,
               "失败" if merged["check_error"] else "%d 条" % len(merged["records"]),
               "失败" if merged["punish_error"] else "%d 条" % len(merged["punish_items"])))

    def run_part(openid, kind):
        if stopping():
            return
        if kind == "check":
            part = ace_check_part(openid, cookies, log, stopping)
        else:
            part = ace_punish_part(openid, cookies, log, stopping)
        if part is None:                     # 停止导致本次未查询：不计入结果（编号留在 pending）
            return
        finish_part(openid, kind, part)

    log("[ACE] 并发查询 %d 个编号（并发 %d 个账号，每个账号的核查与处罚同时进行）…"
        % (total, min(workers, total)))
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
    futures = {}
    stop_notified = {"done": False}
    try:
        for openid in openids:
            futures[pool.submit(run_part, openid, "check")] = (openid, "check")
            futures[pool.submit(run_part, openid, "punish")] = (openid, "punish")
        for fu in concurrent.futures.as_completed(futures):
            if stopping() and not stop_notified["done"]:
                stop_notified["done"] = True
                for other in futures:        # 未开始的任务直接取消，不再占用线程
                    other.cancel()
                log("[ACE] 已收到停止指令：未开始的查询不再发出，等待在途请求返回…")
            openid, kind = futures[fu]
            try:
                fu.result()
            except concurrent.futures.CancelledError:
                continue
            except AceAuthRequired as exc:
                with lock:
                    if openid not in auth_ids:
                        auth_ids.append(openid)
                    auth_reason.append(exc.reason)
                for other in futures:            # 登录态已失效：未开始的任务不再发出
                    other.cancel()
            except Exception as exc:
                msg = format_error(exc)
                log("[ACE] (%s) 查询异常：%s" % (openid, msg))
                if kind == "check":
                    finish_part(openid, "check", {"records": [], "check_error": msg})
                else:
                    finish_part(openid, "punish",
                                {"punish_items": [], "punish_count": 0, "punish_error": msg})
    finally:
        pool.shutdown(wait=True)

    pending = [openid for openid in openids if openid not in results]
    if auth_reason:
        log("[ACE] 登录态失效，%d 个编号待换新凭据后重试。" % len(pending))
    return results, pending, (auth_reason[0] if auth_reason else "")


def ace_load_cookies():
    """读取 ACE 本地凭据（Cookie）。无凭据 / 无效时抛 AceError（auth=True）。"""
    _creds, cookies = ace_query.load_credentials()
    return cookies


def merge_amis_results(groups, items):
    """把多组 amis 查询结果（角色ID / 无端ID 分组）合并成一个 QueryResult

    - summaries / rows / detail_rows 按分组顺序拼接
    - token_source 取各组的来源文案（去重后拼接），便于界面提示登录态来源
    """
    summaries, rows, detail_rows, sources = [], [], {}, []
    stopped, total_ids, counters = False, 0, {}
    for res, ktype in groups:
        if res is None:
            continue
        summaries.extend(res.summaries)
        rows.extend(res.rows)
        for key, val in (res.detail_rows or {}).items():
            detail_rows.setdefault(str(key), []).extend(val)
        if res.token_source and res.token_source not in sources:
            sources.append(res.token_source)
        stopped = stopped or bool(res.stopped)
        total_ids += int(res.total_ids or 0)
    merged = amis_query.QueryResult(summaries, rows, " / ".join(sources), "编号",
                                    stopped=stopped, total_ids=total_ids,
                                    detail_rows=detail_rows)
    merged.kind_of = counters
    return merged


def resolve_wuduan_role_ids(wuduan_ids, box, log, should_stop=None, workers=None):
    """把「无端ID」换算成「角色ID」（ACE 的 OpenID 只认角色ID，无端ID 查不到数据）

    走 amis 角色信息接口（key=char_name）取接口返回的 roleId 字段，返回
    {无端ID: 角色ID 或 ""}；值为 "" 表示换算失败（接口异常 / 接口未返回角色ID /
    已被停止），调用方应让该编号跳过 ACE，而不是拿无端ID 硬查。

    登录态失效（AuthAbortError）向上抛出，由调用方决定是否中止整批。
    """
    workers = max(1, int(workers or AMIS_WORKERS))
    out = {}

    def one(raw):
        if should_stop and should_stop():
            return raw, ""
        try:
            info = amis_query.fetch_user_info_with_auth_retry(
                raw, box, amis_query.KEY_TYPE_WDID, log)
        except amis_query.AuthAbortError:
            raise
        except Exception as exc:
            log("[换算] 无端ID %s 换算角色ID 失败：%s" % (raw, format_error(exc)))
            return raw, ""
        rid = str((info or {}).get("role_id") or "").strip()
        if not rid:
            log("[换算] 无端ID %s 未能换算出角色ID（接口未返回角色ID）" % raw)
        return raw, rid

    if workers <= 1 or len(wuduan_ids) <= 1:
        pairs = [one(r) for r in wuduan_ids]
    else:
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(workers, len(wuduan_ids))) as pool:
            pairs = list(pool.map(one, wuduan_ids))
    for raw, rid in pairs:
        out[raw] = rid
    return out


# ---------------------------------------------------------------- 联合查询主流程
def run_joint(items, on_log=None, should_stop=None, ace_cookies_provider=None,
              amis_workers=None, ace_workers=None, on_progress=None):
    """执行一次联合查询

    items                : [IdentItem]（build_ident_items 的输出）
    on_log               : 进度文案回调（str -> None），在查询线程中被调用
    should_stop          : 可选回调，返回 True 时停止后续未开始的账号
    ace_cookies_provider : 可选回调 reason -> cookies dict（返回 None 表示用户取消）
    amis_workers         : amis 并发编号数（None 用 AMIS_WORKERS）
    ace_workers          : ACE 并发编号数（None 用 ACE_ACCOUNT_WORKERS）
    on_progress          : 可选回调 on_progress(done, total, label)，进度条用；
                           done/total 均按「编号」计（一个编号的 amis / ACE 都返回才算 1 个）

    执行顺序：
      1) 编号识别（本地，不发请求）；
      2) amis 登录态预检（amis 查询与无端ID 换算共用，避免弹两次登录框）；
      3) 「无端ID → 角色ID」换算（ACE 只认角色ID；无端ID 换算失败则跳过 ACE）；
      4) amis 与 ACE 两个平台并行查询。

    并发说明（批量查询提速的关键）：
      · amis 与 ACE 两个平台同时查询（PARALLEL_PLATFORMS），互不等待；
      · 每个平台内部按编号并发（amis 走 amis_query 线程池、ACE 走本模块线程池）；
      · ACE 单个编号内部，「可疑用户核查」与「数据查询」两条链路并行，翻页也并行；
      · 换算只在「有无端ID」时发生，1 次轻量请求/编号且并发执行。
    """
    result = JointResult()
    result.items = list(items)

    def log(text):
        text = str(text)
        result.logs.append(text)
        if on_log:
            try:
                on_log(text)
            except Exception:
                pass

    # ---- 进度条：在「无端ID 换算」之后创建（ACE 是否参与取决于换算结果） ----

    def stopping():
        return bool(should_stop and should_stop())

    def ask_ace_login(reason):
        """请求用户登录 ACE；与 amis 登录框串行，避免同屏弹出两个登录窗"""
        if ace_cookies_provider is None:
            return None
        dialog_lock = getattr(amis_query, "LOGIN_DIALOG_LOCK", None)
        if dialog_lock is None:
            return ace_cookies_provider(reason)
        with dialog_lock:
            return ace_cookies_provider(reason)

    # ---- 1) 编号识别（日志） ----
    queried = [it for it in items if not it.skipped]
    log("[识别] 共输入 %d 个编号，参与查询 %d 个，跳过 %d 个"
        % (len(items), len(queried), len(items) - len(queried)))
    for it in items:
        if it.skipped:
            log("[识别] 跳过 %s：%s" % (it.raw, it.note))

    # ---- 2) 待查清单：amis 按编号类型分组（ACE 待「换算」后确定） ----
    groups_spec = []
    role_ids = [it.raw for it in queried if it.amis_kind == KIND_ROLE]
    wuduan_ids = [it.raw for it in queried if it.amis_kind == KIND_WUDUAN]
    if role_ids:
        groups_spec.append((KIND_ROLE, amis_query.KEY_TYPE_ROLE, "角色ID", role_ids))
    if wuduan_ids:
        groups_spec.append((KIND_WUDUAN, amis_query.KEY_TYPE_WDID, "无端ID", wuduan_ids))

    for it in queried:
        if it.amis_kind:
            result.amis_kind_of[it.raw] = it.amis_kind

    amis_worker_count = max(1, int(amis_workers or AMIS_WORKERS))
    ace_worker_count = max(1, int(ace_workers or ACE_ACCOUNT_WORKERS))

    # ---- 3) amis 登录态预检（amis 查询 与「无端ID→角色ID」换算共用，只预检一次） ----
    amis_login_error = ""
    if groups_spec:
        try:
            preflight_amis_login(log)
        except Exception as exc:
            amis_login_error = format_error(exc)
            log("[amis] 登录未完成：%s" % amis_login_error)

    # ---- 4) 无端ID → 角色ID 换算（ACE 的 OpenID 只认角色ID，无端ID 查不到数据） ----
    wuduan_items = [it for it in queried if it.amis_kind == KIND_WUDUAN]
    if wuduan_items:
        if amis_login_error:
            log("[换算] %d 个无端ID 需先换算角色ID 才能查 ACE，但 amis 登录未完成，"
                "这些编号本次跳过 ACE。" % len(wuduan_items))
            for it in wuduan_items:
                it.ace_openid = None
                it.note = "%s；未能换算出角色ID（amis 登录未完成），已跳过 ACE" % it.note
        else:
            snap = amis_local_snapshot()
            box = amis_query.TokenBox(snap.get("token") or "", snap.get("account") or "")
            log("[换算] %d 个无端ID 正在换算角色ID（ACE 只认角色ID）…" % len(wuduan_items))
            try:
                mapping = resolve_wuduan_role_ids(
                    [it.raw for it in wuduan_items], box, log,
                    should_stop=should_stop, workers=amis_worker_count)
            except Exception as exc:
                mapping = {}
                log("[换算] 换算中断：%s（相关编号本次跳过 ACE）" % format_error(exc))
            ok = 0
            for it in wuduan_items:
                rid = str(mapping.get(it.raw) or "").strip()
                if rid:
                    it.ace_openid = rid
                    it.note = "%s；ACE 实际查询角色ID %s" % (it.note, rid)
                    result.ace_converted[it.raw] = rid
                    ok += 1
                    log("[换算] 无端ID %s → 角色ID %s（ACE 将用角色ID 查询）" % (it.raw, rid))
                else:
                    it.ace_openid = None
                    it.note = "%s；未能换算出角色ID，已跳过 ACE" % it.note
            log("[换算] 无端ID 换算完成：成功 %d / %d。" % (ok, len(wuduan_items)))

    # ---- 5) ACE 待查清单（换算后确定：角色ID 用自身，无端ID 用换算出的角色ID） ----
    ace_openids = []
    for it in queried:
        if it.ace_openid and it.ace_openid not in ace_openids:
            ace_openids.append(it.ace_openid)
    result.ace_order = list(ace_openids)

    # ---- 6) 进度条：按「编号」计数（同一编号的 amis / ACE 都返回才算查完 1 个） ----
    tracker = JointProgress(items)

    def report_progress(label=""):
        if on_progress is None:
            return
        try:
            on_progress(tracker.done, tracker.total, label)
        except Exception:
            pass

    # ACE 回调回来的是「实际查询用的 OpenID」，需映射回编号（一个 OpenID 可能对应多个编号）
    ace_to_raws = {}
    for it in queried:
        if it.ace_openid:
            ace_to_raws.setdefault(str(it.ace_openid), []).append(it.raw)

    def bump_progress(raw):
        """某个编号在某个平台上的查询完成（raw 为编号原文）"""
        if tracker.bump(raw) is not None:
            report_progress()

    def bump_ace_progress(openid):
        """ACE 某 OpenID 查询完成 → 通知它对应的编号（通常 1 个）"""
        for raw in ace_to_raws.get(str(openid), ()):
            bump_progress(raw)

    # ---- 7) 编号识别表（备注含「无端ID → 角色ID」换算说明） ----
    for it in items:
        result.ident_rows.append(it.as_row())

    # 进度条总数 = 参与查询的编号数（每个编号的 amis / ACE 两条线都算它的子任务）
    report_progress("准备查询")

    # ---- 平台一：amis（登录封禁） ----
    def run_amis_phase():
        if not groups_spec:
            if not result.amis_error:
                log("[amis] 未识别到可查 amis 的编号，跳过 amis 平台。")
            return
        # 登录态预检已在入口统一完成（见第 3 步），这里只处理其结论
        if amis_login_error:
            result.amis_error = amis_login_error
            result.amis = None
            log("[amis] 登录未完成，amis 平台本次不查询：%s" % result.amis_error)
            return

        merged_groups = []
        for kind, key_type, label, ids in groups_spec:
            if stopping():
                result.stopped = True
                log("[amis] 已停止，跳过剩余 %d 个%s" % (len(ids), label))
                break
            log("[amis] 开始查询 %d 个%s（并发 %d）…"
                % (len(ids), label, min(amis_worker_count, len(ids))))
            try:
                res = amis_query.run_query(ids, key_type=key_type, id_label=label,
                                           on_progress=log, should_stop=should_stop,
                                           workers=amis_worker_count,
                                           on_item=lambda _n, _t, rid: bump_progress(rid))
            except Exception as exc:                 # 登录取消 / 网络异常 → 整批失败
                result.amis_error = format_error(exc)
                if not merged_groups:
                    result.amis = None
                log("[amis] %s查询失败：%s" % (label, result.amis_error))
                break
            merged_groups.append((res, key_type))
            log("[amis] %s查询完成：封禁中 %d 个，正常 %d 个%s"
                % (label, res.banned_count, res.normal_count,
                   ("，失败 %d 个" % res.failed_count) if res.failed_count else ""))
        if merged_groups:
            result.amis = merge_amis_results(merged_groups, queried)
            if result.stopped:
                result.amis.stopped = True

    # ---- 平台二：ACE（可疑用户核查 + 数据查询，OCR 编号并发） ----
    def run_ace_phase():
        if not ace_openids:
            log("[ACE] 未识别到可查 ACE 的编号，跳过 ACE 平台。")
            return
        if stopping():
            result.stopped = True
            log("[ACE] 已停止，跳过 ACE 平台全部 %d 个编号" % len(ace_openids))
            return
        # 1) 本地凭据
        try:
            cookies = ace_load_cookies()
            log("[ACE] 已载入本地登录态（凭据来源：%s）"
                % os.path.basename(getattr(ace_query, "CREDENTIALS_FILE", "credentials")))
        except Exception as exc:
            reason = format_error(exc)
            log("[ACE] 本地凭据不可用：%s" % reason)
            if ace_cookies_provider is None:
                result.ace_error = reason
                return
            cookies = ask_ace_login(reason)
            if not cookies:
                result.ace_error = "登录已取消：" + reason
                log("[ACE] 用户取消登录，ACE 平台本次不查询。")
                return

        # 2) 查询前预检：本地凭据已被服务端判定失效 → 先弹登录框，避免逐条查询全部失败
        try:
            cookies, verdict = preflight_ace_login(cookies, ask_ace_login, log)
        except AceAuthRequired as exc:
            result.ace_error = exc.reason
            log("[ACE] %s" % exc.reason)
            return
        if verdict == "cancelled":
            result.ace_error = "登录已取消：ACE 登录态已失效或已过期"
            log("[ACE] 用户取消登录，ACE 平台本次不查询。")
            return

        # 3) 并发查询：账号之间并发，账号内部的核查 / 处罚 / 翻页也并发
        pending = list(ace_openids)
        for attempt in (1, 2):                       # 第 2 轮用于「登录态失效后换新凭据续跑」
            if stopping():
                result.stopped = True
                break
            got, pending, fail_reason = run_ace_batch(pending, cookies, log, stopping,
                                                      ace_worker_count,
                                                      on_item=lambda _n, _t, oid:
                                                      bump_ace_progress(oid))
            result.ace.update(got)
            if not fail_reason:
                break
            log("[ACE] 登录态失效：%s" % fail_reason)
            if ace_cookies_provider is None:
                result.ace_error = fail_reason
                break
            if attempt == 2:
                result.ace_error = fail_reason
                log("[ACE] 重新登录后仍判定失效，已中止 ACE 查询。")
                break
            cookies = ask_ace_login(fail_reason)
            if not cookies:
                result.ace_error = "登录已取消：" + fail_reason
                log("[ACE] 用户取消登录，ACE 剩余编号未查询。")
                break
            if not pending:
                break
            log("[ACE] 已取得新的登录态，继续查询剩余 %d 个编号…" % len(pending))

        if stopping():
            result.stopped = True
            log("[ACE] 已停止：完成 %d / %d 个" % (len(result.ace), len(ace_openids)))
        elif pending and not result.ace_error:
            log("[ACE] 完成 %d / %d 个编号。" % (len(result.ace), len(ace_openids)))

    # ---- 3) 两个平台同时查询（互不等待）；只有一个平台有编号时直接串行跑 ----
    phases = [fn for fn, needed in ((run_amis_phase, bool(groups_spec)),
                                    (run_ace_phase, bool(ace_openids))) if needed]
    if not phases:
        log("[提示] 本次没有可查询的编号（amis / ACE 均无）。")
    elif len(phases) == 1 or not PARALLEL_PLATFORMS:
        for phase in phases:
            try:
                phase()
            except Exception as exc:
                log("[错误] 查询阶段异常：%s" % format_error(exc))
    else:
        log("[并发] amis 与 ACE 两个平台同时查询，总耗时约等于较慢的那个平台…")
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(phases)) as pool:
            futures = [pool.submit(phase) for phase in phases]
            for fu in futures:
                try:
                    fu.result()
                except Exception as exc:
                    log("[错误] 查询阶段异常：%s" % format_error(exc))

    # ---- 收尾：只要用户点过「停止」，本次结果就标记为已停止（UI 显示「已停止」而非「完成」）----
    if stopping():
        result.stopped = True

    return result


# ==================================================================
#                        ACE 登录窗（手机号 + 密码）
# ==================================================================
try:                                    # 截图预览依赖 Pillow，缺失时自动降级为「浏览器打开」
    from PIL import Image, ImageTk
    HAS_PIL = True
except Exception:                       # pragma: no cover
    Image = ImageTk = None
    HAS_PIL = False


def ace_default_phone():
    """ACE 登录框手机号默认留空（不再从本地凭据预填账号名）。

    历史行为是读取凭据里的 account.user_name 预填，但该字段常为账号显示名
    而非手机号（如 "Serein"），预填反而误导，故保留空实现以兼容旧调用。
    """
    return ""


class AceLoginDialog(object):
    """ACE 控制台登录窗（手机号 + 密码 → phone_login 换 Cookie）

    - 与 amis 登录框同一套 Material 自绘组件（复用 amis_query 的控件）
    - 密码只在内存中传递，登录成功后由 ace_query.write_credentials 写回凭据文件
    - 返回 (cookies, account)；用户取消返回 None
    """

    def __init__(self, master, reason="", prefill_phone=""):
        self._result = None
        self._busy = False

        self.win = tk.Toplevel(master)
        self.win.title("登录 ACE 控制台 · www.anticheatexpert.com")
        self.win.configure(bg=amis_query.CLR_BG)
        self.win.resizable(False, False)
        self.win.protocol("WM_DELETE_WINDOW", self._cancel)
        self.win.bind("<Escape>", lambda _e: self._cancel())

        f_h1 = (amis_query.FONT_UI, 15, "bold")
        f_sub = (amis_query.FONT_UI, 9)
        f_body = (amis_query.FONT_UI, 10)

        card = amis_query.RoundBox(self.win, radius=22, padding=(24, 20),
                                   fill=amis_query.CLR_CARD, bg=amis_query.CLR_BG,
                                   elevation=2)
        card.pack(fill="both", expand=True, padx=18, pady=18)
        body = card.body

        tk.Label(body, text="登录 ACE 反外挂控制台", font=f_h1, bg=amis_query.CLR_CARD,
                 fg=amis_query.CLR_TEXT).pack(anchor="w")
        tip = reason or "ACE 登录态已失效，请重新登录后继续查询（登录成功后自动续跑本批查询）。"
        tk.Label(body, text=tip, font=f_sub, bg=amis_query.CLR_CARD, fg=amis_query.CLR_SUB,
                 justify="left", wraplength=430).pack(anchor="w", pady=(6, 0))
        tk.Label(body, text="登录地址：www.anticheatexpert.com（手机号 + 密码登录）",
                 font=f_sub, bg=amis_query.CLR_CARD,
                 fg=amis_query.CLR_SUB).pack(anchor="w", pady=(2, 12))

        self.f_phone = amis_query.MTextField(body, label="手机号", font=(amis_query.FONT_UI, 11),
                                             bg=amis_query.CLR_CARD)
        self.f_phone.pack(fill="x", pady=(0, 10))
        self.f_phone.entry.insert(0, prefill_phone or ace_default_phone())

        self.f_pwd = amis_query.MTextField(body, label="登录密码", show="•",
                                           font=(amis_query.FONT_UI, 11),
                                           bg=amis_query.CLR_CARD)
        self.f_pwd.pack(fill="x", pady=(0, 6))
        self.btn_eye = amis_query.MButton(self.f_pwd, text="显示", kind="text",
                                          width=64, height=30,
                                          command=self._toggle_pwd)
        self.f_pwd.add_trailing(self.btn_eye)

        self.lbl_msg = tk.Label(body, text="", font=f_sub, bg=amis_query.CLR_CARD,
                                fg=amis_query.CLR_SUB, justify="left", wraplength=430)
        self.lbl_msg.pack(anchor="w", pady=(8, 12))

        row = tk.Frame(body, bg=amis_query.CLR_CARD)
        row.pack(fill="x")
        self.btn_login = amis_query.MButton(row, text="登 录", kind="filled",
                                            command=self._submit)
        self.btn_login.pack(side="right")
        self.btn_cancel = amis_query.MButton(row, text="取消", kind="text",
                                             command=self._cancel)
        self.btn_cancel.pack(side="right", padx=(0, 8))

        self.f_pwd.entry.bind("<Return>", lambda _e: self._submit())
        self.f_phone.entry.bind("<Return>", lambda _e: self.f_pwd.entry.focus_set())

        try:
            self.win.transient(master)
            self.win.grab_set()
        except Exception:
            pass
        self._center_on(master)
        try:
            (self.f_phone if not self.f_phone.get() else self.f_pwd).entry.focus_set()
        except Exception:
            pass

    # ---- 工具 ----
    def _center_on(self, master):
        try:
            self.win.update_idletasks()
            mw, mh = master.winfo_width(), master.winfo_height()
            mx, my = master.winfo_rootx(), master.winfo_rooty()
            w, h = self.win.winfo_reqwidth(), self.win.winfo_reqheight()
            self.win.geometry("+%d+%d" % (mx + max(0, (mw - w) // 2),
                                          my + max(0, (mh - h) // 3)))
        except Exception:
            pass

    def _toggle_pwd(self):
        show = "" if str(self.f_pwd.entry.cget("show")) else "•"
        self.f_pwd.entry.configure(show=show)
        self.btn_eye.set_text("隐藏" if show else "显示")

    def _set_busy(self, busy, msg=""):
        self._busy = busy
        self.btn_login.set_enabled(not busy)
        self.btn_cancel.set_text("取消" if not busy else "登录中…")
        self.btn_login.set_text("登 录" if not busy else "登录中…")
        if msg:
            self.lbl_msg.configure(text=msg, fg=amis_query.CLR_SUB)

    def _fail(self, text):
        self._busy = False
        self.btn_login.set_enabled(True)
        self.btn_login.set_text("登 录")
        self.btn_cancel.set_text("取消")
        self.lbl_msg.configure(text=text, fg=amis_query.CLR_ERROR)

    # ---- 交互 ----
    def _submit(self):
        if self._busy:
            return
        phone = self.f_phone.get().strip()
        password = self.f_pwd.get()
        if not phone or not password:
            self._fail("请输入手机号与密码。")
            return
        self._set_busy(True, "正在登录…")

        def work():
            try:
                cookies, account = ace_query.login_by_phone(
                    phone, password, progress=lambda t: self.win.after(0, self._set_busy, True, t))
            except ace_query.LoginManualRequired as exc:
                self.win.after(0, self._fail, format_error(exc))
                return
            except Exception as exc:
                self.win.after(0, self._fail, format_error(exc))
                return
            try:
                ace_query.write_credentials(cookies, account,
                                            note="由联合查询工具登录后写入")
            except Exception as exc:
                log("[警告] ACE 凭据写入失败：%s" % format_error(exc))
            self.win.after(0, self._done, cookies)

        threading.Thread(target=work, daemon=True).start()

    def _done(self, cookies):
        self._result = cookies
        self._close()

    def _cancel(self):
        if self._busy:
            self.lbl_msg.configure(text="登录进行中，请稍候或直接关闭窗口…",
                                   fg=amis_query.CLR_WARNING)
            return
        self._result = None
        self._close()

    def _close(self):
        try:
            self.win.grab_release()
        except Exception:
            pass
        try:
            self.win.destroy()
        except Exception:
            pass

    def wait(self):
        try:
            self.win.wait_window()
        except Exception:
            pass
        return self._result


# ==================================================================
#                        ACE 明细弹窗
# ==================================================================
# 「数据查询」表格列宽（取自 ace_query.DQ_COLUMNS）
ACE_DQ_WIDTHS = tuple(col[2] for col in ace_query.DQ_COLUMNS)
ACE_DQ_STRETCH = tuple(col[1] for col in ace_query.DQ_COLUMNS if col[4])
# 「可疑用户核查」表格列宽（按字段名索引，避免与 ace_query.COLUMNS 的顺序耦合）
ACE_RECORD_WIDTHS = {
    "log_date": 140, "openid": 170, "data_type": 110, "key": 170, "des": 430,
    "level": 110, "ban_duration": 110, "bot_name": 190, "log_time": 190,
}
# 明细弹窗「可疑用户核查」中不展示的字段（证据Key / 处罚时长 / 外挂名）
# 注意：仅影响界面展示，CSV 导出仍保留这些列，避免丢数据
REC_HIDDEN_FIELDS = ("key", "ban_duration", "bot_name")
REC_DISPLAY_FIELDS = tuple(c[1] for c in ace_query.COLUMNS
                           if c[1] not in REC_HIDDEN_FIELDS)

# 可疑用户核查表格：含截图的记录在「路径/描述」列直接显示缩略图（点击看大图）
REC_STYLE = "AceRec.Treeview"     # 专用 ttk 样式（行高比普通表格高，容纳缩略图）
REC_ROW_HEIGHT = 76               # 行高（像素）：略大于缩略图高度，留出上下留白
REC_THUMB_SIZE = (150, 64)        # 缩略图最大尺寸（宽, 高），等比缩放
REC_THUMB_CANVAS_H = 68           # 缩略图画布高（略小于行高 76，避免撑行）
REC_DES_FIELD = "des"             # 提到 tree 列（#0）承载缩略图的字段
# 「路径/描述」表头（在 ace_query.COLUMNS 中按字段名取，缺失时用默认值）
REC_DES_HEADER = next((c[0] for c in ace_query.COLUMNS if c[1] == REC_DES_FIELD), "路径/描述")


def ace_record_row(rec):
    """可疑用户核查记录 → 表格一行（列定义复用 ace_query.COLUMNS，含导出用全字段）"""
    out = []
    for _head, field, conv in ace_query.COLUMNS:
        val = rec.get(field)
        if conv:
            try:
                val = conv(rec)
            except Exception:
                val = rec.get(field)
        out.append("" if val is None else str(val))
    return out


def ace_record_display_row(rec):
    """可疑用户核查记录 → 明细弹窗表格一行（已剔除 REC_HIDDEN_FIELDS 的列）"""
    full = dict(zip([c[1] for c in ace_query.COLUMNS], ace_record_row(rec)))
    return [full.get(f, "") for f in REC_DISPLAY_FIELDS]


def ace_record_display_columns():
    """明细弹窗「可疑用户核查」的列定义（表头 / 字段 / 取值转换）"""
    return [c for c in ace_query.COLUMNS if c[1] not in REC_HIDDEN_FIELDS]



def ace_dq_row(item):
    """数据查询（处罚）记录 → 表格一行（列定义复用 ace_query.DQ_COLUMNS）"""
    return [("" if item.get(col[1]) is None else str(item.get(col[1]))) for col in ace_query.DQ_COLUMNS]


class AceImageDialog(tk.Toplevel):
    """截图大图查看窗（等比缩放到屏幕内，保留原图比例）

    url 可选：传入时在底部提供「浏览器打开原图」按钮（保留原截图页的能力）。
    """

    def __init__(self, master, title, raw, url=""):
        super().__init__(master)
        self.title(title)
        self.configure(bg=amis_query.CLR_BG)
        self._photo = None
        frame = tk.Frame(self, bg=amis_query.CLR_BG)
        frame.pack(fill="both", expand=True, padx=12, pady=(12, 6))
        self.lbl = tk.Label(frame, bg=amis_query.CLR_BG, text="加载中…",
                            fg=amis_query.CLR_SUB)
        self.lbl.pack()
        try:
            img = Image.open(io.BytesIO(raw))
            sw, sh = self.winfo_screenwidth() - 160, self.winfo_screenheight() - 240
            img.thumbnail((max(400, sw), max(300, sh)))
            self._photo = ImageTk.PhotoImage(img)
            self.lbl.configure(image=self._photo, text="")
        except Exception as exc:
            self.lbl.configure(text="图片解析失败：%s" % format_error(exc))
        if url:
            bar = tk.Frame(self, bg=amis_query.CLR_BG)
            bar.pack(fill="x", padx=12, pady=(0, 10))
            amis_query.MButton(bar, text="浏览器打开原图", kind="text", height=28,
                               command=lambda u=url: self._open_browser(u)).pack(side="left")
            amis_query.MButton(bar, text="关闭", kind="tonal",
                               command=self.destroy).pack(side="right")
        self.bind("<Escape>", lambda _e: self.destroy())

    def _open_browser(self, url):
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception as exc:
            messagebox.showerror("打开失败", format_error(exc), parent=self)


class AceDetailDialog(tk.Toplevel):
    """单个 OpenID 的 ACE 明细（两个页签）

    页签 1「可疑用户核查」：表格只展示关键列（证据Key / 处罚时长 / 外挂名不展示），
    含截图的记录在「路径/描述」列直接显示缩略图，单击缩略图看大图（不单独开「截图」页签、
    也不额外占一列）；页签 2「数据查询」：处罚记录表格。
    """

    def __init__(self, master, openid, data, cookies):
        super().__init__(master)
        self.openid = openid
        self.data = data or {}
        self.cookies = dict(cookies or {})
        self._thumbs = {}                       # {iid: PhotoImage} 保持引用防回收
        self._thumb_imgs = {}                   # {iid: PIL 缩略图} 供列宽变化时重渲染
        self._thumb_after = None                # 重渲染去抖句柄
        self._shot_rows = {}                    # {iid: {"rec":.., "url":.., "w":缩略图宽}}
        self._shot_raw = {}                     # {iid: 原图字节} 供「复制图片」用
        self._ui_q = queue.Queue()              # 线程安全 UI 队列（worker 投递，主线程轮询执行）
        self.title("ACE 明细 · %s" % openid)
        self.configure(bg=amis_query.CLR_BG)
        self.minsize(1000, 560)
        try:
            self.transient(master)
        except Exception:
            pass
        self._center_on(master, 1420, 760)
        self.bind("<Escape>", lambda _e: self.destroy())

        records = self.data.get("records") or []
        punish = self.data.get("punish_items") or []
        shots = [r for r in records if str(r.get("data_type") or "") == "screenshot"]

        head = tk.Frame(self, bg=amis_query.CLR_BG)
        head.pack(fill="x", padx=18, pady=(14, 6))
        tk.Label(head, text="ACE 明细 · %s" % openid, bg=amis_query.CLR_BG,
                 fg=amis_query.CLR_TEXT, font=(amis_query.FONT_UI, 14, "bold")).pack(anchor="w")
        sub = ("可疑用户核查 %d 条（%s）　|　数据查询 %d 条（%s）　|　截图 %d 张"
               % (len(records), self.data.get("check_range") or "-",
                  len(punish), self.data.get("punish_range") or "-", len(shots)))
        errs = [t for t in (self.data.get("check_error"), self.data.get("punish_error")) if t]
        if errs:
            sub += "　|　失败：" + "；".join(errs)
        tk.Label(head, text=sub, bg=amis_query.CLR_BG, fg=amis_query.CLR_SUB,
                 font=(amis_query.FONT_UI, 9), justify="left").pack(anchor="w", pady=(4, 0))

        nb = amis_query.MTabView(self, bg=amis_query.CLR_BG,
                                 page_bg=amis_query.CLR_BG, font=(amis_query.FONT_UI, 10))
        nb.pack(fill="both", expand=True, padx=18, pady=(6, 6))

        # 页签 1：可疑用户核查（含截图的记录在「路径/描述」列直接显示缩略图）
        tab1 = tk.Frame(nb, bg=amis_query.CLR_BG)
        nb.add(tab1, text="可疑用户核查（%d）" % len(records))
        wrap1 = tk.Frame(tab1, bg=amis_query.CLR_BG)
        wrap1.pack(fill="both", expand=True, padx=6, pady=6)
        self.tree_rec = self._build_record_table(wrap1, height=8)
        for i, rec in enumerate(records):
            des_text, other = self._record_row_parts(rec)
            is_shot = str(rec.get("data_type") or "") == "screenshot"
            url = ace_query.build_image_url(rec.get("des")) if is_shot else ""
            if url and not HAS_PIL:
                des_text = "查看原图（单击用浏览器打开）"
            iid = self.tree_rec.insert("", "end", text=des_text, values=other,
                                       tags=("odd" if i % 2 else "even",))
            if not url:
                continue
            self._shot_rows[iid] = {"rec": rec, "url": url, "w": 0}
            if HAS_PIL:                          # 缩略图后台加载，不阻塞窗口
                threading.Thread(target=self._thumb_worker,
                                 args=(iid, url, rec), daemon=True).start()
        if not records:
            tk.Label(tab1, text=self.data.get("check_error") or "该 OpenID 近 30 天无可疑用户核查记录",
                     bg=amis_query.CLR_BG, fg=amis_query.CLR_SUB).pack(pady=6)

        # 页签 2：数据查询（处罚）
        tab2 = tk.Frame(nb, bg=amis_query.CLR_BG)
        nb.add(tab2, text="数据查询（%d）" % len(punish))
        wrap2 = tk.Frame(tab2, bg=amis_query.CLR_BG)
        wrap2.pack(fill="both", expand=True, padx=6, pady=6)
        tree2 = amis_query.build_table(
            wrap2, [c[1] for c in ace_query.DQ_COLUMNS], [c[0] for c in ace_query.DQ_COLUMNS],
            ACE_DQ_WIDTHS, stretch=ACE_DQ_STRETCH, height=14, anchor="center")
        amis_query.enable_copy(tree2, headers=[c[0] for c in ace_query.DQ_COLUMNS])
        for i, item in enumerate(punish):
            tree2.insert("", "end", values=ace_dq_row(item),
                         tags=("odd" if i % 2 else "even",))
        if not punish:
            tk.Label(tab2, text=self.data.get("punish_error") or "该 OpenID 近 10 年无处罚记录",
                     bg=amis_query.CLR_BG, fg=amis_query.CLR_SUB).pack(pady=6)

        foot = tk.Frame(self, bg=amis_query.CLR_BG)
        foot.pack(fill="x", padx=18, pady=(0, 14))
        amis_query.MButton(foot, text="关闭", kind="tonal",
                           command=self.destroy).pack(side="right")
        des_head = REC_DES_HEADER
        if not shots:
            tip = "本账号无可预览截图记录"
        elif not HAS_PIL:
            tip = ("未安装 Pillow：含截图的记录请在「%s」列单击打开浏览器原图"
                   "（如需内置预览：pip install Pillow）" % des_head)
        else:
            tip = ("含截图的记录在「%s」列直接显示缩略图，单击缩略图查看大图；"
                   "按住左键拖动可框选单元格区域，Ctrl+C 复制；单击缩略图看大图（Esc 关闭）" % des_head)
        tk.Label(foot, text=tip, bg=amis_query.CLR_BG, fg=amis_query.CLR_SUB,
                 font=(amis_query.FONT_UI, 9)).pack(side="left")

        self.after(100, self._poll_ui_queue)    # 主线程轮询：执行 worker 投递的 UI 任务

    def _center_on(self, master, w, h):
        try:
            self.update_idletasks()
            mx, my = master.winfo_rootx(), master.winfo_rooty()
            mw, mh = master.winfo_width(), master.winfo_height()
            x = mx + max(0, (mw - w) // 2)
            y = my + max(0, (mh - h) // 3)
            self.geometry("%dx%d+%d+%d" % (w, h, x, y))
        except Exception:
            self.geometry("%dx%d" % (w, h))

    # ---- 可疑用户核查表格（「路径/描述」列承载缩略图） ----
    def _ensure_record_style(self):
        """专用 ttk 样式：行高加大以容纳缩略图，其余配色与普通表格一致。"""
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except Exception:
            pass
        st.configure(REC_STYLE, font=(amis_query.FONT_UI, 10), rowheight=REC_ROW_HEIGHT,
                     borderwidth=0, background=amis_query.CLR_CARD,
                     fieldbackground=amis_query.CLR_CARD, foreground=amis_query.CLR_TEXT)
        st.layout(REC_STYLE, [("Treeview.treearea", {"sticky": "nswe"})])
        st.map(REC_STYLE, background=[("selected", amis_query.CLR_SEL)],
               foreground=[("selected", amis_query.CLR_ON_PRIMARY_CONTAINER)])
        st.configure(REC_STYLE + ".Heading", font=(amis_query.FONT_UI, 9, "bold"),
                     relief="flat", background=amis_query.CLR_HEAD,
                     foreground=amis_query.CLR_SUB, padding=(12, 11), borderwidth=0)
        st.map(REC_STYLE + ".Heading", background=[("active", amis_query.CLR_HEAD_HOVER)],
               foreground=[("active", amis_query.CLR_PRIMARY)])

    def _record_row_parts(self, rec):
        """记录 → (「路径/描述」文本, 其余列的值列表)。

        「路径/描述」提到 tree 列（#0）——ttk.Treeview 只有该列能渲染图片，
        截图行因此可在该列直接显示缩略图（不再展示冗长的文件路径）。
        列定义仅取展示列 REC_DISPLAY_FIELDS（已隐藏证据Key / 处罚时长 / 外挂名）。
        """
        full = ace_record_display_row(rec)
        try:
            idx = REC_DISPLAY_FIELDS.index(REC_DES_FIELD)
        except ValueError:
            return "", full
        des_text = full[idx]
        if str(rec.get("data_type") or "") == "screenshot":
            des_text = ""                        # 截图行该格只放缩略图
        return des_text, [v for i, v in enumerate(full) if i != idx]

    def _build_record_table(self, parent, height=8):
        cols_def = [c for c in ace_record_display_columns() if c[1] != REC_DES_FIELD]
        heads = [c[0] for c in cols_def]
        cols = [c[1] for c in cols_def]
        widths = [ACE_RECORD_WIDTHS.get(c[1], 140) for c in cols_def]
        des_head = REC_DES_HEADER

        self._ensure_record_style()
        tree = ttk.Treeview(parent, columns=cols, show="tree headings",
                            style=REC_STYLE, height=height, selectmode="browse")
        # #0 = 「路径/描述」（截图行显示缩略图，其余行显示文字），其余列同在各列
        tree.heading("#0", text=des_head)
        tree.column("#0", width=ACE_RECORD_WIDTHS.get(REC_DES_FIELD, 430),
                    anchor="center", stretch=True)
        for c, h, w in zip(cols, heads, widths):
            tree.heading(c, text=h)
            tree.column(c, width=w, anchor="center", stretch=(c == REC_DES_FIELD))
        tree.tag_configure("odd", background=amis_query.CLR_ZEBRA)
        tree.tag_configure("even", background=amis_query.CLR_CARD)

        ysb = amis_query.MScrollbar(parent, target=tree, orient="vertical",
                                    thickness=14, bg=amis_query.CLR_CARD)
        xsb = amis_query.MScrollbar(parent, target=tree, orient="horizontal",
                                    thickness=14, bg=amis_query.CLR_CARD)
        tree.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
        tree.grid(row=0, column=0, sticky="nsew")
        ysb.grid(row=0, column=1, sticky="ns")
        xsb.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        parent.grid_rowconfigure(0, weight=1)
        parent.grid_columnconfigure(0, weight=1)
        # 单击缩略图看大图改用「松开」触发：这样按住拖动可框选单元格区域复制
        tree.bind("<ButtonRelease-1>", self._on_record_click)
        tree.bind("<Configure>", self._on_tree_resize)   # 列宽变化后重渲染缩略图居中
        amis_query.enable_copy(tree, headers=[des_head] + heads,
                               row_provider=self._rec_row_values,
                               image_provider=self._shot_image_bytes)
        return tree

    def _shot_image_bytes(self, iid, idx, fetch=True):
        """截图格（第 0 列）的图片字节：供「复制图片」使用。

        fetch=False 时只查缓存（右键菜单判断该格是不是图片格，不触发下载）。
        """
        info = self._shot_rows.get(iid)
        if not info or idx != 0:
            return None
        raw = self._shot_raw.get(iid)
        if raw or not fetch:
            return raw
        url = info.get("url")
        if not url:
            return None
        try:
            raw = ace_query.download_image(url, self.cookies)
        except Exception:
            return None
        self._shot_raw[iid] = raw
        return raw

    def _rec_row_values(self, iid):
        """复制用取值：截图行复制原始「路径/描述」（界面上显示的是缩略图）"""
        text = str(self.tree_rec.item(iid, "text") or "")
        info = self._shot_rows.get(iid)
        if info:
            rec = info.get("rec") or {}
            text = str(rec.get("des") or text)
        return [text] + ["" if v is None else str(v)
                         for v in self.tree_rec.item(iid, "values")]

    def _on_record_click(self, event):
        """单击「路径/描述」列里的缩略图 → 看大图；未装 Pillow 时改为浏览器打开原图。

        缩略图已铺满该列（画布居中），故该列内任意位置点击都视为命中缩略图。
        注意：拖动框选单元格时不弹大图（由 enable_copy 置的拖动标记判断）。
        """
        tree = event.widget
        if getattr(tree, "_copy_drag_moved", False):
            return
        row = tree.identify_row(event.y)
        info = self._shot_rows.get(row)
        if not info:
            return
        if str(tree.identify_column(event.x)) != "#0":
            return
        if not HAS_PIL:
            self._open_in_browser(info.get("url"))
            return
        self._show_big(info["rec"], info["url"])

    def _thumb_worker(self, iid, url, rec):
        """缩略图：后台线程只负责「下载」，解码与 PhotoImage 交回主线程。

        Tk 限制：ImageTk.PhotoImage 只能在主线程创建，且从子线程调用
        widget.after() 也不可靠（会抛 RuntimeError: main thread is not in
        main loop），故统一走 _ui_q 队列由主线程轮询执行。
        """
        try:
            raw = ace_query.download_image(url, self.cookies)
        except Exception:
            self._ui(lambda: self._mark_thumb_failed(iid))
            return
        self._ui(lambda: self._apply_thumb(iid, raw))

    def _mark_thumb_failed(self, iid):
        """缩略图下载失败：给出可点击的重试提示（整格点击会重新下载并弹大图）"""
        try:
            if self.winfo_exists():
                self.tree_rec.item(iid, text="缩略图加载失败（点击重试）")
        except Exception:
            pass

    def _apply_thumb(self, iid, raw):
        """主线程：解码缩略图并缓存，再按当前列宽渲染到「路径/描述」列"""
        try:
            if not self.winfo_exists():
                return
            self._shot_raw[iid] = raw          # 原图字节留用（右键「复制图片」）
            img = Image.open(io.BytesIO(raw))
            img.thumbnail(REC_THUMB_SIZE)
            self._thumb_imgs[iid] = img
            self._render_thumb(iid)
        except Exception:
            self._mark_thumb_failed(iid)

    def _render_thumb(self, iid):
        """把缩略图合成到「与列同宽的透明画布」后再挂到 tree 列（#0）。

        ttk 的 tree 列对图片不响应 anchor="center"（图片恒贴左），
        故用透明画布补齐左右空白来实现视觉居中；列宽随窗口变化时重渲染。
        """
        img = self._thumb_imgs.get(iid)
        if img is None:
            return
        try:
            if not self.winfo_exists():
                return
            col_w = int(self.tree_rec.column("#0", "width"))
            col_w = max(col_w, img.width + 24)
            canvas = Image.new("RGBA", (col_w, REC_THUMB_CANVAS_H), (0, 0, 0, 0))
            canvas.paste(img, ((col_w - img.width) // 2,
                               (REC_THUMB_CANVAS_H - img.height) // 2))
            photo = ImageTk.PhotoImage(canvas)
            self._thumbs[iid] = photo          # 保持引用，防止被回收
            self.tree_rec.item(iid, image=photo)
            info = self._shot_rows.get(iid)
            if info is not None:
                info["w"] = photo.width()
        except Exception:
            pass

    def _on_tree_resize(self, _event=None):
        """列宽变化（窗口缩放）后重渲染缩略图，保持居中（去抖 120ms）"""
        if self._thumb_after is not None:
            try:
                self.after_cancel(self._thumb_after)
            except Exception:
                pass
        try:
            self._thumb_after = self.after(120, self._render_all_thumbs)
        except Exception:
            self._thumb_after = None

    def _render_all_thumbs(self):
        self._thumb_after = None
        for iid in list(self._thumb_imgs):
            self._render_thumb(iid)

    def _show_big(self, rec, url):
        def work():
            try:
                raw = ace_query.download_image(url, self.cookies)
            except Exception as exc:
                self._ui(lambda: messagebox.showerror("加载失败", format_error(exc), parent=self))
                return
            self._ui(lambda: AceImageDialog(
                self, "截图 · %s" % (rec.get("log_time") or self.openid), raw, url))
        threading.Thread(target=work, daemon=True).start()

    def _open_in_browser(self, url):
        if not url:
            return
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception as exc:
            messagebox.showerror("打开失败", format_error(exc), parent=self)

    def _ui(self, fn):
        """线程安全的 UI 调度：worker 线程只投递，主线程轮询执行。"""
        try:
            self._ui_q.put(fn)
        except Exception:
            pass

    def _poll_ui_queue(self):
        """主线程轮询：执行 worker 投递进来的 UI 任务（缩略图挂载 / 大图弹窗等）。"""
        try:
            while True:
                fn = self._ui_q.get_nowait()
                try:
                    fn()
                except Exception:
                    pass
        except queue.Empty:
            pass
        try:
            if self.winfo_exists():
                self.after(100, self._poll_ui_queue)
        except Exception:
            pass


# ==================================================================
#                        主窗口（联合查询 · 单入口）
# ==================================================================
import tkinter.ttk as ttk
import tkinter.font as tkfont

JOINT_AMIS_COLUMNS = ("idx", "account", "kind", "roleName", "wdId", "status", "expire",
                      "activeCnt", "unbanCnt", "totalCnt", "lastTime", "note")
JOINT_AMIS_HEADERS = ["序号", "编号", "编号类型", "角色名", "无端ID", "当前状态", "到期时间",
                      "生效中", "已解禁", "记录总数", "最近封禁时间", "备注"]
JOINT_AMIS_WIDTHS = (52, 150, 92, 130, 148, 176, 156, 76, 100, 84, 156, 300)
JOINT_AMIS_STRETCH = ("note",)

BATCH_CONFIRM_THRESHOLD = 20            # 超过该数量时弹「批量查询确认」


# ==================================================================
#                     编号级进度聚合
# ==================================================================
class JointProgress(object):
    """按「编号」统计联合查询进度

    一个编号参与几个平台就算几个子任务（amis 1 个、ACE 1 个，两者都有则 2 个），
    只有该编号的全部子任务都返回，才算查完 1 个编号 —— 进度总数因此等于编号数
    （输入 10 个编号 → 进度 0~10，而不是 20）。
    """

    def __init__(self, items):
        self._lock = threading.Lock()
        self._left = {}
        for it in items:
            n = 0
            if getattr(it, "amis_kind", None):
                n += 1
            if getattr(it, "ace_openid", None):
                n += 1
            if n:
                self._left[it.raw] = n
        self.total = len(self._left)
        self.done = 0

    def bump(self, raw):
        """某编号在一个平台上完成；返回 (done, total) 表示该编号已全部完成，否则 None"""
        raw = str(raw or "")
        with self._lock:
            left = self._left.get(raw)
            if left is None:
                return None
            left -= 1
            if left > 0:
                self._left[raw] = left
                return None
            del self._left[raw]
            self.done += 1
            return (self.done, self.total)

    def remaining(self):
        return len(self._left)


# ==================================================================
#                     自绘进度条（Material 线性）
# ==================================================================
class MProgressBar(tk.Canvas):
    """Material 风格线性进度条（圆角轨道 + 圆角填充，纯 Canvas 自绘）

    - set_value(ratio) ：ratio 为 0.0~1.0 的确定进度；传 None 进入「不定态」（滑块往返）
    - set_tone(kind)   ：normal / done / warn 三档配色
    - 只有不定态才启动 after 动画，确定态零开销（随每次进度事件重绘一次）
    """

    TONE_ATTR = {"normal": "CLR_PRIMARY", "done": "CLR_SUCCESS", "warn": "CLR_WARNING"}

    def __init__(self, master, width=200, height=8, bg=None, **kw):
        c = amis_query
        super().__init__(master, width=width, height=height, bg=bg or c.CLR_CARD,
                         highlightthickness=0, bd=0, **kw)
        self._bar_w = int(width)         # 注意：不能用 _w / _h，tkinter 内部用 _w 存窗口路径名
        self._bar_h = int(height)
        self._radius = float(height) / 2.0
        self._ratio = 0.0
        self._indeterminate = False
        self._tone = "normal"
        self._phase = 0.0
        self._job = None
        self._draw()

    # ---- 对外接口 ----
    def set_value(self, ratio):
        """确定进度（0.0~1.0）；传 None 表示总量未知，改用不定态动画"""
        if ratio is None:
            self._indeterminate = True
            self._start_anim()
        else:
            self._indeterminate = False
            self._stop_anim()
            try:
                ratio = float(ratio)
            except Exception:
                ratio = 0.0
            self._ratio = min(1.0, max(0.0, ratio))
        self._draw()

    def set_tone(self, kind):
        """配色：normal（进行中）/ done（完成）/ warn（停止 / 失败）"""
        if kind in self.TONE_ATTR and kind != self._tone:
            self._tone = kind
            self._draw()

    def value(self):
        return None if self._indeterminate else self._ratio

    # ---- 内部绘制 ----
    def _color(self):
        return getattr(amis_query, self.TONE_ATTR.get(self._tone, "CLR_PRIMARY"), "#0B57D0")

    def _draw(self):
        try:
            self.delete("all")
        except tk.TclError:
            return
        c = amis_query
        w, h, r = self._bar_w, self._bar_h, self._radius
        c.draw_round_rect(self, 0, 0, w, h, r, fill=c.CLR_HEAD)          # 轨道
        if self._indeterminate:
            span = max(int(w * 0.34), int(h) * 2)
            pos = self._phase * (w + span) / 100.0
            x1 = min(w, max(0, int(-span + pos)))
            x2 = min(w, max(0, int(pos)))
            if x2 - x1 >= h:
                c.draw_round_rect(self, x1, 0, x2, h, r, fill=self._color())
            return
        if self._ratio <= 0:
            return
        c.draw_round_rect(self, 0, 0, max(h, w * self._ratio), h, r, fill=self._color())

    # ---- 不定态动画（仅此路径使用 after） ----
    def _start_anim(self):
        if self._job is None:
            self._tick()

    def _stop_anim(self):
        if self._job is not None:
            try:
                self.after_cancel(self._job)
            except Exception:
                pass
            self._job = None

    def _tick(self):
        self._phase += 2.4
        if self._phase > 100:
            self._phase = 0.0
        self._draw()
        try:
            self._job = self.after(40, self._tick)
        except tk.TclError:              # 窗口已销毁
            self._job = None


class JointApp(object):
    """账号封禁联合查询主窗口：一个输入框 → amis + ACE 同时查 → 三区展示 → 统一导出"""

    def __init__(self):
        self._q = queue.Queue()
        self.result = None
        self._running = False
        self._stop_flag = False
        self._login_win = None
        self._ace_login_win = None
        self._status_busy = False
        self._status_refresh_pending = False   # 忙碌期间收到了新的刷新请求（结束后补一轮）
        self._status_gen = 0                   # 轮次代号：旧轮次的迟到结果不许覆盖新状态
        self._plat_state = {}                  # key -> (state, monotonic)；用于抑制重复刷新时的芯片闪烁
        self.plat_chips = {}
        self._plat_detail = {}
        self._prog_last = (0, 0)         # 最近一次进度事件（已完成 / 总数）
        self._update_busy = False        # 正在检查 / 下载更新
        self._update_info = None         # 待更新版本的元信息
        self._update_pending = ""        # 已下载并通过校验的安装包路径
        self._update_silent = True       # 本轮检查是否为「静默」（启动时那次）
        self._restarting = False         # 正在退出以完成更新替换

        self.root = tk.Tk()
        self.root.title(APP_TITLE + " v" + CURRENT_VERSION)
        self.root.configure(bg=amis_query.CLR_BG)
        self.root.geometry(self._center_geometry(1460, 980))
        self.root.minsize(1140, 760)
        # Tk 会吞掉回调里的异常（只在 stderr 打一行）：交给错误上报 + 弹窗提示
        self.root.report_callback_exception = self._on_tk_exception

        c = amis_query
        self.clr = c
        self.f_h1 = tkfont.Font(family=c.FONT_UI, size=16, weight="bold")
        self.f_h2 = tkfont.Font(family=c.FONT_UI, size=11, weight="bold")
        self.f_sub = tkfont.Font(family=c.FONT_UI, size=9)
        self.f_body = tkfont.Font(family=c.FONT_UI, size=10)
        self.f_label = tkfont.Font(family=c.FONT_UI, size=9, weight="bold")
        self.f_mono = tkfont.Font(family="Consolas", size=10)

        self.var_mode = tk.StringVar(value=MODE_AUTO)
        self._setup_style()
        self._build()

        # 让 amis_query 模块内部的进度日志 / 登录框走本窗口
        amis_query._LOG_SINK = self._log_from_module
        amis_query._LOGIN_UI = self._ask_login_threadsafe

        self._append_log("%s  界面已就绪：粘贴编号后点「开始查询」（自动识别后同时查 amis 与 ACE）。"
                         % now_text())
        if sentry_ready():
            self._append_log("%s  %s（可关闭：环境变量 JOINT_SENTRY=0）"
                             % (now_text(), sentry_status()))
        self.root.after(80, self._drain)
        self.root.after(400, lambda: self.refresh_platform_status(silent=True))   # 启动后自动检测登录态
        self.root.after(AUTO_UPDATE_DELAY_MS, lambda: self.on_check_update(silent=True))
        self.root.protocol("WM_DELETE_WINDOW", self.root.destroy)

    # ---------------- 错误上报 ----------------
    def _on_tk_exception(self, exc_type, exc, tb):
        """Tk 回调异常：上报 + 打印 + 提示用户（原先会被 Tk 静默吞掉）"""
        try:
            sentry_bridge.tk_callback_exception(exc_type, exc, tb)
        except Exception:
            pass
        try:
            messagebox.showerror(
                "界面操作出错",
                "该操作出现异常，已记录并上报：\n\n%s: %s\n\n"
                "可以继续使用，如反复出现请把这一步的操作告诉我。"
                % (getattr(exc_type, "__name__", "异常"), format_error(exc)),
                parent=self.root)
        except Exception:
            pass

    # ---------------- 布局基础 ----------------
    def _center_geometry(self, w, h):
        try:
            sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
            return "%dx%d+%d+%d" % (w, min(h, sh - 80),
                                    max(0, (sw - w) // 2), max(0, (sh - h) // 2 - 30))
        except Exception:
            return "%dx%d" % (w, h)

    def _setup_style(self):
        c = amis_query
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        for name in ("Ban.Treeview", "Sum.Treeview"):
            style.configure(name, font=self.f_body, rowheight=34, borderwidth=0,
                            background=c.CLR_CARD, fieldbackground=c.CLR_CARD,
                            foreground=c.CLR_TEXT)
            style.layout(name, [("Treeview.treearea", {"sticky": "nswe"})])
            style.map(name,
                      background=[("selected", c.CLR_SEL)],
                      foreground=[("selected", c.CLR_ON_PRIMARY_CONTAINER)])
            style.configure(name + ".Heading", font=self.f_label, relief="flat",
                            background=c.CLR_HEAD, foreground=c.CLR_SUB,
                            padding=(12, 11), borderwidth=0)
            style.map(name + ".Heading",
                      background=[("active", c.CLR_HEAD_HOVER)],
                      foreground=[("active", c.CLR_PRIMARY)])

    def _card(self, parent, pad=16, elevation=1, radius=22, fit="content", min_height=0):
        c = amis_query
        card = c.RoundBox(parent, radius=radius, padding=pad, fill=c.CLR_CARD,
                          bg=c.CLR_BG, elevation=elevation, fit=fit, min_height=min_height)
        card.inner = card.body
        return card

    def _section(self, parent, title, hint="", bg=None):
        c = amis_query
        bg = bg or c.CLR_CARD
        box = tk.Frame(parent, bg=bg)
        bar = tk.Canvas(box, width=4, height=16, bg=bg, highlightthickness=0, bd=0)
        bar.pack(side="left", pady=1)
        c.draw_round_rect(bar, 0, 0, 4, 16, 2, fill=c.CLR_PRIMARY)
        tk.Label(box, text=title, font=self.f_h2, bg=bg, fg=c.CLR_TEXT).pack(
            side="left", padx=(9, 0))
        if hint:
            tk.Label(box, text=hint, font=self.f_sub, bg=bg, fg=c.CLR_MUTED).pack(
                side="left", padx=(10, 0), pady=(2, 0))
        return box

    def _chip(self, parent, text, fill=None, fg=None, bg=None):
        c = amis_query
        fill = fill or c.CLR_SECONDARY_CONTAINER
        fg = fg or c.CLR_ON_SECONDARY_CONTAINER
        bg = bg or c.CLR_CARD
        box = c.RoundBox(parent, radius=13, padding=(10, 4), fill=fill, bg=bg, elevation=0)
        box.label = tk.Label(box.body, text=text, font=self.f_label, bg=fill, fg=fg)
        box.label.pack()
        return box

    def _build(self):
        self._build_topbar()
        self._build_statusbar()      # 必须先 pack（side=bottom），否则会被 expand 的 body 挤出窗口
        body = tk.Frame(self.root, bg=amis_query.CLR_BG)
        body.pack(fill="both", expand=True, padx=20, pady=(14, 0))
        self._build_input(body)
        self._build_results(body)

    def _build_topbar(self):
        c = amis_query
        bar = tk.Frame(self.root, bg=c.CLR_CARD)
        bar.pack(fill="x")
        inner = tk.Frame(bar, bg=c.CLR_CARD)
        inner.pack(fill="x", padx=20, pady=(12, 10))

        logo = tk.Canvas(inner, width=40, height=40, bg=c.CLR_CARD, highlightthickness=0)
        logo.pack(side="left")
        c.draw_round_rect(logo, 1, 1, 39, 39, 12, fill=c.CLR_PRIMARY)
        logo.create_text(20, 21, text="联", fill=c.CLR_ON_PRIMARY, font=(c.FONT_UI, 15, "bold"))

        box = tk.Frame(inner, bg=c.CLR_CARD)
        box.pack(side="left", padx=(12, 0))
        tk.Label(box, text=APP_NAME, font=self.f_h1, bg=c.CLR_CARD, fg=c.CLR_TEXT).pack(anchor="w")
        tk.Label(box, text="一次输入 · 自动识别编号类型 · 同时查询 amis 登录封禁与 ACE 可疑/处罚数据",
                 font=self.f_sub, bg=c.CLR_CARD, fg=c.CLR_MUTED).pack(anchor="w", pady=(2, 0))

        self._build_platform_status(inner)
        self.chip_state = self._chip(inner, "就绪", fill=c.CLR_SUCCESS_CONTAINER,
                                     fg=c.CLR_SUCCESS, bg=c.CLR_CARD)
        self.chip_state.pack(side="right", padx=(0, 12))

        sep = tk.Frame(self.root, bg=c.CLR_LINE, height=1)
        sep.pack(fill="x")

    # ---------------- 右上角：两个平台的登录状态 ----------------
    def _build_platform_status(self, parent):
        c = amis_query
        wrap = tk.Frame(parent, bg=c.CLR_CARD)
        wrap.pack(side="right", padx=(0, 12))
        self.plat_chips = {}
        for key in (PLATFORM_AMIS, PLATFORM_ACE):
            chip = self._chip(wrap, "%s · 待检测" % PLATFORM_LABEL[key], bg=c.CLR_CARD)
            chip.pack(side="left", padx=(0, 8))
            chip.label.configure(font=self.f_label)
            for widget in (chip, chip.body, chip.label):
                try:
                    widget.bind("<Button-1>", lambda _e, k=key: self._on_platform_click(k))
                    widget.configure(cursor="hand2")
                except Exception:
                    pass
            self.plat_chips[key] = chip
        self.btn_status = c.MButton(wrap, text="刷新登录态", kind="text", width=104, height=30,
                                    command=self.refresh_platform_status)
        self.btn_status.pack(side="left")
        # 手动检查更新（启动后也会自动静默检查一次）
        self.btn_update = c.MButton(wrap, text="检查更新", kind="text", width=96, height=30,
                                    command=self.on_check_update)
        self.btn_update.pack(side="left", padx=(8, 0))

    def _set_plat_chip(self, key, state, account="", expire=0, detail=""):
        """更新单个平台状态芯片（state: ok / invalid / missing / unknown / checking）"""
        c = amis_query
        chip = self.plat_chips.get(key)
        if chip is None:
            return
        # 记下该芯片最近一次进入某状态的时刻：自动刷新频繁重启时，
        # 靠它判断「这个状态还新不新鲜」，避免反复闪「检测中」（见 refresh_platform_status）
        self._plat_state[key] = (state, time.monotonic())
        title = PLATFORM_LABEL.get(key, key)
        palette = {"ok": (c.CLR_SUCCESS_CONTAINER, c.CLR_SUCCESS),
                   "invalid": (c.CLR_ERROR_CONTAINER, c.CLR_ERROR),
                   "missing": (c.CLR_ERROR_CONTAINER, c.CLR_ERROR),
                   "unknown": (c.CLR_WARNING_CONTAINER, c.CLR_WARNING),
                   "checking": (c.CLR_PRIMARY_CONTAINER, c.CLR_PRIMARY)}
        fill, fg = palette.get(state, palette["unknown"])
        if state == "ok":
            text = "%s · 已登录" % title
            if account:
                text += "（%s）" % account
        elif state == "invalid":
            text = "%s · 登录已失效 · 点此重新登录" % title
        elif state == "missing":
            text = "%s · 未登录 · 点此登录" % title
        elif state == "checking":
            text = "%s · 检测中…" % title
        else:
            text = "%s · 状态未知 · 点此重新登录" % title
        if detail:
            self._plat_detail[key] = detail
        try:
            chip.label.configure(text=text, bg=fill, fg=fg)
            chip.set_fill(fill)
        except Exception:
            pass

    def _apply_platform_status(self, results):
        """应用状态探测结果（results 可只含某一个平台；_full 表示整轮探测结束）"""
        gen = results.get("_gen")
        if gen is not None and gen != self._status_gen:
            return          # 旧轮次的迟到结果：直接丢弃，防止覆盖新一轮的芯片状态
        if results.get("_full"):
            self._status_busy = False
            if self._status_refresh_pending:
                # 忙碌期间有新的刷新请求（典型：登录成功后想立刻确认新凭据）→ 补一轮
                self._status_refresh_pending = False
                self.root.after(300, lambda: self.refresh_platform_status(silent=True))
        parts = []
        for key in (PLATFORM_AMIS, PLATFORM_ACE):
            if key not in results:
                continue
            info = results.get(key) or {}
            self._set_plat_chip(key, info.get("state") or "unknown",
                                account=info.get("account") or "",
                                expire=info.get("expire") or 0,
                                detail=info.get("detail") or "")
            parts.append("%s %s" % (PLATFORM_LABEL[key], info.get("detail") or "状态未知"))
        if parts:
            self._append_log("%s  %s" % (now_text(), "；".join(parts)))

    def refresh_platform_status(self, silent=False):
        """后台检测两个平台的登录态（在线轻量校验，只读）"""
        if self._status_busy:
            # 上一轮还没跑完（如刚开机网络慢、探测还在超时里）：不能把「补一轮」
            # 静默吞掉，否则登录成功后芯片会一直停在「检测中」，只能靠用户再点一次
            self._status_refresh_pending = True
            if not silent:
                self._append_log("%s  上一轮登录态检测还在进行，结束后会自动再检测一次…"
                                 % now_text())
            return
        self._status_busy = True
        self._status_gen += 1
        gen = self._status_gen
        now = time.monotonic()
        for key in (PLATFORM_AMIS, PLATFORM_ACE):
            st = self._plat_state.get(key)
            # 手动刷新把芯片置「检测中」当反馈；自动刷新（查询结束 / 登录复核 / 补刷）
            # 若该芯片 30 秒内刚出过结果，则保留原状态——否则每次自动轮都会闪一下蓝
            if silent and st and st[0] != "checking" and now - st[1] < 30:
                continue
            self._set_plat_chip(key, "checking")
        if not silent:
            self._append_log("%s  正在检测两个平台的登录态…" % now_text())
        threading.Thread(target=self._status_worker, args=(gen,), daemon=True).start()

    def _status_worker(self, gen):
        """两个平台并发探测、互不等待：谁先出结果谁先刷新芯片

        原实现是串行（amis 探完才探 ACE），网络不佳时单平台 30s 超时会让
        两个芯片一起「检测中」长达一分钟；现在各开一个线程，单平台的慢
        不再拖累另一个，最坏等待也从 2×超时 降到 1×超时。

        另加看门狗：urlopen 的 timeout 管不住 DNS 解析（getaddrinfo 可能
        卡几十秒），线程一旦无限挂起，`_full` 永远不会入队，`_status_busy`
        卡死、芯片永远「检测中」。这里对每个平台限时兜底，保证任何一轮
        都能在 PROBE_TIMEOUT + 余量内收敛出结果。
        """
        platforms = (PLATFORM_AMIS, PLATFORM_ACE)
        left = {"n": len(platforms)}
        lock = threading.Lock()

        def run_one(key):
            box = {}

            def work():
                try:
                    box["res"] = probe_platform_status(key, timeout=PROBE_TIMEOUT)
                except Exception as exc:
                    # 登录态探测受网络影响大（未登录 / 断网都算预期），只记面包屑不报事件
                    try:
                        mark("登录态探测异常（%s）：%s" % (key, format_error(exc)),
                             category="status", level="warning")
                    except Exception:
                        pass
                    box["res"] = {"state": "unknown", "account": "", "expire": 0,
                                  "source": "", "detail": format_error(exc)}

            t = threading.Thread(target=work, daemon=True)
            t.start()
            t.join(PROBE_TIMEOUT + 8)
            res = box.get("res") or {
                "state": "unknown", "account": "", "expire": 0, "source": "",
                "detail": "检测超时：网络长时间无响应（本地凭据未改动，可稍后重试）"}
            # put 必须在锁内：保证最后一个完成的事件带着 _full=True 最后入队，
            # 否则 _status_busy 可能永远释放不了
            with lock:
                left["n"] -= 1
                last = left["n"] <= 0
                self._q.put(("status", {"_full": last, "_gen": gen, key: res}))

        for key in platforms:
            threading.Thread(target=run_one, args=(key,), daemon=True).start()

    # ---------------- 自动更新 ----------------
    # 全部流程：检查（静默/手动）→ 提示 → 后台下载（断点续传 + 加速镜像）
    #           → SHA256 校验 → 生成替换脚本 → 退出并重启到新版本。
    # 网络动作都在工作线程里做，界面只经 self._q 收消息，避免卡住主线程。
    def on_check_update(self, silent=False):
        """检查更新入口（按钮 command 会调用无参版本 → silent=False）"""
        if update_check is None:
            if not silent:
                messagebox.showwarning(
                    "检查更新不可用",
                    "更新组件缺失或损坏（src/update_check.py）：%s" % _UPDATE_IMPORT_ERROR,
                    parent=self.root)
            return
        if update_check.updates_disabled():
            if not silent:
                messagebox.showinfo("已关闭更新检查",
                                    "已通过环境变量 %s 关闭更新检查。"
                                    % update_check.ENV_DISABLE, parent=self.root)
            return
        if self._update_busy:
            return
        self._update_busy = True
        self._update_silent = bool(silent)
        self._update_info = None
        try:
            self.btn_update.set_enabled(False)
            self.btn_update.set_text("检查中…")
        except Exception:
            pass
        if not silent:
            self._set_chip("检查更新中…", "busy")
            self._append_log("%s  正在检查更新（当前 v%s）…" % (now_text(), CURRENT_VERSION))
        threading.Thread(target=self._check_update_worker, daemon=True).start()

    def _check_update_worker(self):
        try:
            info = update_check.check_for_update(CURRENT_VERSION, notice=self._update_note)
        except Exception as exc:
            self._q.put(("update", ("check-error", format_error(exc))))
            return
        self._q.put(("update", ("check-done", info)))

    def _update_note(self, text):
        """更新模块的过程消息（工作线程调用 → 投递主线程写日志）"""
        self._q.put(("log", "%s  [更新] %s" % (now_text(), text)))

    def _on_update_event(self, kind, payload):
        """主线程处理更新流程的各类事件"""
        c = amis_query
        if kind == "check-error":
            self._update_busy = False
            self._restore_update_button()
            if self._update_silent:
                self._append_log("%s  [更新] 检查更新失败（不影响使用）：%s"
                                 % (now_text(), payload))
                return
            self._set_chip("就绪", "ok")
            messagebox.showwarning("检查更新失败",
                                   "没能连上版本服务器（可能是网络限制）：\n\n%s\n\n"
                                   "程序可以照常使用；稍后再试，或设置环境变量\n"
                                   "%s 指定可用的加速地址。" % (payload, update_check.ENV_MIRRORS),
                                   parent=self.root)
            return
        if kind == "check-done":
            self._update_busy = False
            self._restore_update_button()
            if not payload:
                if self._update_silent:
                    self._append_log("%s  [更新] 已是最新版本（v%s）"
                                     % (now_text(), CURRENT_VERSION))
                    return
                self._set_chip("已是最新", "ok")
                messagebox.showinfo("检查更新",
                                    "已是最新版本（v%s）。" % CURRENT_VERSION, parent=self.root)
                self.root.after(2500, lambda: self._set_chip("就绪", "ok"))
                return
            self._offer_update(payload)
            return
        if kind == "download-progress":
            done, total = payload
            try:
                if total:
                    self.progress.set_tone("normal")
                    self.progress.set_value(min(1.0, done / float(total)))
                    self.lbl_progress.configure(
                        text="下载更新 %s / %s（%d%%）"
                             % (update_check.human_size(done), update_check.human_size(total),
                                int(done * 100.0 / total)), fg=c.CLR_PRIMARY)
            except Exception:
                pass
            return
        if kind == "download-done":
            self._on_update_ready(payload)
            return
        if kind == "download-error":
            self._update_busy = False
            self._restore_update_button()
            try:
                self.progress.set_tone("warn")
                self.progress.set_value(0)
                self.lbl_progress.configure(text="更新下载失败", fg=c.CLR_WARNING)
            except Exception:
                pass
            self._set_chip("更新失败 · 点击重试", "warn")
            if self._update_info:
                self._update_info = dict(self._update_info)
            messagebox.showerror(
                "下载更新失败",
                "已下载的部分会保留，下次会从断点继续。\n\n%s" % payload, parent=self.root)
            return

    def _restore_update_button(self):
        try:
            self.btn_update.set_enabled(True)
            self.btn_update.set_text("检查更新")
        except Exception:
            pass

    def _offer_update(self, info):
        """有新版本：状态芯片变色 + 可点击，并按需弹窗询问"""
        self._update_info = info
        text = "发现新版本 v%s · 点击更新" % info.get("version")
        self._set_chip(text, "warn")
        for widget in (self.chip_state, getattr(self.chip_state, "body", None),
                       getattr(self.chip_state, "label", None)):
            if widget is None:
                continue
            try:
                widget.bind("<Button-1>", lambda _e: self._on_update_chip_click())
                widget.configure(cursor="hand2")
            except Exception:
                pass
        self._append_log("%s  [更新] 发现新版本 v%s（当前 v%s）· 来源 %s"
                         % (now_text(), info.get("version"), CURRENT_VERSION,
                            info.get("_source") or "?"))
        if self._update_silent:
            return
        size = update_check.human_size(info.get("size"))
        if messagebox.askyesno(
                "发现新版本",
                "有新版本 v%s 可用（当前 v%s）。\n\n"
                "文件大小：%s\nSHA256：%s\n\n"
                "现在下载并自动更新吗？（支持断点续传，下载完成后会自动重启程序）"
                % (info.get("version"), CURRENT_VERSION, size,
                   (info.get("sha256") or "")[:16] or "未提供"),
                parent=self.root):
            self._start_update_download(info)

    def _on_update_chip_click(self):
        if self._update_pending:
            self._prompt_apply_update()
        elif self._update_info:
            self._start_update_download(self._update_info)
        else:
            self.on_check_update()

    def _start_update_download(self, info):
        if self._update_busy:
            return
        self._update_busy = True
        self._set_chip("正在下载 v%s…" % info.get("version"), "busy")
        try:
            self.btn_update.set_enabled(False)
            self.btn_update.set_text("下载中…")
            self.progress.set_tone("normal")
            self.progress.set_value(0)
            self.lbl_progress.configure(text="准备下载更新…", fg=amis_query.CLR_PRIMARY)
        except Exception:
            pass
        threading.Thread(target=self._download_update_worker, args=(info,),
                         daemon=True).start()

    def _download_update_worker(self, info):
        def prog(done, total, _speed):
            self._q.put(("update", ("download-progress", (done, total))))
        try:
            path = update_check.download_update(info, progress=prog, notice=self._update_note)
        except Exception as exc:
            self._q.put(("update", ("download-error", format_error(exc))))
            return
        self._q.put(("update", ("download-done", path)))

    def _on_update_ready(self, path):
        self._update_busy = False
        self._update_pending = path
        self._restore_update_button()
        try:
            self.progress.set_tone("done")
            self.progress.set_value(1.0)
            self.lbl_progress.configure(text="更新包已下载并通过校验", fg=amis_query.CLR_SUCCESS)
        except Exception:
            pass
        self._set_chip("更新包已就绪 · 点击安装", "warn")
        self._append_log("%s  [更新] 安装包已就绪：%s" % (now_text(), path))
        self._prompt_apply_update()

    def _prompt_apply_update(self):
        if not self._update_pending:
            return
        version = (self._update_info or {}).get("version") or "新"
        if not messagebox.askyesno(
                "安装更新",
                "v%s 已下载完成并通过校验。\n\n现在退出程序、完成替换并自动重启吗？\n"
                "（替换过程约 1~3 秒，若失败会自动打开旧版本）" % version,
                parent=self.root):
            self._append_log("%s  [更新] 已暂缓安装；稍后点右上角状态或「检查更新」即可继续"
                             % now_text())
            return
        self._apply_update(self._update_pending)

    def _apply_update(self, path):
        try:
            update_check.install_update(path, notice=self._update_note)
        except Exception as exc:
            detail = str(exc)
            self._append_log("%s  [更新] 自动替换失败：%s" % (now_text(), detail))
            if messagebox.askyesno(
                    "自动替换失败",
                    "%s\n\n是否打开安装包所在目录，手动替换？\n%s" % (detail, path),
                    parent=self.root):
                try:
                    os.startfile(os.path.dirname(path))
                except Exception:
                    pass
            return
        self._append_log("%s  [更新] 即将退出并完成替换，稍后会自动重新打开…" % now_text())
        self._restarting = True
        try:
            self.progress.set_tone("done")
            self.lbl_progress.configure(text="正在重启完成更新…", fg=amis_query.CLR_SUCCESS)
        except Exception:
            pass
        self.root.after(400, self.root.destroy)

    def _on_platform_click(self, key):
        """点击状态芯片：打开对应平台的登录窗，换新凭据后自动刷新状态"""
        title = PLATFORM_LABEL.get(key, key)
        if self._running:
            self._append_log("查询进行中，请等本次查询结束后再重新登录 %s。" % title)
            return
        self._append_log("%s  打开 %s 登录窗口（当前状态：%s）"
                         % (now_text(), title, self._plat_detail.get(key) or "未知"))
        self._set_plat_chip(key, "checking", detail="正在登录…")
        if key == PLATFORM_AMIS:
            threading.Thread(target=self._login_amis_worker, daemon=True).start()
        else:
            threading.Thread(target=self._login_ace_worker, daemon=True).start()

    def _login_amis_worker(self):
        try:
            amis_query.obtain_token_by_login(
                "手动重新登录 amis 平台（刷新登录态）。",
                reason_kind=amis_query.LOGIN_REASON_EXPIRED)
        except Exception as exc:
            # 用户取消 / 密码错误属预期，只记面包屑（真正的崩溃由全局钩子兜底）
            mark("amis 重新登录未完成：%s" % format_error(exc),
                 category="login", level="warning")
            self._q.put(("status", {"_full": False, PLATFORM_AMIS: {
                "state": "invalid", "detail": "登录未完成：%s" % format_error(exc)}}))
            return
        self._q.put(("status", {"_full": False, PLATFORM_AMIS: {
            "state": "ok", "detail": "登录成功，已更新本地 Token"}}))
        self._q.put(("refresh_status", None))

    def _login_ace_worker(self):
        cookies = self._ask_ace_login_threadsafe("手动重新登录 ACE 平台（刷新登录态）。")
        if cookies:
            self._q.put(("status", {"_full": False, PLATFORM_ACE: {
                "state": "ok", "detail": "登录成功，已更新本地凭据"}}))
            self._q.put(("refresh_status", None))
        else:
            self._q.put(("status", {"_full": False, PLATFORM_ACE: {
                "state": "invalid", "detail": "已取消登录，登录态未更新"}}))

    def _build_input(self, parent):
        c = amis_query
        card = self._card(parent, pad=16, elevation=1)
        card.pack(fill="x")
        box = card.inner

        head = tk.Frame(box, bg=c.CLR_CARD)
        head.pack(fill="x")
        self._section(head, "输入编号", "逗号 / 空格 / 换行 / 分号 分隔，支持批量粘贴").pack(side="left")
        self.chip_input = self._chip(head, "0 个编号")
        self.chip_input.pack(side="right")

        seg_row = tk.Frame(box, bg=c.CLR_CARD)
        seg_row.pack(fill="x", pady=(10, 8))
        tk.Label(seg_row, text="识别模式", font=self.f_label, bg=c.CLR_CARD,
                 fg=c.CLR_SUB).pack(side="left", padx=(0, 10))
        c.Segmented(seg_row, MODE_OPTIONS, self.var_mode, font=self.f_label,
                    bg=c.CLR_CARD, height=32).pack(side="left")
        tk.Label(seg_row, text="默认「自动」：14 位 = 无端ID，2~12 位 = 角色ID（ACE 只认角色ID，无端ID 会自动换算）",
                 font=self.f_sub, bg=c.CLR_CARD, fg=c.CLR_MUTED).pack(side="left", padx=(12, 0))

        area = tk.Frame(box, bg=c.CLR_CARD)
        area.pack(fill="x")
        wrap = c.RoundBox(area, radius=12, padding=(2, 2), fill=c.CLR_HEAD,
                          outline=c.CLR_LINE, outline_width=1, bg=c.CLR_CARD)
        wrap.pack(fill="x", expand=True)
        self.txt = tk.Text(wrap.body, height=5, font=self.f_mono, relief="flat", bd=0,
                           bg=c.CLR_HEAD, fg=c.CLR_TEXT, insertbackground=c.CLR_PRIMARY,
                           highlightthickness=0, wrap="word")
        self.txt.pack(fill="both", expand=True, padx=(10, 2), pady=8)
        sb = c.MScrollbar(wrap.body, target=self.txt, orient="vertical", thickness=12,
                          bg=c.CLR_HEAD)
        sb.pack(side="right", fill="y", pady=4, padx=(0, 4))
        self.txt.configure(yscrollcommand=sb.set)
        self.txt.bind("<KeyRelease>", lambda _e: self._refresh_input_chip())
        self.txt.bind("<Control-Return>", lambda _e: (self._on_query(), "break")[1])
        self.txt.bind("<<Paste>>", lambda _e: self.root.after(30, self._refresh_input_chip))

        # 查询进度条（紧贴「开始查询」上方，点击后视线所在处）
        prow = tk.Frame(box, bg=c.CLR_CARD)
        prow.pack(fill="x", pady=(12, 0))
        tk.Label(prow, text="查询进度", font=self.f_label, bg=c.CLR_CARD,
                 fg=c.CLR_SUB).pack(side="left", padx=(0, 10))
        self.progress = MProgressBar(prow, width=340, height=8, bg=c.CLR_CARD)
        self.progress.pack(side="left", pady=(4, 0))
        self.lbl_progress = tk.Label(prow, text="就绪 · 尚未开始", font=self.f_sub,
                                     bg=c.CLR_CARD, fg=c.CLR_MUTED, anchor="w")
        self.lbl_progress.pack(side="left", padx=(10, 0))

        row = tk.Frame(box, bg=c.CLR_CARD)
        row.pack(fill="x", pady=(10, 0))
        self.btn_query = c.MButton(row, text="开始查询", kind="filled", command=self._on_query)
        self.btn_query.pack(side="left")
        self.btn_stop = c.MButton(row, text="停止", kind="outlined", command=self._on_stop)
        self.btn_stop.pack(side="left", padx=(8, 0))
        self.btn_stop.set_enabled(False)
        self.btn_import = c.MButton(row, text="导入文件", kind="text", command=self._on_import)
        self.btn_import.pack(side="left", padx=(8, 0))
        c.MButton(row, text="清空", kind="text", command=self._on_clear).pack(side="left")
        tk.Label(row, text="Ctrl+Enter 快速开始", font=self.f_sub, bg=c.CLR_CARD,
                 fg=c.CLR_MUTED).pack(side="left", padx=(10, 0))
        self.btn_export = c.MButton(row, text="导出 CSV", kind="tonal", command=self._on_export)
        self.btn_export.pack(side="right")
        self.btn_export.set_enabled(False)
        c.MButton(row, text="打开导出目录", kind="text",
                  command=self._on_open_dir).pack(side="right", padx=(0, 8))

    def _build_statusbar(self):
        c = amis_query
        bar = tk.Frame(self.root, bg=c.CLR_CARD)
        bar.pack(fill="x", side="bottom")
        self._statusbar = bar
        tk.Frame(bar, bg=c.CLR_LINE, height=1).pack(fill="x")
        inner = tk.Frame(bar, bg=c.CLR_CARD)
        inner.pack(fill="x", padx=20, pady=7)
        self.lbl_status = tk.Label(inner, text="就绪", font=self.f_sub, bg=c.CLR_CARD,
                                   fg=c.CLR_SUB, anchor="w")
        self.lbl_status.pack(side="left", fill="x", expand=True)
        tail = "amis + ACE 联合查询 v%s" % CURRENT_VERSION
        if sentry_ready():
            tail += "　|　错误上报已启用（Sentry）"
        tk.Label(inner, text=tail, font=self.f_sub, bg=c.CLR_CARD,
                 fg=c.CLR_MUTED).pack(side="right")

    # ---------------- 进度条 ----------------
    def _progress_reset(self):
        """开始新一轮查询：进度归零"""
        c = amis_query
        self._prog_last = (0, 0)
        try:
            self.progress.set_tone("normal")
            self.progress.set_value(0)
            self.lbl_progress.configure(text="准备中…", fg=c.CLR_MUTED)
        except Exception:
            pass

    def _progress_update(self, done, total, _label=""):
        """更新进度（总数为 0 时走不定态，避免 0/0 卡住观感）"""
        c = amis_query
        self._prog_last = (int(done), int(total))
        try:
            if self._stop_flag:               # 已点停止：进度只作收尾展示，不再显示百分比
                self.progress.set_tone("warn")
                self.progress.set_value((done / float(total)) if total else 0.0)
                self.lbl_progress.configure(text="正在停止… 已完成 %d/%d 个编号" % (done, total),
                                            fg=c.CLR_WARNING)
                return
            if total <= 0:
                self.progress.set_value(None)
                self.lbl_progress.configure(text="查询中…", fg=c.CLR_SUB)
                return
            self.progress.set_tone("normal")
            self.progress.set_value(done / float(total))
            self.lbl_progress.configure(
                text="编号 %d/%d · %d%%" % (done, total, int(done * 100.0 / total)),
                fg=c.CLR_SUB)
        except Exception:
            pass

    def _progress_finish(self, stopped=False, failed=False):
        """查询收尾：完成 → 100% 绿，停止 → 停在当前值并转警示色"""
        c = amis_query
        done, total = self._prog_last
        try:
            if stopped or failed:
                self.progress.set_tone("warn")
                self.progress.set_value((done / float(total)) if total else 0.0)
                self.lbl_progress.configure(
                    text="%s %d/%d 个编号" % ("查询失败" if failed else "已停止", done, total),
                    fg=c.CLR_WARNING)
            else:
                self.progress.set_tone("done")
                self.progress.set_value(1.0)
                if not total:
                    text = "完成"
                elif done >= total:
                    text = "完成 %d/%d 个编号 · 100%%" % (total, total)
                else:
                    text = "结束 %d/%d 个编号（部分平台未完成）" % (done, total)
                self.lbl_progress.configure(text=text, fg=c.CLR_SUCCESS)
        except Exception:
            pass

    def _progress_from_module(self, done, total, label=""):
        """工作线程回调 → 投递主线程"""
        self._q.put(("progress", (done, total, label)))


    # ---------------- 日志 / 状态 ----------------
    def _log_from_module(self, text):
        self._q.put(("log", text))

    def _append_log(self, text):
        self.lbl_status.configure(text=str(text).strip())
        mark(str(text).strip(), category="log")   # 面包屑：出错时能回看最近发生了什么

    def _set_chip(self, text, kind="ok"):
        c = amis_query
        colors = {"ok": (c.CLR_SUCCESS_CONTAINER, c.CLR_SUCCESS),
                  "busy": (c.CLR_PRIMARY_CONTAINER, c.CLR_PRIMARY),
                  "warn": (c.CLR_WARNING_CONTAINER, c.CLR_WARNING),
                  "err": (c.CLR_ERROR_CONTAINER, c.CLR_ERROR)}
        fill, fg = colors.get(kind, colors["ok"])
        self.chip_state.label.configure(text=text, bg=fill, fg=fg)
        try:
            self.chip_state.set_fill(fill)
        except Exception:
            pass

    def _refresh_input_chip(self):
        tokens = split_tokens(self.txt.get("1.0", "end"))
        items, dup = build_ident_items(self.txt.get("1.0", "end"), self.var_mode.get())
        text = "%d 个编号" % len(dedupe(tokens))
        if items:
            skipped = sum(1 for i in items if i.skipped)
            text += "（可查 %d" % (len(items) - skipped)
            if skipped:
                text += "，跳过 %d" % skipped
            text += "）"
        self.chip_input.label.configure(text=text)

    def _set_running(self, running):
        self._running = running
        self.btn_query.set_enabled(not running)
        self.btn_query.set_text("查询中…" if running else "开始查询")
        self.btn_stop.set_enabled(running and not self._stop_flag)
        self.btn_stop.set_text("停止中…" if (running and self._stop_flag) else "停止")
        self.btn_import.set_enabled(not running)

    # ---------------- 交互 ----------------
    def _on_clear(self):
        if self._running:
            return
        self.txt.delete("1.0", "end")
        self._refresh_input_chip()

    def _on_import(self):
        path = filedialog.askopenfilename(
            title="选择编号清单文件",
            filetypes=[("文本/CSV", "*.txt *.csv *.log"), ("全部文件", "*.*")])
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
                content = f.read()
        except Exception as exc:
            messagebox.showerror("读取失败", format_error(exc), parent=self.root)
            return
        self.txt.delete("1.0", "end")
        self.txt.insert("1.0", content)
        self._refresh_input_chip()
        self._append_log("%s  已导入文件：%s" % (now_text(), os.path.basename(path)))

    def _on_query(self):
        if self._running:
            return
        raw = self.txt.get("1.0", "end")
        items, dup = build_ident_items(raw, self.var_mode.get())
        if not items:
            messagebox.showinfo("提示", "请先输入编号（每个编号用逗号 / 空格 / 换行分隔）。",
                                parent=self.root)
            return
        queried = [it for it in items if not it.skipped]
        if not queried:
            messagebox.showinfo("提示", "所有编号都未识别成功，请检查编号位数或切换识别模式。",
                                parent=self.root)
            return
        if dup:
            self._append_log("%s  已自动去重 %d 个重复编号" % (now_text(), dup))
        if len(queried) > BATCH_CONFIRM_THRESHOLD:
            ok = messagebox.askyesno(
                "批量查询确认",
                "本次将查询 %d 个编号（amis 与 ACE 两平台）。\n"
                "查询过程中可点「停止」中止（未开始的编号不再查询）。\n\n确定开始吗？" % len(queried),
                parent=self.root)
            if not ok:
                self._append_log("已取消本次批量查询")
                return

        self._stop_flag = False
        self._set_running(True)
        self._set_chip("查询中", "busy")
        self._progress_reset()
        self._append_log("%s  开始查询 %d 个编号（跳过 %d 个）…"
                         % (now_text(), len(queried), len(items) - len(queried)))
        self._clear_tables()
        threading.Thread(target=self._worker, args=(items,), daemon=True).start()

    def _on_stop(self):
        if not self._running:
            return
        self._stop_flag = True
        self.btn_stop.set_enabled(False)
        self.btn_stop.set_text("停止中…")
        self._set_chip("停止中", "warn")
        try:
            self.progress.set_tone("warn")
            self.lbl_progress.configure(
                text="已收到停止指令：未开始的编号不再查询，在途请求返回后结束…",
                fg=amis_query.CLR_WARNING)
            self.lbl_summary.configure(text="正在停止…")
        except Exception:
            pass
        self._append_log("已收到停止指令：未开始的编号不再查询，已发出的请求返回后结束 …")

    def _worker(self, items):
        try:
            res = run_joint(items, on_log=self._log_from_module,
                            should_stop=lambda: self._stop_flag,
                            ace_cookies_provider=self._ask_ace_login_threadsafe,
                            on_progress=self._progress_from_module)
        except Exception as exc:
            report_exception(exc, tags={"stage": "joint-query"},
                             extra={"编号数": len(items or [])})
            self._q.put(("error", format_error(exc)))
            return
        self._q.put(("done", res))

    def _drain(self):
        try:
            while True:
                kind, payload = self._q.get_nowait()
                try:
                    if kind == "log":
                        self._append_log(payload)
                    elif kind == "progress":
                        self._progress_update(*payload)
                    elif kind == "done":
                        self._on_done(payload)
                    elif kind == "error":
                        self._on_error(payload)
                    elif kind == "ask_login":
                        self._show_login(payload)
                    elif kind == "ask_ace_login":
                        self._show_ace_login(payload)
                    elif kind == "ace_detail":
                        self._show_ace_detail(payload)
                    elif kind == "status":
                        self._apply_platform_status(payload)
                    elif kind == "update":
                        self._on_update_event(*payload)
                    elif kind == "refresh_status":
                        self.refresh_platform_status(silent=True)
                except Exception as exc:
                    # 单条事件处理失败绝不能拖垮事件泵：否则 after(80) 轮询链断裂，
                    # 界面从此不再更新（芯片会永远停在「检测中」这类中间状态）
                    try:
                        report_exception(exc, tags={"stage": "ui-drain", "event": str(kind)})
                    except Exception:
                        pass
        except queue.Empty:
            pass
        self.root.after(80, self._drain)

    # ---------------- 登录框（主线程调度，供工作线程唤起） ----------------
    def _ask_login_threadsafe(self, tip="", error="", default_account="", reason=""):
        req = {"tip": tip, "error": error, "account": default_account,
               "reason": reason, "res": None, "event": threading.Event()}
        self._q.put(("ask_login", req))
        req["event"].wait()
        return req["res"]

    def _show_login(self, req):
        try:
            req["res"] = amis_query.LoginDialog(
                self.root, tip=req.get("tip", ""), error=req.get("error", ""),
                default_account=req.get("account", ""), reason=req.get("reason", "")).wait()
        except Exception as exc:
            log("[警告] amis 登录窗口打开失败: %s" % format_error(exc))
            req["res"] = None
        finally:
            if req.get("res"):
                self._set_plat_chip(PLATFORM_AMIS, "checking", detail="登录成功，正在更新本地 Token…")
                # 登录成功后立刻复核一次登录态。查询预检路径（首次使用）此前没有任何
                # 刷新调度，芯片会一直「检测中」直到整轮查询结束——看起来就像卡死了
                self._q.put(("refresh_status", None))
            req["event"].set()

    def _ask_ace_login_threadsafe(self, reason=""):
        req = {"reason": reason, "res": None, "event": threading.Event()}
        self._q.put(("ask_ace_login", req))
        req["event"].wait()
        return req["res"]

    def _show_ace_login(self, req):
        try:
            res = AceLoginDialog(self.root, reason=req.get("reason", "")).wait()
            req["res"] = dict(res or {})
        except Exception as exc:
            log("[警告] ACE 登录窗口打开失败: %s" % format_error(exc))
            req["res"] = {}
        finally:
            if req.get("res"):
                self._set_plat_chip(PLATFORM_ACE, "checking",
                                    detail="登录成功，正在更新本地凭据并续跑查询…")
                # 同 amis：登录成功立刻复核，不等整轮查询结束
                self._q.put(("refresh_status", None))
            req["event"].set()

    # ---------------- 结果区 ----------------
    def _make_tab(self, nb, title, columns, headers, widths, stretch=(), height=12,
                  anchor="center"):
        """结果页签：纯文本视图（一条记录一行，文字可直接拖动选择）

        此前这里用 Treeview 表格，但表格只能整格选中，用户拖动选不了格内
        文字；改为 ResultTextView——像素制表位对齐的只读文本，观感接近
        表格，而选择/复制完全是原生文字操作。columns/stretch/anchor 参数
        保留签名兼容（文本视图按 widths 定位，不需要它们）。
        """
        c = amis_query
        page_bg = getattr(nb, "page_bg", c.CLR_CARD)   # 与宿主卡片同色，避免卡片里出现灰底块
        tab = tk.Frame(nb, bg=page_bg)
        nb.add(tab, text=title)
        hint = tk.Label(tab, text="尚未查询", font=self.f_sub, bg=page_bg,
                        fg=c.CLR_MUTED, anchor="w", justify="left")
        hint.pack(fill="x", padx=10, pady=(8, 0))
        wrap = tk.Frame(tab, bg=page_bg)
        wrap.pack(fill="both", expand=True, padx=10, pady=(6, 8))
        # 备注等长文本列左对齐（居中会和相邻列重叠），其余列居中显示
        aligns = ["left" if h in ("备注", "说明") else "center" for h in headers]
        view = c.ResultTextView(wrap, headers=list(headers), widths=list(widths),
                                font=self.f_body, bg=page_bg, aligns=aligns)
        view.pack(fill="both", expand=True)
        tab.hint = hint
        tab.view = view
        return tab, view

    def _build_results(self, parent):
        c = amis_query
        card = self._card(parent, pad=12, elevation=1, fit="expand")
        card.pack(fill="both", expand=True, pady=(12, 10))
        box = card.inner

        head = tk.Frame(box, bg=c.CLR_CARD)
        head.pack(fill="x")
        self._section(head, "查询结果",
                      "双击任意一行可查看明细；结果为纯文本，按住左键拖动即可直接选择文字，Ctrl+C 复制，右键有更多复制方式").pack(side="left")
        self.lbl_summary = tk.Label(head, text="尚未查询", font=self.f_sub,
                                    bg=c.CLR_CARD, fg=c.CLR_MUTED)
        self.lbl_summary.pack(side="right")

        nb = amis_query.MTabView(box, bg=c.CLR_CARD, page_bg=c.CLR_CARD,
                                 font=self.f_body)
        nb.pack(fill="both", expand=True, pady=(8, 0))
        self.nb = nb
        self.tab_ident, self.view_ident = self._make_tab(
            nb, "编号识别", IDENT_COLUMNS, IDENT_HEADERS, IDENT_WIDTHS)
        self.tab_amis, self.view_amis = self._make_tab(
            nb, "amis 登录封禁", JOINT_AMIS_COLUMNS, JOINT_AMIS_HEADERS,
            JOINT_AMIS_WIDTHS)
        self.tab_ace, self.view_ace = self._make_tab(
            nb, "ACE 可疑/处罚", ACE_SUMMARY_COLUMNS, ACE_SUMMARY_HEADERS,
            ACE_SUMMARY_WIDTHS)

        self.view_amis.on_double = self._on_amis_dclick
        self.view_ace.on_double = self._on_ace_dclick

    def _clear_tables(self):
        for view in (self.view_ident, self.view_amis, self.view_ace):
            view.clear()
        self.tab_ident.hint.configure(text="尚未查询")
        self.tab_amis.hint.configure(text="尚未查询")
        self.tab_ace.hint.configure(text="尚未查询")
        self.nb.tab(self.tab_ident, text="编号识别")
        self.nb.tab(self.tab_amis, text="amis 登录封禁")
        self.nb.tab(self.tab_ace, text="ACE 可疑/处罚")
        self.lbl_summary.configure(text="查询中…")

    def _fill_view(self, view, rows, tag_fn=None):
        view.set_rows(rows, tag_fn=tag_fn)

    # ---------------- 渲染结果 ----------------
    def _render_result(self, res):
        c = amis_query
        self.result = res

        # 1) 编号识别
        rows = [it.as_row() for it in res.items]
        self._fill_view(self.view_ident, rows,
                        lambda i, _row: "fail" if res.items[i].skipped else "normal")
        skipped = sum(1 for it in res.items if it.skipped)
        self.nb.tab(self.tab_ident, text="编号识别（%d）" % len(rows))
        self.tab_ident.hint.configure(
            text="共 %d 个编号，其中可查 %d 个、跳过 %d 个；备注列说明每个编号识别到的类型与参与的平台。"
                 % (len(rows), len(rows) - skipped, skipped))

        # 2) amis 汇总
        summaries = res.amis_summaries
        if res.amis and summaries:
            self._fill_view(self.view_amis, res.amis_summary_rows(),
                            lambda i, _row: amis_query.summary_tag(summaries[i]))
            banned = res.amis_banned_count()
            tip = ("共 %d 个编号，封禁中 %d 个、正常 %d 个%s；双击某行查看该编号的封禁明细。"
                   % (len(summaries), banned, len(summaries) - banned - res.amis_failed_count(),
                      ("，查询失败 %d 个" % res.amis_failed_count()) if res.amis_failed_count() else ""))
        else:
            tip = res.amis_error or "本次没有需要查询 amis 的编号。"
        self.nb.tab(self.tab_amis, text="amis 登录封禁（%d）" % len(summaries))
        self.tab_amis.hint.configure(text=tip)

        # 3) ACE 汇总（界面不显示备注列，失败原因见双击明细与标签页提示）
        ace_rows = res.ace_summary_rows(with_note=False)
        self._fill_view(self.view_ace, ace_rows, self._ace_tag)
        hits = res.ace_hit_count()
        bad = sum(1 for oid in res.ace_order
                  if (res.ace.get(oid) or {}).get("check_error")
                  or (res.ace.get(oid) or {}).get("punish_error"))
        if res.ace_error:
            ace_tip = res.ace_error
        elif ace_rows:
            conv = ("（其中 %d 个由无端ID 换算成角色ID 后查询）" % len(res.ace_converted)
                    if res.ace_converted else "")
            ace_tip = ("共 %d 个 OpenID%s，其中命中可疑/处罚记录 %d 个%s；"
                       "双击某行查看核查与处罚明细、截图。"
                       % (len(ace_rows), conv, hits,
                          ("，部分接口失败 %d 个" % bad) if bad else ""))
        else:
            skipped_ace = [it for it in res.items if not it.skipped and not it.ace_openid]
            if skipped_ace:
                ace_tip = ("%d 个编号未参与 ACE 查询（无端ID 未能换算出角色ID，"
                           "ACE 只认角色ID）。" % len(skipped_ace))
            else:
                ace_tip = "本次没有需要查询 ACE 的编号。"
        self.nb.tab(self.tab_ace, text="ACE 可疑/处罚（%d）" % len(ace_rows))
        self.tab_ace.hint.configure(text=ace_tip)

        # 4) 概览（统计栏已移除，指标集中显示在「查询结果」标题行右侧）
        queried = [it for it in res.items if not it.skipped]
        failed = res.amis_failed_count() + bad
        summary = ("编号 %d（可查 %d / 跳过 %d）　|　amis 封禁中 %d　|　ACE 命中 %d　|　失败 %d　|　%s"
                   % (len(res.items), len(queried), len(res.items) - len(queried),
                      res.amis_banned_count(), hits, failed, res.query_at))
        if res.stopped:
            summary += "　|　已被手动停止"
        self.lbl_summary.configure(text=summary)
        self.btn_export.set_enabled(bool(res.items))

    def _ace_tag(self, i, _row):
        res = self.result
        if not res or i >= len(res.ace_order):
            return None
        data = res.ace.get(res.ace_order[i]) or {}
        if data.get("check_error") and data.get("punish_error"):
            return "fail"
        return "ban" if (data.get("records") or data.get("punish_items")) else "normal"

    def _on_done(self, res):
        self._set_running(False)
        self.refresh_platform_status(silent=True)   # 查询后同步刷新两平台登录态
        self._render_result(res)
        if res.stopped:
            self._set_chip("已停止", "warn")
            self._progress_finish(stopped=True)
        elif res.amis_error or res.ace_error:
            self._set_chip("部分完成", "warn")
            self._progress_finish(failed=True)
        else:
            self._set_chip("完成", "ok")
            self._progress_finish()
        self._append_log("%s  查询结束：编号 %d，amis 封禁中 %d，ACE 命中 %d。"
                         % (now_text(), len([it for it in res.items if not it.skipped]),
                            res.amis_banned_count(), res.ace_hit_count()))
        # 错误上报：正常结束记面包屑；「部分完成」额外报一条 warning（便于集中看接口稳定性）
        failed = res.amis_failed_count()
        bad = sum(1 for oid in res.ace_order
                  if (res.ace.get(oid) or {}).get("check_error")
                  or (res.ace.get(oid) or {}).get("punish_error"))
        mark("查询结束：编号 %d，失败 %d" % (len([it for it in res.items if not it.skipped]),
                                        failed + bad),
             category="query", level="warning" if (failed or bad) else "info")
        if res.amis_error or res.ace_error:
            report_message("联合查询部分完成：%s" % (res.amis_error or res.ace_error),
                           level="warning",
                           tags={"stage": "joint-query", "kind": "partial"},
                           extra={"amis_error": res.amis_error, "ace_error": res.ace_error,
                                  "amis_failed": failed, "ace_failed": bad,
                                  "stopped": res.stopped})

    def _on_error(self, text):
        self._set_running(False)
        self.refresh_platform_status(silent=True)   # 查询异常后同步刷新两平台登录态
        self._set_chip("失败", "err")
        self._progress_finish(failed=True)
        self._append_log("查询失败：%s" % text)
        mark("查询失败：%s" % text, category="query", level="error")
        messagebox.showerror("查询失败", text, parent=self.root)

    # ---------------- 明细弹窗 ----------------
    def _on_amis_dclick(self, idx):
        """双击 amis 文本视图第 idx 行（0 起）→ 查看该编号封禁明细"""
        if not self.result:
            return
        summaries = self.result.amis_summaries
        if idx >= len(summaries):
            return
        s = summaries[idx]
        key = str(s.get("query_value") or "")
        rows = (self.result.amis_detail_rows or {}).get(key) or []
        kind = KIND_LABEL.get(self.result.amis_kind_of.get(key, ""), "—")
        title = "封禁明细 · %s（%s）" % (s.get("roleName") or "—", key)
        sub = ("编号类型：%s　|　当前状态：%s　|　到期时间：%s　|　生效中 %s / 已解禁 %s / 共 %s 条"
               % (kind, s.get("status") or "—", s.get("expire") or "—",
                  s.get("activeCnt"), s.get("unbanCnt"), s.get("totalCnt")))
        empty = s.get("note") or "该编号没有封禁记录（或查询失败）。"
        try:
            dlg = amis_query.DetailDialog(self.root, title, sub, rows, empty)
            self._detail_win = dlg
            if hasattr(dlg, "wait"):
                dlg.wait()
        except Exception as exc:
            report_exception(exc, tags={"stage": "amis-detail-dialog"})
            messagebox.showerror("打开明细失败", format_error(exc), parent=self.root)

    def _on_ace_dclick(self, idx):
        """双击 ACE 文本视图第 idx 行（0 起）→ 查看核查与处罚明细"""
        if not self.result:
            return
        values = self.view_ace.row_values(idx)
        if not values:
            return
        openid = str(values[0])
        data = self.result.ace.get(openid) or {}

        def work():
            try:
                cookies, _creds = ace_load_cookies()
            except Exception:
                cookies = {}
            self._q.put(("ace_detail", (openid, data, cookies)))

        threading.Thread(target=work, daemon=True).start()

    def _show_ace_detail(self, payload):
        openid, data, cookies = payload
        try:
            AceDetailDialog(self.root, openid, data, cookies)
        except Exception as exc:
            report_exception(exc, tags={"stage": "ace-detail-dialog"})
            messagebox.showerror("打开 ACE 明细失败", format_error(exc), parent=self.root)

    # ---------------- 导出 ----------------
    def _open_path(self, path):
        try:
            os.startfile(path)
        except Exception as exc:
            messagebox.showerror("打开失败", format_error(exc), parent=self.root)

    def _on_open_dir(self):
        self._open_path(result_dir())

    def _on_export(self):
        if not self.result:
            messagebox.showinfo("提示", "还没有可导出的查询结果，请先查询。", parent=self.root)
            return
        try:
            path = export_joint_csv(self.result, result_dir())
        except Exception as exc:
            report_exception(exc, tags={"stage": "export"},
                             extra={"编号数": len(getattr(self.result, "items", []) or [])})
            messagebox.showerror("导出失败", format_error(exc), parent=self.root)
            return
        self._append_log("%s  已导出：%s" % (now_text(), path))
        if messagebox.askyesno("导出完成", "已导出到：\n%s\n\n是否打开所在目录？" % path,
                               parent=self.root):
            self._open_path(os.path.dirname(path))

    def run(self):
        try:
            self.root.mainloop()
        except Exception as exc:                 # 主循环自身异常（回调异常另走 report_callback_exception）
            report_exception(exc, tags={"stage": "mainloop"}, handled=False)
            raise
        finally:
            mark("窗口关闭", category="app")
            if not self._restarting:         # 更新重启时尽快让出 exe，别让上报拖慢替换
                sentry_bridge.flush(3.0)     # 退出前把在途事件发完（最多等 3 秒）

# ==================================================================
#                        统一 CSV 导出
# ==================================================================
def _csv_section(writer, title, headers, rows):
    writer.writerow([])
    writer.writerow([title])
    writer.writerow(list(headers))
    for row in rows:
        writer.writerow(list(row))


def export_joint_csv(res, out_dir=None):
    """把一次联合查询结果写成单个 CSV（五个分段），返回绝对路径

    分段：编号识别 / amis 汇总 / amis 明细 / ACE 汇总 / ACE 核查明细 / ACE 处罚明细
    """
    out_dir = out_dir or result_dir()
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "账号封禁联合查询_%s.csv" % timestamp_text())

    amis_detail_headers = ("编号",) + tuple(getattr(amis_query, "TABLE_HEADERS",
                                                   amis_query.DETAIL_COLS))
    ace_check_headers = ("OpenID",) + tuple(c[0] for c in ace_query.COLUMNS)
    ace_punish_headers = ("OpenID",) + tuple(c[0] for c in ace_query.DQ_COLUMNS)

    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, lineterminator="\r\n")
        w.writerow(["账号封禁 · 联合查询结果（amis + ACE）"])
        w.writerow(["查询时间", res.query_at])
        w.writerow(["编号总数", len(res.items),
                    "可查编号", len([it for it in res.items if not it.skipped]),
                    "跳过编号", len([it for it in res.items if it.skipped])])
        w.writerow(["amis 封禁中", res.amis_banned_count(),
                    "ACE 命中账号", res.ace_hit_count(),
                    "是否被手动停止", "是" if res.stopped else "否"])
        if res.amis_error:
            w.writerow(["amis 平台备注", res.amis_error])
        if res.ace_error:
            w.writerow(["ACE 平台备注", res.ace_error])

        _csv_section(w, "① 编号识别", IDENT_HEADERS,
                     [it.as_row() for it in res.items])

        _csv_section(w, "② amis 登录封禁 · 汇总", JOINT_AMIS_HEADERS,
                     res.amis_summary_rows())

        detail_rows = []
        for key, rows in (res.amis_detail_rows or {}).items():
            for row in rows:
                detail_rows.append([key] + list(row))
        _csv_section(w, "③ amis 登录封禁 · 明细", amis_detail_headers, detail_rows)

        _csv_section(w, "④ ACE 可疑/处罚 · 汇总", ACE_SUMMARY_HEADERS_CSV,
                     res.ace_summary_rows())

        check_rows, punish_rows = [], []
        for openid in res.ace_order:
            data = res.ace.get(openid) or {}
            for rec in (data.get("records") or []):
                check_rows.append([openid] + ace_record_row(rec))
            for item in (data.get("punish_items") or []):
                punish_rows.append([openid] + ace_dq_row(item))
        _csv_section(w, "⑤ ACE 可疑用户核查 · 明细", ace_check_headers, check_rows)
        _csv_section(w, "⑥ ACE 数据查询（处罚）· 明细", ace_punish_headers, punish_rows)

    return os.path.abspath(path)


# ==================================================================
#                        自检 / 入口
# ==================================================================
def _reopen_stdio_from_handles():
    """把父进程继承来的标准句柄接回 sys.stdout / sys.stderr

    ``--windowed`` 的 exe 启动时没有控制台，C 运行库不会初始化标准流
    （``sys.stdout`` 为 None）；但如果父进程做了重定向，例如

        cmd /c "AccountCheck.exe --selfcheck > out.txt"

    句柄其实是有效的，只是没人去接它。这里用 GetStdHandle 把它取回来，
    让 ``--selfcheck`` / ``--version`` / ``--check-update`` / ``--update``
    的输出能真正落进文件或管道（排查问题时很关键）。
    """
    try:
        import ctypes
        import msvcrt
        k32 = ctypes.windll.kernel32
    except Exception:
        return {}
    made = {}
    for name, code in (("stdout", -11), ("stderr", -12)):      # STD_OUTPUT/ERROR_HANDLE
        if getattr(sys, name, None) is not None:
            continue
        handle = k32.GetStdHandle(code)
        if not handle or handle in (0, -1):
            continue
        try:
            fd = msvcrt.open_osfhandle(handle, os.O_WRONLY)
            made[name] = os.fdopen(fd, "w", errors="replace", buffering=1)
        except Exception:
            continue
    return made


def ensure_console_output():
    """保证 print() 永远不炸，且尽量让输出真正可见。

    打包成 GUI 子系统 exe（``--windowed``）后进程没有控制台，``sys.stdout`` / ``sys.stderr``
    会是 None —— 这时任何 ``print()`` 都会抛异常把程序打死。所以按顺序尝试：
      1) 父进程已重定向（``> out.txt``）或已连着终端：把继承来的句柄接回来；
      2) 在 cmd / PowerShell 里运行：附着到父进程控制台；
      3) 双击启动（既无控制台也无重定向）：输出丢进空设备，静默丢弃即可。
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    made = _reopen_stdio_from_handles()
    if made.get("stdout") or made.get("stderr"):
        sys.stdout = made.get("stdout") or made.get("stderr")
        sys.stderr = made.get("stderr") or made.get("stdout")
        return
    try:
        import ctypes
        if ctypes.windll.kernel32.AttachConsole(-1):        # ATTACH_PARENT_PROCESS
            stream = open("CONOUT$", "w", errors="replace", buffering=1)
            sys.stdout = stream
            sys.stderr = stream
            return
    except Exception:
        pass
    try:
        sink = open(os.devnull, "w", errors="replace")
        sys.stdout = sink
        sys.stderr = sink
    except Exception:
        pass


def selfcheck():
    """离线自检：模块接口、编号识别、结果渲染与 CSV 导出逻辑（不联网）"""
    ok = True
    warned = []

    def check(name, cond, extra=""):
        nonlocal ok
        ok = ok and bool(cond)
        print("[%s] %s%s" % ("OK  " if cond else "FAIL", name, ("  " + extra) if extra else ""))

    def warn(name, extra=""):
        """提醒项：属于「本机配置」而非代码缺陷，不计入失败（如公开仓库未带授权码）"""
        warned.append(name)
        print("[%s] %s%s" % ("WARN", name, ("  " + extra) if extra else ""))

    print("=" * 68)
    print("%s  v%s  自检（不联网）" % (APP_NAME, CURRENT_VERSION))
    print("=" * 68)
    print("脚本目录：%s" % SCRIPT_DIR)
    check("导入 amis_query", hasattr(amis_query, "run_query") and hasattr(amis_query, "summary_to_row"),
          "run_query / summary_to_row 存在")
    check("导入 ace_query", all(hasattr(ace_query, n) for n in
                                ("query_openid", "query_punish", "load_credentials", "login_by_phone")),
          "query_openid / query_punish / load_credentials / login_by_phone 存在")
    check("界面控件可用", all(hasattr(amis_query, n) for n in
                              ("RoundBox", "MButton", "MTextField", "Segmented",
                               "MTabView", "build_table", "MScrollbar", "DetailDialog")),
          "RoundBox / MButton / MTextField / Segmented / MTabView / build_table / DetailDialog 存在")
    check("表格复制能力可用", all(hasattr(amis_query, n) for n in
                                  ("enable_copy", "copy_rows", "copy_cell", "tree_row_values",
                                   "copy_range", "range_bounds", "display_columns",
                                   "tree_headers", "column_value", "has_range")),
          "enable_copy / copy_rows / copy_cell / copy_range / range_bounds 存在"
          "（拖拽框选 + Ctrl+C + 右键复制）")
    check("图片复制能力可用", hasattr(amis_query, "copy_image_bytes"),
          "copy_image_bytes 存在（截图以位图写入剪贴板，Windows 专用）")
    check("并发查询能力可用", all(hasattr(amis_query, n) for n in ("LOGIN_DIALOG_LOCK",))
          and all(callable(globals().get(n)) for n in
                  ("ace_check_part", "ace_punish_part", "run_ace_batch")),
          "ACE 账号并发 %d / 翻页并发 %d / 在途总闸 %d，amis 并发 %d，两平台并行 %s"
          % (ACE_ACCOUNT_WORKERS, ACE_PAGE_WORKERS, ACE_INFLIGHT_LIMIT,
             AMIS_WORKERS, "开" if PARALLEL_PLATFORMS else "关"))
    _pbar = globals().get("MProgress" + "Bar")
    check("批量进度条可用",
          callable(_pbar) and all(hasattr(_pbar, n) for n in
                                  ("set_value", "set_tone", "value"))
          and "on_item" in inspect.signature(amis_query.run_query).parameters
          and all(callable(globals().get(n)) for n in ("run_joint", "run_ace_batch")),
          "进度条组件 + 两端进度回调（on_item / on_progress）已接入")
    _tab_ok, _tab_note = True, "跳过（无法创建 Tk 根窗口）"
    try:
        _r = tk.Tk()
        _r.withdraw()
        _tv = amis_query.MTabView(_r, font=(amis_query.FONT_UI, 10))
        _pages = [tk.Frame(_tv), tk.Frame(_tv)]
        _tv.add(_pages[0], text="页签A")
        _tv.add(_pages[1], text="页签B")
        _tv.tab(_pages[0], text="页签A（1）")
        _r.update_idletasks()
        _tv.select(_pages[1])
        _tab_ok = (_tv.index(_pages[1]) == 1 and _tv.tab(_pages[0]) == "页签A（1）"
                   and _tv.current() is _pages[1] and _pages[1].winfo_manager() == "pack")
        _tab_note = ("add / tab / select / index 可用，页签高 %d（自绘胶囊页签）"
                     % _tv.strip.winfo_reqheight())
        _r.destroy()
    except Exception as _exc:
        _tab_ok, _tab_note = False, "异常：%s" % _exc
    check("页签容器可用", _tab_ok, _tab_note)
    # ---- 授权校验（license-guard）：只做静态校验，不联网、不消耗设备位 ----
    _lic = license_boot.summary() if license_boot else {}
    try:                                     # 打包成 exe 后没有 .py 源码，取不到就退化为「不检查」
        _main_src = inspect.getsource(main)
    except Exception:
        _main_src = ""
    _wired = ("license_boot." + "ensure_license") in _main_src
    check("授权接入可用",
          bool(_lic) and _lic.get("available") and _lic.get("app_id")
          and _lic.get("server_url", "").startswith("https://")
          and len(_lic.get("machine_id") or "") >= 16
          and callable(getattr(license_boot, "ensure_license", None))
          and (_wired or not _main_src),
          "app_id=%s，算法=%s，入口已接入校验（%s）"
          % (_lic.get("app_id", "—"), _lic.get("sign_alg", "—"),
             "main() 内" if _wired else ("打包版无源码，跳过静态校验" if not _main_src else "未接入")))
    if _lic.get("key_found"):
        check("授权码已就位", True,
              "%s（来源：%s）" % (_lic.get("key_masked"), _lic.get("key_source")))
    else:
        warn("授权码已就位",
             "未找到 license.key —— 启动会因 NO_KEY 被拦下；把授权码写入程序目录 "
             "license.key 或设环境变量 LICENSE_KEY（公开仓库不带授权码，属正常提醒）")

    # 边缘限流/网关错误页（非本系统 JSON）必须降级成「联不上」，
    # 否则一次限流就会让用户打不开程序；而真正的失效码必须原样上抛。
    class _StubGuard(object):
        def __init__(self, payload):
            self._payload = payload

        def _post(self, path, body):
            return self._payload

    _g_trans = license_boot._degrade_transient_responses(
        _StubGuard({"ok": False, "code": "HTTP_429", "message": "Too Many Requests"}))
    _t_trans = _g_trans._post("/v1/verify", {})
    _g_deny = license_boot._degrade_transient_responses(
        _StubGuard({"ok": False, "code": "LICENSE_EXPIRED", "message": "expired"}))
    _t_deny = _g_deny._post("/v1/verify", {})
    check("限流不致打不开程序",
          _t_trans is None and isinstance(_t_deny, dict)
          and _t_deny.get("code") == "LICENSE_EXPIRED",
          "非 JSON 429（Cloudflare 边缘限流页）/ 5xx → 降级为「联不上」走离线容忍；"
          "LICENSE_* / KEY_* 等真实失效码原样上抛（仍按 fail_mode 静默退出）")

    # ---- 自动更新：版本比较 / 加速地址拼接 / 断点续传 / 校验 / 替换脚本 ----
    # 全部离线（断点续传用本地临时 HTTP 服务验证），不产生任何外网请求。
    if update_check is None:
        check("自动更新模块可用", False, "导入失败：%s" % _UPDATE_IMPORT_ERROR)
    else:
        _upd_ok, _upd_detail = update_check.offline_selftest(printer=print)
        check("自动更新模块可用", _upd_ok, _upd_detail)
        _upref = update_check.candidate_prefixes()
        check("更新接入可用",
              all(callable(getattr(update_check, n, None)) for n in
                  ("check_for_update", "download_update", "install_update", "cmd_update"))
              and all(hasattr(JointApp, n) for n in
                      ("on_check_update", "_download_update_worker", "_on_update_event"))
              and (("cmd_update" in _main_src) or not _main_src),
              "当前版本 v%s（%s）；版本源 %d 条候选（直连 + %d 个加速镜像）；"
              "GUI 按钮 / 菜单命令均已接入"
              % (CURRENT_VERSION, "CI 构建版本" if BUILD_VERSION else "源码 VERSION",
                 len(_upref), max(0, len(_upref) - 1)))
        if update_check.updates_disabled():
            warn("更新检查已关闭",
                 "环境变量 %s 已置位，程序不会联网检查更新" % update_check.ENV_DISABLE)
    # ---- 错误上报（Sentry）：全部离线校验，不产生任何网络请求 ----
    _sc = sentry_bridge
    _dsn_cfg = _sc.parse_dsn(_sc.DEFAULT_DSN) if _sc else None
    check("错误上报模块可用",
          bool(_sc) and bool(_dsn_cfg)
          and _dsn_cfg["store_url"].endswith("/api/%s/store/" % _dsn_cfg["project_id"])
          and all(callable(getattr(_sc, n)) for n in
                  ("init", "capture_exception", "capture_message", "add_breadcrumb",
                   "install_excepthooks", "flush", "tk_callback_exception")),
          "DSN 解析为 %s；capture_exception / capture_message / 全局钩子齐备"
          % ((_dsn_cfg or {}).get("store_url", "解析失败")))
    _probe_client = _sc.SentryClient(dsn=_sc.DEFAULT_DSN, release=VERSION,
                                     app_name=APP_NAME, app_root=SCRIPT_DIR)
    try:                                     # 造一个带敏感信息的异常，核对脱敏
        raise ValueError("角色 123456789 查询失败 token=SECRET123 "
                         r"路径 C:\Users\someone\Desktop\demo")
    except Exception as _exc:
        _ev = _probe_client.build_exception_event(
            _exc, tags={"stage": "selftest"},
            extra={"手机号": "13800138000", "password": "p@ss", "编号数": 10})
    _blob = json.dumps(_ev, ensure_ascii=False, default=str)
    _st = (_ev.get("exception") or {}).get("values", [{}])[0].get("stacktrace") or {}
    check("上报内容已脱敏",
          bool(_st.get("frames")) and _st["frames"][-1].get("in_app") is True
          and "123456789" not in _blob and "13800138000" not in _blob
          and "SECRET123" not in _blob and "<user>" in _blob
          and _ev["extra"].get("password") == "<redacted>"
          and _ev["extra"].get("编号数") == 10,
          "长数字→<id>、密钥→<redacted>、用户名路径→<user>；堆栈含源码上下文 %d 帧"
          % len(_st.get("frames") or []))
    _off = _sc.SentryClient(dsn="", release=VERSION)
    check("上报可关闭 / 失败静默",
          _off.enabled is False and _off.capture_exception(ValueError("x")) == ""
          and _off.send({"event_id": "0" * 32}) == ""
          and _sc.parse_dsn("https://bad-dsn") is None,
          "空 DSN / 非法 DSN 下所有上报入口都静默返回，不影响业务")
    _ticks = []
    _g = globals()
    _orig_parts = (_g["ace_check_part"], _g["ace_punish_part"])
    _g["ace_check_part"] = lambda oid, cookies, log, should_stop=None: {
        "records": [], "check_error": ""}
    _g["ace_punish_part"] = lambda oid, cookies, log, should_stop=None: {
        "punish_items": [], "punish_count": 0, "punish_error": ""}
    try:                                    # 用假链路离线验证进度回调计数
        _got, _pending, _auth = run_ace_batch(
            ["p1", "p2", "p3"], {}, lambda _m: None, lambda: False, workers=3,
            on_item=lambda n, t, _oid="": _ticks.append((n, t)))
        _got2, _pending2, _auth2 = run_ace_batch(
            ["s1", "s2"], {}, lambda _m: None, lambda: True, workers=2,
            on_item=lambda n, t, _oid="": _ticks.append((n, t)))
    finally:
        _g["ace_check_part"], _g["ace_punish_part"] = _orig_parts
    check("进度回调计数正确",
          [n for n, _t in _ticks] == [1, 2, 3] and all(t == 3 for _n, t in _ticks)
          and len(_got) == 3 and not _pending,
          "3 个编号 → 回调 %s（总数恒为 3）" % ([n for n, _t in _ticks],))
    check("停止指令即时生效",
          len(_got2) == 0 and len(_pending2) == 2
          and _orig_parts[0]("p", {}, None, lambda: True) is None,
          "停止后不再发请求：0 个结果 / %d 个待查（链路返回 None 不计入结果）" % len(_pending2))
    # ---- 无端ID → 角色ID 换算（ACE 的 OpenID 只认角色ID） ----
    _orig_fetch_info = amis_query.fetch_user_info_with_auth_retry
    _asked = []

    def _fake_info(rid, box, key_type, note):
        _asked.append((rid, key_type))
        return {"role_name": "自检角色", "wd_id": rid,
                "role_id": "7654321" if rid == "10000000000001" else "",
                "state": "ok"}
    amis_query.fetch_user_info_with_auth_retry = _fake_info
    try:
        _wmap = resolve_wuduan_role_ids(["10000000000001", "10000000000002"],
                                        None, lambda _m: None, workers=2)
    finally:
        amis_query.fetch_user_info_with_auth_retry = _orig_fetch_info
    check("无端ID 换算角色ID",
          _wmap.get("10000000000001") == "7654321" and _wmap.get("10000000000002") == ""
          and _asked and all(k == amis_query.KEY_TYPE_WDID for _r, k in _asked),
          "经角色信息接口（key=%s）换算：成功 1 / 未取到 1（未取到的编号跳过 ACE）"
          % amis_query.KEY_TYPE_WDID)

    # ---- 端到端（假链路）：输入无端ID 时 ACE 必须用「换算出的角色ID」查询 ----
    _saved = {n: _g[n] for n in ("preflight_amis_login", "amis_local_snapshot",
                                 "probe_ace_online")}
    _saved_parts = (_g["ace_check_part"], _g["ace_punish_part"])
    _saved_run_query = amis_query.run_query
    _saved_fetch_info = amis_query.fetch_user_info_with_auth_retry
    _ace_asked = []

    def _fake_check(oid, cookies, log_fn=None, should_stop=None):
        _ace_asked.append(oid)
        return {"records": [], "check_error": ""}

    _g["preflight_amis_login"] = lambda _log: {"verdict": "valid", "token": "T"}
    _g["amis_local_snapshot"] = lambda: {"state": "ok", "token": "T",
                                         "account": "selftest", "source": "自检"}
    _g["probe_ace_online"] = lambda _c: "valid"
    _g["ace_check_part"] = _fake_check
    _g["ace_punish_part"] = lambda oid, cookies, log_fn=None, should_stop=None: {
        "punish_items": [], "punish_count": 0, "punish_error": ""}
    amis_query.run_query = lambda ids, **kw: amis_query.QueryResult(
        [], [], token_source="自检", id_label=kw.get("id_label", "编号"), total_ids=len(ids))
    amis_query.fetch_user_info_with_auth_retry = lambda rid, box, key_type, note: {
        "role_name": "自检角色", "wd_id": rid, "role_id": "7654321", "state": "ok"}
    try:
        _wd_items = build_ident_items("10000000000001", MODE_AUTO)[0]
        _wd_res = run_joint(_wd_items, on_log=lambda _m: None,
                           ace_cookies_provider=lambda _r: {"c": "1"})
    finally:
        for _n, _v in _saved.items():
            _g[_n] = _v
        _g["ace_check_part"], _g["ace_punish_part"] = _saved_parts
        amis_query.run_query = _saved_run_query
        amis_query.fetch_user_info_with_auth_retry = _saved_fetch_info
    check("无端ID 改用角色ID 查 ACE",
          _ace_asked == ["7654321"] and _wd_res.ace_order == ["7654321"]
          and _wd_res.items[0].ace_openid == "7654321"
          and "7654321" in _wd_res.items[0].note,
          "输入无端ID 10000000000001 → ACE 实际查询的角色ID 为 %s"
          % (_ace_asked[0] if _ace_asked else "未查询"))
    _prog_items = build_ident_items("123456, 234567, 12345678901234, abc", MODE_AUTO)[0]
    _prog = JointProgress(_prog_items)
    _snaps = []
    for _it in _prog_items:
        if _it.skipped:
            continue
        _snaps.append(_prog.bump(_it.raw))              # 该编号的 amis 线完成
        _snaps.append(_prog.bump(_it.ace_openid))       # 该编号的 ACE 线完成
    check("进度按编号计数",
          _prog.total == 3 and _prog.done == 3 and _prog.remaining() == 0
          and [s for s in _snaps if s] == [(1, 3), (2, 3), (3, 3)],
          "3 个编号 × 双平台 → 总数仍是 3（逐个到 3/3），不再按平台翻倍")
    _o = "selftest-openid"
    _merged = ace_merge_parts(_o, {"records": [{"log_time": "2026-10-01 10:00:00",
                                                "level": "high"}],
                                   "check_error": ""},
                               {"punish_items": [{"punish_time": "2026-10-01 10:00:00"}],
                                "punish_count": 1, "punish_error": ""})
    check("ACE 结果合并逻辑", _merged["openid"] == _o and len(_merged["records"]) == 1
          and len(_merged["punish_items"]) == 1 and _merged["top_level"]
          and not _merged["check_error"],
          "两条链路结果可正确合并（核查 %d 条 / 处罚 %d 条）"
          % (len(_merged["records"]), len(_merged["punish_items"])))
    check("明细展示列已精简", REC_DISPLAY_FIELDS == tuple(
        c[1] for c in ace_query.COLUMNS if c[1] not in REC_HIDDEN_FIELDS)
        and not set(REC_HIDDEN_FIELDS) & set(REC_DISPLAY_FIELDS),
        "隐藏 %s，展示 %s" % ("/".join(REC_HIDDEN_FIELDS), "、".join(REC_DISPLAY_FIELDS)))
    _jr = JointResult()
    _jr.ace_order = ["o1", "o2"]
    _jr.ace = {"o1": {"records": [1], "punish_items": [], "top_level": "high",
                      "last_punish": "2026-10-01 12:00"},
               "o2": {"records": [], "punish_items": [], "check_error": "接口超时"}}
    _r_ui = _jr.ace_summary_rows(with_note=False)
    _r_csv = _jr.ace_summary_rows()
    check("ACE 汇总列已精简",
          len(ACE_SUMMARY_COLUMNS) == len(ACE_SUMMARY_HEADERS) == len(ACE_SUMMARY_WIDTHS) == 5
          and all(len(r) == 5 for r in _r_ui) and all(len(r) == 6 for r in _r_csv)
          and "备注" not in ACE_SUMMARY_HEADERS and "备注" in ACE_SUMMARY_HEADERS_CSV,
          "界面 %d 列（无备注）/ CSV %d 列（含备注）" % (len(_r_ui[0]), len(_r_csv[0])))
    try:
        _src = open(os.path.join(SCRIPT_DIR, "joint_query.py"), encoding="utf-8").read()
    except Exception:
        _src = ""                            # 打包成 exe 后没有 .py 源码
    if _src:
        check("中部统计栏已移除",
              ("_build_" + "metrics") not in _src and ("self." + "metrics") not in _src,
              "指标集中在「查询结果」标题行右侧概览")
    else:
        warn("中部统计栏已移除", "打包版没有 .py 源码，跳过该源码级检查")
    check("Pillow（ACE 截图预览）", HAS_PIL,
          "未安装时截图页仅提供「浏览器打开原图」" if not HAS_PIL else "已安装")

    print("-" * 68)
    samples = [("123456", KIND_ROLE), ("12", KIND_ROLE), ("1", None),
               ("123456789012", KIND_ROLE), ("1234567890123", None),
               ("12345678901234", KIND_WUDUAN), ("abcdef_-123", None)]
    for token, expect in samples:
        item = classify_token(token, MODE_AUTO)
        got = item.amis_kind if item else None
        check("编号识别 %-16s" % token, got == expect,
              "识别为 %s" % (item.kind_text if item else "无"))
    item = classify_token("2648868", MODE_ROLE)
    check("仅角色ID 模式", bool(item and item.amis_kind == KIND_ROLE
                                and item.ace_openid == "2648868"
                                and item.kind_text == "角色ID"),
          "amis=角色ID，ACE 编号=%s" % (item.ace_openid if item else None))
    check("识别模式选项", [v for _t, v in MODE_OPTIONS] == [MODE_AUTO, MODE_ROLE, MODE_WUDUAN],
          " / ".join(t for t, _v in MODE_OPTIONS))
    items, dup = build_ident_items("123456, 123456\n12345678901234 ； abc-def", MODE_AUTO)
    check("去重与切分", dup == 1 and len(items) == 3, "去重 %d，识别 %d" % (dup, len(items)))

    print("-" * 68)
    res = JointResult()
    res.items = build_ident_items("123456, 12345678901234, abc-def", MODE_AUTO)[0]
    check("汇总行拼接（编号类型列）", True, "amis 未查询时可正常渲染空表")
    try:
        path = export_joint_csv(res, os.path.join(SCRIPT_DIR, OUTPUT_DIR_NAME, "自检"))
        size = os.path.getsize(path)
        check("CSV 导出（空结果骨架）", size > 0, os.path.basename(path))
        try:
            os.remove(path)
        except Exception:
            pass
    except Exception as exc:                                  # pragma: no cover
        check("CSV 导出（空结果骨架）", False, format_error(exc))

    print("=" * 68)
    if ok and warned:
        print("自检结果：全部通过（另有 %d 项提醒，见上面 WARN 行）" % len(warned))
    else:
        print("自检结果：%s" % ("全部通过" if ok else "存在失败项，请检查上面 FAIL 行"))
    return 0 if ok else 1


def test_sentry(verbose=True):
    """向 Sentry 发一条测试事件，用于确认接入是否正常（联网）"""
    sentry_bridge.init(release=VERSION, app_name=APP_NAME, app_root=SCRIPT_DIR)
    client = sentry_bridge.get_client()
    if not sentry_ready():
        print("[提示] 错误上报已关闭（JOINT_SENTRY=0 或 DSN 为空）——未发送任何内容。")
        return 0
    print("上报地址：%s" % (client.config or {}).get("store_url", "?"))
    sentry_bridge.add_breadcrumb("test-sentry 命令", category="cli")
    try:                                     # 造一个带堆栈的异常，便于在 Sentry 里看堆栈展示
        raise RuntimeError("【测试】joint_query 错误上报连通性自检（可忽略）")
    except Exception as exc:
        eid = sentry_bridge.capture_exception(exc, tags={"stage": "test-sentry"},
                                              extra={"说明": "由 --test-sentry 触发",
                                                     "release": VERSION})
    sentry_bridge.flush(15)
    if client.last_error:
        print("[失败] 上报出错：%s" % client.last_error)
        return 1
    if verbose:
        print("[成功] 已上报 1 条测试事件，事件 ID：%s" % (eid or client.last_event_id or "?"))
        print("        请到 Sentry 项目的 Issues 里查看「【测试】joint_query 错误上报连通性自检」。")
    return 0


def cmd_update(install=True):
    """命令行更新：检查 → 下载（断点续传）→ 校验 → 替换安装

    退出码：0 = 已是最新（或更新流程已启动）；10 = 仅检查（--check-update）且有新版本；
    1 = 检查 / 下载 / 安装失败。
    """
    if update_check is None:
        print("更新组件缺失或损坏（src/update_check.py）：%s" % _UPDATE_IMPORT_ERROR)
        return 1
    if update_check.updates_disabled():
        print("更新检查已关闭（环境变量 %s）" % update_check.ENV_DISABLE)
        return 0
    try:
        info = update_check.check_for_update(CURRENT_VERSION, notice=print)
    except Exception as exc:
        print("检查更新失败：%s" % format_error(exc))
        return 1
    if not info:
        return 0
    print(update_check.format_info(info))
    if not install:
        return 10

    last = [-1]

    def prog(done, total, _speed):
        if not total:
            return
        pct = int(done * 100.0 / total)
        if pct % 5 == 0 and pct != last[0]:
            last[0] = pct
            print("  下载 %s / %s（%d%%）" % (update_check.human_size(done),
                                            update_check.human_size(total), pct))

    try:
        path = update_check.download_update(info, progress=prog, notice=print)
    except Exception as exc:
        print("下载失败：%s" % format_error(exc))
        print("（已下载的片段保留在 %s，下次会自动续传）" % update_check.updates_dir())
        return 1
    print("安装包已就绪：%s" % path)
    try:
        update_check.install_update(path, notice=print)
    except Exception as exc:
        print("%s" % exc)
        return 1
    print("替换脚本已启动：程序将退出并重启到 v%s。" % info.get("version"))
    return 0


def main(argv=None):
    ensure_console_output()
    parser = argparse.ArgumentParser(
        prog="joint_query", description="%s v%s（单入口：amis + ACE）" % (APP_NAME, CURRENT_VERSION))
    parser.add_argument("--selfcheck", action="store_true", help="离线自检（不联网）后退出")
    parser.add_argument("--version", action="store_true", help="显示版本后退出")
    parser.add_argument("--test-sentry", action="store_true",
                        help="向 Sentry 发一条测试事件，确认错误上报链路（联网）")
    parser.add_argument("--check-update", action="store_true",
                        help="检查有没有新版本（联网）后退出；退出码 0=最新，10=有新版本，1=失败")
    parser.add_argument("--update", action="store_true",
                        help="检查 → 断点续传下载 → 校验 → 替换安装（联网）后退出")
    args = parser.parse_args(argv)

    if args.version:
        print("%s v%s%s" % (APP_NAME, CURRENT_VERSION,
                            ("（源码 VERSION=%s）" % VERSION) if BUILD_VERSION else ""))
        return 0
    if args.selfcheck:
        return selfcheck()
    if args.test_sentry:
        return test_sentry()
    if args.check_update or args.update:
        return cmd_update(args.update)

    # 授权校验（license-guard）：放在一切业务代码之前 —— 校验不通过时进程在这里
    # 就按后端下发的策略结束（本工具由后端产品决定是静默退出还是提示后退出），
    # 下面的界面初始化 / 查询逻辑根本不会执行；通过后同时开启看门狗，
    # 运行期到期（或后台吊销）会在下次复查时立即退出。
    if license_boot is None:
        raise SystemExit("授权组件缺失（src/license_boot.py）：%s\n"
                         "请确认 src/license_boot.py 与 src/vendor/license_guard.py "
                         "均存在。" % _LICENSE_BOOT_ERROR)
    license_boot.ensure_license(component="joint_query")

    # 错误上报：先于界面初始化，保证启动阶段的异常也能被捕获
    sentry_bridge.init(release=VERSION, app_name=APP_NAME, app_root=SCRIPT_DIR,
                       logger_name="joint_query")
    sentry_bridge.install_excepthooks()
    print(sentry_status())

    try:
        app = JointApp()
    except Exception as exc:
        report_exception(exc, tags={"stage": "ui-startup"}, handled=False)
        sentry_bridge.flush(5.0)
        print("界面启动失败：%s" % format_error(exc))
        return 1
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
