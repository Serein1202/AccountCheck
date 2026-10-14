#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
账号封禁查询工具（图形界面版）
=================================================================
输入「角色ID」或「无端ID」，去 amis 后台「游戏管理 → 数据封禁 → 登录封禁解禁」
(http://amis.ssjj.cn/#/game_manager/data_forbidden/login) 查询该玩家是否存在登录封禁记录。
两种查询条件二选一，通过界面「查询条件」单选切换（命令行用 --key wdid）。

界面用法（默认）:
  1. 运行脚本 / exe 打开窗口
  2. 选择「查询条件」：角色ID（默认）或无端ID，再填入对应编号；支持一次查多个（批量查询），
     用空格、逗号、分号、换行分隔，一行一个编号（如 2648868,14473396，无端ID 如 10000141237188）
  3. 「导入文件」可从 txt / csv 里批量导入编号（每行一个，或编号占某一列），导入后请核对
  4. 点击「查询」（或按 Ctrl+回车）：下方「账号汇总」按账号列出封禁概况、
     「封禁明细」列出全部登录封禁记录（含封禁中 / 已解禁）；
     批量查询时多个账号并发查询（默认 4 个账号并发），可随时点「停止」中止
  5. 「导出CSV」把账号汇总 + 明细导出到脚本同目录的「查询结果」文件夹

命令行用法:
  python 账号封禁查询.py --query 2648868,14473396   # 静默查询并打印结果（无界面，按角色ID）
  python 账号封禁查询.py --query 10000141237188 --key wdid   # 按无端ID 查询
  python 账号封禁查询.py --file 账号清单.txt --csv           # 从文件批量导入编号查询并导出 CSV
  python 账号封禁查询.py --selfcheck                # 界面自检（启动后自动退出）

批量查询说明:
  - 一次可查任意多个编号（界面 / 命令行均支持），并发账号数由 BATCH_CONCURRENCY 控制（默认 4）
  - 单账号查询前会先调用角色信息接口（/userInfo/getUserInfoByUserKeyword，取自角色管理页）
    取角色名：无封禁记录也照常显示角色名，接口查不到该角色时显示「未查询到该角色」
  - 单个账号查询失败（接口异常等）只影响该账号，状态栏标注「查询失败」，其余账号照常出结果
  - 登录态失效时只需重新登录一次，同一批其余账号自动复用新 Token，不会重复弹登录框
  - 导出的 CSV 含「账号汇总」「封禁明细」两段，便于按账号快速定位封禁情况

登录态说明（获取顺序，优先级由高到低）:
  1. 环境变量 AMIS_TOKEN
  2. 本地 Token 缓存（脚本 / exe 同目录下的 .amis_token.json，未过期时直接复用）
  3. 本机 Chrome / Edge 中 amis.ssjj.cn 的有效 Token（AMIS-USER-TOKEN）
  4. 以上均不可用时（即「首次使用」）状态栏提示「需要重新登录（首次使用）」，
     并弹出标题为「首次使用，请登录」的登录窗口，手动登录换取新 Token 后继续查询
  - 查询过程中若被服务端判定登录态失效（401/403 或鉴权类错误码），自动删除缓存，
    状态栏提示「需要重新登录（登录状态已失效）」，并弹出标题为「登录状态已失效，请重新登录」
    的登录窗口，无需重跑
  - 登录请求: POST http://amis.ssjj.cn/auth/signIn
    Header X-PROJECT-ID 必须固定为 "1"，体 {"account","password","ldap"}
    （查询接口的 X-PROJECT-ID 仍为 "28"，两者不可混用）
  - 账号密码仅在内存中使用，绝不写入缓存、日志或代码；脚本内不含任何硬编码凭据

后台接口（实测，即「登录封禁解禁」页面所用接口）:
  GET http://amis.ssjj.cn/gameConfigs/getLoginOrChatForbid
      ?page=1&perPage=200&key=channel_uid&keyword=<角色ID>&forbid_type=1
  Header: X-AMIS-TOKEN / X-AMIS-ACCOUNT / X-PROJECT-ID: 28
  返回: {"code":0,"data":{"items":[{id,role_id,roleName,wdId,forbid_type,operate_type,
         inner_reason,message,expire_time,report_time,operator,...}],"total":N}}
  - key=channel_uid 按「角色ID」检索，key=char_name 按「无端ID」检索（与页面
    「玩家信息类型」下拉选项一致），keyword 为对应的纯数字编号；
    服务端不校验时 keyword 传空会返回全量数据，故本工具强制校验非空且为数字
  - forbid_type: 1=登录封禁（页面默认），3=聊天封禁
  - operate_type: 1=封禁中，2=已解禁
  - expire_time / report_time 为毫秒时间戳；operator: 0=ace，1=运营平台，3=ace短时封禁，4=ace截图

操作员名称来源（对应「游戏管理 → 数据封禁 → 封禁解禁日志」页）:
  GET http://amis.ssjj.cn/gameConfigs/getForbidLog
      ?page=1&perPage=200&startDate=<YYYY-MM-DD>&endDate=<YYYY-MM-DD>&forbid_type=1&roleId=<角色ID>
  Header: X-AMIS-TOKEN / X-AMIS-ACCOUNT / X-PROJECT-ID: 28
  返回: {"code":0,"data":{"items":[{id,roleId,wdId,roleName,admin,type,forbidType,reason,
         outer_reason,date,createdAt,...}],"total":N}}
  - 明细中 operator=1（运营平台）的记录，会用该页的 admin（操作员）覆盖「运营平台」展示
  - 匹配方式：角色ID + 操作时间（report_time ↔ createdAt，秒级）一致；
    对不上时回退为「同封禁类型 + 同内部理由 + 操作时间相差 ≤ 120 秒」且候选唯一
  - 日志页未取到 / 请求失败时保留原「运营平台」，不影响主体查询

依赖: 无第三方依赖（仅标准库 tkinter）
打包 exe:
  pyinstaller --noconsole --onefile --name "账号封禁查询" 账号封禁查询.py
"""

import base64
import concurrent.futures
import csv
import json
import os
import queue
import re
import shutil
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timedelta, timezone

# ====================== 可配置项 ======================
HOST = "http://amis.ssjj.cn"

# 数据接口（对应 amis 后台「游戏管理 → 数据封禁 → 登录封禁解禁」页）
QUERY_API = "/gameConfigs/getLoginOrChatForbid"
# 角色信息接口（角色管理页「角色查询」页签）：按查询条件取角色名 / 已登记的无端ID
USER_INFO_API = "/userInfo/getUserInfoByUserKeyword"
INFO_NOT_FOUND_CODE = 10001      # 角色信息接口「未找到角色信息」的业务码
PROJECT_ID = "28"                # 数据接口专用项目号
PER_PAGE = 200                   # 单页拉取条数（实测可支持 1000）
HTTP_TIMEOUT = 30                # 单次请求超时（秒）

# 操作员名称来源（对应 amis 后台「游戏管理 → 数据封禁 → 封禁解禁日志」页）
LOG_API = "/gameConfigs/getForbidLog"
LOG_PER_PAGE = 200               # 日志页单页拉取条数

# 批量查询（一次查询多个账号）
BATCH_CONCURRENCY = 4            # 同时查询的账号数（并发线程数）；设为 1 即逐个串行查询
BATCH_CONFIRM_THRESHOLD = 300    # 界面一次查询的账号数超过该值时，先弹窗确认再查

# 登录弹窗串行锁：联合查询（joint_query）里 amis 与 ACE 两个平台可能同时在查，
# 若两边同时判定登录态失效，会各弹一个登录框。用这把锁保证同一时刻只弹一个。
LOGIN_DIALOG_LOCK = threading.Lock()
ID_MIN_DIGITS = 2                # 从文件导入时认定「像编号」的最小位数（角色ID 通常 2~12 位）
ID_MAX_DIGITS = 12               # 角色ID 最大位数
WDID_DIGITS = 14                 # 无端ID 固定位数（用于从文件里过滤时间戳等杂项数字）

# 登录接口
LOGIN_PAGE = HOST + "/"
LOGIN_API = HOST + "/auth/signIn"
LOGIN_PROJECT_ID = "1"           # 登录接口专用项目号，固定 "1"（用 28 会返回 400）
LOGIN_MAX_RETRY = 3              # 手动登录最大重试次数

# 「需要重新登录」的原因分类：用于状态栏提示 + 登录窗口标题 / 副标题
LOGIN_REASON_FIRST = "first_use"     # 首次使用：本地无缓存 Token，浏览器里也取不到
LOGIN_REASON_EXPIRED = "expired"     # 登录状态已失效 / 已过期：需要重新登录

LOGIN_REASON_TEXT = {
    LOGIN_REASON_FIRST: (
        "首次使用，请登录",
        "本机未找到可用的登录 Token（环境变量 / 本地缓存 / 浏览器均无），"
        "请使用 amis.ssjj.cn 账号登录，登录后将继续本次查询。",
    ),
    LOGIN_REASON_EXPIRED: (
        "登录状态已失效，请重新登录",
        "服务端判定当前登录 Token 已失效或已过期，请重新登录以换取新 Token，"
        "登录后将继续本次查询。",
    ),
}

LOGIN_STATUS_TEXT = {
    LOGIN_REASON_FIRST:
        "需要重新登录（首次使用）：本机未找到可用的登录 Token"
        "（环境变量 / 本地缓存 / 浏览器均无），请在登录窗口中登录后继续。",
    LOGIN_REASON_EXPIRED:
        "需要重新登录（登录状态已失效）：服务端判定当前 Token 已失效 / 已过期，"
        "请在登录窗口中重新登录（本批其余账号会复用新 Token）。",
}


def login_reason_texts(reason="", tip=""):
    """返回登录窗口的 (标题, 副标题)：标题明确写出「需要重新登录」的原因"""
    title, default_sub = LOGIN_REASON_TEXT.get(
        reason, ("需要重新登录", "自动获取登录 Token 失败，请手动登录后继续查询。"))
    return title, (tip or default_sub)


def login_reason_status(reason=""):
    """状态栏文案：明确提示「需要重新登录」并说明原因"""
    return LOGIN_STATUS_TEXT.get(reason, "需要重新登录：请在登录窗口中登录后继续。")


# 本地 Token 缓存：脚本 / exe 同目录，仅存 token / account / expire / saved_at
TOKEN_CACHE_NAME = ".amis_token.json"

# 结果导出目录（脚本 / exe 同目录下）
OUTPUT_DIR_NAME = "查询结果"

# 查询参数（与页面「玩家信息类型 / 封禁类型」筛选项一致）
KEY_TYPE_ROLE = "channel_uid"    # 玩家信息类型 = 角色ID
KEY_TYPE_WDID = "char_name"      # 玩家信息类型 = 无端ID
KEY_TYPE = KEY_TYPE_ROLE         # 默认查询条件
QUERY_KEY_OPTIONS = [
    ("角色ID", KEY_TYPE_ROLE),
    ("无端ID", KEY_TYPE_WDID),
]
FORBID_TYPE = 1                  # 1=登录封禁（页面默认值）

# 字段文案映射
FORBID_STATUS_TEXT = {
    1: "封禁中",
    2: "已解禁",
}
FORBID_TYPE_TEXT = {
    1: "登录封禁",
    2: "IP封禁",
    3: "聊天封禁",
    4: "MAC封禁",
    5: "设备封禁",
    6: "语音封禁",
}

# 「当前是否处于封禁状态」判定：operate_type（1=封禁中 / 2=已解禁）+ expire_time
# （为空视为永久封禁）、并校验是否已过期；按 forbid_type 区分登录封禁（1）/ 聊天封禁（3）
FORBID_OPERATE_ACTIVE = 1        # operate_type=1 封禁中
FORBID_OPERATE_RELEASED = 2      # operate_type=2 已解禁
FORBID_KIND_LOGIN = 1            # forbid_type=1 登录封禁
FORBID_KIND_CHAT = 3             # forbid_type=3 聊天封禁

STATUS_TEXT_NORMAL = "正常"
STATUS_TEXT_BANNED = "封禁中"
STATUS_TEXT_FAILED = "查询失败"
STATUS_TEXT_PERMANENT = "永久"
# 角色名补齐：接口明确返回「未找到角色信息」/ 接口本身不可用时，用明确文案占位而不是留空
STATUS_TEXT_NO_ROLE = "未查询到该角色"
STATUS_TEXT_ROLE_FAILED = "角色名获取失败"
ROLE_NAME_PLACEHOLDERS = (STATUS_TEXT_NO_ROLE, STATUS_TEXT_ROLE_FAILED)
OPERATOR_TEXT = {
    0: "ace",
    1: "运营平台",
    3: "ace短时封禁",
    4: "ace截图",
}
OPERATOR_PLATFORM = 1            # operator=1 即「运营平台」，需用日志页操作员名称覆盖
CST_TZ = timezone(timedelta(hours=8))   # 后台时间统一按 UTC+8 处理

APP_TITLE = "账号封禁查询工具"
FONT_UI = "Microsoft YaHei UI"

# ---- 以下是内部实现，一般无需修改 ----

# 界面日志接收器：GUI 启动时替换为向界面推送的回调，其余场景为 None
_LOG_SINK = None


def script_dir():
    """脚本所在目录（PyInstaller 打包成 exe 时取 exe 所在目录）"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _console(msg):
    """控制台输出；exe 无控制台（--noconsole）时静默忽略"""
    try:
        print(msg, flush=True)
    except Exception:
        pass


def log(msg):
    """统一日志出口：控制台 + 界面日志区"""
    _console(msg)
    sink = _LOG_SINK
    if sink is not None:
        try:
            sink(f"{datetime.now():%H:%M:%S}  {msg}")
        except Exception:
            pass


# ---------------------------------------------------------------- 基础工具
def map_text(mapping, value, prefix="其他"):
    """把后台枚举值转成可读文案，未知值原样带前缀展示"""
    try:
        key = int(value)
    except (TypeError, ValueError):
        return str(value if value is not None else "")
    return mapping.get(key, f"{prefix}({key})")


def fmt_ms_ts(value):
    """毫秒时间戳 → 'YYYY-MM-DD HH:MM:SS'；空值 / 非法值返回空串"""
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return ""
    if ms <= 0:
        return ""
    try:
        return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return str(value)


def parse_role_ids(text):
    """从输入文本中解析角色ID列表：支持空格 / 逗号 / 分号 / 换行分隔，自动去重保序"""
    ids, seen = [], set()
    for raw in re.split(r"[^0-9]+", text or ""):
        if not raw:
            continue
        rid = raw.lstrip("0") or "0"
        if rid in seen:
            continue
        seen.add(rid)
        ids.append(rid)
    return ids


def parse_bad_tokens(text):
    """提取输入里「不像合法角色ID」的片段，用于输入校验提示（返回去重后的片段列表）"""
    bad, seen = [], set()
    for raw in re.split(r"[\s,，;；、/|]+", (text or "").strip()):
        if raw and not raw.isdigit():
            if raw not in seen:
                seen.add(raw)
                bad.append(raw)
    return bad


# ---------------------------------------------------------------- 批量编号导入
def _looks_like_id(text):
    """粗判一段文本是否像编号：角色ID 为 2~12 位纯数字，无端ID 为 14 位纯数字

    用于从文件中挑出编号，同时过滤时间戳（13 位）、日期（8 位）等杂项数字。
    """
    if not text or not text.isdigit():
        return False
    return ID_MIN_DIGITS <= len(text) <= ID_MAX_DIGITS or len(text) == WDID_DIGITS


def _norm_id(text):
    """编号归一化：去掉首部 0（与界面输入 parse_role_ids 的处理保持一致）"""
    return text.lstrip("0") or "0"


def _extract_ids_in_line(line):
    """从一行文本里提取编号（供文件导入使用）

    优先按分隔符（逗号 / 分号 / 制表符 / 空格）切分后取「像编号」的字段，
    适用于「编号,角色名,备注」这类表格行；切不出时再取行内首个像编号的数字片段，
    适用于「角色ID：2648868」这类带文字的说明行。
    """
    text = (line or "").strip()
    if not text:
        return []
    if _looks_like_id(text):
        return [_norm_id(text)]
    fields = [f.strip().strip('"').strip("'").strip()
              for f in re.split(r"[,;\t|\s]+", text) if f.strip()]
    hits = [_norm_id(f) for f in fields if _looks_like_id(f)]
    if hits:
        return hits
    for m in re.findall(r"\d+", text):
        if _looks_like_id(m):
            return [_norm_id(m)]
    return []


def parse_ids_from_file(path):
    """从 txt / csv 文件读取编号列表（去重并保持出现顺序）

    支持：每行一个编号；一行多个编号（逗号 / 空格 / 分号分隔）；
    带表头的表格（如 Excel 另存的 CSV，取其中像编号的列）。
    编码依次尝试 UTF-8(BOM) / GBK / UTF-8。
    """
    text, last_err = None, None
    for enc in ("utf-8-sig", "gbk", "utf-8"):
        try:
            with open(path, "r", encoding=enc) as f:
                text = f.read()
            break
        except UnicodeDecodeError as e:
            last_err = e
        except OSError as e:
            raise RuntimeError(f"读取文件失败：{e}")
    if text is None or not text.strip():
        # 非常规编码（如 UTF-16 残片）：忽略无法解码的字符再试一次
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read()
        except OSError as e:
            raise RuntimeError(f"读取文件失败：{e}")
        if not text.strip():
            raise RuntimeError(f"文件内容或编码无法识别，请另存为 UTF-8 / GBK 后重试（{last_err}）")

    ids, seen = [], set()
    for line in text.splitlines():
        for rid in _extract_ids_in_line(line):
            if rid not in seen:
                seen.add(rid)
                ids.append(rid)
    return ids


def open_folder(path):
    """在资源管理器中打开目录（Windows 下用 explorer，避免被同名 exe 抢走）"""
    import subprocess

    path = os.path.normpath(path)
    os.makedirs(path, exist_ok=True)
    try:
        if hasattr(os, "startfile"):
            try:
                subprocess.Popen(["explorer", path])
            except OSError:
                os.startfile(path + os.sep)
        else:
            subprocess.Popen(["xdg-open", path])
        log(f"[提示] 已打开目录: {path}")
    except Exception as e:
        log(f"[警告] 打开目录失败: {e}")
    return path


# ---------------------------------------------------------------- 登录 Token
JWT_RE = re.compile(rb"eyJ[A-Za-z0-9_\-+/=]+\.eyJ[A-Za-z0-9_\-+/=]+\.[0-9a-fA-F]{64}")
BROWSER_ROOTS = [
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "User Data"),
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Edge", "User Data"),
]


def parse_token_info(token):
    """解析 JWT 中间段，取 account / expire 等字段"""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
    except Exception:
        return {}


def _fmt_expire(expire):
    """把 expire 时间戳格式化为可读文本；无效时返回「未知」"""
    try:
        expire = int(expire or 0)
    except (TypeError, ValueError):
        return "未知"
    if not expire:
        return "未知"
    try:
        return datetime.fromtimestamp(expire).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "未知"


def scan_leveldb_dir(leveldb_dir, tmp_root):
    """复制 LevelDB 文件后扫描，返回 [(expire, token, info), ...]"""
    found = []
    tmp_dir = os.path.join(tmp_root, "ls_" + str(abs(hash(leveldb_dir)) % 10 ** 8))
    os.makedirs(tmp_dir, exist_ok=True)
    for name in os.listdir(leveldb_dir):
        src = os.path.join(leveldb_dir, name)
        if not os.path.isfile(src) or name == "LOCK":
            continue
        dst = os.path.join(tmp_dir, name)
        try:
            shutil.copy2(src, dst)
            blob = open(dst, "rb").read()
        except Exception:
            continue
        for m in JWT_RE.finditer(blob):
            ctx = blob[max(0, m.start() - 600): m.start()]
            if b"amis.ssjj.cn" not in ctx and b"AMIS-USER-TOKEN" not in ctx:
                continue
            token = m.group(0).decode("ascii")
            info = parse_token_info(token)
            if info.get("account"):
                found.append((int(info.get("expire") or 0), token, info))
    return found


# 可选的「原项目目录」：以前是同目录下的另一份副本，靠它复用已有登录态。
# 公开发布后这个回退没有意义，默认留空；需要时可用环境变量 AMIS_PROJECT_DIR 指定。
ORIGINAL_PROJECT_DIR = (os.environ.get("AMIS_PROJECT_DIR") or "").strip()


def token_cache_path():
    """本地 Token 缓存文件路径（.amis_token.json）。

    查找顺序：自身所在目录 → 原项目目录；两处都不存在时返回自身目录
    （后续登录写入落在自身目录，不改动原项目文件）。
    """
    local = os.path.join(script_dir(), TOKEN_CACHE_NAME)
    if os.path.isfile(local):
        return local
    if ORIGINAL_PROJECT_DIR:
        fallback = os.path.join(ORIGINAL_PROJECT_DIR, TOKEN_CACHE_NAME)
        if os.path.isfile(fallback):
            return fallback
    return local


def cache_is_fresh(info):
    """缓存 Token 是否仍在有效期内（expire 缺失时视为可用，真失效会被服务端判定）"""
    try:
        expire = int((info or {}).get("expire") or 0)
    except (TypeError, ValueError):
        return False
    if not expire:
        return True
    return expire > datetime.now().timestamp()


def load_token_cache():
    """读取本地 Token 缓存；返回 (token, info)；无缓存返回 None，损坏则删除后按无缓存处理"""
    path = token_cache_path()
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("缓存内容不是 JSON 对象")
        token = str(data.get("token") or "").strip()
        if not token:
            raise ValueError("缓存中缺少 token 字段")
        info = parse_token_info(token)
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
    except Exception as e:
        log(f"[警告] 本地 Token 缓存损坏或无法解析，已按无缓存处理并删除: {e}")
        remove_token_cache("缓存损坏")
        return None


def save_token_cache(token, info):
    """写入本地 Token 缓存（仅 token / account / expire / saved_at，不含密码）"""
    if not token:
        return False
    info = info or {}
    try:
        expire = int(info.get("expire") or 0)
    except (TypeError, ValueError):
        expire = 0
    payload = {
        "token": token,
        "account": str(info.get("account") or ""),
        "expire": expire,
        "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    path = token_cache_path()
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        log(f"[缓存] 已写入本地 Token 缓存（账号 {payload['account'] or '未知'}，"
            f"有效期至 {_fmt_expire(expire)}）: {path}")
        return True
    except Exception as e:
        log(f"[警告] 本地 Token 缓存写入失败（不影响本次查询）: {e}")
        return False


def remove_token_cache(reason=""):
    """删除本地 Token 缓存文件（Token 失效 / 缓存损坏时调用）"""
    path = token_cache_path()
    if not os.path.isfile(path):
        return False
    try:
        os.remove(path)
        note = f"（{reason}）" if reason else ""
        log(f"[缓存] 已删除本地 Token 缓存{note}: {path}")
        return True
    except Exception as e:
        log(f"[警告] 本地 Token 缓存删除失败: {e}")
        return False


class TokenInvalidError(RuntimeError):
    """服务端判定登录态失效（HTTP 401/403 或返回鉴权 / 登录失效类错误码、消息）"""


class AuthAbortError(RuntimeError):
    """登录态换新失败（用户取消登录 / 连续登录失败 / Token 连续失效）

    属于整批查询共用的错误，批量查询时直接中止整批，而不是只让单个账号失败。
    """


_AUTH_FAIL_PHRASES = (
    "未登录", "请先登录", "请重新登录", "重新登录", "登录已失效", "登录失效",
    "登录过期", "登录状态失效", "鉴权失败", "鉴权错误", "认证失败", "未授权",
    "无效的token", "token无效", "token 无效", "token已过期", "token 已过期",
    "令牌无效", "令牌已失效", "token过期",
)


def looks_like_auth_failure(code=None, msg=""):
    """判断接口返回是否属于「Token / 登录态失效」类错误"""
    try:
        code = int(code)
    except (TypeError, ValueError):
        code = None
    if code in (401, 403):
        return True
    text = str(msg or "").strip()
    if not text:
        return False
    low = text.lower().replace(" ", "")
    if any(p.replace(" ", "") in low for p in _AUTH_FAIL_PHRASES):
        return True
    has_token_word = ("token" in low) or ("令牌" in text)
    has_auth_word = any(k in text for k in ("登录", "登陆", "失效", "过期", "鉴权", "认证", "授权"))
    return has_token_word and has_auth_word


def find_amis_token():
    """按优先级获取 amis.ssjj.cn 登录 Token

    顺序：环境变量 AMIS_TOKEN > 本地缓存 .amis_token.json（未过期）>
    浏览器 Chrome / Edge Local Storage（取有效期最长者）。
    返回 (token, info, source)；均不可用时返回 (None, {}, "")，由上层弹登录框。
    """
    env_token = os.environ.get("AMIS_TOKEN", "").strip()
    if env_token:
        info = parse_token_info(env_token)
        return env_token, info, "环境变量 AMIS_TOKEN"

    cached = load_token_cache()
    if cached:
        token, info = cached
        if cache_is_fresh(info):
            return token, info, "本地缓存"
        remove_token_cache("本地缓存已过期")

    tmp_root = tempfile.mkdtemp(prefix="banquery_token_")
    candidates = []
    try:
        for root in BROWSER_ROOTS:
            if not os.path.isdir(root):
                continue
            for profile in os.listdir(root):
                leveldb = os.path.join(root, profile, "Local Storage", "leveldb")
                if os.path.isdir(leveldb):
                    candidates.extend(scan_leveldb_dir(leveldb, tmp_root))
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    if not candidates:
        return None, {}, ""
    candidates.sort(key=lambda x: x[0], reverse=True)
    _, token, info = candidates[0]
    return token, info, "浏览器 Local Storage"


# ---------------------------------------------------------------- 登录兜底
# 登录框 UI 挂载点：GUI 启动时注册「主线程弹框」回调；未注册时回退原生 tkinter 对话框
_LOGIN_UI = None


def login_by_password(account, password, ldap=True):
    """调用后台登录接口换取新 Token（与界面完全解耦）

    成功返回 (token, info)；失败抛出 RuntimeError，消息为服务端返回的 msg。
    账号密码仅在本函数调用期间存在于内存中，不做任何持久化。
    """
    body = json.dumps({
        "account": account,
        "password": password,
        "ldap": bool(ldap),
    }).encode("utf-8")
    req = urllib.request.Request(
        LOGIN_API,
        data=body,
        headers={
            "Content-Type": "application/json",
            # 注意: /auth/signIn 的 X-PROJECT-ID 必须固定为 "1"（用 "28" 或不带该头返回 HTTP 400）
            "X-PROJECT-ID": LOGIN_PROJECT_ID,
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
            "Referer": LOGIN_PAGE,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"登录接口返回 HTTP {e.code}（{e.reason}）")
    except Exception as e:
        raise RuntimeError(f"登录请求发送失败：{e}")

    if payload.get("code") != 0:
        raise RuntimeError(payload.get("msg") or f"登录失败（code={payload.get('code')}）")

    token = str(((payload.get("data") or {}).get("token")) or "").strip()
    if not token:
        raise RuntimeError("登录成功但响应中未包含 Token（data.token 为空）")

    info = parse_token_info(token)
    if not info.get("account"):
        info["account"] = account
    return token, info


def ask_credentials(tip="", error="", default_account="", reason=""):
    """弹出登录框收集凭据；返回 (account, password, ldap)，用户取消返回 None

    reason 为「需要重新登录」的原因（LOGIN_REASON_FIRST / LOGIN_REASON_EXPIRED），
    仅用于登录窗口标题 / 副标题与状态提示，不参与鉴权。

    LOGIN_DIALOG_LOCK：联合查询时 amis 与 ACE 两个平台并发跑，可能同时判定登录态失效，
    加锁保证同一时刻只弹出一个登录框（先到的先弹，另一个排队等待）。
    """
    with LOGIN_DIALOG_LOCK:
        ui = _LOGIN_UI
        if ui is not None:
            return ui(tip, error, default_account, reason)
        return _ask_credentials_tk(tip, error, default_account, reason)


def _ask_credentials_tk(tip="", error="", default_account="", reason=""):
    """无 GUI 时的原生 tkinter 登录对话框"""
    import tkinter as tk

    title, subtitle = login_reason_texts(reason, tip)
    result = {"value": None}
    root = tk.Tk()
    root.title(f"{title} · amis.ssjj.cn")
    root.resizable(False, False)
    root.attributes("-topmost", True)

    tk.Label(root, text=title,
             font=(FONT_UI, 13, "bold")).grid(
        row=0, column=0, columnspan=2, sticky="w", padx=14, pady=(14, 2))
    tk.Label(root, text=subtitle,
             fg="#444746", justify="left", wraplength=360).grid(
        row=1, column=0, columnspan=2, sticky="w", padx=14, pady=(0, 8))

    tk.Label(root, text="登录网址").grid(row=2, column=0, sticky="e", padx=14, pady=6)
    url_row = tk.Frame(root)
    url_row.grid(row=2, column=1, sticky="w", padx=14, pady=6)
    tk.Label(url_row, text=LOGIN_PAGE, fg="#2F6BFF").pack(side="left")
    tk.Button(url_row, text="打开",
              command=lambda: webbrowser.open(LOGIN_PAGE)).pack(side="left", padx=(8, 0))

    tk.Label(root, text="账号").grid(row=3, column=0, sticky="e", padx=14, pady=6)
    var_account = tk.StringVar(value=default_account)
    ent_account = tk.Entry(root, textvariable=var_account, width=30)
    ent_account.grid(row=3, column=1, sticky="w", padx=14, pady=6)

    tk.Label(root, text="密码").grid(row=4, column=0, sticky="e", padx=14, pady=6)
    var_password = tk.StringVar()
    ent_password = tk.Entry(root, textvariable=var_password, width=30, show="●")
    ent_password.grid(row=4, column=1, sticky="w", padx=14, pady=6)

    var_ldap = tk.BooleanVar(value=False)
    tk.Checkbutton(root, text="LDAP 登录（本后台使用 LDAP 账号时请勾选）", variable=var_ldap).grid(
        row=5, column=0, columnspan=2, sticky="w", padx=14, pady=(2, 2))

    lbl_msg = tk.Label(root, text=error, fg="#EF4444", justify="left", wraplength=360)
    lbl_msg.grid(row=6, column=0, columnspan=2, sticky="w", padx=14, pady=(2, 4))

    def _submit():
        account = var_account.get().strip()
        password = var_password.get()
        if not account or not password:
            lbl_msg.configure(text="请输入账号和密码。")
            return
        result["value"] = (account, password, bool(var_ldap.get()))
        root.destroy()

    def _cancel():
        result["value"] = None
        root.destroy()

    btns = tk.Frame(root)
    btns.grid(row=7, column=0, columnspan=2, sticky="e", padx=14, pady=(6, 14))
    tk.Button(btns, text="登录", width=12, command=_submit).pack(side="left", padx=(0, 8))
    tk.Button(btns, text="取消", width=8, command=_cancel).pack(side="left")

    root.protocol("WM_DELETE_WINDOW", _cancel)
    root.bind("<Return>", lambda _e: _submit())
    root.bind("<Escape>", lambda _e: _cancel())
    ent_account.focus_set()
    root.update_idletasks()
    x = (root.winfo_screenwidth() - root.winfo_width()) // 2
    y = (root.winfo_screenheight() - root.winfo_height()) // 3
    root.geometry(f"+{max(0, x)}+{max(0, y)}")
    root.mainloop()
    return result["value"]


def obtain_token_by_login(reason="", reason_kind=""):
    """自动获取 Token 失败或已失效时，弹框手动登录换取 Token（最多重试 3 次）

    reason_kind 说明「需要重新登录」的原因，用于登录窗口标题 / 副标题：
    LOGIN_REASON_FIRST（首次使用，本机无可用 Token）/ LOGIN_REASON_EXPIRED（登录状态已失效）。
    """
    kind = reason_kind or LOGIN_REASON_FIRST
    default_account = ""
    error = ""
    for attempt in range(1, LOGIN_MAX_RETRY + 1):
        tip = reason if attempt == 1 else ""
        creds = ask_credentials(tip=tip, error=error, default_account=default_account,
                                reason=kind)
        if creds is None:
            raise RuntimeError(
                "已取消登录：未取得 amis.ssjj.cn 的有效 Token，本次查询中止。\n"
                "可先在 Chrome / Edge 登录 amis.ssjj.cn，或设置环境变量 AMIS_TOKEN 后再试。"
            )
        account, password, ldap = creds
        default_account = account
        try:
            token, info = login_by_password(account, password, ldap)
        except Exception as e:
            error = str(e)
            log(f"[登录失败] 第 {attempt}/{LOGIN_MAX_RETRY} 次：{error}")
            if attempt < LOGIN_MAX_RETRY:
                continue
            raise RuntimeError(
                f"连续 {LOGIN_MAX_RETRY} 次登录失败，本次查询中止。\n"
                f"最后一次错误：{error}\n"
                "请确认账号 / 密码 / LDAP 选项是否正确，或优先在浏览器登录 amis.ssjj.cn。"
            )
        log(f"[登录成功] 已获取登录 Token（第 {attempt} 次尝试，LDAP={'是' if ldap else '否'}）。")
        save_token_cache(token, info)
        return token, info


class TokenBox:
    """批量查询共享的登录态容器

    多个账号并发查询时共用一份 Token：任一账号遇到「登录态失效」只弹一次登录框，
    其余账号自动复用换到的新 Token，避免每个账号各弹一次、各删一次缓存。
    """

    def __init__(self, token="", account=""):
        self._lock = threading.Lock()
        self._login_lock = threading.Lock()
        self._token = token or ""
        self._account = account or ""
        self._gen = 0            # 换代计数：数值变化表示其他线程已换过新 Token
        self._aborted = ""       # 登录失败 / 用户取消后的中止原因

    def snapshot(self):
        """取当前 (token, account, 代号)；代号用于判断 Token 是否已被其他线程换新"""
        with self._lock:
            return self._token, self._account, self._gen

    def renew(self, seen_gen, note):
        """Token 失效后换取新 Token

        seen_gen 为调用方查询时用到的代号：若其他线程已换过新 Token（代号已变）则直接复用，
        避免并发场景下重复弹登录框。
        """
        with self._login_lock:
            with self._lock:
                if self._aborted:
                    raise AuthAbortError(self._aborted)
                if self._gen != seen_gen:
                    return self._token, self._account, self._gen
            remove_token_cache("服务端判定登录态失效")
            note(login_reason_status(LOGIN_REASON_EXPIRED))
            try:
                token, info = obtain_token_by_login(
                    "服务端判定当前登录 Token 已失效或已过期，请重新登录以换取新 Token。",
                    reason_kind=LOGIN_REASON_EXPIRED)
            except Exception as e:
                with self._lock:
                    self._aborted = str(e)
                raise AuthAbortError(str(e))
            with self._lock:
                self._token = token
                self._account = info.get("account") or self._account
                self._gen += 1
                return self._token, self._account, self._gen


def run_parallel(func, args_list, workers):
    """按指定并发度执行 func(arg)，并按入参顺序返回结果列表

    workers<=1 或只有一个入参时退化为串行执行；任一任务抛出异常时中止并向上抛出。
    """
    if workers <= 1 or len(args_list) <= 1:
        return [func(arg) for arg in args_list]
    results = [None] * len(args_list)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(func, arg): i for i, arg in enumerate(args_list)}
        try:
            for fu in concurrent.futures.as_completed(futures):
                results[futures[fu]] = fu.result()
        except Exception:
            for fu in futures:
                fu.cancel()
            raise
    return results


# ---------------------------------------------------------------- 登录封禁记录查询
def _request_forbid_page(role_id, page, token, account, key_type=KEY_TYPE_ROLE):
    """请求一页登录封禁记录；Token 失效抛 TokenInvalidError，其他失败抛 RuntimeError"""
    qs = urllib.parse.urlencode({
        "page": page,
        "perPage": PER_PAGE,
        "key": key_type,
        "keyword": str(role_id),
        "forbid_type": FORBID_TYPE,
    })
    req = urllib.request.Request(
        f"{HOST}{QUERY_API}?{qs}",
        headers={
            "X-AMIS-TOKEN": token,
            "X-AMIS-ACCOUNT": account,
            "X-PROJECT-ID": PROJECT_ID,
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
            "Referer": HOST + "/",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise TokenInvalidError(f"接口返回 HTTP {e.code}（{e.reason}），登录态已失效")
        if e.code == 400:
            raise RuntimeError(
                f"接口返回 HTTP 400：{role_id} 未被接口接受，请确认查询条件类型与输入内容匹配"
                "（角色ID / 无端ID 均为纯数字）")
        raise RuntimeError(f"接口请求失败：HTTP {e.code}（{e.reason}）")
    except urllib.error.URLError as e:
        raise RuntimeError(f"接口请求失败：{e.reason}")

    if payload.get("code") != 0:
        msg = payload.get("msg")
        if "未找到角色信息" in str(msg or ""):
            # 角色ID 不存在：服务端返回 code=10001，此处按「无记录」处理，不影响其他角色继续查询
            return [], 0
        if looks_like_auth_failure(payload.get("code"), msg):
            raise TokenInvalidError(f"接口返回鉴权失败（code={payload.get('code')}，msg={msg}）")
        raise RuntimeError(f"接口返回异常：{msg or payload}")

    data = payload.get("data") or {}
    return data.get("items") or [], int(data.get("total") or 0)


def fetch_forbid_records(role_id, token, account, key_type=KEY_TYPE_ROLE):
    """按指定查询条件抓取全部登录封禁记录（自动翻页），返回 (items, total)"""
    items, page, total = [], 1, 0
    while True:
        batch, total = _request_forbid_page(role_id, page, token, account, key_type)
        items.extend(batch)
        if not batch or len(items) >= total or len(batch) < PER_PAGE:
            break
        page += 1
    return items, total


def _with_auth_retry(role_id, box, note, action):
    """带登录态自愈的请求包装：Token 被服务端判失效时换新 Token 后重试同一请求

    box 为 TokenBox（批量查询时多个账号共用）；连续失效或换新失败时抛 AuthAbortError，
    由上层中止整批查询，而不是只让这一个账号失败。
    """
    for attempt in range(LOGIN_MAX_RETRY + 1):
        token, account, gen = box.snapshot()
        try:
            return action(token, account)
        except TokenInvalidError as e:
            note(f"[登录态失效] {role_id}：{e}")
            if attempt >= LOGIN_MAX_RETRY:
                raise AuthAbortError(
                    f"登录 Token 连续被服务端判定失效，本次查询中止。\n最后一次错误：{e}")
            box.renew(gen, note)
    raise AuthAbortError("登录 Token 连续被服务端判定失效，本次查询中止。")


def fetch_records_with_auth_retry(role_id, box, key_type, note):
    """单个账号查询：登录态失效时通过共享登录态换新 Token 后重试

    连续失效或换新失败时抛 AuthAbortError，由上层中止整批查询，而不是只让这一个账号失败。
    """
    return _with_auth_retry(
        role_id, box, note,
        lambda token, account: fetch_forbid_records(role_id, token, account, key_type))


# ---------------------------------------------------------------- 角色信息（角色名补齐）
def _request_user_info(role_id, token, account, key_type=KEY_TYPE_ROLE, timeout=None):
    """按角色ID / 无端ID 取角色信息（角色名 / 已登记无端ID / 角色ID）

    返回 {"role_name": 展示用角色名, "wd_id": 无端ID, "role_id": 角色ID,
          "state": "ok"/"not_found"}；
    接口明确返回「未找到角色信息」时 state=not_found 且角色名为占位文案（不抛异常）；
    Token 失效抛 TokenInvalidError，其他失败抛 RuntimeError。

    注：role_id（接口字段 roleId）是「无端ID → 角色ID」换算的来源，ACE 侧只认角色ID。
    """
    qs = urllib.parse.urlencode({
        "key": key_type,
        "keyword": str(role_id),
        "needDetail": "true",
    })
    req = urllib.request.Request(
        f"{HOST}{USER_INFO_API}?{qs}",
        headers={
            "X-AMIS-TOKEN": token,
            "X-AMIS-ACCOUNT": account,
            "X-PROJECT-ID": PROJECT_ID,
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
            "Referer": HOST + "/",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout or HTTP_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise TokenInvalidError(f"接口返回 HTTP {e.code}（{e.reason}），登录态已失效")
        if e.code == 400:
            raise RuntimeError(
                f"接口返回 HTTP 400：{role_id} 未被接口接受，请确认查询条件类型与输入内容匹配"
                "（角色ID / 无端ID 均为纯数字）")
        raise RuntimeError(f"接口请求失败：HTTP {e.code}（{e.reason}）")
    except urllib.error.URLError as e:
        raise RuntimeError(f"接口请求失败：{e.reason}")

    if payload.get("code") != 0:
        msg = payload.get("msg")
        if "未找到角色信息" in str(msg or "") or payload.get("code") == INFO_NOT_FOUND_CODE:
            return {"role_name": STATUS_TEXT_NO_ROLE, "wd_id": "", "role_id": "",
                    "state": "not_found"}
        if looks_like_auth_failure(payload.get("code"), msg):
            raise TokenInvalidError(f"接口返回鉴权失败（code={payload.get('code')}，msg={msg}）")
        raise RuntimeError(f"接口返回异常：{msg or payload}")

    data = payload.get("data") or {}
    return {
        "role_name": str(data.get("roleName") or ""),
        "wd_id": str(data.get("wdId") or ""),
        "role_id": str(data.get("roleId") or ""),
        "state": "ok",
    }


def fetch_user_info_with_auth_retry(role_id, box, key_type, note):
    """取角色信息（带登录态自愈）：接口异常时返回占位角色名「角色名获取失败」而不是留空

    登录态失效仍按整批中止处理（抛 AuthAbortError）；接口本身不可用只影响当前账号的角色名，
    不阻断该账号的封禁记录查询。
    """
    def action(token, account):
        try:
            return _request_user_info(role_id, token, account, key_type)
        except TokenInvalidError:
            raise
        except Exception as e:
            note(f"[警告] {role_id} 角色名获取失败（不影响封禁状态判定）：{e}")
            return {"role_name": STATUS_TEXT_ROLE_FAILED, "wd_id": "", "role_id": "",
                    "state": "failed"}

    return _with_auth_retry(role_id, box, note, action)


# ---------------------------------------------------------------- 操作员名称（运营平台）
def _operator_value(item):
    """取记录的 operator 数值；缺失 / 非数字返回 -1"""
    try:
        return int(item.get("operator"))
    except (TypeError, ValueError):
        return -1


def _ms_to_cst_text(ms):
    """毫秒时间戳 → 'YYYY-MM-DD HH:MM:SS'（UTC+8）；无效返回空串"""
    try:
        ts = int(ms)
    except (TypeError, ValueError):
        return ""
    if ts <= 0:
        return ""
    return datetime.fromtimestamp(ts / 1000, CST_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _log_created_text(item):
    """日志记录的 createdAt（形如 2026-09-30T14:00:11+08:00）→ 'YYYY-MM-DD HH:MM:SS'"""
    raw = str(item.get("createdAt") or "").strip()
    return raw[:19].replace("T", " ") if len(raw) >= 19 else ""


def _request_log_page(role_id, start_date, end_date, page, token, account):
    """请求一页「封禁解禁日志」；Token 失效抛 TokenInvalidError，其他失败抛 RuntimeError"""
    qs = urllib.parse.urlencode({
        "page": page,
        "perPage": LOG_PER_PAGE,
        "startDate": start_date,
        "endDate": end_date,
        "forbid_type": FORBID_TYPE,
        "roleId": str(role_id),
    })
    req = urllib.request.Request(
        f"{HOST}{LOG_API}?{qs}",
        headers={
            "X-AMIS-TOKEN": token,
            "X-AMIS-ACCOUNT": account,
            "X-PROJECT-ID": PROJECT_ID,
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
            "Referer": HOST + "/",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise TokenInvalidError(f"日志接口返回 HTTP {e.code}（{e.reason}），登录态已失效")
        raise RuntimeError(f"日志接口请求失败：HTTP {e.code}（{e.reason}）")
    except urllib.error.URLError as e:
        raise RuntimeError(f"日志接口请求失败：{e.reason}")

    if payload.get("code") != 0:
        msg = payload.get("msg")
        if looks_like_auth_failure(payload.get("code"), msg):
            raise TokenInvalidError(f"日志接口返回鉴权失败（code={payload.get('code')}，msg={msg}）")
        raise RuntimeError(f"日志接口返回异常：{msg or payload}")

    data = payload.get("data") or {}
    return data.get("items") or [], int(data.get("total") or 0)


def fetch_forbid_logs(role_id, start_date, end_date, token, account):
    """按角色ID + 日期区间抓取「封禁解禁日志」全部记录（自动翻页）"""
    items, page, total = [], 1, 0
    while True:
        batch, total = _request_log_page(role_id, start_date, end_date, page, token, account)
        items.extend(batch)
        if not batch or len(items) >= total or len(batch) < LOG_PER_PAGE:
            break
        page += 1
    return items


def _fallback_admin(item, log_rows):
    """操作时间对不上时的兜底匹配：同封禁类型 + 同内部理由 + 操作时间相差 ≤120 秒，且候选唯一"""
    when = _ms_to_cst_text(item.get("report_time"))
    if not when:
        return ""
    target = datetime.strptime(when, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST_TZ)
    ftype = str(item.get("forbid_type") or "")
    reason = str(item.get("inner_reason") or "").strip()
    hits = []
    for lg, admin in log_rows:
        if str(lg.get("forbidType") or "") != ftype:
            continue
        if str(lg.get("reason") or "").strip() != reason:
            continue
        created = _log_created_text(lg)
        if not created:
            continue
        ts = datetime.strptime(created, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST_TZ)
        if abs((ts - target).total_seconds()) <= 120:
            hits.append(admin)
    return hits[0] if len(set(hits)) == 1 else ""


def resolve_platform_operator_names(items, token, account):
    """operator=运营平台 的记录 → 用「封禁解禁日志」页的操作员名称（admin）覆盖

    返回 {记录 id: 操作员名称}；未匹配到 / 拉取失败不写入，调用方保留原「运营平台」。
    """
    targets = [it for it in items if _operator_value(it) == OPERATOR_PLATFORM]
    if not targets:
        return {}

    groups = {}
    for it in targets:
        role_id = str(it.get("role_id") or "").strip()
        if role_id.isdigit():
            groups.setdefault(role_id, []).append(it)
    if not groups:
        return {}

    names = {}
    for role_id, group in groups.items():
        days = sorted(d for d in (_ms_to_cst_text(it.get("report_time"))[:10] for it in group) if d)
        if days:
            start_date = (datetime.strptime(days[0], "%Y-%m-%d")
                          - timedelta(days=1)).strftime("%Y-%m-%d")
            end_date = (datetime.strptime(days[-1], "%Y-%m-%d")
                        + timedelta(days=1)).strftime("%Y-%m-%d")
        else:
            start_date, end_date = "2000-01-01", datetime.now(CST_TZ).strftime("%Y-%m-%d")

        logs = fetch_forbid_logs(role_id, start_date, end_date, token, account)
        by_time, log_rows = {}, []
        for lg in logs:
            admin = str(lg.get("admin") or "").strip()
            if not admin:
                continue
            log_rows.append((lg, admin))
            created = _log_created_text(lg)
            if created:
                by_time.setdefault(created, admin)

        hit = 0
        for it in group:
            when = _ms_to_cst_text(it.get("report_time"))
            admin = by_time.get(when) if when else ""
            if not admin:
                admin = _fallback_admin(it, log_rows)
            rid = str(it.get("id") or "")
            if admin and rid:
                names[rid] = admin
                hit += 1
        log(f"[操作人] 角色ID {role_id}：日志页 {len(logs)} 条，覆盖 {hit}/{len(group)} 条「运营平台」记录")
    return names


# ---------------------------------------------------------------- 当前封禁状态判定
def _to_int(value, default=0):
    """宽松取整：非数字 / 空值返回 default"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _ms_now():
    """当前时间的毫秒时间戳（与接口 expire_time / report_time 同单位）"""
    return int(datetime.now().timestamp() * 1000)


def record_is_active(item, now_ms=None):
    """单条记录当前是否生效：operate_type=1 且未过期（expire_time 为空视为永久封禁）"""
    if _to_int(item.get("operate_type")) != FORBID_OPERATE_ACTIVE:
        return False
    expire = _to_int(item.get("expire_time"))
    if expire <= 0:                                   # 无到期时间 → 永久封禁
        return True
    return expire > (_ms_now() if now_ms is None else now_ms)


def record_status_key(item, now_ms=None):
    """单条记录的当前状态：active（封禁中）/ expired（已过期）/ released（已解禁）/ unknown"""
    operate = _to_int(item.get("operate_type"))
    if operate == FORBID_OPERATE_RELEASED:
        return "released"
    if operate == FORBID_OPERATE_ACTIVE:
        return "active" if record_is_active(item, now_ms) else "expired"
    return "unknown"


RECORD_STATUS_TEXT = {
    "active": "封禁中",
    "expired": "已过期（未生效）",
    "released": "已解禁",
}


def record_status_text(item, now_ms=None):
    """明细表用：单条记录的当前状态文案（operate_type 未知时保留原始值）"""
    key = record_status_key(item, now_ms)
    if key == "unknown":
        return map_text(FORBID_STATUS_TEXT, item.get("operate_type"), prefix="未知状态")
    return RECORD_STATUS_TEXT[key]


def forbid_kind_name(forbid_type):
    """forbid_type → 状态文案里的封禁种类：1=登录、3=聊天，其他类型回退为类型名"""
    if _to_int(forbid_type) == FORBID_KIND_LOGIN:
        return "登录"
    if _to_int(forbid_type) == FORBID_KIND_CHAT:
        return "聊天"
    return map_text(FORBID_TYPE_TEXT, forbid_type, prefix="未知封禁")


def active_records(items, now_ms=None):
    """当前处于封禁中的记录（operate_type=1 且未过期）"""
    now_ms = _ms_now() if now_ms is None else now_ms
    return [it for it in items if record_is_active(it, now_ms)]


def active_kind_names(items, now_ms=None):
    """生效中的封禁种类名（按 forbid_type 升序去重：登录 → 聊天 → 其他类型）"""
    names = []
    for kind in sorted({_to_int(it.get("forbid_type")) for it in active_records(items, now_ms)}):
        name = forbid_kind_name(kind)
        if name not in names:
            names.append(name)
    return names


def ban_status_text(items, now_ms=None):
    """账号当前状态：正常 / 封禁中 / 封禁中（聊天）

    本工具查的就是登录封禁（FORBID_TYPE=1），所以「登录」这种封禁种类不再重复标注，
    处于登录封禁时直接显示「封禁中」；只有混入其他种类（如聊天封禁）时才在括号里注明。
    """
    names = active_kind_names(items, now_ms)
    if not names:
        return STATUS_TEXT_NORMAL
    others = [n for n in names if n != forbid_kind_name(FORBID_KIND_LOGIN)]
    if not others:
        return STATUS_TEXT_BANNED
    return f"{STATUS_TEXT_BANNED}（{'+'.join(others)}）"


def ban_expire_text(items, now_ms=None):
    """当前封禁的到期时间：无生效封禁返回空串；含永久封禁返回「永久」，否则取最晚到期时间"""
    expires = [_to_int(it.get("expire_time")) for it in active_records(items, now_ms)]
    if not expires:
        return ""
    if any(e <= 0 for e in expires):
        return STATUS_TEXT_PERMANENT
    return fmt_ms_ts(max(expires))


def summarize_records(query_value, items, id_label="角色ID", info=None):
    """把某查询条件（角色ID / 无端ID）的记录汇总成「当前是否处于封禁状态」的判定结论

    判定依据：operate_type（1=封禁中 / 2=已解禁）+ expire_time（为空视为永久封禁）并校验
    是否过期，按 forbid_type 区分登录封禁（1）与聊天封禁（3）；记录条数仅作历史参考。

    info：角色信息接口结果（{"role_name":..., "wd_id":..., "state":...}）。封禁记录里
    没有角色名（含完全无记录）时用接口返回的角色名补齐；接口查不到/不可用时为明确占位文案。
    """
    now_ms = _ms_now()
    name = next((str(i.get("roleName") or "") for i in items if i.get("roleName")), "")
    wd_id = next((str(i.get("wdId") or "") for i in items if i.get("wdId")), "")
    state = str((info or {}).get("state") or "")
    if not name:
        name = str((info or {}).get("role_name") or "")
    if not wd_id:
        wd_id = str((info or {}).get("wd_id") or "")
    head = f"{id_label} {query_value}" + (f"（{name}）" if name else "")

    if not items:
        return {
            "query_value": query_value, "role_name": name, "wd_id": wd_id,
            "role_name_state": state,
            "banned": False, "failed": False, "status": STATUS_TEXT_NORMAL,
            "kinds": [], "expire_text": "", "active_count": 0, "ban_count": 0,
            "unban_count": 0, "expired_count": 0, "total": 0, "latest": {},
            "text": f"{head}：当前{STATUS_TEXT_NORMAL}（未查询到封禁记录）",
        }

    actives = active_records(items, now_ms)
    released = [i for i in items if record_status_key(i, now_ms) == "released"]
    expired = [i for i in items if record_status_key(i, now_ms) == "expired"]
    status = ban_status_text(items, now_ms)
    expire_text = ban_expire_text(items, now_ms)
    latest = sorted(items, key=lambda i: _to_int(i.get("report_time")), reverse=True)[0]

    hist = []
    if released:
        hist.append(f"已解禁 {len(released)} 条")
    if expired:
        hist.append(f"已过期 {len(expired)} 条")
    hist_text = ("历史记录：" + "、".join(hist)) if hist else ""

    if actives:
        extra = f"；{hist_text}" if hist_text else ""
        text = (f"{head}：当前{status}，到期时间：{expire_text}"
                f"（生效中 {len(actives)} 条）{extra}")
    elif hist_text:
        text = f"{head}：当前{STATUS_TEXT_NORMAL}，无生效封禁；{hist_text}（均已不再生效）"
    else:
        text = f"{head}：当前{STATUS_TEXT_NORMAL}（未查询到封禁记录）"

    return {
        "query_value": query_value, "role_name": name, "wd_id": wd_id,
        "role_name_state": state,
        "banned": bool(actives), "failed": False, "status": status,
        "kinds": active_kind_names(items, now_ms), "expire_text": expire_text,
        "active_count": len(actives), "ban_count": len(actives),
        "unban_count": len(released), "expired_count": len(expired),
        "total": len(items), "latest": latest, "text": text,
    }


def failed_summary(query_value, message, id_label="角色ID", info=None):
    """单个账号查询失败时的占位结论（批量查询中不影响其他账号继续查询）

    info：角色信息接口结果；即便封禁记录查询失败，角色名也照常回填，便于用户核对是哪个角色。
    """
    return {
        "query_value": query_value,
        "role_name": str((info or {}).get("role_name") or ""),
        "wd_id": str((info or {}).get("wd_id") or ""),
        "role_name_state": str((info or {}).get("state") or ""),
        "banned": False,
        "failed": True, "status": STATUS_TEXT_FAILED, "kinds": [], "expire_text": "",
        "active_count": 0, "ban_count": 0, "unban_count": 0, "expired_count": 0,
        "total": 0, "latest": {}, "error": str(message),
        "text": f"{id_label} {query_value}：查询失败 —— {message}",
    }


def record_to_row(role_id, it, operator_names=None):
    """后台记录 → 表格 / CSV 一行

    operator_names: {记录 id: 操作员名称}，用于把「运营平台」替换为日志页的操作员名称
    """
    operator = map_text(OPERATOR_TEXT, it.get("operator"))
    if operator_names:
        operator = operator_names.get(str(it.get("id") or ""), operator)
    expire = fmt_ms_ts(it.get("expire_time"))
    if not expire and record_is_active(it):          # 生效中且无到期时间 → 永久封禁
        expire = STATUS_TEXT_PERMANENT
    return [
        str(it.get("role_id", role_id)),
        str(it.get("roleName") or ""),
        str(it.get("wdId") or ""),
        record_status_text(it),
        map_text(FORBID_TYPE_TEXT, it.get("forbid_type"), prefix="未知封禁类型"),
        str(it.get("inner_reason") or ""),
        str(it.get("message") or ""),
        expire,
        fmt_ms_ts(it.get("report_time")),
        operator,
    ]


TABLE_HEADERS = ["角色ID", "角色名", "无端ID", "记录状态", "封禁类型",
                 "内部原因", "对外原因", "到期时间", "操作时间", "操作人"]

# 账号汇总表（核心列 = 当前状态 / 到期时间；界面「当前封禁状态」页与导出 CSV 共用）
SUMMARY_COLS = ("idx", "account", "roleName", "wdId", "status", "expire",
                "activeCnt", "unbanCnt", "totalCnt", "lastTime", "note")
SUMMARY_WIDTHS = (48, 150, 130, 148, 176, 156, 76, 100, 84, 156, 280)
SUMMARY_STRETCH = ("note",)


def summary_headers(id_label="角色ID"):
    """账号汇总表表头（当前状态 = 该账号此刻是否处于封禁状态）"""
    return ["序号", id_label, "角色名", "无端ID", "当前状态", "到期时间", "生效中",
            "已解禁", "记录总数", "最近封禁时间", "备注"]


def summary_tag(summary):
    """汇总行标签：ban = 当前封禁中，fail = 查询失败，normal = 正常"""
    if summary.get("failed"):
        return "fail"
    return "ban" if summary.get("banned") else "normal"


def summary_to_row(idx, summary):
    """账号汇总结论 → 汇总表一行（每行明确给出「当前状态」与「到期时间」）"""
    if summary.get("failed"):
        return [str(idx), str(summary.get("query_value", "")),
                str(summary.get("role_name") or ""), str(summary.get("wd_id") or ""),
                STATUS_TEXT_FAILED, "", "", "", "", "", str(summary.get("error") or "")]
    latest = summary.get("latest") or {}
    hist = []
    if summary.get("unban_count"):
        hist.append(f"已解禁 {summary['unban_count']} 条")
    if summary.get("expired_count"):
        hist.append(f"已过期 {summary['expired_count']} 条")
    if summary.get("banned"):
        kinds = "、".join(summary.get("kinds") or []) or "封禁"
        note = f"生效中 {summary.get('active_count', 0)} 条（{kinds}封禁）"
        if hist:
            note += "；历史：" + "、".join(hist)
    elif hist:
        note = "无生效封禁；历史：" + "、".join(hist)
    else:
        note = "无封禁记录"
        if summary.get("role_name_state") == "not_found":
            note += "；角色信息接口未查到该角色"
    return [
        str(idx),
        str(summary.get("query_value", "")),
        str(summary.get("role_name") or ""),
        str(summary.get("wd_id") or ""),
        str(summary.get("status") or STATUS_TEXT_NORMAL),
        str(summary.get("expire_text") or ""),
        str(summary.get("active_count", 0)),
        str(summary.get("unban_count", 0)),
        str(summary.get("total", 0)),
        fmt_ms_ts(latest.get("report_time")),
        note,
    ]


class QueryResult:
    """一次查询的完整结果（供界面与命令行共用）"""

    def __init__(self, summaries, rows, token_source="", id_label="角色ID",
                 stopped=False, total_ids=None, detail_rows=None):
        self.summaries = summaries      # [summarize_records(...), ...]
        self.rows = rows                # [[10 列], ...]（全部账号明细，供导出 CSV / 命令行）
        self.detail_rows = dict(detail_rows or {})   # {查询编号: [[10 列], ...]} 供明细弹窗按角色取用
        self.token_source = token_source
        self.id_label = id_label
        self.stopped = bool(stopped)    # 是否被用户手动停止（批量查询时可能有账号未查）
        self.total_ids = len(summaries) if total_ids is None else int(total_ids)
        self.query_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    @property
    def banned_count(self):
        return sum(1 for s in self.summaries if s["banned"] and not s.get("failed"))

    @property
    def banned_list(self):
        return [s for s in self.summaries if s.get("banned") and not s.get("failed")]

    @property
    def normal_count(self):
        """当前未封禁（且查询成功）的账号数"""
        return sum(1 for s in self.summaries if not s.get("failed") and not s.get("banned"))

    @property
    def failed_count(self):
        return sum(1 for s in self.summaries if s.get("failed"))

    @property
    def summary_rows(self):
        """账号汇总表行（与 summary_headers 对应）"""
        return [summary_to_row(i, s) for i, s in enumerate(self.summaries, 1)]

    def conclusion_text(self):
        """结论文本（命令行 / 复制用）：核心答案是「当前是否处于封禁状态」+ 逐账号判定"""
        head = (f"查询时间：{self.query_at}    共查询 {len(self.summaries)} 个{self.id_label}："
                f"当前封禁中 {self.banned_count} 个，正常 {self.normal_count} 个")
        if self.failed_count:
            head += f"，查询失败 {self.failed_count} 个"
        if self.stopped and self.total_ids != len(self.summaries):
            head += f"（计划 {self.total_ids} 个，已手动停止）"
        lines = [head, ""]
        for s in self.summaries:
            mark = "● " if s.get("banned") else ("! " if s.get("failed") else "○ ")
            lines.append(mark + s["text"])
        banned = self.banned_list
        if banned:
            lines.append("")
            lines.append(f"当前封禁中账号清单（{len(banned)} 个）：")
            for s in banned:
                name = f"（{s['role_name']}）" if s.get("role_name") else ""
                expire = s.get("expire_text") or ""
                tail = f"，到期时间：{expire}" if expire else ""
                lines.append(f"  {s['query_value']}{name}：{s['status']}{tail}")
        return "\n".join(lines)


def run_query(role_ids, token=None, account=None, on_progress=None,
              key_type=KEY_TYPE_ROLE, id_label="角色ID", should_stop=None,
              workers=BATCH_CONCURRENCY, on_item=None):
    """批量查询一组角色ID / 无端ID，返回 QueryResult

    - token 为空时自动获取（缓存 / 浏览器 / 弹登录框）；查询中被判定登录态失效时
      自动删除缓存并弹登录框换取新 Token 后重试（同一批账号共用一次登录）
    - workers 为并发查询的账号数（默认 BATCH_CONCURRENCY=4），设为 1 即逐个串行查询
    - should_stop 为可选回调，返回 True 时停止后续未开始的账号（已发出的请求仍会返回）
    - on_item(done, total) 为可选回调：每查完一个编号调用一次（进度条用，可能在工作线程中调用）
    - 单个账号失败不影响其他账号；登录态类错误则中止整批
    """
    def note(msg):
        log(msg)
        if on_progress:
            try:
                on_progress(msg)
            except Exception:
                pass

    source = ""
    if not token:
        token, info, source = find_amis_token()
        if not token:
            note(login_reason_status(LOGIN_REASON_FIRST))
            token, info = obtain_token_by_login(
                "未能自动获取 amis.ssjj.cn 登录 Token（环境变量 / 本地缓存 / 浏览器均无），"
                "请在登录窗口中输入账号密码换取 Token。",
                reason_kind=LOGIN_REASON_FIRST)
            source = "登录框"
        else:
            # 注意: 服务端并不严格校验 expire，本地看似过期的 Token 往往仍可用，
            # 因此这里不预先弹登录框，直接带着它查询，被服务端判失效时再走登录兜底。
            expire = int(info.get("expire") or 0)
            detail = f"，有效期至 {_fmt_expire(expire)}" if expire else ""
            extra = "（本地显示已过期，先尝试使用）" if expire and expire < datetime.now().timestamp() else ""
            note(f"[登录态] 已获取 Token（来源：{source}{detail}）{extra}。")
        account = info.get("account") or account or "未知账号"

    role_ids = [str(r).strip() for r in role_ids if str(r).strip()]
    if not role_ids:
        raise RuntimeError(f"未提供有效的{id_label}。")
    summaries, rows = [], []
    total_ids = len(role_ids)
    workers = max(1, min(int(workers or 1), total_ids))
    box = TokenBox(token, account)
    counter, counter_lock = {"done": 0}, threading.Lock()

    def step(rid=""):
        done = None
        with counter_lock:
            counter["done"] += 1
            done = counter["done"]
        if on_item:                       # 进度回调：一个编号查完（成功或失败）即计数
            try:
                on_item(done, total_ids, rid)
            except Exception:
                pass
        return done

    def query_one(rid):
        """查询单个账号：单账号失败只影响自己，登录态异常则抛 AuthAbortError 中止整批

        先取角色信息（角色名），再查封禁记录：无论有无封禁记录都带上角色名，
        角色信息接口不可用时仅角色名降级为占位文案，不影响封禁状态判定。
        """
        if should_stop and should_stop():
            return None
        try:
            info = fetch_user_info_with_auth_retry(rid, box, key_type, note)
        except AuthAbortError:
            raise
        except Exception as e:                     # 理论上不会走到（接口异常已在上面降级）
            note(f"[警告] {id_label} {rid} 角色名获取失败（不影响封禁状态判定）：{e}")
            info = {"role_name": STATUS_TEXT_ROLE_FAILED, "wd_id": "", "state": "failed"}
        try:
            items, _total = fetch_records_with_auth_retry(rid, box, key_type, note)
        except AuthAbortError:
            raise
        except Exception as e:
            note(f"[失败] ({step(rid)}/{total_ids}) {id_label} {rid}：{e}")
            return failed_summary(rid, str(e), id_label, info), []
        summary = summarize_records(rid, items, id_label, info)
        name = summary.get("role_name") or "—"
        note(f"[结果] ({step(rid)}/{total_ids}) {id_label} {rid}（{name}）：{summary['total']} 条记录"
             f"（封禁中 {summary['ban_count']} / 已解禁 {summary['unban_count']}）")
        return summary, items

    note(f"[角色名] 已接入角色信息接口（{USER_INFO_API}），将按{id_label}补齐角色名："
         f"无封禁记录也会显示，接口查不到时显示「{STATUS_TEXT_NO_ROLE}」。")

    if total_ids > 1:
        note(f"[批量] 共 {total_ids} 个{id_label}待查询，并发 {workers} 个账号"
             + ("（可点「停止」中止）" if should_stop else "") + " …")

    pairs = run_parallel(query_one, role_ids, workers)
    stopped = any(p is None for p in pairs)

    queried = []                       # [(序号, 编号, 明细)]，顺序同输入
    for idx, (rid, pair) in enumerate(zip(role_ids, pairs)):
        if pair is None:               # 被停止跳过的账号
            continue
        summary, items = pair
        summaries.append(summary)
        queried.append((idx, rid, items))

    # 操作员名称（「运营平台」覆盖）：只对存在「运营平台」记录的账号请求「封禁解禁日志」页
    targets = [(idx, rid, items) for idx, rid, items in queried
               if items and any(_operator_value(it) == OPERATOR_PLATFORM for it in items)]
    op_names = {}
    if targets:
        note(f"[操作人] {len(targets)} 个{id_label}存在「运营平台」操作记录，"
             f"正在从「封禁解禁日志」获取操作员名称 …")
        token_now, account_now, _gen = box.snapshot()

        def resolve_one(arg):
            idx, rid, items = arg
            try:
                return idx, resolve_platform_operator_names(items, token_now, account_now)
            except Exception as e:
                note(f"[警告] {id_label} {rid} 操作员名称获取失败，相关明细保留「运营平台」：{e}")
                return idx, {}

        for idx, names in run_parallel(resolve_one, targets, workers):
            op_names[idx] = names or {}

    detail_rows = {}                   # {查询编号: 该角色的明细行}，供界面双击查看
    for idx, rid, items in queried:
        names = op_names.get(idx)
        bucket = detail_rows.setdefault(str(rid), [])
        for it in items:
            row = record_to_row(rid, it, names)
            rows.append(row)
            bucket.append(row)

    if stopped:
        note(f"[停止] 已停止，本次共完成 {len(summaries)}/{total_ids} 个{id_label}的查询。")
    return QueryResult(summaries, rows, source, id_label,
                       stopped=stopped, total_ids=total_ids,
                       detail_rows=detail_rows)


def export_csv(result, out_dir=None):
    """把账号汇总与明细导出为 CSV（不含查询结论段落），返回文件路径"""
    out_dir = out_dir or os.path.join(script_dir(), OUTPUT_DIR_NAME)
    os.makedirs(out_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    tag = f"_{len(result.summaries)}个" if len(result.summaries) > 1 else ""
    path = os.path.join(out_dir, f"账号封禁查询_{stamp}{tag}.csv")
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, quoting=csv.QUOTE_ALL)
        w.writerow(["查询时间", result.query_at])
        w.writerow(["查询条件", result.id_label, "账号数", len(result.summaries),
                    "当前封禁中", result.banned_count, "正常", result.normal_count,
                    "查询失败", result.failed_count])
        w.writerow([])
        w.writerow(["账号汇总"])
        w.writerow(summary_headers(result.id_label))
        for row in result.summary_rows:
            w.writerow(row)
        w.writerow([])
        w.writerow(["封禁明细"])
        w.writerow(TABLE_HEADERS)
        w.writerows(result.rows)
    log(f"[导出] 已导出 CSV: {path}")
    return path


# ==================================================================
#                    图形界面（Material Design 3 · tkinter）
# ==================================================================
# 全部控件基于标准库 tkinter / ttk 手工绘制，不引入任何第三方依赖。
# 滚动条为自绘圆角滑块（无箭头按钮、悬停与拖拽时加宽加深），覆盖
# 编号输入框 / 账号汇总表 / 封禁明细表（含横向）全部滚动位置。
import math
import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox, ttk


# ---------------------------------------------------------------- Material 色板
# 命名沿用 Material 3 语义（Primary / Surface / Outline / Container / On-*）
CLR_PRIMARY = "#0B57D0"                    # Primary
CLR_PRIMARY_HOVER = "#0A4FBC"
CLR_ON_PRIMARY = "#FFFFFF"                 # On Primary
CLR_PRIMARY_CONTAINER = "#D3E3FD"          # Primary Container
CLR_ON_PRIMARY_CONTAINER = "#041E49"       # On Primary Container
CLR_SECONDARY_CONTAINER = "#E4E7F2"        # Surface Container（低饱和容器）
CLR_ON_SECONDARY_CONTAINER = "#1A1C1E"
CLR_SUCCESS = "#1E8E3E"                    # 已解禁 / 正常
CLR_ON_SUCCESS = "#FFFFFF"
CLR_SUCCESS_CONTAINER = "#D7F0DB"
CLR_ON_SUCCESS_CONTAINER = "#072711"
CLR_ERROR = "#B3261E"                      # 封禁中 / 错误
CLR_ON_ERROR = "#FFFFFF"
CLR_ERROR_CONTAINER = "#F9DEDC"
CLR_ON_ERROR_CONTAINER = "#410E0B"
CLR_WARNING = "#8F5000"                    # 查询失败 / 提醒
CLR_WARNING_CONTAINER = "#FFDDB8"
CLR_ON_WARNING_CONTAINER = "#2E1500"

CLR_BG = "#F8FAFD"                         # Surface（页面底色）
CLR_CARD = "#FFFFFF"                       # Surface（卡片）
CLR_CARD_LOW = "#F7F9FC"                   # Surface Container Low
CLR_HEAD = "#F0F4F9"                       # Surface Container（表头 / 输入域）
CLR_HEAD_HOVER = "#E7EDF6"                 # Surface Container High
CLR_LINE = "#E1E3E1"                       # Outline Variant（分隔线 / 描边）
CLR_OUTLINE = "#C4C7C5"                    # Outline
CLR_TEXT = "#1F1F1F"                       # On Surface
CLR_SUB = "#444746"                        # On Surface Variant
CLR_MUTED = "#8E918F"                      # 禁用 / 弱提示
CLR_ZEBRA = "#FAFBFD"                      # 表格斑马纹（偶数行）
CLR_STATEFUL = "#EBF1FB"                   # 状态层（悬停 / 选中）
CLR_SEL = CLR_PRIMARY_CONTAINER           # 表格选中行底色
CLR_SHADOW_TINT = "#0B1B33"                # 阴影染色

# 兼容旧命名（提示文字等沿用）
CLR_ACCENT = CLR_PRIMARY
CLR_ACCENT_H = CLR_PRIMARY_HOVER
CLR_ACCENT_L = CLR_PRIMARY_CONTAINER
CLR_GHOST = CLR_HEAD
CLR_GHOST_H = CLR_HEAD_HOVER
CLR_OK = CLR_SUCCESS
CLR_WARN = CLR_WARNING
CLR_ERR = CLR_ERROR
CLR_BAN = CLR_ERROR

# 明细表列定义（角色ID / 角色名 / 无端ID / 封禁状态 / 封禁类型 / 内部原因 / 对外原因 / 到期时间 / 操作时间 / 操作人）
DETAIL_COLS = ("roleId", "roleName", "wdId", "status", "forbidType",
               "reason", "outerReason", "expire", "report", "admin")
DETAIL_WIDTHS = (96, 132, 148, 124, 96, 210, 250, 156, 156, 96)
DETAIL_STRETCH = ("reason", "outerReason")


# ---------------------------------------------------------------- 基础绘制工具
def _rgb(color):
    """#RRGGBB / #RGB -> (r, g, b)"""
    color = str(color).strip().lstrip("#")
    if len(color) == 3:
        color = "".join(c * 2 for c in color)
    return tuple(int(color[i:i + 2], 16) for i in (0, 2, 4))


def _hex(rgb):
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(round(v)))) for v in rgb)


def _mix(c1, c2, t):
    """两个颜色按比例 t 插值（t 为 c2 的占比），用于状态层与阴影渐变"""
    t = max(0.0, min(1.0, float(t)))
    a, b = _rgb(c1), _rgb(c2)
    return _hex(tuple(a[i] + (b[i] - a[i]) * t for i in range(3)))


def _bold_font(font):
    """由常规字体派生粗体（保持字号与字族不变，兼容 tkfont.Font 与元组描述）"""
    try:
        if isinstance(font, tkfont.Font):
            d = font.actual()
            return (d.get("family"), d.get("size"), "bold")
    except Exception:
        pass
    if isinstance(font, str):
        return font if "bold" in font else font + " bold"
    try:
        parts = list(font)
    except Exception:
        return font
    if not any(str(p).lower() == "bold" for p in parts):
        parts.append("bold")
    return tuple(parts)


def _round_rect_points(x1, y1, x2, y2, r, seg=8):
    """圆角矩形顶点（用平滑多边形逼近，避免依赖图像库）"""
    r = max(0.0, min(float(r), (x2 - x1) / 2.0, (y2 - y1) / 2.0))
    pts = []
    for cx, cy, a0, a1 in ((x2 - r, y1 + r, -90, 0), (x2 - r, y2 - r, 0, 90),
                           (x1 + r, y2 - r, 90, 180), (x1 + r, y1 + r, 180, 270)):
        if r <= 0:
            pts.extend((cx, cy))
            continue
        for i in range(seg + 1):
            a = math.radians(a0 + (a1 - a0) * i / seg)
            pts.extend((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def draw_round_rect(cv, x1, y1, x2, y2, r, fill=None, outline=None, width=1, tags=None):
    """在 Canvas 上绘制圆角矩形"""
    kw = {"smooth": True, "splinesteps": 24}
    if tags:
        kw["tags"] = tags
    pts = _round_rect_points(x1, y1, x2, y2, r)
    if fill:
        cv.create_polygon(pts, fill=fill, outline=outline or fill,
                          width=max(1, int(width)) if outline else 1, **kw)
    elif outline:
        cv.create_polygon(pts, fill="", outline=outline, width=max(1, int(width)), **kw)


def _shadow_spread(elev):
    return 0 if elev <= 0 else (9 if elev >= 2 else 6)


def _paint_shadow(cv, x1, y1, x2, y2, r, elev, bg, tags=None):
    """用同心圆角矩形叠出柔和投影（Material Elevation）"""
    if elev <= 0:
        return
    layers = 7
    spread = _shadow_spread(elev)
    base = 0.10 + 0.07 * elev
    for i in range(layers, 0, -1):
        t = i / float(layers)
        g = spread * t
        dy = 1.2 + 1.8 * elev * t
        col = _mix(bg, CLR_SHADOW_TINT, base * (1.0 - t) ** 1.30)
        draw_round_rect(cv, x1 - g, y1 - g + dy, x2 + g, y2 + g + dy, r + g,
                        fill=col, tags=tags)


def _parent_bg(widget, default=CLR_CARD):
    for key in ("bg", "background"):
        try:
            return widget.cget(key)
        except Exception:
            continue
    return default


# ---------------------------------------------------------------- Material 容器
class RoundBox(tk.Frame):
    """Material 圆角容器：Elevation 阴影 + 圆角面 + 可选描边

    实现方式：Frame（参与正常布局，尺寸由内容/父容器决定）
              + Canvas（同一格内铺满，仅负责绘制圆角与阴影）
              + body（同一格内按内边距内缩，承载真实内容）
    这样高度完全由内容决定，不会出现“Canvas 控高 → 内容不映射”的死锁。
    """

    def __init__(self, master, radius=20, padding=16, fill=CLR_CARD, bg=None,
                 elevation=0, outline=None, outline_width=1, fit="content",
                 min_height=0, **kw):
        self._bg = bg or _parent_bg(master)
        self._fill = fill
        self._radius = radius
        if isinstance(padding, (tuple, list)):
            self._pad = (int(padding[0]), int(padding[1]))
        else:
            self._pad = (int(padding), int(padding))
        self._elev = max(0, int(elevation))
        self._outline = outline
        self._ow = outline_width
        self._fit = fit
        self._sh = _shadow_spread(self._elev)
        super().__init__(master, bg=self._bg, highlightthickness=0, bd=0, **kw)
        self.canvas = tk.Canvas(self, bg=self._bg, highlightthickness=0, bd=0,
                                width=1, height=1)
        self.body = tk.Frame(self, bg=fill)
        ix, iy = self._sh + self._pad[0], self._sh + self._pad[1]
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.body.grid(row=0, column=0, sticky="nsew", padx=ix, pady=iy)
        if min_height:
            self.configure(height=int(min_height))
            self.grid_propagate(False)
        self.canvas.bind("<Configure>", lambda _e: self._paint())

    def _paint(self):
        cv = self.canvas
        cv.delete("rs")
        w, h = cv.winfo_width(), cv.winfo_height()
        if w <= 1 or h <= 1:
            return
        sh = self._sh
        x0, y0, x1, y1 = sh, sh, w - sh, h - sh
        if x1 - x0 < 6 or y1 - y0 < 6:
            return
        if self._elev:
            _paint_shadow(cv, x0, y0, x1, y1, self._radius, self._elev, self._bg, tags="rs")
        draw_round_rect(cv, x0, y0, x1, y1, self._radius, fill=self._fill,
                        outline=self._outline, width=self._ow, tags="rs")
        cv.tag_lower("rs")

    # ---- 外部 ----
    def set_outline(self, color, width=1):
        self._outline = color
        self._ow = width
        self._paint()

    def set_fill(self, color):
        self._fill = color
        self.body.configure(bg=color)
        self._paint()


# ---------------------------------------------------------------- Material 按钮
class MButton(tk.Canvas):
    """Material 按钮：filled / tonal / outlined / text 四种类型

    含 hover 与 pressed 状态层（状态层叠加色 / 禁用态灰化），无第三方依赖。
    """

    TYPES = {
        "filled":   dict(bg=CLR_PRIMARY, fg=CLR_ON_PRIMARY, line=None),
        "tonal":    dict(bg=CLR_PRIMARY_CONTAINER, fg=CLR_ON_PRIMARY_CONTAINER, line=None),
        "outlined": dict(bg=CLR_CARD, fg=CLR_PRIMARY, line=CLR_OUTLINE),
        "text":     dict(bg=CLR_CARD, fg=CLR_PRIMARY, line=None),
    }

    def __init__(self, master, text="", command=None, kind="filled", font=None,
                 width=None, height=40, padx=22, radius=None, bg=None, tone=None):
        cfg = dict(self.TYPES.get(kind, self.TYPES["filled"]))
        if tone:
            cfg["bg"] = tone
        self._cfg = cfg
        self._ready = False
        self._bg = bg or _parent_bg(master)
        self._font = font or (FONT_UI, 10)
        self._command = command
        self._text = text
        self._state = "normal"
        self._hover = False
        self._pressed = False
        self._pad = padx
        self._radius = radius if radius is not None else height / 2.0
        self._h = height
        if width is None:
            width = self._measure(text) + padx * 2
        super().__init__(master, width=max(1, int(width)), height=max(1, int(height)),
                         bg=self._bg, highlightthickness=0, bd=0, takefocus=1,
                         cursor="hand2")
        self._ready = True
        self.bind("<Configure>", lambda _e: self._render())
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self._render()

    def _measure(self, text):
        try:
            return tkfont.Font(font=self._font).measure(text)
        except Exception:
            return len(text) * 12

    # ---- 状态 ----
    def _current_fill(self):
        base = self._cfg["bg"]
        if self._pressed:
            return _mix(base, CLR_TEXT, 0.22)
        if self._hover:
            return _mix(base, CLR_TEXT, 0.10)
        return base

    def _render(self):
        self.delete("all")
        w = max(self.winfo_width(), self.winfo_reqwidth(), 2)
        h = max(self.winfo_height(), self._h, 2)
        r = min(self._radius, h / 2.0)
        if self._state != "normal":
            fill = _mix(self._bg, CLR_MUTED, 0.16)
            fg = _mix(self._bg, CLR_MUTED, 0.85)
            border = None
        else:
            fill = self._current_fill()
            fg = self._cfg["fg"]
            border = self._cfg["line"]
        draw_round_rect(self, 1, 1, w - 1, h - 1, r, fill=fill, outline=border, width=1)
        self.create_text(w / 2.0, h / 2.0, text=self._text, fill=fg, font=self._font)

    def _on_enter(self, _e=None):
        self._hover = True
        if self._state == "normal":
            self.configure(cursor="hand2")
        self._render()

    def _on_leave(self, _e=None):
        self._hover = False
        self._pressed = False
        self._render()

    def _on_press(self, _e=None):
        if self._state != "normal":
            return
        self._pressed = True
        self._render()

    def _on_release(self, e=None):
        was = self._pressed
        self._pressed = False
        self._render()
        if was and self._state == "normal" and self._command:
            inside = True
            if e is not None:
                inside = 0 <= e.x <= self.winfo_width() and 0 <= e.y <= self.winfo_height()
            if inside:
                self._command()

    def set_text(self, text):
        self._text = text
        self.configure(width=self._measure(text) + self._pad * 2)
        self._render()

    def set_enabled(self, enabled):
        self._state = "normal" if enabled else "disabled"
        self.configure(cursor="hand2" if enabled else "arrow")
        self._render()

    # ---- 兼容 tk 接口（text / state / font 由本类接管） ----
    def configure(self, cnf=None, **kw):
        if not getattr(self, "_ready", False):
            return tk.Canvas.configure(self, cnf, **kw) if cnf else tk.Canvas.configure(self, **kw)
        rebuild = False
        if "text" in kw:
            self._text = kw.pop("text")
            kw["width"] = self._measure(self._text) + self._pad * 2
        if "state" in kw:
            self._state = kw.pop("state")
        if "font" in kw:
            self._font = kw.pop("font")
            rebuild = True
        if cnf:
            tk.Canvas.configure(self, cnf)
        if kw:
            tk.Canvas.configure(self, **kw)
        self._render()
        return None

    config = configure

    def cget(self, key):
        if key == "text":
            return self._text
        if key == "state":
            return self._state
        return tk.Canvas.cget(self, key)

    def invoke(self):
        if self._state == "normal" and self._command:
            self._command()


# ---------------------------------------------------------------- Material 滚动条
class MScrollbar(tk.Canvas):
    """Material 自绘滚动条

    - 圆角细滑块、无箭头按钮；
    - 悬停 / 拖拽时滑块加宽并加深（状态层）；
    - 内容不足一屏时自动隐藏；
    - set(first, last) 由目标控件（Text / ttk.Treeview）的 yscrollcommand / xscrollcommand 驱动，
      拖拽与点击轨道则回调目标控件的 yview_moveto / xview_scroll。
    """

    MIN_THUMB = 34

    def __init__(self, master, target=None, orient="vertical", thickness=12,
                 bg=None, command=None):
        self._bg = bg or _parent_bg(master)
        self._orient = orient
        self._th = thickness
        self._target = target
        self._cmd = command
        self._first, self._last = 0.0, 1.0
        self._hover = False
        self._active = False
        self._drag_off = None
        self._thumb = None
        self._state = None
        kw = ({"width": thickness, "height": 1} if orient == "vertical"
              else {"width": 1, "height": thickness})
        super().__init__(master, bg=self._bg, highlightthickness=0, bd=0, **kw)
        self.bind("<Configure>", lambda _e: self._render(force=True))
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)

    # ---- 与目标控件对接 ----
    def set(self, first, last):
        try:
            self._first, self._last = float(first), float(last)
        except (TypeError, ValueError):
            return
        self._render()

    def _length(self):
        return self.winfo_height() if self._orient == "vertical" else self.winfo_width()

    def _scroll_to(self, frac):
        frac = max(0.0, min(1.0, float(frac)))
        if self._cmd:
            try:
                self._cmd("moveto", frac)
                return
            except Exception:
                pass
        if not self._target:
            return
        try:
            if self._orient == "vertical":
                self._target.yview_moveto(frac)
            else:
                self._target.xview_moveto(frac)
        except Exception:
            pass

    def _scroll_pages(self, direction):
        move = (lambda: self._scroll_to(self._first + direction * max(0.05, self._last - self._first)))
        try:
            if self._cmd:
                self._cmd("scroll", direction, "pages")
                return
            if not self._target:
                return
            if self._orient == "vertical":
                self._target.yview_scroll(direction, "pages")
            else:
                self._target.xview_scroll(direction, "pages")
        except Exception:
            move()

    # ---- 绘制 ----
    def _thumb_color(self):
        if self._active:
            return _mix(self._bg, CLR_SUB, 0.72)
        if self._hover:
            return _mix(self._bg, CLR_SUB, 0.55)
        return _mix(self._bg, CLR_SUB, 0.40)

    def _render(self, force=False):
        vertical = self._orient == "vertical"
        length = self._length()
        if length <= 1:
            return
        span = self._last - self._first
        if span >= 0.999:                       # 内容不足一屏：隐藏滑块
            if force or self._state is not None:
                self.delete("all")
            self._thumb = None
            self._state = None
            return
        span = max(span, 0.02)
        thumb_len = max(self.MIN_THUMB, span * length)
        offset = max(0.0, min(self._first * length, length - thumb_len))
        state = (self._active, self._hover, self._first, self._last, length)
        if not force and state == self._state:
            return
        self._state = state
        self.delete("all")
        pad = 2 if self._active else (3 if self._hover else 4)
        half = 2
        if vertical:
            x0, x1 = pad, self._th - pad
            y0, y1 = offset + half, offset + thumb_len - half
            if y1 - y0 < 6:
                y1 = y0 + 6
            self._thumb = (x0, y0, x1, y1)
            draw_round_rect(self, x0, y0, x1, y1, (x1 - x0) / 2.0, fill=self._thumb_color())
        else:
            y0, y1 = pad, self._th - pad
            x0, x1 = offset + half, offset + thumb_len - half
            if x1 - x0 < 6:
                x1 = x0 + 6
            self._thumb = (x0, y0, x1, y1)
            draw_round_rect(self, x0, y0, x1, y1, (y1 - y0) / 2.0, fill=self._thumb_color())

    # ---- 交互 ----
    def _on_enter(self, _e=None):
        self._hover = True
        self.configure(cursor="hand2")
        self._render()

    def _on_leave(self, _e=None):
        self._hover = False
        if not self._active:
            self.configure(cursor="arrow")
        self._render()

    def _on_press(self, e=None):
        if not self._thumb:
            self._scroll_pages(1 if (e.y if self._orient == "vertical" else e.x)
                               > self._length() / 2 else -1)
            return
        self._active = True
        vertical = self._orient == "vertical"
        pos = (e.y if vertical else e.x) if e is not None else 0
        lo, hi = (self._thumb[1], self._thumb[3]) if vertical else (self._thumb[0], self._thumb[2])
        if lo <= pos <= hi:
            self._drag_off = pos - lo
        else:
            self._drag_off = None
            self._scroll_pages(1 if pos > hi else -1)
        self._render(force=True)

    def _on_drag(self, e=None):
        if self._drag_off is None or e is None:
            return
        vertical = self._orient == "vertical"
        length = max(1, self._length())
        pos = (e.y if vertical else e.x) - self._drag_off
        self._scroll_to(pos / float(length))

    def _on_release(self, _e=None):
        self._drag_off = None
        self._active = False
        self._render(force=True)


# ---------------------------------------------------------------- Material 页签容器
class MTabView(tk.Frame):
    """Material 3 页签容器（全自绘，替代 ttk.Notebook 的原生观感）

    对 ttk.Notebook 只保留项目用到的这部分 API：
        add(child, text=...)  /  tab(child, text=...)  /  select(child)  /  index(child)

    视觉（与应用其余自绘控件同一套配色）：
      · 顶部一行胶囊页签：选中 = 主色容器底 + 主色加粗文字，悬停 = 状态层，
        未选中 = 次级文字色；
      · 页签行下方一条 1px 分隔线，与内容区分开；
      · 页面区（self.pages）铺满剩余空间，底色由 page_bg 指定，保证与宿主卡片同色。
    """

    TAB_PADX = 18                             # 页签左右内边距
    PILL_DY = 5                               # 胶囊上下留白

    def __init__(self, master, bg=None, page_bg=None, font=None, height=46):
        self._bg = bg or _parent_bg(master)
        self.page_bg = page_bg or self._bg
        self._font = font or (FONT_UI, 10)
        self._bold = _bold_font(self._font)
        self._h = max(34, int(height))
        self._tabs = []                       # [{"frame":.., "text":..}]
        self._cur = -1
        self._hover = -1
        try:
            self._fm = tkfont.Font(font=self._font)
            self._fmb = tkfont.Font(font=self._bold)
        except Exception:
            self._fm = self._fmb = None
        super().__init__(master, bg=self._bg)
        self.strip = tk.Canvas(self, height=self._h, bg=self._bg, highlightthickness=0,
                               bd=0, cursor="hand2")
        self.strip.pack(fill="x")
        self.pages = tk.Frame(self, bg=self.page_bg)
        self.pages.pack(fill="both", expand=True)
        self.strip.bind("<Button-1>", self._on_click)
        self.strip.bind("<Motion>", self._on_motion)
        self.strip.bind("<Leave>", lambda _e: self._set_hover(-1))
        self.strip.bind("<Configure>", lambda _e: self.redraw())

    # ---------------- ttk.Notebook 兼容 API ----------------
    def add(self, child, text=""):
        self._tabs.append({"frame": child, "text": text})
        if self._cur < 0:
            self._cur = len(self._tabs) - 1
            try:
                child.pack(in_=self.pages, fill="both", expand=True)
            except Exception:
                pass
        else:
            try:
                child.pack_forget()
            except Exception:
                pass
        self.redraw()
        return child

    def tab(self, child, text=None, **_kw):
        i = self.index(child)
        if i < 0:
            return None
        if text is not None:
            self._tabs[i]["text"] = str(text)
            self.redraw()
        return self._tabs[i]["text"]

    def index(self, child):
        for i, t in enumerate(self._tabs):
            if t["frame"] is child:
                return i
        return -1

    def select(self, child=None):
        i = self.index(child) if child is not None else self._cur
        if i < 0 or i >= len(self._tabs):
            return None
        if i != self._cur:
            if 0 <= self._cur < len(self._tabs):
                try:
                    self._tabs[self._cur]["frame"].pack_forget()
                except Exception:
                    pass
            self._cur = i
            try:
                self._tabs[i]["frame"].pack(in_=self.pages, fill="both", expand=True)
            except Exception:
                pass
        self.redraw()
        return self._tabs[i]["frame"]

    def current(self):
        return self._tabs[self._cur]["frame"] if 0 <= self._cur < len(self._tabs) else None

    def tabs(self):
        return [t["frame"] for t in self._tabs]

    # ---------------- 绘制 ----------------
    def _measure(self, text):
        try:
            return max(self._fm.measure(str(text)), self._fmb.measure(str(text)))
        except Exception:
            return len(str(text)) * 14

    def _bounds(self):
        out, x = [], 0
        for t in self._tabs:
            w = self._measure(t["text"]) + self.TAB_PADX * 2
            out.append((x, x + w))
            x += w
        return out

    def _index_at(self, x):
        for i, (a, b) in enumerate(self._bounds()):
            if a <= x <= b:
                return i
        return -1

    def _set_hover(self, idx):
        if idx != self._hover:
            self._hover = idx
            self.redraw()

    def _on_motion(self, e):
        self._set_hover(self._index_at(e.x))

    def _on_click(self, e):
        idx = self._index_at(e.x)
        if idx >= 0:
            self.select(self._tabs[idx]["frame"])

    def redraw(self):
        cv = self.strip
        cv.delete("all")
        if not self._tabs:
            return
        h = self._h
        bounds = self._bounds()
        total = bounds[-1][1] if bounds else 0
        try:
            w = cv.winfo_width()
        except Exception:
            w = 0
        cv.create_line(0, h - 0.5, max(w, total), h - 0.5, fill=CLR_LINE)
        y0, y1 = self.PILL_DY, h - self.PILL_DY - 2
        cy = (y0 + y1) / 2.0
        r = (y1 - y0) / 2.0
        for i, (a, b) in enumerate(bounds):
            text = self._tabs[i]["text"]
            if i == self._cur:
                draw_round_rect(cv, a + 3, y0, b - 3, y1, r, fill=CLR_PRIMARY_CONTAINER)
                fill, ft = CLR_ON_PRIMARY_CONTAINER, self._bold
            elif i == self._hover:
                draw_round_rect(cv, a + 3, y0, b - 3, y1, r, fill=CLR_STATEFUL)
                fill, ft = CLR_TEXT, self._font
            else:
                fill, ft = CLR_SUB, self._font
            cv.create_text((a + b) / 2.0, cy, text=text, fill=fill, font=ft)


# ---------------------------------------------------------------- Material 分段按钮
class Segmented(tk.Canvas):
    """Material 分段按钮（单选）：主色胶囊表示选中，状态层表示悬停"""

    def __init__(self, master, options, variable, command=None, font=None, bg=None,
                 height=40):
        self._opts = list(options)          # [(显示文本, 取值), ...]
        self._var = variable
        self._command = command
        self._font = font or (FONT_UI, 9)
        self._bg = bg or _parent_bg(master)
        self._hover = -1
        self._h = height
        try:
            f = tkfont.Font(font=self._font)
            self._widths = [max(92, f.measure(str(o[0])) + 38) for o in self._opts]
        except Exception:
            self._widths = [100 for _ in self._opts]
        super().__init__(master, width=max(1, sum(self._widths)), height=max(1, int(height)),
                         bg=self._bg, highlightthickness=0, bd=0)
        self.bind("<Button-1>", self._on_click)
        self.bind("<Motion>", self._on_motion)
        self.bind("<Leave>", lambda _e: self._set_hover(-1))
        try:
            self._var.trace_add("write", lambda *a: self.refresh())
        except Exception:
            pass
        self.refresh()

    def pack(self, **kw):
        tk.Canvas.pack(self, **kw)
        return self

    def grid(self, **kw):
        tk.Canvas.grid(self, **kw)
        return self

    def _bounds(self):
        out, x = [], 0
        for w in self._widths:
            out.append((x, x + w))
            x += w
        return out

    def _index_at(self, x):
        for i, (a, b) in enumerate(self._bounds()):
            if a <= x <= b:
                return i
        return -1

    def _set_hover(self, idx):
        if idx != self._hover:
            self._hover = idx
            self.refresh()

    def _on_motion(self, e):
        self._set_hover(self._index_at(e.x))

    def _on_click(self, e):
        idx = self._index_at(e.x)
        if idx < 0:
            return
        self._var.set(self._opts[idx][1])
        self.refresh()
        if self._command:
            self._command()

    def refresh(self):
        self.delete("all")
        w, h = sum(self._widths), self._h
        draw_round_rect(self, 1, 1, w - 1, h - 1, (h - 2) / 2.0,
                        fill=CLR_HEAD, outline=CLR_LINE, width=1)
        cur = self._var.get()
        for i, (a, b) in enumerate(self._bounds()):
            text, value = self._opts[i][0], self._opts[i][1]
            if value == cur:
                draw_round_rect(self, a + 3, 4, b - 3, h - 4, (h - 8) / 2.0, fill=CLR_PRIMARY)
                fg, ft = CLR_ON_PRIMARY, _bold_font(self._font)
            elif i == self._hover:
                draw_round_rect(self, a + 3, 4, b - 3, h - 4, (h - 8) / 2.0,
                                fill=CLR_STATEFUL)
                fg, ft = CLR_TEXT, self._font
            else:
                fg, ft = CLR_SUB, self._font
            self.create_text((a + b) / 2.0, h / 2.0 + 1, text=str(text), fill=fg, font=ft)




# ---------------------------------------------------------------- Material 复选框
class MCheckbox(tk.Canvas):
    """Material 复选框：圆角方块 + 主色勾选"""

    def __init__(self, master, text="", variable=None, font=None, bg=None, command=None):
        self._var = variable or tk.BooleanVar(value=False)
        self._text = text
        self._font = font or (FONT_UI, 10)
        self._bg = bg or _parent_bg(master)
        self._command = command
        self._hover = False
        try:
            tw = tkfont.Font(font=self._font).measure(text)
        except Exception:
            tw = len(text) * 12
        super().__init__(master, width=26 + tw + 10, height=32, bg=self._bg,
                         highlightthickness=0, bd=0, cursor="hand2")
        self.bind("<Button-1>", self._toggle)
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        try:
            self._var.trace_add("write", lambda *a: self.redraw())
        except Exception:
            pass
        self.redraw()

    def _set_hover(self, val):
        self._hover = val
        self.redraw()

    def _toggle(self, _e=None):
        self._var.set(not bool(self._var.get()))
        self.redraw()
        if self._command:
            self._command()

    def redraw(self):
        self.delete("all")
        checked = bool(self._var.get())
        size = 20
        y0 = (32 - size) / 2.0
        if checked:
            draw_round_rect(self, 1, y0 + 1, 1 + size, y0 + 1 + size, 6, fill=CLR_PRIMARY)
            self.create_line(6, y0 + 11, 10, y0 + 15, 16, y0 + 6, fill=CLR_ON_PRIMARY,
                             width=2.4, capstyle="round", joinstyle="round", smooth=True)
        else:
            draw_round_rect(self, 2, y0 + 2, size, y0 + size, 6, fill=CLR_CARD,
                            outline=CLR_MUTED if self._hover else CLR_OUTLINE, width=2)
        self.create_text(size + 10, 16, anchor="w", text=self._text,
                         fill=CLR_SUB if not checked else CLR_TEXT, font=self._font)


# ---------------------------------------------------------------- 表格构件
def build_table(parent, columns, headers, widths, stretch=(), style_name="Ban.Treeview",
                font_bold=None, height=8, anchor="w"):
    """表格 + 自绘滚动条（垂直 / 水平）：主界面汇总表与封禁明细弹窗共用

    anchor：单元格内容对齐方式（"w" 左对齐 / "center" 居中），作用于所有列。
    """
    tree = ttk.Treeview(parent, columns=columns, show="headings", style=style_name,
                        height=height, selectmode="browse")
    for c, h, w in zip(columns, headers, widths):
        tree.heading(c, text=h)
        tree.column(c, width=w, anchor=anchor, stretch=(c in stretch))
    tree.tag_configure("ban", foreground=CLR_ERROR, font=font_bold or (FONT_UI, 10, "bold"))
    tree.tag_configure("unban", foreground=CLR_SUB)
    tree.tag_configure("normal", foreground=CLR_SUCCESS)
    tree.tag_configure("fail", foreground=CLR_WARNING)
    tree.tag_configure("odd", background=CLR_ZEBRA)
    tree.tag_configure("even", background=CLR_CARD)

    ysb = MScrollbar(parent, target=tree, orient="vertical", thickness=14, bg=CLR_CARD)
    xsb = MScrollbar(parent, target=tree, orient="horizontal", thickness=14, bg=CLR_CARD)
    tree.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
    tree.grid(row=0, column=0, sticky="nsew")
    ysb.grid(row=0, column=1, sticky="ns")
    xsb.grid(row=1, column=0, sticky="ew", pady=(6, 0))
    parent.grid_rowconfigure(0, weight=1)
    parent.grid_columnconfigure(0, weight=1)
    tree.scrollbars = (ysb, xsb)
    return tree


class ResultTextView(tk.Frame):
    """结果文本视图：一条记录一行，替代 Treeview——文字可直接拖动选择

    用 tk.Text 的像素制表位对齐各列（左对齐，列首固定像素位置）：
    - 鼠标拖动就是原生文字选区，想选哪段选哪段，Ctrl+C 直接复制所选
      （列间是制表符，粘进 Excel/WPS 仍可自动分列）
    - 右键菜单：复制 / 复制整行 / 全选
    - on_double(i) 回调实现「双击一行看明细」（i 为记录下标，0 起）
    - set_rows(rows, tag_fn)：tag_fn(i,row) 返回 ban / unban / normal / fail，
      整行按状态着色（与原表格同名标签、同色）；斑马底色自动交替
    """

    HEADER_LINES = 2                          # 表头行 + 分隔线

    def __init__(self, master, headers, widths, font=None, bg=CLR_CARD,
                 on_double=None):
        super().__init__(master, bg=bg)
        self.headers = [str(h) for h in headers]
        self._widths = [max(40, int(w)) for w in widths]
        self._rows = []
        self.on_double = on_double
        self._bg = bg
        self._font = font or (FONT_UI, 10)

        self.text = tk.Text(
            self, wrap="none", state="disabled", bd=0, highlightthickness=0,
            background=bg, foreground=CLR_TEXT, font=self._font,
            cursor="xterm", spacing1=2, spacing3=2, padx=10, pady=4,
            insertwidth=0,
            selectbackground=CLR_SEL, selectforeground=CLR_ON_PRIMARY_CONTAINER,
            tabs=self._tabstops(),
        )
        try:                                  # 字重加粗的表头（复制自 MTabView 的做法）
            fb = tkfont.Font(font=self._font) if not isinstance(self._font, tkfont.Font) \
                else self._font
            self._font_hdr = fb.copy() if isinstance(fb, tkfont.Font) else fb
            if isinstance(self._font_hdr, tkfont.Font):
                self._font_hdr.configure(weight="bold")
        except Exception:
            self._font_hdr = self._font
        for name, fg in (("ban", CLR_ERROR), ("unban", CLR_SUB),
                         ("normal", CLR_SUCCESS), ("fail", CLR_WARNING)):
            self.text.tag_configure(name, foreground=fg)
        self.text.tag_configure("hdr", foreground=CLR_SUB, font=self._font_hdr)
        self.text.tag_configure("sep", foreground=CLR_LINE)
        self.text.tag_configure("z0", background=bg)
        self.text.tag_configure("z1", background=CLR_ZEBRA)

        ysb = MScrollbar(self, target=self.text, orient="vertical",
                         thickness=14, bg=CLR_CARD)
        xsb = MScrollbar(self, target=self.text, orient="horizontal",
                         thickness=14, bg=CLR_CARD)
        self.text.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
        self.text.grid(row=0, column=0, sticky="nsew")
        ysb.grid(row=0, column=1, sticky="ns")
        xsb.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

        self.text.bind("<Double-Button-1>", self._on_double)
        self.text.bind("<Button-3>", self._on_menu)
        self.text.bind("<Button-2>", self._on_menu)     # macOS 习惯
        self._menu_pt = (0, 0)

    # ---- 布局 ----
    def _tabstops(self):
        """按列宽累计出像素制表位（左对齐，每列留 12px 间隙）

        注意：Tk 制表位的对齐写法是 {像素 center}；"30c" 会被当成
        **30 厘米**（c 是距离单位后缀），初版因此把列全排到了几千像素外。
        """
        stops, x = [], 0
        for w in self._widths[:-1]:
            x += w + 12
            stops.append(x)
        return tuple(stops)

    # ---- 数据 ----
    @staticmethod
    def _clean_cell(v):
        return str(v).replace("\t", "    ").replace("\r", " ").replace("\n", " ⏎ ")

    def set_rows(self, rows, tag_fn=None):
        self._rows = [tuple("" if v is None else str(v) for v in r) for r in rows]
        t = self.text
        t.configure(state="normal")
        t.delete("1.0", "end")
        t.insert("end", "\t".join(self.headers) + "\n", "hdr")
        t.insert("end", "─" * max(40, sum(self._widths) // 8) + "\n", "sep")
        for i, row in enumerate(self._rows):
            status = None
            if tag_fn:
                try:
                    status = tag_fn(i, row)
                except Exception:
                    status = None
            tags = ["z%d" % (i % 2)]
            if status:
                tags.append(str(status))
            t.insert("end", "\t".join(self._clean_cell(v) for v in row) + "\n",
                     tuple(tags))
        t.configure(state="disabled")

    def clear(self):
        self._rows = []
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")

    def row_values(self, i):
        """第 i 条记录的原始单元格值（列表），供双击明细等使用"""
        return list(self._rows[i]) if 0 <= i < len(self._rows) else []

    # ---- 交互 ----
    def _line_at(self, x, y):
        try:
            return int(str(self.text.index("@%d,%d" % (x, y))).split(".")[0])
        except Exception:
            return 0

    def _on_double(self, event):
        n = self._line_at(event.x, event.y) - self.HEADER_LINES
        if self.on_double and 1 <= n <= len(self._rows):
            self.on_double(n - 1)
        return "break"

    def _copy_to_clip(self, s):
        if not s:
            return
        self.text.clipboard_clear()
        self.text.clipboard_append(s)
        _copy_toast(self.text, "已复制")

    def _copy_sel(self):
        try:
            self._copy_to_clip(self.text.get("sel.first", "sel.last"))
        except Exception:
            pass

    def _on_menu(self, event):
        self._menu_pt = (event.x, event.y)
        menu = tk.Menu(self, tearoff=0, bg=CLR_CARD, fg=CLR_TEXT,
                       activebackground=CLR_PRIMARY, activeforeground=CLR_ON_PRIMARY,
                       bd=0, relief="flat", font=(FONT_UI, 9))
        menu.add_command(label="复制所选文字", command=self._copy_sel)
        menu.add_command(
            label="复制整行",
            command=lambda: self._copy_to_clip(
                self.text.get("%d.0" % self._line_at(*self._menu_pt),
                              "%d.end" % self._line_at(*self._menu_pt))))
        menu.add_command(label="全选",
                         command=lambda: self.text.tag_add("sel", "1.0", "end"))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            try:
                menu.grab_release()
            except Exception:
                pass
        return "break"


# ---------------------------------------------------------------- 表格复制能力
def _tree_shows_tree(tree):
    """该表格是否带 tree 列（#0）——tree 列不能放数据列，取值方式不同"""
    try:
        return "tree" in str(tree.cget("show"))
    except Exception:
        return False


def tree_row_values(tree, iid):
    """取一行的文本列表，顺序与界面列序一致（tree 列排在最前）"""
    vals = ["" if v is None else str(v) for v in tree.item(iid, "values")]
    if _tree_shows_tree(tree):
        return [str(tree.item(iid, "text") or "")] + vals
    return vals


def tree_all_iids(tree):
    """按界面顺序取全部行 id（含子行，兼容后续出现树形数据）"""
    out = []

    def walk(parent):
        for iid in tree.get_children(parent):
            out.append(iid)
            walk(iid)

    try:
        walk("")
    except Exception:
        pass
    return out


def _copy_toast(widget, text, ms=1200):
    """在鼠标附近弹一个短暂的提示（复制成功反馈）"""
    try:
        top = tk.Toplevel(widget)
        top.overrideredirect(True)
        top.configure(bg=CLR_TEXT)
        tk.Label(top, text=text, bg=CLR_TEXT, fg=CLR_CARD, font=(FONT_UI, 9),
                 padx=10, pady=5).pack()
        top.update_idletasks()
        try:
            x, y = widget.winfo_pointerx() + 14, widget.winfo_pointery() + 14
        except Exception:
            x, y = widget.winfo_rootx() + 40, widget.winfo_rooty() + 40
        top.geometry("+%d+%d" % (x, y))
        try:
            top.attributes("-topmost", True)
        except Exception:
            pass
        top.after(ms, top.destroy)
    except Exception:
        pass


def copy_rows(tree, iids, headers=None, row_provider=None, toast=True):
    """把若干行以制表符分隔写入剪贴板（可直接粘进 Excel / WPS / 表格）

    headers 非空时在最前面写入一行表头；row_provider(iid) 可自定义取值。
    """
    iids = [i for i in (iids or []) if i]
    if not iids:
        return 0
    lines = []
    if headers:
        lines.append("\t".join("" if h is None else str(h) for h in headers))
    for iid in iids:
        try:
            vals = row_provider(iid) if row_provider else tree_row_values(tree, iid)
        except Exception:
            vals = []
        lines.append("\t".join("" if v is None else str(v) for v in vals))
    try:
        tree.clipboard_clear()
        tree.clipboard_append("\n".join(lines))
    except Exception:
        return 0
    if toast:
        _copy_toast(tree, "已复制 %d 行" % len(iids))
    return len(iids)


def display_columns(tree):
    """按界面顺序返回列标识（若该表显示 tree 列，#0 排在最前）"""
    cols = ["#0"] if _tree_shows_tree(tree) else []
    try:
        cols.extend([str(c) for c in tree.cget("columns")])
    except Exception:
        pass
    return cols


def tree_headers(tree):
    """按界面顺序返回列标题（与 display_columns 一一对应）"""
    out = []
    for c in display_columns(tree):
        try:
            out.append(str(tree.heading(c, "text") or ""))
        except Exception:
            out.append("")
    return out


def column_value(tree, iid, idx, row_provider=None):
    """取某行「界面顺序第 idx 列」的文本（idx 从 0 起）"""
    try:
        vals = row_provider(iid) if row_provider else tree_row_values(tree, iid)
    except Exception:
        return ""
    if 0 <= idx < len(vals):
        return "" if vals[idx] is None else str(vals[idx])
    return ""


def range_bounds(tree):
    """当前拖拽选中的单元格区域 → {i0,i1,c0,c1,rows,cols}；无选区返回 None"""
    rng = getattr(tree, "_cell_range", None)
    if not rng:
        return None
    try:
        n_rows = len(tree.get_children())
    except Exception:
        return None
    n_cols = len(display_columns(tree))
    if not n_rows or not n_cols:
        return None
    i0, i1 = sorted((int(rng["r0"]), int(rng["r1"])))
    c0, c1 = sorted((int(rng["c0"]), int(rng["c1"])))
    return {"i0": max(0, min(i0, n_rows - 1)), "i1": max(0, min(i1, n_rows - 1)),
            "c0": max(0, min(c0, n_cols - 1)), "c1": max(0, min(c1, n_cols - 1)),
            "rows": n_rows, "cols": n_cols}


def has_range(tree):
    """当前是否有拖拽选中的单元格区域"""
    return range_bounds(tree) is not None


def copy_range(tree, headers=None, row_provider=None, toast=True, text=None):
    """复制拖拽选中的单元格区域（制表符分隔，粘进 Excel 即按行列铺开）

    headers 非空时在首行写入所选列的表头。
    """
    b = range_bounds(tree)
    if not b:
        return 0
    rows = tree.get_children()
    lines = []
    if headers:
        hs = tree_headers(tree)
        lines.append("\t".join(hs[b["c0"]:b["c1"] + 1]))
    for i in range(b["i0"], b["i1"] + 1):
        lines.append("\t".join(column_value(tree, rows[i], c, row_provider)
                               for c in range(b["c0"], b["c1"] + 1)))
    content = "\n".join(lines) if text is None else text
    try:
        tree.clipboard_clear()
        tree.clipboard_append(content)
    except Exception:
        return 0
    n = b["i1"] - b["i0"] + 1
    m = b["c1"] - b["c0"] + 1
    if toast:
        _copy_toast(tree, "已复制 %d 行 × %d 列" % (n, m))
    return n


def _put_dib_to_clipboard(dib):
    """把 DIB 数据放进 Windows 剪贴板（CF_DIB）——纯 ctypes，无第三方依赖"""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except Exception:
        return False
    CF_DIB, GMEM_MOVEABLE = 8, 0x0002
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.EmptyClipboard.argtypes = []
    user32.SetClipboardData.restype = ctypes.c_void_p
    user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
    user32.CloseClipboard.argtypes = []
    if not user32.OpenClipboard(None):
        return False
    try:
        user32.EmptyClipboard()
        h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(dib))
        if not h:
            return False
        p = kernel32.GlobalLock(h)
        if not p:
            return False
        ctypes.memmove(p, dib, len(dib))
        kernel32.GlobalUnlock(h)
        if not user32.SetClipboardData(CF_DIB, h):
            return False
        # 成功后该内存所有权归系统，不能再 GlobalFree
    finally:
        user32.CloseClipboard()
    return True


def copy_image_bytes(widget, raw, toast=True):
    """把图片字节以「位图」写进剪贴板：可直接粘进微信 / QQ / Word / Excel"""
    if not raw or sys.platform != "win32":
        return False
    try:
        import io as _io
        import time as _time
        from PIL import Image
    except Exception:
        if toast:
            _copy_toast(widget, "未安装 Pillow，无法复制图片")
        return False
    try:
        img = Image.open(_io.BytesIO(raw))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        buf = _io.BytesIO()
        img.save(buf, "BMP")
        dib = buf.getvalue()[14:]              # 去掉 BMP 文件头，得到 DIB
    except Exception:
        return False
    ok = False
    for _ in range(3):                         # 剪贴板偶发被占用，重试几次
        try:
            ok = _put_dib_to_clipboard(dib)
        except Exception:
            ok = False
        if ok:
            break
        _time.sleep(0.08)
    if toast:
        _copy_toast(widget, "已复制图片，可直接粘贴" if ok else "复制图片失败，请重试")
    return ok


def copy_cell(tree, iid, col, row_provider=None, toast=True, image_provider=None):
    """复制单个单元格内容（col 为列标识 "#0"/"#1"/…，与 identify_column 一致）

    image_provider(iid, idx, fetch)：返回图片字节时，该格按「图片」复制
    （截图格可直接粘出图片而不是路径）；fetch=False 表示只查缓存不下载。
    """
    if not iid or not col:
        return False
    try:
        n = int(str(col).lstrip("#"))
    except Exception:
        return False
    idx = n if _tree_shows_tree(tree) else n - 1
    if idx < 0:
        return False
    if image_provider:
        raw = None
        try:
            raw = image_provider(iid, idx, True)
        except Exception:
            raw = None
        if raw and copy_image_bytes(tree, raw, toast):
            return True
    val = column_value(tree, iid, idx, row_provider)
    try:
        tree.clipboard_clear()
        tree.clipboard_append(val)
    except Exception:
        return False
    if toast:
        _copy_toast(tree, "已复制单元格内容" if val else "该单元格为空")
    return True


class _CellRangeHighlight(object):
    """单元格区域高亮：4 条细边条叠在表格控件上，标示拖拽选区的行列范围。

    用 tk.Frame + place() 实现（Treeview 内部允许叠加子控件），
    只占 2px 边框、不遮挡格内点击，也不需要额外的透明窗口。
    """

    EDGE = 3

    def __init__(self, tree, on_border_click=None):
        self.tree = tree
        self.frames = [tk.Frame(tree, bg=CLR_PRIMARY, bd=0, highlightthickness=0)
                       for _ in range(4)]
        if on_border_click is not None:
            for f in self.frames:
                f.bind("<Button-1>", lambda _e: on_border_click())

    def bind_motion(self, handler):
        for f in self.frames:
            f.bind("<B1-Motion>", handler, add="+")

    def show(self, x, y, w, h):
        e = self.EDGE
        if w < 2 * e or h < 2 * e:
            self.hide()
            return
        boxes = ((x, y, w, e), (x, y + h - e, w, e),
                 (x, y, e, h), (x + w - e, y, e, h))
        for f, (px, py, pw, ph) in zip(self.frames, boxes):
            try:
                f.place(x=px, y=py, width=pw, height=ph)
                f.lift()
            except Exception:
                pass

    def hide(self):
        for f in self.frames:
            try:
                f.place_forget()
            except Exception:
                pass

    def destroy(self):
        for f in self.frames:
            try:
                f.destroy()
            except Exception:
                pass


def enable_copy(tree, headers=None, row_provider=None, image_provider=None):
    """给表格加复制能力（主界面结果表与各明细表共用）

    - 鼠标按住左键在表格上拖动：框选任意单元格区域（行、列都自由），
      松开后 Ctrl+C 或右键「复制选中区域」即可复制该块数据
    - 选中若干行后 Ctrl+C：复制选中行；既无选区也无选中行时复制全部
    - 右键菜单：复制该单元格 / 复制图片（截图格）/ 复制选中区域 /
      复制选中行（含表头）/ 复制全部
    - 双击单元格（或右键「选取该单元格文字」）：该格原地变为可选中文字
      （无边框、底色与所在行一致，看起来就是格里的文字），内容全选，
      可自由选择其中一部分再 Ctrl+C（复制后自动复原；点击别处 / Esc /
      滚动也会复原），适合「只要无端ID前几位」这类场景
    内容为制表符分隔的纯文本，粘到表格类软件可自动分列。
    row_provider(iid) 自定义一行的取值；image_provider(iid, idx, fetch)
    返回图片字节时该格按图片复制（fetch=False 只查缓存，不触发下载）。
    """
    try:
        tree.configure(selectmode="extended")   # 支持多选，便于批量复制
    except Exception:
        pass
    disp_cols = display_columns(tree)
    state = {"iid": "", "col": ""}
    drag = {"on": False, "moved": False, "r0": -1, "c0": -1, "x": 0, "y": 0}
    tree._cell_range = None
    tree._copy_drag_moved = False
    tree._range_tick = None
    hl = _CellRangeHighlight(tree)

    def all_rows():
        return tree.get_children()

    def row_at(y):
        """按 y 取行 id；拖到表格上下边界外时就近取首 / 末行"""
        iid = tree.identify_row(max(1, y))
        if iid:
            return iid
        rows = all_rows()
        if not rows:
            return ""
        return rows[-1] if y > 0 else rows[0]

    def col_id_index(col_id):
        """列标识 "#N" → 界面顺序下标（有 tree 列时 #0 即第 1 列，否则 #1 起）"""
        try:
            n = int(str(col_id).lstrip("#"))
        except Exception:
            return -1
        idx = n if _tree_shows_tree(tree) else n - 1
        return idx if 0 <= idx < len(disp_cols) else -1

    def col_idx(x):
        return col_id_index(tree.identify_column(x))

    def draw_range():
        b = range_bounds(tree)
        if not b:
            hl.hide()
            return
        rows = all_rows()
        x0 = y0 = 10 ** 9
        x1 = y1 = -1
        for i in range(b["i0"], b["i1"] + 1):
            for c in range(b["c0"], b["c1"] + 1):
                try:
                    bb = tree.bbox(rows[i], disp_cols[c])
                except Exception:
                    bb = ""
                if not bb:
                    continue
                bx, by, bw, bh = bb
                x0, y0 = min(x0, bx), min(y0, by)
                x1, y1 = max(x1, bx + bw), max(y1, by + bh)
        w, h = tree.winfo_width(), tree.winfo_height()
        if x1 <= 0 or y1 <= 0 or x0 >= w or y0 >= h:
            hl.hide()
            return
        x0, y0 = max(x0, 0), max(y0, 0)
        x1, y1 = min(x1, w), min(y1, h)
        hl.show(x0, y0, x1 - x0, y1 - y0)

    def range_tick():
        """滚动 / 缩放后保持高亮框贴合选区（每 120ms 校正一次）"""
        tree._range_tick = None
        try:
            if not tree.winfo_exists():
                return
        except Exception:
            return
        if not tree._cell_range:
            hl.hide()
            return
        draw_range()
        try:
            tree._range_tick = tree.after(120, range_tick)
        except Exception:
            tree._range_tick = None

    def clear_range():
        if getattr(tree, "_cell_range", None):
            tree._cell_range = None
            hl.hide()

    def set_range(r0, r1, c0, c1):
        tree._cell_range = {"r0": r0, "r1": r1, "c0": c0, "c1": c1}
        draw_range()
        if tree._range_tick is None:
            try:
                tree._range_tick = tree.after(120, range_tick)
            except Exception:
                tree._range_tick = None

    def update_range_to(x, y):
        iid = row_at(y)
        ci = col_idx(x)
        if not iid or ci < 0:
            return
        try:
            ri = tree.index(iid)
        except Exception:
            return
        set_range(drag["r0"], ri, drag["c0"], ci)

    def on_press(event):
        close_overlay()                     # 点击别处先收起文字浮层
        tree._copy_drag_moved = False
        if getattr(tree, "_cell_range", None):
            clear_range()
        drag["on"] = False
        try:
            if str(tree.identify_region(event.x, event.y)) not in ("cell", "tree"):
                return
        except Exception:
            return
        iid = tree.identify_row(event.y)
        ci = col_idx(event.x)
        if not iid or ci < 0:
            return
        try:
            drag.update(on=True, moved=False, x=event.x, y=event.y,
                        r0=tree.index(iid), c0=ci)
        except Exception:
            drag["on"] = False

    def on_motion(event):
        if not drag["on"]:
            return
        if not drag["moved"]:
            if abs(event.x - drag["x"]) + abs(event.y - drag["y"]) < 5:
                return                      # 微小抖动不算框选，保持普通点击
            drag["moved"] = True
            tree._copy_drag_moved = True
            try:
                tree.selection_remove(*tree.selection())
            except Exception:
                pass
        update_range_to(max(event.x, 1), max(event.y, 1))
        return "break"                      # 阻止 Tk 默认的整行扫选

    def on_release(event):
        if not drag["on"]:
            return
        drag["on"] = False
        if not drag["moved"]:
            return
        update_range_to(max(event.x, 1), max(event.y, 1))
        draw_range()

    class _Ev(object):
        __slots__ = ("x", "y")

    def on_border_motion(event):
        """指针划过高亮边框时把事件转发给框选逻辑（边框只占 2px）"""
        ev = _Ev()
        try:
            ev.x = event.x_root - tree.winfo_rootx()
            ev.y = event.y_root - tree.winfo_rooty()
        except Exception:
            return
        return on_motion(ev)

    hl = _CellRangeHighlight(tree, on_border_click=clear_range)
    hl.bind_motion(on_border_motion)
    tree._copy_highlight = hl               # 防被回收

    # ---- 单元格文字选取浮层：双击后叠一个 Entry，可自由选部分文字复制 ----
    box = {"entry": None}

    def close_overlay(_e=None):
        ent = box["entry"]
        if ent is not None:
            box["entry"] = None
            try:
                ent.destroy()
            except Exception:
                pass

    def _is_image_cell(iid, ci):
        if not image_provider or ci < 0:
            return False
        try:
            return bool(image_provider(iid, ci, False))
        except Exception:
            return False

    def open_overlay_at(iid, col):
        """在 (iid, col) 上叠文本框；成功返回 True。图片格 / 空格 / 格不可见时放弃"""
        close_overlay()
        if not iid or not col:
            return False
        ci = col_id_index(col)
        if ci < 0 or _is_image_cell(iid, ci):
            return False
        val = column_value(tree, iid, ci, row_provider)
        if not val:
            return False
        try:
            bb = tree.bbox(iid, col)
        except Exception:
            bb = ""
        if not bb:
            return False
        bx, by, bw, bh = bb
        try:    # 字体跟随表格样式（各表字号不同），取不到再用界面默认
            fnt = ttk.Style().lookup(str(tree["style"] or "Treeview"), "font") \
                  or (FONT_UI, 10)
        except Exception:
            fnt = (FONT_UI, 10)
        # 底色跟随所在行（斑马行 / 普通行），无边框 —— 视觉上就是「格子里的
        # 文字直接可选中」，而不是弹出一个输入框
        bg = CLR_CARD
        for t in (tree.item(iid, "tags") or ()):
            try:
                c = str(tree.tag_configure(str(t), "background") or "")
            except Exception:
                c = ""
            if c and c.lower() not in ("none", "systembuttonface"):
                bg = c
                break
        ent = tk.Entry(tree, justify="center",
                       font=fnt,
                       relief="flat", bd=0, highlightthickness=0,
                       background=bg, foreground=CLR_TEXT,
                       insertbackground=CLR_TEXT, disabledbackground=bg)
        ent.insert(0, val)
        ent.selection_range(0, "end")
        ent.icursor("end")
        ent.place(x=bx + 1, y=by + 1, width=max(24, bw - 2),
                  height=max(18, bh - 2))
        ent.lift()
        box["entry"] = ent
        ent.bind("<Return>", close_overlay)
        ent.bind("<Escape>", close_overlay)
        ent.bind("<FocusOut>", close_overlay)
        # Ctrl+C：让 Entry 的类绑定先完成复制，再顺手收起浮层
        ent.bind("<Control-c>", lambda _e: ent.after_idle(close_overlay))
        ent.bind("<Control-C>", lambda _e: ent.after_idle(close_overlay))
        ent.bind("<Control-Insert>", lambda _e: ent.after_idle(close_overlay))
        try:
            ent.focus_set()
        except Exception:
            pass
        return True

    def on_double(event):
        try:
            if str(tree.identify_region(event.x, event.y)) not in ("cell", "tree"):
                return
        except Exception:
            return
        iid = tree.identify_row(event.y)
        col = str(tree.identify_column(event.x) or "")
        if open_overlay_at(iid, col):
            return "break"                  # 已开浮层：别让默认双击行为捣乱

    def menu_pick_text():
        open_overlay_at(state.get("iid") or "", state.get("col") or "")

    def sel_or_all():
        sel = list(tree.selection())
        return sel or tree_all_iids(tree)

    def do_copy(iids, with_header):
        if not iids:
            return
        copy_rows(tree, iids, headers if with_header else None, row_provider)

    def do_copy_range():
        copy_range(tree, None, row_provider)

    def do_copy_cell():
        iid, col = state.get("iid"), state.get("col")
        if iid:
            copy_cell(tree, iid, col, row_provider, image_provider=image_provider)

    def do_copy_image():
        iid, col = state.get("iid"), state.get("col")
        if not iid or not col or not image_provider:
            return
        idx = col_id_index(col)
        if idx < 0:
            return
        raw = None
        try:
            raw = image_provider(iid, idx, True)
        except Exception:
            raw = None
        if raw:
            copy_image_bytes(tree, raw)
        else:
            _copy_toast(tree, "该单元格没有可复制的图片")

    def on_copy_key(_e=None):
        if getattr(tree, "_cell_range", None) and copy_range(tree, None, row_provider):
            return "break"
        do_copy(sel_or_all(), False)
        return "break"

    # 菜单项下标（entryconfigure 用）
    I_CELL, I_IMG, I_RANGE, I_SEL, I_ALLH, I_ALLD, I_TEXT = 0, 1, 3, 4, 6, 7, 8
    menu = tk.Menu(tree, tearoff=0, bg=CLR_CARD, fg=CLR_TEXT, activebackground=CLR_PRIMARY,
                   activeforeground=CLR_ON_PRIMARY, bd=0, relief="flat",
                   font=(FONT_UI, 9))
    menu.add_command(label="复制该单元格", command=do_copy_cell)
    menu.add_command(label="复制图片", command=do_copy_image)
    menu.add_separator()
    menu.add_command(label="复制选中区域", command=do_copy_range)
    menu.add_command(label="复制选中行（含表头）",
                     command=lambda: do_copy(list(tree.selection()), True))
    menu.add_separator()
    menu.add_command(label="复制全部（含表头）",
                     command=lambda: do_copy(tree_all_iids(tree), True))
    menu.add_command(label="复制全部（仅数据）",
                     command=lambda: do_copy(tree_all_iids(tree), False))
    menu.add_command(label="选取该单元格文字（可自由选择部分，双击亦可）",
                     command=menu_pick_text)

    def on_right_click(event):
        iid = tree.identify_row(event.y)
        col = str(tree.identify_column(event.x) or "")
        b = range_bounds(tree)
        in_range = False
        if b and iid:
            try:
                ri = tree.index(iid)
                in_range = b["i0"] <= ri <= b["i1"]
            except Exception:
                in_range = False
        if iid and not in_range and iid not in tree.selection():
            tree.selection_set(iid)
            tree.focus(iid)
        state["iid"] = iid
        state["col"] = col
        ci = col_id_index(col)
        has_img = False
        if iid and ci >= 0 and image_provider:
            try:
                has_img = bool(image_provider(iid, ci, False))
            except Exception:
                has_img = False
        try:
            menu.entryconfigure(I_CELL, state=("normal" if (iid and col) else "disabled"))
            menu.entryconfigure(I_TEXT, state=("normal" if (iid and col) else "disabled"))
            menu.entryconfigure(I_IMG, state=("normal" if has_img else "disabled"))
            if b:
                menu.entryconfigure(
                    I_RANGE, state="normal",
                    label="复制选中区域（%d 行 × %d 列）"
                          % (b["i1"] - b["i0"] + 1, b["c1"] - b["c0"] + 1))
            else:
                menu.entryconfigure(I_RANGE, state="disabled", label="复制选中区域")
            menu.entryconfigure(I_SEL, state=("normal" if tree.selection() else "disabled"))
        except Exception:
            pass
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            try:
                menu.grab_release()
            except Exception:
                pass
        return "break"

    tree.bind("<Control-c>", on_copy_key)
    tree.bind("<Control-C>", on_copy_key)
    tree.bind("<Control-Insert>", on_copy_key)
    tree.bind("<Double-Button-1>", on_double, add="+")
    tree.bind("<MouseWheel>", close_overlay, add="+")   # 滚动后格会移位，直接收起
    tree.bind("<Button-4>", close_overlay, add="+")     # X11 滚轮向上
    tree.bind("<Button-5>", close_overlay, add="+")     # X11 滚轮向下
    tree.bind("<Configure>", close_overlay, add="+")    # 列宽 / 窗口变化同理
    tree.bind("<ButtonPress-1>", on_press, add="+")
    tree.bind("<B1-Motion>", on_motion, add="+")
    tree.bind("<ButtonRelease-1>", on_release, add="+")
    tree.bind("<Button-3>", on_right_click)
    tree.bind("<Button-2>", on_right_click)      # macOS 习惯
    tree._copy_menu = menu                        # 防被回收
    return tree


# ---------------------------------------------------------------- 封禁明细弹窗
class DetailDialog(tk.Toplevel):
    """单角色封禁明细弹窗（Material：圆角卡片 + Elevation 阴影 + 自绘滚动条）

    - 标题体现所查角色：`封禁明细 · 角色名（角色ID 123）`
    - 只展示该角色的记录；无记录 / 查询失败时给出明确文案
    - 支持 Esc、弹窗内「关闭」按钮与标题栏关闭按钮退出
    """

    def __init__(self, master, title_text, subtitle, rows, empty_text="",
                 width=1240, height=620):
        super().__init__(master)
        self._closed = False
        self.title(title_text)
        self.configure(bg=CLR_BG)
        self.minsize(880, 400)
        try:
            self.transient(master)
        except Exception:
            pass
        self._center_on(master, width, height)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda _e: self.close())

        f_h2 = (FONT_UI, 13, "bold")
        f_sec = (FONT_UI, 10, "bold")
        f_sub = (FONT_UI, 9)
        f_body = (FONT_UI, 10)

        card = RoundBox(self, radius=22, padding=(20, 16), fill=CLR_CARD, bg=CLR_BG,
                        elevation=2)
        card.pack(fill="both", expand=True, padx=18, pady=18)
        inner = card.body

        head = tk.Frame(inner, bg=CLR_CARD)
        head.pack(fill="x")
        MButton(head, "关闭", self.close, "tonal", f_sec, height=36, padx=22,
                bg=CLR_CARD).pack(side="right")
        left = tk.Frame(head, bg=CLR_CARD)
        left.pack(side="left", fill="x", expand=True)
        tk.Label(left, text=title_text, font=f_h2, bg=CLR_CARD, fg=CLR_TEXT).pack(
            anchor="w")
        tk.Label(left, text=subtitle, font=f_sub, bg=CLR_CARD, fg=CLR_SUB,
                 justify="left").pack(anchor="w", pady=(5, 0))

        tk.Frame(inner, bg=CLR_LINE, height=1).pack(fill="x", pady=(14, 0))

        if rows:
            holder = tk.Frame(inner, bg=CLR_CARD)
            holder.pack(fill="both", expand=True, pady=(12, 0))
            tree = build_table(holder, DETAIL_COLS, TABLE_HEADERS, DETAIL_WIDTHS,
                               DETAIL_STRETCH, font_bold=f_sec, anchor="center")
            enable_copy(tree, headers=TABLE_HEADERS)
            for i, r in enumerate(rows):
                ban = str(r[3]) == FORBID_STATUS_TEXT[1]
                tree.insert("", "end", values=r,
                            tags=("odd" if i % 2 else "even", "ban" if ban else "unban"))
            tk.Label(inner, text="按住左键拖动可框选单元格区域，Ctrl+C 复制；右键可复制单元格/整行/全部，或按 Esc 退出",
                     font=f_sub, bg=CLR_CARD, fg=CLR_MUTED).pack(anchor="w", pady=(10, 0))
        else:
            box = RoundBox(inner, radius=16, padding=(18, 18), fill=CLR_HEAD,
                           bg=CLR_CARD, elevation=0)
            box.pack(fill="x", pady=(18, 0))
            tk.Label(box.body, text="无明细记录", font=f_h2, bg=CLR_HEAD,
                     fg=CLR_SUB).pack(anchor="w")
            tk.Label(box.body, text=empty_text or "该角色暂无封禁明细记录。", font=f_body,
                     bg=CLR_HEAD, fg=CLR_SUB, justify="left", wraplength=640).pack(
                anchor="w", pady=(8, 0))

        self.after(60, self._activate)

    def _center_on(self, master, width, height):
        try:
            master.update_idletasks()
            x = master.winfo_rootx() + max((master.winfo_width() - width) // 2, 0)
            y = master.winfo_rooty() + max((master.winfo_height() - height) // 3, 0)
        except Exception:
            x, y = 120, 120
        self.geometry(f"{width}x{height}+{x}+{y}")

    def _activate(self):
        """显示后置顶取焦（不阻塞主事件循环，便于批量查询时随时关闭）"""
        if self._closed:
            return
        try:
            self.lift()
            self.focus_force()
        except Exception:
            pass

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self.grab_release()
        except Exception:
            pass
        try:
            self.destroy()
        except Exception:
            pass


# ---------------------------------------------------------------- 主窗口
class BanQueryApp:
    """主窗口（Material Design 3）"""

    def __init__(self):
        self.tk = tk
        self._q = queue.Queue()
        self._running = False
        self._stop_flag = False                          # 「停止」标记：置 True 后未开始的账号不再查询
        self.var_key = None                              # 查询条件：角色ID / 无端ID（须在 Tk 根窗口之后创建）
        self.result = None                               # 最近一次查询结果（供导出 / 复制使用）
        self.metrics = {}                                # 顶部指标卡上的数值标签

        self.root = tk.Tk()
        self.root.title(APP_TITLE)
        self.root.configure(bg=CLR_BG)
        self.root.geometry(self._center_geometry(1240, 980))
        self.root.minsize(1080, 720)
        self.var_key = tk.StringVar(master=self.root, value=KEY_TYPE)

        # ---- Material 字号层级（Display / Headline / Title / Body / Label）----
        self.f_h1 = tkfont.Font(family=FONT_UI, size=16, weight="bold")     # Headline
        self.f_h2 = tkfont.Font(family=FONT_UI, size=11, weight="bold")     # Title
        self.f_sub = tkfont.Font(family=FONT_UI, size=9)                    # Body Small
        self.f_body = tkfont.Font(family=FONT_UI, size=10)                  # Body
        self.f_sec = tkfont.Font(family=FONT_UI, size=10, weight="bold")    # Label Large
        self.f_btn = tkfont.Font(family=FONT_UI, size=10, weight="bold")    # Label
        self.f_label = tkfont.Font(family=FONT_UI, size=9, weight="bold")   # Label / Chip
        self.f_metric = tkfont.Font(family=FONT_UI, size=20, weight="bold")  # Display
        self.f_entry = tkfont.Font(family="Consolas", size=12)
        self.f_mono = tkfont.Font(family="Consolas", size=9)

        self._setup_style()
        self._build()

        global _LOG_SINK, _LOGIN_UI
        _LOG_SINK = lambda text: self._q.put(("log", text))
        _LOGIN_UI = self._ask_login_threadsafe

        self._append_log(f"{datetime.now():%H:%M:%S}  界面已就绪。输入编号后点击「查询」。")
        self.root.after(80, self._drain)
        self.root.protocol("WM_DELETE_WINDOW", self.root.destroy)

    # ---------------- 布局 ----------------
    def _center_geometry(self, w, h):
        try:
            sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
            return f"{w}x{min(h, sh - 80)}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 2 - 30)}"
        except Exception:
            return f"{w}x{h}"

    def _setup_style(self):
        """ttk 样式（仅用于表格）：无外框、浅色表头、斑马纹、主色选中行

        注意：滚动条已全部改为自绘（MScrollbar），此处不再配置 ttk 滚动条样式。
        """
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass

        for name in ("Ban.Treeview", "Sum.Treeview"):
            style.configure(name, font=self.f_body, rowheight=40, borderwidth=0,
                            background=CLR_CARD, fieldbackground=CLR_CARD,
                            foreground=CLR_TEXT)
            style.layout(name, [("Treeview.treearea", {"sticky": "nswe"})])
            style.map(name,
                      background=[("selected", CLR_SEL)],
                      foreground=[("selected", CLR_ON_PRIMARY_CONTAINER)])
            style.configure(name + ".Heading", font=self.f_label, relief="flat",
                            background=CLR_HEAD, foreground=CLR_SUB,
                            padding=(12, 11), borderwidth=0)
            style.map(name + ".Heading",
                      background=[("active", CLR_HEAD_HOVER)],
                      foreground=[("active", CLR_PRIMARY)])

    # ---------------- 通用小部件 ----------------
    def _card(self, parent, pad=18, elevation=1, radius=22, bg=CLR_BG, fit="content",
              min_height=0):
        """Material 圆角卡片（Elevation 阴影 + 可选描边）"""
        card = RoundBox(parent, radius=radius, padding=pad, fill=CLR_CARD, bg=bg,
                        elevation=elevation, fit=fit, min_height=min_height)
        card.inner = card.body
        return card

    def _section(self, parent, title, hint="", bg=CLR_CARD):
        """卡片内分区标题：主色圆角竖条 + 标题（可选右侧说明）"""
        box = tk.Frame(parent, bg=bg)
        bar = tk.Canvas(box, width=4, height=16, bg=bg, highlightthickness=0, bd=0)
        bar.pack(side="left", pady=1)
        draw_round_rect(bar, 0, 0, 4, 16, 2, fill=CLR_PRIMARY)
        tk.Label(box, text=title, font=self.f_h2, bg=bg, fg=CLR_TEXT).pack(
            side="left", padx=(9, 0))
        if hint:
            tk.Label(box, text=hint, font=self.f_sub, bg=bg, fg=CLR_MUTED).pack(
                side="left", padx=(10, 0), pady=(2, 0))
        return box

    def _sep(self, parent):
        sep = tk.Frame(parent, bg=CLR_LINE, height=1)
        sep.pack_propagate(False)
        return sep

    def _chip(self, parent, text, fill=CLR_SECONDARY_CONTAINER,
              fg=CLR_ON_SECONDARY_CONTAINER, bg=CLR_CARD):
        """Material Chip 标签（返回容器，标签本体为 .label）"""
        box = RoundBox(parent, radius=13, padding=(12, 5), fill=fill, bg=bg, elevation=0)
        lbl = tk.Label(box.body, text=text, font=self.f_label, bg=fill, fg=fg)
        lbl.pack()
        box.label = lbl
        return box

    # ---------------- 界面搭建 ----------------
    def _build(self):
        self._build_topbar()
        body = tk.Frame(self.root, bg=CLR_BG)
        body.pack(fill="both", expand=True, padx=20, pady=(16, 0))
        self._build_input(body)
        self._build_metrics(body)
        self._build_results(body)
        self._build_footer()

    def _build_topbar(self):
        """顶部应用栏（Surface + 主色圆角徽标 + 1px 分隔线）"""
        bar = tk.Frame(self.root, bg=CLR_CARD)
        bar.pack(fill="x")
        inner = tk.Frame(bar, bg=CLR_CARD)
        inner.pack(fill="x", padx=20, pady=(12, 10))

        icon = tk.Canvas(inner, width=46, height=46, bg=CLR_CARD, highlightthickness=0, bd=0)
        icon.pack(side="left")
        draw_round_rect(icon, 0, 0, 46, 46, 14, fill=CLR_PRIMARY)
        icon.create_oval(12, 10, 30, 28, outline=CLR_ON_PRIMARY, width=2.4)
        icon.create_line(28, 26, 35, 34, fill=CLR_ON_PRIMARY, width=3.2, capstyle="round")

        title_box = tk.Frame(inner, bg=CLR_CARD)
        title_box.pack(side="left", padx=(14, 0))
        tk.Label(title_box, text=APP_TITLE, font=self.f_h1, bg=CLR_CARD,
                 fg=CLR_TEXT).pack(anchor="w")
        tk.Label(title_box, text="数据来源：amis 后台「游戏管理 → 数据封禁 → 登录封禁解禁」"
                                 "；「运营平台」操作人取自「封禁解禁日志」页",
                 font=self.f_sub, bg=CLR_CARD, fg=CLR_SUB).pack(anchor="w", pady=(3, 0))

        right = tk.Frame(inner, bg=CLR_CARD)
        right.pack(side="right")
        tk.Label(right, text=f"批量查询 · 并发 {BATCH_CONCURRENCY} · 支持导入文件",
                 font=self.f_sub, bg=CLR_CARD, fg=CLR_SUB).pack(anchor="e")
        tk.Label(right, text=f"{HOST}/#/game_manager/data_forbidden/login",
                 font=self.f_sub, bg=CLR_CARD, fg=CLR_PRIMARY).pack(anchor="e", pady=(3, 0))

        tk.Frame(bar, bg=CLR_LINE, height=1).pack(fill="x")

    def _build_input(self, body):
        card = self._card(body, pad=16)
        card.pack(fill="x")
        cin = card.inner

        head = tk.Frame(cin, bg=CLR_CARD)
        head.pack(fill="x")
        self._section(head, "待查询账号").pack(side="left")
        chip = self._chip(head, "角色ID", fill=CLR_PRIMARY_CONTAINER,
                          fg=CLR_ON_PRIMARY_CONTAINER)
        chip.pack(side="left", padx=(12, 0))
        self.lbl_id_label = chip.label
        self.lbl_tip = tk.Label(head, text="", font=self.f_sub, bg=CLR_CARD, fg=CLR_ERR)
        self.lbl_tip.pack(side="right")

        row = tk.Frame(cin, bg=CLR_CARD)
        row.pack(fill="x", pady=(12, 0))

        self.seg = Segmented(row, QUERY_KEY_OPTIONS, self.var_key,
                             command=self._on_key_change, font=self.f_body, bg=CLR_CARD)
        self.seg.pack(side="left", anchor="n")

        # 编号输入框（自绘滚动条）
        self.box_ids_wrap = RoundBox(row, radius=16, padding=(5, 4), fill=CLR_HEAD,
                                     outline=CLR_LINE, bg=CLR_CARD)
        self.box_ids_wrap.pack(side="left", fill="both", expand=True, padx=14)
        self.txt_ids = tk.Text(self.box_ids_wrap.body, font=self.f_entry, width=40,
                               height=3, relief="flat", bd=0, wrap="none", undo=True,
                               bg=CLR_HEAD, fg=CLR_TEXT, insertbackground=CLR_PRIMARY,
                               padx=10, pady=6, highlightthickness=0,
                               selectbackground=CLR_PRIMARY_CONTAINER,
                               selectforeground=CLR_ON_PRIMARY_CONTAINER)
        self.txt_ids.pack(side="left", fill="both", expand=True)
        ids_sbar = MScrollbar(self.box_ids_wrap.body, target=self.txt_ids,
                              orient="vertical", thickness=12, bg=CLR_HEAD)
        ids_sbar.pack(side="left", fill="y")
        self.txt_ids.configure(yscrollcommand=ids_sbar.set)
        self.txt_ids.bind("<Control-Return>", lambda _e: (self._on_query(), "break")[1])
        self.txt_ids.bind("<FocusIn>", lambda _e: self.box_ids_wrap.set_outline(CLR_PRIMARY, 2))
        self.txt_ids.bind("<FocusOut>",
                          lambda _e: self.box_ids_wrap.set_outline(CLR_LINE, 1))

        btns = tk.Frame(row, bg=CLR_CARD)
        btns.pack(side="left", anchor="n")
        self.btn_query = MButton(btns, "查询", self._on_query, "filled", self.f_btn,
                                 height=44, padx=32, bg=CLR_CARD)
        self.btn_query.pack(fill="x")
        self.btn_stop = MButton(btns, "停止", self._on_stop, "tonal", self.f_body,
                                height=38, padx=20, bg=CLR_CARD)
        self.btn_stop.pack(fill="x", pady=(8, 0))
        self.btn_stop.set_enabled(False)

        self._sep(cin).pack(fill="x", pady=(12, 10))

        actions = tk.Frame(cin, bg=CLR_CARD)
        actions.pack(fill="x")
        self.lbl_hint = tk.Label(actions, text=self._hint_text("角色ID"), font=self.f_sub,
                                 bg=CLR_CARD, fg=CLR_MUTED, justify="left")
        self.lbl_hint.pack(side="left")
        self.btn_clear = MButton(actions, "清空", self._on_clear, "text", self.f_body,
                                 height=36, padx=18, bg=CLR_CARD)
        self.btn_clear.pack(side="right")
        self.btn_export = MButton(actions, "导出 CSV", self._on_export, "outlined",
                                  self.f_body, height=36, padx=20, bg=CLR_CARD)
        self.btn_export.pack(side="right", padx=(0, 10))
        self.btn_export.set_enabled(False)
        self.btn_import = MButton(actions, "导入文件", self._on_import, "tonal",
                                  self.f_body, height=36, padx=20, bg=CLR_CARD)
        self.btn_import.pack(side="right", padx=(0, 10))

    def _build_metrics(self, body):
        """指标卡：Material 容器色 + 圆角 + 字号层级"""
        row = tk.Frame(body, bg=CLR_BG)
        row.pack(fill="x", pady=(14, 0))
        specs = (
            ("accounts", "查询账号数", CLR_PRIMARY_CONTAINER, CLR_ON_PRIMARY_CONTAINER,
             CLR_PRIMARY),
            ("banned", "当前封禁中", CLR_ERROR_CONTAINER, CLR_ON_ERROR_CONTAINER, CLR_ERROR),
            ("normal", "正常", CLR_SUCCESS_CONTAINER, CLR_ON_SUCCESS_CONTAINER, CLR_SUCCESS),
            ("failed", "查询失败", CLR_WARNING_CONTAINER, CLR_ON_WARNING_CONTAINER, CLR_WARNING),
        )
        for i, (key, caption, fill, on_fill, val_fg) in enumerate(specs):
            row.grid_columnconfigure(i, weight=1, uniform="metric")
            card = RoundBox(row, radius=18, padding=(14, 11), fill=fill, bg=CLR_BG,
                            min_height=84)
            card.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 12, 0))
            val = tk.Label(card.body, text="—", font=self.f_metric, bg=fill, fg=val_fg)
            val.pack(anchor="w")
            tk.Label(card.body, text=caption, font=self.f_label, bg=fill, fg=on_fill).pack(
                anchor="w", pady=(3, 0))
            self.metrics[key] = val

    def _build_results(self, body):
        card = self._card(body, pad=12, fit="expand")
        card.pack(fill="both", expand=True, pady=(14, 12))
        cres = card.inner

        head = tk.Frame(cres, bg=CLR_CARD)
        head.pack(fill="x")
        self._section(head, "查询结果").pack(side="left")
        tk.Label(head, text="双击某行查看该角色的封禁明细", font=self.f_sub,
                 bg=CLR_CARD, fg=CLR_MUTED).pack(side="right", padx=(0, 16))
        self.lbl_rows = tk.Label(head, text="", font=self.f_sub, bg=CLR_CARD, fg=CLR_SUB)
        self.lbl_rows.pack(side="right")

        # 主区域直接展示「当前封禁状态」列表（不再有页签）；明细改为双击行后弹窗查看
        holder = tk.Frame(cres, bg=CLR_CARD)
        holder.pack(fill="both", expand=True, pady=(10, 0))
        self.tree_sum = build_table(holder, SUMMARY_COLS,
                                    summary_headers(self._key_label()),
                                    SUMMARY_WIDTHS, SUMMARY_STRETCH,
                                    style_name="Sum.Treeview",
                                    font_bold=self.f_sec)
        self.tree_sum.bind("<Double-1>", self._on_row_activate)
        self.tree_sum.bind("<Return>", self._on_row_activate)
        enable_copy(self.tree_sum, headers=summary_headers(self._key_label()))

    def _build_footer(self):
        foot = tk.Frame(self.root, bg=CLR_BG)
        foot.pack(fill="x", padx=20, pady=(0, 12))
        self.lbl_status = tk.Label(foot, text="就绪，等待查询", font=self.f_sub,
                                   bg=CLR_BG, fg=CLR_SUB)
        self.lbl_status.pack(side="left")

    def _on_row_activate(self, event=None):
        """双击（或回车）汇总行 → 弹出该角色的封禁明细弹窗"""
        tree = getattr(self, "tree_sum", None)
        if tree is None:
            return
        iid = ""
        if event is not None and getattr(event, "y", None) is not None:
            iid = tree.identify_row(int(event.y))
        if not iid:
            iid = tree.focus()
        if not iid or iid not in tree.get_children():
            return
        try:
            idx = int(tree.index(iid))
        except Exception:
            return
        res = getattr(self, "result", None)
        if not res or idx >= len(res.summaries):
            return
        self._show_detail(res, res.summaries[idx])

    def _show_detail(self, res, summary):
        """打开某角色的明细弹窗（无记录时给出明确文案；同屏只保留一个弹窗）"""
        label = res.id_label
        qv = str(summary.get("query_value", ""))
        name = str(summary.get("role_name") or "")
        if name in ROLE_NAME_PLACEHOLDERS:      # 占位文案不当角色名用于标题
            name = ""
        state = str(summary.get("role_name_state") or "")
        title_text = (f"封禁明细 · {name}（{label} {qv}）" if name
                      else f"封禁明细 · {label} {qv}")
        rows = (res.detail_rows or {}).get(qv, [])
        status = summary.get("status") or STATUS_TEXT_NORMAL
        if summary.get("failed") and not rows:
            empty = (f"该{label}本次查询失败，暂无封禁明细数据。\n"
                     f"失败原因：{summary.get('error') or '未知错误'}")
        elif not rows:
            empty = (f"未查询到该{label}的封禁 / 解禁记录，当前状态：{status}。"
                     f"\n角色名：{summary.get('role_name') or '—'}")
            if state == "not_found":
                empty += f"\n角色信息接口提示「{STATUS_TEXT_NO_ROLE}」，该{label}可能不存在或尚未注册。"
            elif state == "failed":
                empty += "\n角色信息接口本次不可用，角色名暂无法获取。"
        else:
            empty = ""
        sub = (f"当前状态：{status}　　记录 {len(rows)} 条（生效中 "
               f"{summary.get('active_count', 0)} 条 / 已解禁 "
               f"{summary.get('unban_count', 0)} 条 / 已过期 "
               f"{summary.get('expired_count', 0)} 条）")
        if summary.get("expire_text"):
            sub += f"　　到期时间：{summary['expire_text']}"
        self._close_detail()
        self._detail_win = DetailDialog(self.root, title_text, sub, rows, empty)

    def _close_detail(self):
        """关闭已打开的明细弹窗（若有）"""
        win = getattr(self, "_detail_win", None)
        self._detail_win = None
        if win is not None:
            try:
                win.close()
            except Exception:
                pass

    def _set_metrics(self, res=None):
        """刷新指标卡数值（无结果时回到占位符）"""
        if res is None:
            for lbl in self.metrics.values():
                lbl.configure(text="—")
            return
        self.metrics["accounts"].configure(text=str(len(res.summaries)))
        self.metrics["banned"].configure(text=str(res.banned_count))
        self.metrics["normal"].configure(text=str(res.normal_count))
        self.metrics["failed"].configure(text=str(res.failed_count))

    # ---------------- 日志 / 状态 ----------------
    def _append_log(self, text):
        self.lbl_status.configure(text=text)

    def _set_tip(self, text="", color=CLR_ERR):
        self.lbl_tip.configure(text=text, fg=color)

    # ---------------- 事件 ----------------
    def _on_clear(self):
        self.txt_ids.delete("1.0", "end")
        for i in self.tree_sum.get_children():
            self.tree_sum.delete(i)
        self.result = None
        self._close_detail()
        self.btn_export.set_enabled(False)
        self.lbl_rows.configure(text="")
        self._set_metrics(None)
        self._set_tip("")
        self.lbl_status.configure(text="已清空")

    def _on_export(self):
        if not getattr(self, "result", None):
            return
        try:
            path = export_csv(self.result)
        except Exception as e:
            self.lbl_status.configure(text=f"导出失败：{e}")
            return
        self.lbl_status.configure(text=f"已导出：{path}")
        try:
            open_folder(os.path.dirname(path))
        except Exception:
            pass

    def _on_import(self):
        """从 txt / csv 文件导入待查询编号（一行一个，或按分隔符 / 行内混排）"""
        if self._running:
            return
        label = self._key_label()
        path = filedialog.askopenfilename(
            title=f"选择包含{label}的文件",
            filetypes=[("文本 / CSV 文件", "*.txt *.csv *.log *.list"), ("所有文件", "*.*")])
        if not path:
            return
        try:
            ids = parse_ids_from_file(path)
        except Exception as e:
            self._set_tip(f"导入失败：{e}")
            return
        if not ids:
            self._set_tip(f"文件中未识别到{label}")
            return
        self.txt_ids.delete("1.0", "end")
        self.txt_ids.insert("1.0", "\n".join(ids))
        tip = f"已从 {os.path.basename(path)} 导入 {len(ids)} 个{label}"
        if len(ids) > BATCH_CONFIRM_THRESHOLD:
            tip += "（数量较多，点击「查询」开始，可随时「停止」）"
        self._set_tip(tip, color=CLR_SUB)
        self.lbl_status.configure(text=f"已导入 {len(ids)} 个{label}，等待查询")

    @staticmethod
    def _hint_text(label):
        sample = "2648868,14473396" if label == "角色ID" else "10000141237188,10000179370773"
        return f"提示：一行一个编号，或用空格 / 逗号 / 换行分隔；Ctrl+回车 直接查询，例如 {sample}"

    def _key_label(self):
        return "无端ID" if self.var_key.get() == KEY_TYPE_WDID else "角色ID"

    def _on_key_change(self):
        label = self._key_label()
        try:
            self.seg.refresh()
        except Exception:
            pass
        self.lbl_id_label.configure(text=label)
        self.lbl_hint.configure(text=self._hint_text(label))
        try:
            for c, h in zip(SUMMARY_COLS, summary_headers(label)):
                self.tree_sum.heading(c, text=h)
        except Exception:
            pass
        self._set_tip("")

    def _on_query(self):
        if self._running:
            return
        raw = self.txt_ids.get("1.0", "end").strip()
        ids = parse_role_ids(raw)
        bad = parse_bad_tokens(raw)
        label = self._key_label()
        if not raw:
            self._set_tip(f"请输入{label}")
            return
        if not ids:
            self._set_tip(f"未识别到数字{label}")
            return
        if bad:
            self._set_tip(f"已忽略非法输入：{'、'.join(bad[:3])}")
        else:
            self._set_tip("")

        if len(ids) > BATCH_CONFIRM_THRESHOLD:
            ok = messagebox.askyesno(
                "批量查询确认",
                f"本次将查询 {len(ids)} 个{label}，耗时可能较长。\n"
                "查询过程中可点「停止」中止（未开始的账号不再查询）。\n\n确定开始吗？",
                parent=self.root)
            if not ok:
                self.lbl_status.configure(text="已取消本次批量查询")
                return

        self._running = True
        self._stop_flag = False
        self.btn_query.set_enabled(False)
        self.btn_query.set_text("查询中…")
        self.btn_stop.set_enabled(True)
        self.btn_import.set_enabled(False)
        self.lbl_status.configure(text=f"正在查询 {len(ids)} 个{label} …")
        for i in self.tree_sum.get_children():
            self.tree_sum.delete(i)
        self.lbl_rows.configure(text="")
        self._set_metrics(None)
        self._close_detail()
        threading.Thread(target=self._worker,
                         args=(ids, self.var_key.get(), label), daemon=True).start()

    def _on_stop(self):
        if not self._running:
            return
        self._stop_flag = True
        self.btn_stop.set_enabled(False)
        self.lbl_status.configure(text="正在停止：已发出的请求返回后结束，未开始的账号不再查询 …")

    def _worker(self, ids, key_type, label):
        try:
            res = run_query(ids, key_type=key_type, id_label=label,
                            should_stop=lambda: self._stop_flag)
        except Exception as e:
            self._q.put(("error", str(e)))
            return
        self._q.put(("done", res))

    def _drain(self):
        try:
            while True:
                kind, payload = self._q.get_nowait()
                if kind == "log":
                    self.lbl_status.configure(text=payload.strip())
                elif kind == "done":
                    self._on_done(payload)
                elif kind == "error":
                    self._on_error(payload)
                elif kind == "ask_login":
                    self._show_login(payload)
        except queue.Empty:
            pass
        self.root.after(80, self._drain)

    def _on_done(self, res):
        self._running = False
        self.btn_query.set_enabled(True)
        self.btn_query.set_text("查询")
        self.btn_stop.set_enabled(False)
        self.btn_import.set_enabled(True)
        self.result = res

        for i, (s, r) in enumerate(zip(res.summaries, res.summary_rows)):
            self.tree_sum.insert("", "end", values=r,
                                 tags=("odd" if i % 2 else "even", summary_tag(s)))
        self.lbl_rows.configure(
            text=f"当前封禁中 {res.banned_count} 个 / 正常 {res.normal_count} 个"
                 f"（明细 {len(res.rows)} 条，双击行查看）")
        self._set_metrics(res)
        self.btn_export.set_enabled(bool(res.rows or res.summaries))
        text = (f"查询完成：{len(res.summaries)} 个{res.id_label}，"
                f"当前封禁中 {res.banned_count} 个，正常 {res.normal_count} 个")
        if res.failed_count:
            text += f"，查询失败 {res.failed_count} 个"
        if res.stopped:
            text += f"（已停止，计划 {res.total_ids} 个）"
        self.lbl_status.configure(text=text)

    def _on_error(self, msg):
        self._running = False
        self.btn_query.set_enabled(True)
        self.btn_query.set_text("查询")
        self.btn_stop.set_enabled(False)
        self.btn_import.set_enabled(True)
        self.lbl_status.configure(text=f"查询失败：{msg}")

    # ---------------- 登录框（主线程调度） ----------------
    def _ask_login_threadsafe(self, tip="", error="", default_account="", reason=""):
        req = {"tip": tip, "error": error, "account": default_account,
               "reason": reason, "res": None, "event": threading.Event()}
        self._q.put(("ask_login", req))
        req["event"].wait()
        return req["res"]

    def _show_login(self, req):
        try:
            req["res"] = LoginDialog(self.root, tip=req.get("tip", ""),
                                     error=req.get("error", ""),
                                     default_account=req.get("account", ""),
                                     reason=req.get("reason", "")).wait()
        except Exception as e:
            log(f"[警告] 登录窗口打开失败: {e}")
            req["res"] = None
        finally:
            req["event"].set()

    def run(self):
        self.root.mainloop()


# ---------------------------------------------------------------- 登录对话框（Material 3）
class MTextField(tk.Frame):
    """Material 3 填充式输入框（全自绘，无 ttk 原生观感）

    - 常态：Surface Container 填充 + 1px Outline Variant 描边
    - 悬停：描边加深（Outline）
    - 聚焦：标签与描边转主色（Primary）且描边加粗到 2px，即 Material 聚焦描边
    - 可用 add_trailing() 在输入行尾部放文本按钮（如密码「显示 / 隐藏」）
    """

    def __init__(self, master, label="", font=None, show=None, width=28,
                 bg=CLR_CARD, f_label=None):
        self._bg = bg
        self._f_label = f_label or (FONT_UI, 9, "bold")
        self._hover = False
        super().__init__(master, bg=bg)
        self.lbl = tk.Label(self, text=label, font=self._f_label, bg=bg, fg=CLR_SUB)
        self.lbl.pack(anchor="w", padx=(4, 0), pady=(0, 5))
        self.box = RoundBox(self, radius=12, padding=(3, 2), fill=CLR_HEAD,
                            outline=CLR_LINE, outline_width=1, bg=bg)
        self.box.pack(fill="x")
        kw = {"show": show} if show else {}
        self.entry = tk.Entry(self.box.body, font=font or (FONT_UI, 11), width=width,
                              relief="flat", bd=0, bg=CLR_HEAD, fg=CLR_TEXT,
                              insertbackground=CLR_PRIMARY, highlightthickness=0, **kw)
        self.entry.pack(side="left", fill="x", expand=True, padx=(10, 4), pady=9)
        self.entry.bind("<FocusIn>", lambda _e: self._paint(True))
        self.entry.bind("<FocusOut>", lambda _e: self._paint(False))
        for widget in (self, self.lbl, self.box, self.box.canvas, self.box.body,
                       self.entry):
            widget.bind("<Enter>", lambda _e: self._set_hover(True), add="+")
            widget.bind("<Leave>", lambda _e: self._set_hover(False), add="+")

    def add_trailing(self, widget):
        """在输入行尾部（右侧）放置控件；调用方需保证其 bg 与输入域填充色一致"""
        widget.pack(side="right", padx=(2, 6), pady=5)
        return widget

    def _set_hover(self, val):
        if self._hover == val:
            return
        self._hover = val
        self._paint(None)

    def _paint(self, focused=None):
        if focused is None:
            try:
                focused = self.focus_get() is self.entry
            except Exception:
                focused = False
        if focused:
            self.box.set_outline(CLR_PRIMARY, 2)
            self.lbl.configure(fg=CLR_PRIMARY)
        else:
            self.box.set_outline(CLR_OUTLINE if self._hover else CLR_LINE, 1)
            self.lbl.configure(fg=CLR_SUB)

    # ---- 便捷访问 ----
    def get(self):
        return self.entry.get()

    def focus_input(self):
        self.entry.focus_set()


class LoginDialog:
    """登录对话框（Material 3 风格，与主界面同一套自绘组件，不用 ttk 原生控件）

    - 顶部 Primary Container 头图：锁形徽标 + 标题 + 副标题。标题明确写出
      「首次使用，请登录」或「登录状态已失效，请重新登录」，副标题说明原因
    - 内容区：登录网址 / 账号 / 密码（Material 填充式输入框，聚焦主色描边）+ LDAP 复选
    - 底部：文本按钮「取消」+ 填充按钮「登 录」（含 hover / pressed 状态层）
    - 账号密码仅在本进程内存中传递（缓存文件只写 token），绝不落盘
    """

    def __init__(self, master, tip="", error="", default_account="", reason=""):
        self._result = None
        self._error = error or ""
        self._title, self._subtitle = login_reason_texts(reason, tip)

        self.win = tk.Toplevel(master)
        self.win.title(f"{self._title} · amis.ssjj.cn")
        self.win.configure(bg=CLR_BG)
        self.win.resizable(False, False)
        self.win.protocol("WM_DELETE_WINDOW", self._cancel)

        f_h1 = (FONT_UI, 15, "bold")     # Headline Small（标题）
        f_sec = (FONT_UI, 10, "bold")    # Label Large（主按钮）
        f_sub = (FONT_UI, 9)             # Body Small（说明 / 标签）
        f_body = (FONT_UI, 10)           # Body Medium
        f_mono = ("Consolas", 12)        # 账号 / 密码（等宽，便于核对）

        card = RoundBox(self.win, radius=24, padding=0, fill=CLR_CARD, bg=CLR_BG,
                        elevation=3)
        card.pack(padx=26, pady=26)
        outer = card.body

        # 顶部标题区（Primary Container，向下超出被裁切，得到上方圆角）
        head = tk.Canvas(outer, height=106, bg=CLR_CARD, highlightthickness=0, bd=0)
        head.pack(fill="x")
        head.bind("<Configure>", lambda _e: self._paint_head(head, f_h1, f_sub))
        head.after(30, lambda: self._paint_head(head, f_h1, f_sub))

        body = tk.Frame(outer, bg=CLR_CARD)
        body.pack(fill="x", padx=26, pady=(18, 0))
        body.grid_columnconfigure(1, weight=1)

        tk.Label(body, text="登录网址", bg=CLR_CARD, fg=CLR_SUB, font=f_sub).grid(
            row=0, column=0, sticky="w", pady=(4, 16))
        url_row = tk.Frame(body, bg=CLR_CARD)
        url_row.grid(row=0, column=1, sticky="ew", pady=(4, 16))
        tk.Label(url_row, text=LOGIN_PAGE, bg=CLR_CARD, fg=CLR_PRIMARY, font=f_body,
                 cursor="hand2").pack(side="left")
        MButton(url_row, "打开", lambda: webbrowser.open(LOGIN_PAGE), "text", f_sub,
                height=30, padx=12, bg=CLR_CARD).pack(side="left", padx=(8, 0))

        tk.Label(body, text="账号", bg=CLR_CARD, fg=CLR_SUB, font=f_sub).grid(
            row=1, column=0, sticky="nw", pady=(0, 16))
        self.fld_account = MTextField(body, "账号", font=f_mono, bg=CLR_CARD)
        self.fld_account.grid(row=1, column=1, sticky="ew", pady=(0, 16))
        self.ent_account = self.fld_account.entry

        tk.Label(body, text="密码", bg=CLR_CARD, fg=CLR_SUB, font=f_sub).grid(
            row=2, column=0, sticky="nw")
        self.fld_password = MTextField(body, "密码", font=f_mono, show="●", bg=CLR_CARD)
        self.fld_password.grid(row=2, column=1, sticky="ew")
        self.ent_password = self.fld_password.entry
        self._pwd_shown = False
        self.btn_show = self.fld_password.add_trailing(
            MButton(self.fld_password.box.body, "显示", self._toggle_password, "text",
                    f_sub, height=28, padx=10, bg=CLR_HEAD))

        self.var_ldap = tk.BooleanVar(value=False)
        MCheckbox(outer, text="LDAP 登录（本后台使用 LDAP 账号时请勾选）",
                  variable=self.var_ldap, font=f_body, bg=CLR_CARD).pack(
            anchor="w", padx=26, pady=(16, 0))

        self._sep = tk.Frame(outer, bg=CLR_LINE, height=1)
        self._sep.pack(fill="x", pady=(18, 0))

        # 错误提示条（Material 错误容器；无错误时不占位）
        self.err_box = RoundBox(outer, radius=12, padding=(12, 8),
                                fill=CLR_ERROR_CONTAINER, bg=CLR_CARD, elevation=0)
        self.lbl_error = tk.Label(self.err_box.body, text=self._error or "",
                                  bg=CLR_ERROR_CONTAINER, fg=CLR_ON_ERROR_CONTAINER,
                                  justify="left", wraplength=400, font=f_sub)
        self.lbl_error.pack(anchor="w", fill="x")
        if self._error:
            self.err_box.pack(anchor="w", fill="x", padx=26, pady=(14, 0),
                              before=self._sep)

        btns = tk.Frame(outer, bg=CLR_CARD)
        btns.pack(anchor="e", padx=26, pady=18)
        MButton(btns, "取消", self._cancel, "text", f_body, height=38, padx=20,
                bg=CLR_CARD).pack(side="left", padx=(0, 10))
        MButton(btns, "登 录", self._submit, "filled", f_sec, height=40, padx=30,
                bg=CLR_CARD).pack(side="left")

        self.win.bind("<Return>", lambda _e: self._submit())
        self.win.bind("<Escape>", lambda _e: self._cancel())
        if default_account:
            self.ent_account.insert(0, default_account)
        self.win.update_idletasks()
        self.win.transient(master)
        self._center_on(master)
        self.win.attributes("-topmost", True)
        (self.ent_password if default_account else self.ent_account).focus_set()

    def _paint_head(self, head, f_h1, f_sub):
        """绘制 Primary Container 头图：锁形徽标 + 标题 + 副标题"""
        w = max(head.winfo_width(), 520)
        head.delete("all")
        draw_round_rect(head, -2, -2, w + 2, 300, 24, fill=CLR_PRIMARY_CONTAINER)
        draw_round_rect(head, 24, 24, 68, 68, 14, fill=CLR_PRIMARY)
        head.create_arc(39, 42, 53, 56, start=0, extent=180, style="arc",
                        outline=CLR_ON_PRIMARY, width=2)
        head.create_rectangle(39, 51, 53, 60, outline=CLR_ON_PRIMARY, width=2)
        head.create_text(82, 34, anchor="w", text=self._title,
                         fill=CLR_ON_PRIMARY_CONTAINER, font=f_h1)
        head.create_text(82, 62, anchor="nw", text=self._subtitle,
                         fill=CLR_SUB, font=f_sub, width=w - 110)

    def _center_on(self, master):
        """登录窗口居中到主窗口（主窗口不可用时居中到屏幕）"""
        try:
            self.win.update_idletasks()
            w, h = self.win.winfo_reqwidth(), self.win.winfo_reqheight()
            mw, mh = master.winfo_width(), master.winfo_height()
            if mw > 1 and mh > 1:
                x = master.winfo_rootx() + (mw - w) // 2
                y = master.winfo_rooty() + (mh - h) // 2
            else:
                x = (self.win.winfo_screenwidth() - w) // 2
                y = (self.win.winfo_screenheight() - h) // 3
            sw, sh = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
            self.win.geometry(f"+{max(0, min(x, sw - w))}+{max(0, min(y, sh - h))}")
        except Exception:
            pass

    def _toggle_password(self):
        """在掩码与明文之间切换（仅内存，不落盘）"""
        self._pwd_shown = not self._pwd_shown
        self.ent_password.configure(show="" if self._pwd_shown else "●")
        self.btn_show.set_text("隐藏" if self._pwd_shown else "显示")
        self.ent_password.focus_set()

    def _show_error(self, msg):
        self.lbl_error.configure(text=msg)
        if not self.err_box.winfo_ismapped():
            self.err_box.pack(anchor="w", fill="x", padx=26, pady=(14, 0),
                              before=self._sep)
        self.win.update_idletasks()

    def _submit(self):
        account = self.ent_account.get().strip()
        password = self.ent_password.get()
        if not account:
            self._show_error("请输入账号。")
            self.ent_account.focus_set()
            return
        if not password:
            self._show_error("请输入密码。")
            self.ent_password.focus_set()
            return
        self._result = (account, password, bool(self.var_ldap.get()))
        self.win.destroy()

    def _cancel(self):
        self._result = None
        self.win.destroy()

    def wait(self):
        self.win.wait_window(self.win)
        return self._result

# ---------------------------------------------------------------- 入口
USAGE = f"""{APP_TITLE}  命令行用法：
  python 账号封禁查询.py                      启动图形界面（支持批量查询 / 导入文件 / 停止）
  python 账号封禁查询.py --query "2648868,89335"        批量查询角色ID（空格/逗号/分号/换行分隔）
  python 账号封禁查询.py --query "..." --csv            查询并导出 CSV（账号汇总 + 明细）
  python 账号封禁查询.py --file ids.txt                从 txt / csv 文件批量导入编号后查询
  python 账号封禁查询.py --file ids.txt --csv           导入文件查询并导出 CSV
  python 账号封禁查询.py --query "..." --wdid          按无端ID（char_name）批量查询
  python 账号封禁查询.py --query "..." --workers 8     指定并发账号数（默认 {BATCH_CONCURRENCY}，1 为逐个串行）
  python 账号封禁查询.py --selfcheck                   界面自检后自动退出
"""


def run_cli(ids_arg="", do_export=False, key_type=KEY_TYPE_ROLE, id_label="角色ID",
            from_file="", workers=BATCH_CONCURRENCY):
    """命令行批量查询：打印账号汇总、结论与明细（--csv 时同时导出 CSV）"""
    ids = parse_role_ids(ids_arg)
    if from_file:
        ids += parse_ids_from_file(from_file)
    seen, uniq = set(), []
    for i in ids:                       # 合并后去重保序
        if i not in seen:
            seen.add(i)
            uniq.append(i)
    ids = uniq
    if not ids:
        print(f"未识别到有效的{id_label}（需为纯数字）。")
        return 2
    try:
        res = run_query(ids, key_type=key_type, id_label=id_label, workers=workers)
    except Exception as e:
        print(f"查询失败：{e}")
        return 1
    print()
    print(res.conclusion_text())
    print()
    if len(res.summaries) > 1:
        print("\t".join(summary_headers(id_label)))
        for row in res.summary_rows:
            print("\t".join(str(x) for x in row))
        print()
    if res.rows:
        print("\t".join(TABLE_HEADERS))
        for r in res.rows:
            print("\t".join(str(x) for x in r))
    print()
    tail = (f"{id_label}共 {len(res.summaries)} 个：当前封禁中 {res.banned_count} 个，"
            f"正常 {res.normal_count} 个")
    if res.failed_count:
        tail += f"，查询失败 {res.failed_count} 个"
    print(tail + f"（明细 {len(res.rows)} 条，仅供核查）")
    if do_export:
        try:
            print(f"已导出 CSV: {export_csv(res)}")
        except Exception as e:
            print(f"导出 CSV 失败：{e}")
            return 1
    return 0


def _parse_cli_key(argv):
    """命令行解析查询条件：--key wdid / --wdid 表示按无端ID 查询，默认角色ID"""
    val = ""
    if "--wdid" in argv:
        val = "wdid"
    elif "--key" in argv:
        i = argv.index("--key")
        val = argv[i + 1] if len(argv) > i + 1 else ""
    if str(val).strip().lower() in ("wdid", "wd", "char_name", "无端id"):
        return KEY_TYPE_WDID, "无端ID"
    return KEY_TYPE_ROLE, "角色ID"


def _parse_cli_opt(argv, name, default=""):
    """取 --name value 形式的命令行参数值"""
    if name in argv:
        i = argv.index(name)
        return argv[i + 1] if len(argv) > i + 1 else ""
    return default


def main(argv):
    if any(a in argv for a in ("-h", "--help", "--usage")):
        print(USAGE)
        return 0

    key_type, id_label = _parse_cli_key(argv)
    from_file = _parse_cli_opt(argv, "--file")
    try:
        workers = int(_parse_cli_opt(argv, "--workers", str(BATCH_CONCURRENCY)) or BATCH_CONCURRENCY)
    except Exception:
        workers = BATCH_CONCURRENCY

    if "--query" in argv or from_file:
        arg = _parse_cli_opt(argv, "--query")
        return run_cli(arg, do_export="--csv" in argv, key_type=key_type,
                       id_label=id_label, from_file=from_file, workers=workers)

    app = BanQueryApp()
    if "--selfcheck" in argv:
        app.root.after(1200, app.root.destroy)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
