#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ACE 控制台 OpenID 联合查询程序（单一入口 + 同页双区块）

    查询入口（窗口顶部，全局唯一）:
        一个 OpenID 输入框 + 一个「查询」按钮；点击一次即同时发起两类查询。

    结果区（同一页面内上下两个区块，各自独立滚动）:
        区块 1「可疑用户核查」（上方）:
            对应「端游客户端安全 → 可疑用户核查 → 账号核查」页面（page_type=user_check_v2）。
            时间范围固定为近 30 天（含今天），界面上不再提供时间/级别选择控件；
            命令行 --begin/--end 可覆盖该范围（可疑级别固定为 all）。
            数据类型为「截图」时直接显示缩略图，单击缩略图（或「查看原图」按钮）弹出窗口查看原大图；
            其他数据类型照常显示文字信息。
        区块 2「数据查询」（下方）:
            对应「前端用户作弊查询 → 数据查询」页面（page_type=query_by_openid）。
            时间范围固定为当天回溯 10 年（含今天），界面上不再提供时间选择控件；
            命令行 --begin/--end 可覆盖该范围。
            表格展示处罚方案 / 处罚描述 / 作弊时间 / 处罚时间 / 处罚时长；
            结果为空时提示「暂无处罚数据记录」；数据条数较多时支持翻页查看更多（上一页 / 下一页）。

两种运行方式:
    1) 图形界面（默认）:
           python ace_user_check.py
    2) 命令行表格（兼容旧用法，仅查询「可疑用户核查」）:
           python ace_user_check.py 12376323 --cli
           python ace_user_check.py 12376323 --cli --begin 2026-09-01 --end 2026-09-30 --level all --json

说明:
    - 登录凭据从同目录下的 ace_session_credentials.json 读取，绝不硬编码。
    - 截图按接口返回的路径（拼接站点域名）携带登录 Cookie 下载后展示缩略图 / 原图。
    - 「可疑用户核查」默认查询近 30 天（含今天）；「数据查询」默认查询近 10 年内的全部处罚记录。
    - 依赖：Python 标准库（tkinter / urllib / queue）+ Pillow（PIL）。
    - 授权校验：本文件独立启动时（GUI / --cli）同样会先做 license-guard 授权校验，
      校验不通过则按后端策略退出；接入层见同目录 license_boot.py。
"""

import argparse
import base64
import csv
import hashlib
import io
import json
import os
import queue
import re
import shutil
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timedelta

try:                                     # 授权校验（license-guard）：缺失则拒绝启动（fail-closed）
    import license_boot
except Exception as _lic_err:            # pragma: no cover - 仅在文件缺失时发生
    license_boot = None
    _LICENSE_BOOT_ERROR = _lic_err

# ---------------------------------------------------------------------------
# 常量配置
# ---------------------------------------------------------------------------
# 程序所在目录：打包成 exe 后必须取「exe 所在目录」，不能用 __file__ ——
# PyInstaller 单文件模式下 __file__ 指向临时解包目录（sys._MEIPASS），
# 在那里写导出的 CSV / 登录态凭据会随进程退出一起被清掉。
if getattr(sys, "frozen", False):
    SCRIPT_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# 可选的「原项目目录」：以前是同目录下的另一份副本，靠它复用已有登录态。
# 公开发布后这个回退没有意义，默认留空；需要时可用环境变量 ACE_PROJECT_DIR 指定。
ORIGINAL_PROJECT_DIR = (os.environ.get("ACE_PROJECT_DIR") or "").strip()
OUTPUT_DIR_NAME = "查询结果"  # 导出默认目录：自身所在目录下的「查询结果」子目录

_CREDENTIALS_NAME = "ace_session_credentials.json"


def _resolve_credentials_file():
    """凭据文件路径：自身所在目录 → 原项目目录 → 自身目录（写入落在实际解析到的文件）。"""
    local = os.path.join(SCRIPT_DIR, _CREDENTIALS_NAME)
    if os.path.isfile(local):
        return local
    if ORIGINAL_PROJECT_DIR:
        fallback = os.path.join(ORIGINAL_PROJECT_DIR, _CREDENTIALS_NAME)
        if os.path.isfile(fallback):
            return fallback
    return local


def output_dir():
    """导出目录（自身所在目录下的「查询结果」子目录），不存在时自动创建。"""
    path = os.path.join(SCRIPT_DIR, OUTPUT_DIR_NAME)
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        return SCRIPT_DIR
    return path


CREDENTIALS_FILE = _resolve_credentials_file()

BASE_ORIGIN = "https://www.anticheatexpert.com"
QUERY_URL = BASE_ORIGIN + "/mtp_esb/cgi/user_check"
QUERY_PARAMS = {
    "service_id": "2110",
    "game_id": "467",
    "page_type": "user_check_v2",
    "data_type": "sample,punish,behavior,screenshot",
}
IMAGE_BASE = BASE_ORIGIN
REFERER = BASE_ORIGIN + "/ibox_2094/"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
TRACE_HEADER = "UENfYWNjb3VudF92ZXJpZmljYXRpb24="  # Base64: PC_account_verification

# 「数据查询」区块接口（前端用户作弊查询 → 数据查询）
DQ_URL = BASE_ORIGIN + "/ibox_2900/cgi/punish/query"
DQ_QUERY_PARAMS = {"game_id": "467", "page_type": "query_by_openid"}
DQ_TRACE_HEADER = "RnJvbnRfdXNlcmNoZWF0X3F1ZXJ5"  # Base64: Front_usercheat_query
DQ_PAGE_SIZE = 20  # 每页条数（与页面默认一致）
DQ_TREE_MIN_ROWS = 3  # 表格最小可视行数（无数据/数据不足时的占位高度，非独立滚动窗口）
DQ_LOOKBACK_YEARS = 10  # 未限定日期时回溯的年数（与页面默认一致）

THUMB_SIZE = (140, 140)  # 缩略图最大边长（像素）
FULL_VIEW_SIZE = (1400, 900)  # 原图查看窗口标准尺寸（超出等比缩放，小于按原尺寸）

# —— 登录相关（依据 ACE 登录页接口分析）——
LOGIN_BASE = BASE_ORIGIN + "/ibox_2206/cgi/account/login"
LOGIN_URL_GET_PUBLIC_KEY = LOGIN_BASE + "?op_type=get_public_key"
LOGIN_URL_PHONE = LOGIN_BASE + "?op_type=phone_login"
LOGIN_APP_ID = 1001
LOGIN_REGION = "86"  # 默认区号（+86）
LOGIN_REDIRECT_URL = (
    BASE_ORIGIN + "/ibox_1914/cgi/phonecallback?redirect_url="
    "https%3A%2F%2Fwww.anticheatexpert.com%2Fcgi%2Flogin%3Fop_type%3Dcommon_login"
)
LOGIN_REFERER = BASE_ORIGIN + "/login"
# 登录成功后需要落盘的 Cookie 名单（取自前端登出清理名单）
LOGIN_COOKIE_KEYS = [
    "user_token",
    "ibox_auth_user_token",
    "ibox_auth_oa_token",
    "user_id",
    "ibox_auth_user_id",
    "user_name",
    "ibox_auth_user_name",
    "source",
    "ibox_auth_source",
]
# 响应中出现这些关键字，说明需要人工完成滑块 / 短信验证（不可自动绕过）
MANUAL_VERIFY_KEYWORDS = (
    "captcha", "ticket", "randstr", "verify", "verification",
    "验证码", "滑块", "滑动", "短信", "人工", "turing",
)
MANUAL_VERIFY_TIP = (
    "请手动打开 https://www.anticheatexpert.com/login 完成滑块 / 短信验证后，"
    "再回到本窗口重新点击「登录」。程序不会也无法绕过人工验证。"
)

DATA_TYPE_LABELS = {
    "screenshot": "截图",
    "sample": "样本",
    "behavior": "行为",
    "punish": "处罚",
}
LEVEL_LABELS = {
    "high": "高",
    "middle": "中",
    "low": "低",
}

# 命令行表格列定义: (表头, 记录字段, 取值转换函数)
COLUMNS = [
    ("上报日期", "log_date", None),
    ("OpenID", "openid", None),
    ("数据类型", "data_type", lambda r: DATA_TYPE_LABELS.get(r.get("data_type", ""), r.get("data_type", ""))),
    ("证据Key", "key", None),
    ("路径/描述", "des", None),
    ("可疑级别", "level", lambda r: LEVEL_LABELS.get(r.get("level", ""), r.get("level", ""))),
    ("处罚时长", "ban_duration", None),
    ("外挂名", "bot_name", None),
    ("记录时间", "log_time", None),
]

# 「数据查询」表格列定义: (表头, 记录字段, 列宽像素, 对齐方式, 是否吸收剩余宽度)
# 对齐规则：文本类（方案 / 描述）左对齐；时间与时长长度固定、居中后纵向扫读更整齐，
# 表头锚点同步取同一对齐，表头文字与列内数据落在同一条中轴上。
# 列宽规则：宽度按内容最长形态预留（时间列按 “YYYY-MM-DD HH:MM:SS” 留足），
# 仅「处罚描述」拉伸吸收剩余宽度，其余列固定宽度，窗口缩放时数字列不忽宽忽窄。
DQ_COLUMNS = [
    ("处罚方案", "punish_plan_desc", 140, "w", False),
    ("处罚描述", "punish_desc", 260, "w", True),
    ("作弊时间", "cheat_time_str", 172, "center", False),
    ("处罚时间", "punish_date", 172, "center", False),
    ("处罚时长", "punish_duration_str", 112, "center", False),
]


# ---------------------------------------------------------------------------
# 批量查询：账号清单解析 + 汇总口径
# ---------------------------------------------------------------------------
# 汇总表列定义：(表头, 结果字段, 列宽像素, 对齐, 是否拉伸吸收剩余宽度)
BATCH_COLUMNS = [
    ("OpenID", "openid", 150, "w", False),
    ("可疑用户核查", "record_count", 140, "center", False),
    ("数据查询", "punish_count", 140, "center", False),
    ("最高可疑级别", "top_level", 130, "center", False),
    ("最近处罚时间", "last_punish", 220, "center", True),
]

# 汇总列表表格高度：随账号数自适应，最少 3 行、最多 14 行（超出后表格内部滚动）
BATCH_TREE_MIN_ROWS = 3
BATCH_TREE_MAX_ROWS = 14

# 可疑级别高低序（数值越大越可疑），用于汇总表取「最高可疑级别」
BATCH_LEVEL_ORDER = {"low": 1, "middle": 2, "high": 3}

# 批量输入分隔符：换行 / 制表符 / 空格 / 逗号（中英文）/ 分号（中英文）/ 竖线 / 顿号
OPENID_SPLIT_RE = re.compile(r"[\s,，;；|、]+")

# 有效账号形态：字母 / 数字 / 下划线 / 短横线（数字型 OpenID 亦满足）
OPENID_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# CSV 清单的表头候选词（命中则跳过首行，避免把表头当成账号）
OPENID_HEADER_WORDS = {
    "openid", "open_id", "openid列表", "id", "uid", "account", "account_id",
    "账号", "账号id", "用户id", "用户", "账号列表",
}


def parse_openid_list(raw):
    """解析账号清单：兼容多行粘贴与逗号 / 空格 / 分号等分隔符。

    返回 (openids, ignored)：openids 为去重后（保持输入顺序）的账号列表，
    ignored 为被忽略的无效片段数量（空片段不计入）。
    """
    openids = []
    ignored = 0
    for token in OPENID_SPLIT_RE.split(raw or ""):
        value = clean_openid(token)
        if not value:
            continue
        if len(value) < 3 or not OPENID_TOKEN_RE.match(value):
            ignored += 1
            continue
        if value not in openids:
            openids.append(value)
    return openids, ignored


def load_openid_file(path):
    """从 txt / csv 清单文件读取账号列表，返回 (openids, ignored)。

    - csv：首行为表头（如 OpenID / 账号 等）时自动跳过，其余行按单元格逐个解析；
    - txt / 其他：整篇按分隔符解析。
    编码依次尝试 utf-8-sig / utf-8 / gbk / utf-16，覆盖记事本与 Excel 导出的常见编码。
    """
    text = None
    for encoding in ("utf-8-sig", "utf-8", "gbk", "utf-16"):
        try:
            with open(path, "r", encoding=encoding) as handle:
                text = handle.read()
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise AceError("清单文件读取失败（编码无法识别）: %s" % os.path.basename(path))

    if os.path.splitext(path)[1].lower() != ".csv":
        return parse_openid_list(text)

    # CSV：先去掉表头行，再统一走分隔符解析（同一行多个单元格 / 空格分隔都能识别）
    lines = text.splitlines()
    if lines:
        first_cell = OPENID_SPLIT_RE.split(lines[0].strip())[:1]
        if first_cell and clean_openid(first_cell[0]).lower() in OPENID_HEADER_WORDS:
            lines = lines[1:]
    return parse_openid_list("\n".join(lines))


def batch_top_level(records):
    """汇总「最高可疑级别」：按 high > middle > low 取最高；无级别信息返回空串。"""
    best = ""
    for record in records or []:
        key = str(record.get("level") or "").strip().lower()
        if key not in BATCH_LEVEL_ORDER:
            continue
        if not best or BATCH_LEVEL_ORDER[key] > BATCH_LEVEL_ORDER[best]:
            best = key
    return best


def batch_last_punish(items):
    """汇总「最近处罚时间」：取给定记录里最大的一项；无数据返回空串。"""
    latest = ""
    for record in items or []:
        value = str(record.get("punish_date") or "").strip()
        if value and value > latest:
            latest = value
    return latest


def default_batch_csv_name():
    """批量汇总 CSV 的默认文件名。"""
    return "ace_batch_openid_%s.csv" % datetime.now().strftime("%Y%m%d_%H%M%S")


class AceError(Exception):
    """业务 / 网络 / 凭据异常，携带给用户看的提示。"""

    def __init__(self, message, tip=None, auth=False):
        super().__init__(message)
        self.message = message
        self.tip = tip
        self.auth = auth  # True 表示登录态失效导致的错误（会触发登录窗口）


class LoginManualRequired(AceError):
    """登录流程需要人工完成（滑块 / 短信验证码等），程序无法自动继续。"""

    def __init__(self, message, tip=MANUAL_VERIFY_TIP):
        super().__init__(message, tip)
        self.manual = True


def _format_error(exc):
    text = exc.message
    if getattr(exc, "tip", None):
        text += "\n\n" + exc.tip
    return text


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------
def setup_stdout():
    # 打包成 GUI 子系统 exe（--windowed）后没有控制台，sys.stdout 会是 None，
    # 此时 print() 会直接抛异常；先附着父控制台，附着不上就丢进空设备。
    if sys.stdout is None or sys.stderr is None:
        try:
            import ctypes
            if ctypes.windll.kernel32.AttachConsole(-1):     # ATTACH_PARENT_PROCESS
                stream = open("CONOUT$", "w", errors="replace", buffering=1)
                sys.stdout = stream
                sys.stderr = stream
        except Exception:
            pass
        if sys.stdout is None or sys.stderr is None:
            try:
                sink = open(os.devnull, "w", errors="replace")
                sys.stdout = sys.stdout or sink
                sys.stderr = sys.stderr or sink
            except Exception:
                pass
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def display_width(text):
    width = 0
    for ch in str(text):
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def clean_openid(value):
    """清理输入：去除首尾空白、BOM 及不可见字符。"""
    if value is None:
        return ""
    return value.replace("\ufeff", "").strip()


def make_square_thumb(img, side, bg=None):
    """等比缩放后居中留白填充为 side×side 正方形缩略图，不拉伸变形。"""
    from PIL import Image

    if bg is None:
        bg = MATERIAL["thumb_bg"]  # 留白底色统一走 MATERIAL 调色板

    img = img.convert("RGB")
    scale = min(side / float(img.width), side / float(img.height))
    w = max(1, int(round(img.width * scale)))
    h = max(1, int(round(img.height * scale)))
    resized = img.resize((w, h), Image.LANCZOS)
    canvas = Image.new("RGB", (side, side), bg)
    canvas.paste(resized, ((side - w) // 2, (side - h) // 2))
    return canvas


def truncate(text, max_chars):
    text = "" if text is None else str(text)
    if max_chars and len(text) > max_chars:
        return text[: max_chars - 3] + "..."
    return text


def pad(text, width):
    text = "" if text is None else str(text)
    gap = width - display_width(text)
    return text + " " * gap if gap > 0 else text


def print_table(headers, rows):
    widths = []
    for i, head in enumerate(headers):
        w = display_width(head)
        for row in rows:
            w = max(w, display_width(row[i]))
        widths.append(w)

    sep = "+" + "+".join("-" * (w + 2) for w in widths) + "+"

    def render(cells):
        return "|" + "|".join(" " + pad(c, widths[i]) + " " for i, c in enumerate(cells)) + "|"

    print(sep)
    print(render(headers))
    print(sep)
    for row in rows:
        print(render(row))
    print(sep)


# ---------------------------------------------------------------------------
# 凭据加载 / 接口调用
# ---------------------------------------------------------------------------
def load_credentials():
    if not os.path.isfile(CREDENTIALS_FILE):
        raise AceError(
            "未找到凭据文件: %s" % CREDENTIALS_FILE,
            "请通过登录窗口登录，程序会自动写入该文件。",
            auth=True,
        )
    try:
        with open(CREDENTIALS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        raise AceError("凭据文件解析失败: %s" % exc, "请检查文件是否为合法 JSON。")

    cookies = data.get("cookies") or {}
    if not cookies:
        raise AceError("凭据文件中没有 cookies 字段，无法完成鉴权。",
                       "请通过登录窗口登录，程序会自动写入该文件。", auth=True)

    token = cookies.get("user_token") or cookies.get("ibox_auth_user_token")
    if not token:
        raise AceError(
            "凭据缺少 user_token / ibox_auth_user_token，登录态无效。",
            "请通过登录窗口登录，程序会自动更新凭据文件。",
            auth=True,
        )
    return data, cookies


def credentials_status():
    """返回 (是否已登录, 说明文本)，供启动时判断是否需要弹出登录窗口。"""
    try:
        data, cookies = load_credentials()
    except AceError as exc:
        return False, _format_error(exc)
    name = (data.get("account") or {}).get("user_name") or cookies.get("user_name") or "未知"
    return True, "登录账号：%s" % name


def build_cookie_header(cookies):
    return "; ".join("%s=%s" % (k, v) for k, v in cookies.items())


# ---------------------------------------------------------------------------
# RSA 加密（密码 sha256 后按 PKCS#1 v1.5 加密，与前端 JSEncrypt 行为一致）
# ---------------------------------------------------------------------------
try:  # 优先使用 cryptography（若环境中可用）；不可用时自动退回纯标准库实现
    from cryptography.hazmat.primitives import hashes as _crypto_hashes  # noqa: F401
    from cryptography.hazmat.primitives import serialization as _crypto_serialization
    from cryptography.hazmat.primitives.asymmetric import padding as _crypto_padding

    _CRYPTOGRAPHY_AVAILABLE = True
except Exception:  # pragma: no cover - 取决于运行环境
    _CRYPTOGRAPHY_AVAILABLE = False


def sha256_hex(text):
    """前端 sha256(密码) 的等价实现：返回小写十六进制字符串。"""
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def _pem_to_der(text):
    stripped = (text or "").strip()
    if not stripped:
        raise AceError("RSA 公钥内容为空。")
    if "-----BEGIN" in stripped:
        stripped = "".join(
            line for line in stripped.splitlines() if "-----" not in line
        )
    compact = "".join(stripped.split())
    compact += "=" * (-len(compact) % 4)
    try:
        return base64.b64decode(compact)
    except Exception:
        raise AceError("RSA 公钥不是合法的 PEM / base64 内容。")


def _der_read_length(data, index):
    first = data[index]
    index += 1
    if first < 0x80:
        return first, index
    count = first & 0x7F
    return int.from_bytes(data[index:index + count], "big"), index + count


def _der_read_tlv(data, index):
    tag = data[index]
    index += 1
    length, index = _der_read_length(data, index)
    value = data[index:index + length]
    if len(value) != length:
        raise AceError("RSA 公钥 DER 结构不完整。")
    return tag, value, index + length


def _parse_rsa_public_numbers(public_key_text):
    """纯标准库解析 RSA 公钥（支持 PEM/DER 的 SPKI 与 PKCS#1），返回 (n, e)。"""
    der = _pem_to_der(public_key_text)
    tag, body, _ = _der_read_tlv(der, 0)
    if tag != 0x30:
        raise AceError("RSA 公钥格式无法识别（顶层不是 DER SEQUENCE）。")

    index = 0
    tag, _algo, index = _der_read_tlv(body, index)  # AlgorithmIdentifier
    tag, bits, index = _der_read_tlv(body, index)
    if tag == 0x03:  # SubjectPublicKeyInfo：BIT STRING 内是 PKCS#1 RSAPublicKey
        payload = bits[1:]
        tag, payload, _ = _der_read_tlv(payload, 0)
        if tag != 0x30:
            raise AceError("RSA 公钥格式无法识别（公钥位串不是 SEQUENCE）。")
    else:  # 已经是 PKCS#1 RSAPublicKey
        payload = body

    index = 0
    tag, modulus, index = _der_read_tlv(payload, index)
    if tag != 0x02:
        raise AceError("RSA 公钥格式无法识别（缺少模数 n）。")
    tag, exponent, index = _der_read_tlv(payload, index)
    if tag != 0x02:
        raise AceError("RSA 公钥格式无法识别（缺少指数 e）。")

    n = int.from_bytes(modulus, "big")
    e = int.from_bytes(exponent, "big")
    if n <= 0 or e <= 0:
        raise AceError("RSA 公钥参数不合法。")
    return n, e


def _rsa_encrypt_pure(n, e, message):
    """纯标准库 RSA 加密：PKCS#1 v1.5（block type 2）填充。"""
    key_bytes = (n.bit_length() + 7) // 8
    if len(message) > key_bytes - 11:
        raise AceError("待加密内容超出 RSA 密钥长度限制。")

    pad_len = key_bytes - len(message) - 3
    padding = bytearray()
    while len(padding) < pad_len:  # 填充字节必须非 0
        chunk = os.urandom(pad_len - len(padding) + 8)
        padding.extend(b for b in chunk if b)
    padding = bytes(padding[:pad_len])

    block = b"\x00\x02" + padding + b"\x00" + message
    if len(block) != key_bytes:
        raise AceError("RSA 填充长度异常，已中止加密。")
    return pow(int.from_bytes(block, "big"), e, n).to_bytes(key_bytes, "big")


def _rsa_encrypt_with_cryptography(public_key_text, message):
    raw = (public_key_text or "").strip()
    loader_inputs = []
    if "-----BEGIN" in raw:
        loader_inputs.append(("pem", raw.encode("utf-8")))
    else:
        loader_inputs.append(("der", base64.b64decode("".join(raw.split()) + "=" * (-len(raw) % 4))))
    key = None
    for kind, blob in loader_inputs:
        try:
            if kind == "pem":
                key = _crypto_serialization.load_pem_public_key(blob)
            else:
                key = _crypto_serialization.load_der_public_key(blob)
            break
        except Exception:
            key = None
    if key is None:
        raise AceError("RSA 公钥解析失败（cryptography 后端）。")
    return key.encrypt(message, _crypto_padding.PKCS1v15())


def rsa_encrypt_password(public_key_text, plain_password, use_cryptography=None):
    """把明文密码按前端逻辑加密：先 sha256 取十六进制，再 RSA(PKCS#1 v1.5) 加密，返回 base64。"""
    digest = sha256_hex(plain_password).encode("utf-8")
    available = _CRYPTOGRAPHY_AVAILABLE if use_cryptography is None else bool(use_cryptography)
    if available:
        try:
            cipher = _rsa_encrypt_with_cryptography(public_key_text, digest)
            return base64.b64encode(cipher).decode("ascii")
        except Exception:
            pass  # 退回纯标准库实现
    n, e = _parse_rsa_public_numbers(public_key_text)
    return base64.b64encode(_rsa_encrypt_pure(n, e, digest)).decode("ascii")


# ---------------------------------------------------------------------------
# 登录会话与自动登录流程
# ---------------------------------------------------------------------------
class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """不自动跟随 3xx，便于识别“被重定向到登录页”的情况。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _LoginSession:
    """极简 HTTP 会话（仅标准库）：POST JSON 并收集 Set-Cookie。"""

    def __init__(self, timeout=30):
        self.cookies = {}
        self.timeout = timeout

    def post_json(self, url, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json;charset=utf-8",
            "Origin": BASE_ORIGIN,
            "Referer": LOGIN_REFERER,
            "User-Agent": USER_AGENT,
        }
        if self.cookies:
            headers["Cookie"] = build_cookie_header(self.cookies)

        request = urllib.request.Request(url=url, data=body, headers=headers, method="POST")
        opener = urllib.request.build_opener(_NoRedirectHandler)
        try:
            with opener.open(request, timeout=self.timeout) as response:
                status = response.status
                text = response.read().decode("utf-8", "replace")
                set_cookies = response.headers.get_all("Set-Cookie") or []
                location = response.headers.get("Location")
        except urllib.error.HTTPError as exc:
            status = exc.code
            location = exc.headers.get("Location") if exc.headers else None
            set_cookies = (exc.headers.get_all("Set-Cookie") or []) if exc.headers else []
            try:
                text = exc.read().decode("utf-8", "replace")
            except Exception:
                text = ""
        except urllib.error.URLError as exc:
            raise AceError(
                "无法连接 ACE 登录服务: %s" % exc.reason,
                "请检查本机网络 / 代理设置，或确认能否访问 www.anticheatexpert.com。",
            )
        except Exception as exc:
            raise AceError("登录请求发生异常: %s" % exc)

        self._store_cookies(set_cookies)
        return status, text, location

    def get(self, url, referer=None):
        """GET 一个 URL（不自动跟随 3xx），返回 (status, text, location) 并收集 Set-Cookie。"""
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "User-Agent": USER_AGENT,
        }
        if referer:
            headers["Referer"] = referer
        if self.cookies:
            headers["Cookie"] = build_cookie_header(self.cookies)

        request = urllib.request.Request(url=url, headers=headers, method="GET")
        opener = urllib.request.build_opener(_NoRedirectHandler)
        try:
            with opener.open(request, timeout=self.timeout) as response:
                status = response.status
                text = response.read().decode("utf-8", "replace")
                set_cookies = response.headers.get_all("Set-Cookie") or []
                location = response.headers.get("Location")
        except urllib.error.HTTPError as exc:
            status = exc.code
            location = exc.headers.get("Location") if exc.headers else None
            set_cookies = (exc.headers.get_all("Set-Cookie") or []) if exc.headers else []
            try:
                text = exc.read().decode("utf-8", "replace")
            except Exception:
                text = ""
        except urllib.error.URLError as exc:
            raise AceError(
                "无法连接 ACE 登录服务: %s" % exc.reason,
                "请检查本机网络 / 代理设置，或确认能否访问 www.anticheatexpert.com。",
            )
        except Exception as exc:
            raise AceError("登录请求发生异常: %s" % exc)

        self._store_cookies(set_cookies)
        return status, text, location

    def follow_redirects(self, url, max_hops=6, referer=None):
        """手动跟随 3xx 跳转链（逐跳收集 Set-Cookie），返回最后一跳 (status, text)。

        登录成功后服务端以 302 → phonecallback?...&token=... 的方式下发登录态，
        浏览器会自动走完这条链并落 Cookie；程序内必须手动跟随才能拿到凭据。
        超过 max_hops 仍未落地时抛 AceError。
        """
        current = url
        for _ in range(max_hops):
            if not re.match(r"^https?://", current or "", re.I):
                current = urllib.parse.urljoin(BASE_ORIGIN, current)
            status, text, location = self.get(current, referer=referer)
            if status in (301, 302, 303, 307, 308) and location:
                referer = current
                current = location
                continue
            return status, text
        raise AceError(
            "登录跳转次数过多（超过 %d 跳仍未完成）。" % max_hops,
            "请稍后重试；若持续出现，请到官网确认账号状态。",
        )

    def _store_cookies(self, set_cookies):
        for raw in set_cookies:
            pair = raw.split(";", 1)[0].strip()
            if "=" not in pair:
                continue
            name, value = pair.split("=", 1)
            name = name.strip()
            value = value.strip()
            if name:
                self.cookies[name] = value


def _looks_like_manual_verify(text):
    """判断响应内容是否指向需要人工完成的滑块 / 短信验证。"""
    lowered = str(text or "").lower()
    for keyword in MANUAL_VERIFY_KEYWORDS:
        if keyword in lowered:
            return True
    return False


def fetch_login_public_key(session=None, timeout=30):
    """获取登录用 RSA 公钥（PEM 字符串）。"""
    session = session or _LoginSession(timeout)
    status, text, location = session.post_json(
        LOGIN_URL_GET_PUBLIC_KEY, {"op_type": "get_public_key"}
    )
    if location or status >= 300:
        raise AceError("获取 RSA 公钥被重定向（HTTP %s → %s）。" % (status, location or "-"),
                       "请稍后重试，或检查本机网络 / 代理。")
    try:
        data = json.loads(text)
    except Exception:
        raise AceError("获取 RSA 公钥失败：响应不是 JSON。", text[:200])
    if str(data.get("ret")) not in ("0", "None"):
        raise AceError("获取 RSA 公钥失败：ret=%s" % data.get("ret"), str(data.get("info"))[:200])
    public_key = data.get("info")
    if not isinstance(public_key, str) or "KEY" not in public_key.upper():
        raise AceError("获取 RSA 公钥失败：info 内容异常。", str(public_key)[:200])
    return public_key


def _account_from_cookies(cookies):
    return {
        "user_name": cookies.get("user_name") or cookies.get("ibox_auth_user_name") or "",
        "user_id": cookies.get("user_id") or cookies.get("ibox_auth_user_id") or "",
    }


def login_by_phone(phone, password, region=LOGIN_REGION, session=None, timeout=30, progress=None):
    """手机号 + 密码自动登录。

    返回 (cookies dict, account dict)；需要人工完成验证时抛 LoginManualRequired，
    其它失败抛 AceError。progress 回调用于把阶段文案回传界面。
    """
    def notify(text):
        if progress is not None:
            try:
                progress(text)
            except Exception:
                pass

    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    if len(digits) < 6:
        raise AceError("手机号格式不正确。", "请输入不含区号的手机号数字（至少 6 位）。")
    if not password:
        raise AceError("请输入登录密码。")

    session = session or _LoginSession(timeout)
    notify("正在获取 RSA 公钥…")
    public_key = fetch_login_public_key(session, timeout)
    notify("正在加密密码并提交登录…")
    encrypted = rsa_encrypt_password(public_key, password)

    payload = {
        "op_type": "phone_login",
        "app_id": LOGIN_APP_ID,
        "redirect_url": LOGIN_REDIRECT_URL,
        "phone": "+%s%s" % (region, digits) if region else digits,
        "password": encrypted,
    }
    status, text, location = session.post_json(LOGIN_URL_PHONE, payload)
    if location and status >= 300:
        # 服务端登录成功后通常以 302 → phonecallback?...&token=... 的方式下发登录态，
        # 手动跟随跳转链并逐跳收集 Set-Cookie，而不是当作失败。
        notify("登录提交成功，正在完成登录跳转…")
        try:
            session.follow_redirects(location, referer=LOGIN_URL_PHONE)
        except AceError:
            raise
        except Exception as exc:
            raise AceError(
                "跟随登录跳转失败: %s" % exc,
                "请稍后重试；若持续出现，请到官网确认账号状态。",
            )
        token = (session.cookies.get("user_token")
                 or session.cookies.get("ibox_auth_user_token")
                 or session.cookies.get("ibox_auth_oa_token"))
        if token:
            return dict(session.cookies), _account_from_cookies(session.cookies)
        raise AceError(
            "登录跳转已完成，但未从 Cookie 中取得登录凭据（HTTP %s）。" % status,
            "账号密码可能正确，但登录态 Cookie 下发失败；"
            "请稍后重试，或到官网确认账号状态。",
        )

    try:
        data = json.loads(text)
    except Exception:
        if _looks_like_manual_verify(text):
            raise LoginManualRequired("登录需要人工完成验证（滑块 / 短信验证码），程序无法自动继续。")
        raise AceError("登录响应解析失败：返回内容不是 JSON。", text[:300])

    ret = data.get("ret")
    info = data.get("info")
    message = ""
    manual_flag = False
    if isinstance(info, dict):
        message = str(info.get("ret_msg") or info.get("msg") or info.get("message") or "")
        for key in ("captcha", "ticket", "randstr", "need_captcha", "verify_type", "verify", "sms"):
            if info.get(key):
                manual_flag = True
    elif isinstance(info, str):
        message = info
        manual_flag = _looks_like_manual_verify(info)

    if str(ret) in ("0",):
        cookies = dict(session.cookies)
        token = (cookies.get("user_token") or cookies.get("ibox_auth_user_token")
                 or cookies.get("ibox_auth_oa_token"))
        if not token:
            raise LoginManualRequired(
                "登录接口未返回登录凭据 Cookie（可能触发了人工验证环节）。"
            )
        return cookies, _account_from_cookies(cookies)

    if manual_flag or _looks_like_manual_verify(message):
        raise LoginManualRequired(
            "登录需要人工完成验证（滑块 / 短信验证码），程序无法自动继续。服务端返回：%s"
            % (message or "captcha required")
        )

    tips = {
        "-3": "该账号需要重置密码后才可登录，请先在官网完成重置再重试。",
        "-1": "手机号或密码不正确，请检查后重试。",
    }
    raise AceError(
        "登录失败（ret=%s）：%s" % (ret, message or "服务端未返回详细原因"),
        tips.get(str(ret), "请确认手机号 / 密码是否正确；多次失败可到官网确认账号状态。"),
    )


def write_credentials(cookies, account=None, note=None, backup=True):
    """把登录态 Cookie 写入 ace_session_credentials.json（旧文件先备份，再原子写入）。"""
    cookies = {k: v for k, v in dict(cookies or {}).items() if k}
    token = cookies.get("user_token") or cookies.get("ibox_auth_user_token")
    if not cookies or not token:
        raise AceError("登录结果缺少 user_token，未写入凭据文件。")

    payload = {}
    if os.path.isfile(CREDENTIALS_FILE):
        try:
            with open(CREDENTIALS_FILE, "r", encoding="utf-8") as f:
                old = json.load(f)
            if isinstance(old, dict):
                payload.update(old)
            if backup:
                shutil.copy2(CREDENTIALS_FILE, CREDENTIALS_FILE + ".bak")
        except Exception:
            pass

    merged_cookies = {}
    if isinstance(payload.get("cookies"), dict):
        merged_cookies.update(payload["cookies"])
    merged_cookies.update(cookies)
    payload["cookies"] = merged_cookies

    merged_account = dict(payload.get("account") or {}) if isinstance(payload.get("account"), dict) else {}
    merged_account.update({k: v for k, v in (account or {}).items() if v})
    payload["account"] = merged_account

    now = datetime.now()
    payload["domain"] = "www.anticheatexpert.com"
    payload["obtained_at"] = now.strftime("%Y-%m-%d")
    payload["note"] = note or (
        "由程序内置登录窗口（phone_login）自动写入；登录时间 "
        + now.strftime("%Y-%m-%d %H:%M:%S")
    )

    tmp_path = CREDENTIALS_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, CREDENTIALS_FILE)
    return CREDENTIALS_FILE


def _post_json(url, payload, trace_header, cookies):
    """向 ACE 接口 POST JSON 并返回解析后的 dict；网络 / 登录态异常统一转为 AceError。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": BASE_ORIGIN,
        "Referer": REFERER,
        "User-Agent": USER_AGENT,
        "ibox-request-trace": trace_header,
        "Cookie": build_cookie_header(cookies),
    }

    request = urllib.request.Request(url=url, data=body, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8", errors="replace")
            status = response.status
    except urllib.error.HTTPError as exc:
        code = exc.code
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
        if code in (401, 403):
            raise AceError(
                "接口返回 HTTP %d，登录凭据已失效或无访问权限。" % code,
                "请在登录窗口完成登录后重试（程序会自动更新凭据文件）。",
                auth=True,
            )
        raise AceError("接口请求失败，HTTP %d。" % code, detail or "服务端返回错误状态码。")
    except urllib.error.URLError as exc:
        raise AceError(
            "无法连接接口服务: %s" % exc.reason,
            "请检查本机网络 / 代理设置，或确认能否访问 www.anticheatexpert.com。",
        )
    except Exception as exc:
        raise AceError("请求过程发生异常: %s" % exc)

    try:
        data = json.loads(raw)
    except Exception:
        if status in (301, 302) or "<html" in raw.lower():
            raise AceError(
                "响应不是 JSON（可能是被重定向到登录页），登录态可能已失效。",
                "请在登录窗口完成登录后重试（程序会自动更新凭据文件）。",
                auth=True,
            )
        raise AceError("响应解析失败，返回内容非合法 JSON。", raw[:300])

    info = data.get("info") or {}
    top_ret = data.get("ret")
    biz_ret = info.get("ret_code")
    if top_ret not in (0, None) or biz_ret not in (0, None):
        msg = info.get("ret_msg") or ""
        tip = "该返回码通常表示登录态失效、无权限或参数错误，请检查凭据或稍后重试。"
        auth = False
        if msg and ("登录" in msg or "token" in msg.lower() or "auth" in msg.lower()):
            tip = "请在登录窗口完成登录后重试（程序会自动更新凭据文件）。"
            auth = True
        raise AceError(
            "接口返回错误，ret=%s / ret_code=%s / ret_msg=%s" % (top_ret, biz_ret, msg or "无"),
            tip,
            auth=auth,
        )
    return data


def query_openid(openid, begin_time, end_time, level, cookies):
    """调用「可疑用户核查」账号核查接口，返回响应 JSON（dict）。失败抛 AceError。"""
    query_string = "&".join("%s=%s" % (k, v) for k, v in QUERY_PARAMS.items())
    url = QUERY_URL + "?" + query_string

    payload = {
        "game_id": "467",
        "page_type": "user_check_v2",
        "openid": openid,
        "data_type": QUERY_PARAMS["data_type"],
        "begin_time": begin_time,
        "end_time": end_time,
        "level": level,
    }
    return _post_json(url, payload, TRACE_HEADER, cookies)


def _normalize_datetime(value, label, is_end):
    """把 YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS 规范成接口需要的完整时间字符串。"""
    value = (value or "").strip()
    if len(value) == 10:
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            raise AceError("%s 格式不正确: %s" % (label, value), "日期需为 YYYY-MM-DD，例如 2026-09-30。")
        return value + (" 23:59:59" if is_end else " 00:00:00")
    try:
        datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        raise AceError(
            "%s 格式不正确: %s" % (label, value),
            "时间需为 YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS。",
        )
    return value


def _years_back(dt, years):
    """按日历年份回溯（处理 2 月 29 日等边界）。"""
    try:
        return dt.replace(year=dt.year - years)
    except ValueError:
        return dt.replace(year=dt.year - years, day=28)


def dq_default_dates():
    """「数据查询」默认日期（YYYY-MM-DD）：当天回溯 10 年（同一日历日期）~ 当天。"""
    today = datetime.now()
    return (
        _years_back(today, DQ_LOOKBACK_YEARS).strftime("%Y-%m-%d"),
        today.strftime("%Y-%m-%d"),
    )


def resolve_dq_dates(begin_arg, end_arg):
    """「数据查询」时间范围解析：默认当天回溯 10 年，返回完整时间字符串。"""
    default_begin, default_end = dq_default_dates()
    begin_time = _normalize_datetime(begin_arg or default_begin, "开始日期", is_end=False)
    end_time = _normalize_datetime(end_arg or default_end, "结束日期", is_end=True)
    return begin_time, end_time


def dq_time_range():
    """「数据查询」默认时间范围：今天回溯 10 年的 00:00:00 ~ 今天的 23:59:59。"""
    return resolve_dq_dates(None, None)


def query_punish(openid, page, page_size, cookies, begin_time=None, end_time=None):
    """调用「数据查询」接口，返回 (总条数 count, 处罚记录列表 punish_list)。失败抛 AceError。"""
    query_string = "&".join("%s=%s" % (k, v) for k, v in DQ_QUERY_PARAMS.items())
    url = DQ_URL + "?" + query_string

    if not begin_time or not end_time:
        begin_time, end_time = dq_time_range()
    payload = {
        "game_id": "467",
        "page_type": "query_by_openid",
        "begin_time": begin_time,
        "end_time": end_time,
        "strategy_src": -1,
        "account_id": openid,
        "request_source": "ace",
        "page": page,
        "page_size": page_size,
    }

    data = _post_json(url, payload, DQ_TRACE_HEADER, cookies)
    info = data.get("info") or {}

    try:
        count = int(info.get("count") or 0)
    except (TypeError, ValueError):
        count = 0

    punish_list = info.get("punish_list") or []
    if not isinstance(punish_list, list):
        punish_list = []
    return count, punish_list


def extract_records(data, openid):
    """从响应中提取记录列表。"""
    info = data.get("info") or {}
    ret_data = info.get("ret_data") or []

    records = []
    for item in ret_data:
        if not isinstance(item, dict):
            continue
        for key, value in item.items():
            if not isinstance(value, list):
                continue
            records.extend(value)
    return records


def build_image_url(des):
    """把接口返回的截图路径拼成完整可访问 URL。"""
    des = (des or "").strip()
    if not des:
        return ""
    if des.lower().startswith(("http://", "https://")):
        return des
    if des.startswith("/"):
        return IMAGE_BASE + des
    return IMAGE_BASE + "/" + des


def download_image(url, cookies):
    """带登录凭据下载图片，返回原始字节。失败抛 AceError。"""
    headers = {
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        "Referer": REFERER,
        "Origin": BASE_ORIGIN,
        "User-Agent": USER_AGENT,
        "Cookie": build_cookie_header(cookies),
    }
    request = urllib.request.Request(url=url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            content_type = response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise AceError("截图下载失败（HTTP %d），登录凭据可能已失效。" % exc.code)
        raise AceError("截图下载失败（HTTP %d）。" % exc.code)
    except urllib.error.URLError as exc:
        raise AceError("截图下载网络异常: %s" % exc.reason)

    if not raw:
        raise AceError("截图下载返回空内容。")
    head = raw[:20].lstrip().lower()
    if "text/html" in content_type.lower() or head.startswith(b"<!doctype") or head.startswith(b"<html"):
        raise AceError("截图下载返回的是 HTML（可能登录态失效或无权限）。")
    return raw


# ---------------------------------------------------------------------------
# 命令行（CLI）模式 —— 保留旧用法
# ---------------------------------------------------------------------------
def resolve_dates(begin_arg, end_arg):
    today = datetime.now()
    end_time = end_arg or today.strftime("%Y-%m-%d")
    begin_time = begin_arg or (today - timedelta(days=29)).strftime("%Y-%m-%d")

    for label, value in (("开始日期", begin_time), ("结束日期", end_time)):
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            raise AceError("%s 格式不正确: %s" % (label, value), "日期需为 YYYY-MM-DD，例如 2026-09-24。")
    return begin_time, end_time


def run_cli(args):
    openid = clean_openid(args.openid)
    if not openid:
        try:
            openid = clean_openid(input("请输入需要查询的 OpenID: "))
        except (EOFError, KeyboardInterrupt):
            print()
            raise AceError("未输入 OpenID，已退出。")
    if not openid:
        raise AceError("OpenID 不能为空。", "示例: python ace_user_check.py 12376323")

    begin_time, end_time = resolve_dates(args.begin, args.end)
    creds, cookies = load_credentials()

    print("=" * 68)
    print("ACE 控制台「账号核查」OpenID 查询")
    print("=" * 68)
    print("查询 OpenID : %s" % openid)
    print("时间范围    : %s ~ %s" % (begin_time, end_time))
    print("可疑级别    : %s" % args.level)
    print("登录账号    : %s" % (creds.get("account", {}).get("user_name", "未知")))
    print("-" * 68)

    data = query_openid(openid, begin_time, end_time, args.level, cookies)

    if args.dump_json:
        print("[原始响应 JSON]")
        print(json.dumps(data, ensure_ascii=False, indent=2))
        print("-" * 68)

    records = extract_records(data, openid)
    if not records:
        print("查询完成：未查询到该 OpenID 的数据记录（可能该用户在所选时间范围内无记录）。")
        print("建议：适当放宽 --begin / --end 时间范围，或将 --level 设为 all 后重试。")
        return

    rows = []
    for rec in records:
        row = []
        for _, field, transform in COLUMNS:
            value = transform(rec) if transform else rec.get(field, "")
            if field == "des":
                value = truncate(value, args.des_max if args.des_max else 0)
            row.append(value if value not in (None, "") else "-")
        rows.append(row)

    print("查询成功，共 %d 条记录：" % len(records))
    print()
    print_table([c[0] for c in COLUMNS], rows)
    print()
    print("数据类型说明: 截图=screenshot, 样本=sample, 行为=behavior, 处罚=punish；可疑级别: 高/中/低。")


# ---------------------------------------------------------------------------
# Material Design 视觉规范（纯 tkinter / ttk 实现，不改变任何业务行为）
# ---------------------------------------------------------------------------
FONT_FAMILY = "Microsoft YaHei UI"  # 统一字体族（中文界面主力字体）

MATERIAL = {
    # —— 主色系（Primary，M3 Blue 基调）——
    "primary": "#1565C0",
    "primary_dark": "#0D47A1",
    "primary_light": "#E3F2FD",
    "primary_container": "#D6E4FF",     # 主色容器（选中底 / 强调块）
    "accent": "#FF4081",
    "on_primary": "#FFFFFF",
    "on_primary_dim": "#CBDCF5",        # 主色底上的次级文字

    # —— Surface 容器层级（lowest -> highest，越靠上越"高"）——
    "surface_container_lowest": "#FFFFFF",  # 卡片 / 弹窗表面
    "surface_container_low": "#F7F9FC",     # 区块底 / 表头
    "surface_container": "#EFF3F9",         # 填充式输入框
    "surface_container_high": "#E7EDF6",    # 悬停底
    "surface_container_highest": "#DEE6F2", # 选中 / 激活底
    "surface": "#FFFFFF",          # 卡片 / 输入区表面
    "surface_variant": "#EFF3F9",  # 填充式输入框底色
    "background": "#F2F5FA",       # 页面底色
    "row_alt": "#F8FAFD",          # 表格隔行
    "row_hover": "#EDF3FB",
    "appbar_bg": "#E8F0FB",        # 应用栏底色（浅主色面，替代实心主色横幅）

    # —— 中性灰阶（文字 / 描边 / 分隔）——
    "on_surface": "#1B1F26",            # 正文 / 标题
    "on_surface_variant": "#5A6473",    # 次级正文
    "secondary_text": "#6B7280",        # 辅助说明
    "outline": "#D3DAE3",               # 描边（控件边界）
    "outline_variant": "#E3E8EF",       # 弱描边
    "divider": "#E6EAF0",
    "shadow": "#C9D2DE",           # 纯 tk 无真实阴影，用偏移描边模拟层次

    # —— 状态色（含容器色）——
    "error": "#C0362C",
    "error_container": "#FDECEA",
    "warning": "#B26A00",
    "warning_container": "#FFF4E5",
    "success": "#2E7D32",
    "success_container": "#E8F5E9",
    "info": "#1565C0",
    "disabled": "#B9C2CC",              # 禁用部件底
    "on_disabled": "#FFFFFF",           # 禁用部件文字

    # —— 专用部件色 ——
    "scrollbar": "#C6CDD8",
    "scrollbar_hover": "#A8B2C0",
    "scrollbar_active": "#7C8798",      # 滚动条拖动中
    "thumb_bg": "#ECF0F5",              # 缩略图留白底
    "viewer_bg": "#1B1F26",             # 原图查看窗口底
    "chip_bg": "#E3EEFC",
    "chip_fg": "#0D47A1",
    "level": {
        "high": ("#FDECEA", "#C0362C"),
        "middle": ("#FFF4E5", "#B26A00"),
        "low": ("#E8F5E9", "#2E7D32"),
    },
}


def md_font(size, weight="normal"):
    """Material 字体层级：统一字体族 + 字号 / 字重分层。"""
    return (FONT_FAMILY, size, weight)


def install_material_styles(root):
    """把 ttk 默认外观替换为 Material 风格（扁平、主色强调、表面色分层）。"""
    from tkinter import ttk

    c = MATERIAL
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except Exception:
        pass

    # —— 容器 / 文本 ——
    style.configure("TFrame", background=c["background"])
    style.configure("Surface.TFrame", background=c["surface"])
    style.configure("Field.TFrame", background=c["surface_variant"])
    style.configure("TLabel", background=c["background"], foreground=c["on_surface"], font=md_font(10))
    style.configure("Caption.TLabel", background=c["background"],
                    foreground=c["secondary_text"], font=md_font(9))
    style.configure("SurfaceCaption.TLabel", background=c["surface"],
                    foreground=c["secondary_text"], font=md_font(9))
    style.configure("Status.TLabel", background=c["background"],
                    foreground=c["secondary_text"], font=md_font(10))

    # —— 按钮：主按钮（实心主色）/ 描边按钮 / 文本按钮 ——
    style.configure(
        "Primary.TButton",
        background=c["primary"], foreground=c["on_primary"],
        borderwidth=0, focusthickness=0, focuscolor=c["primary"],
        relief="flat", padding=(22, 10), font=md_font(12, "bold"),
    )
    style.map(
        "Primary.TButton",
        background=[("pressed", c["primary_dark"]), ("active", c["primary_dark"]),
                    ("disabled", c["disabled"])],
        foreground=[("disabled", c["on_disabled"])],
    )
    style.configure(
        "Outlined.TButton",
        background=c["surface"], foreground=c["primary"],
        bordercolor=c["outline"], borderwidth=1, focusthickness=0, focuscolor=c["surface"],
        relief="flat", padding=(16, 8), font=md_font(10),
    )
    style.map(
        "Outlined.TButton",
        background=[("pressed", c["primary_light"]), ("active", c["primary_light"]),
                    ("disabled", c["surface"])],
        foreground=[("disabled", c["disabled"])],
        bordercolor=[("disabled", c["outline_variant"]), ("active", c["primary"])],
    )
    style.configure(
        "Text.TButton",
        background=c["surface"], foreground=c["primary"],
        borderwidth=0, focusthickness=0, focuscolor=c["surface"],
        relief="flat", padding=(10, 4), font=md_font(10),
    )
    style.map(
        "Text.TButton",
        background=[("pressed", c["primary_light"]), ("active", c["primary_light"]),
                    ("disabled", c["surface"])],
        foreground=[("disabled", c["disabled"])],
    )

    # 顶栏（主色底）上的文字按钮：登录 / 重新登录入口
    style.configure(
        "Appbar.TButton",
        background=c["primary"], foreground=c["on_primary"],
        bordercolor=c["primary"], borderwidth=0, focusthickness=0, focuscolor=c["primary"],
        relief="flat", padding=(14, 6), font=md_font(10),
    )
    style.map(
        "Appbar.TButton",
        background=[("pressed", c["primary_dark"]), ("active", c["primary_dark"]),
                    ("disabled", c["primary"])],
        foreground=[("disabled", c["on_primary_dim"])],
    )

    # —— 输入控件：填充式（浅色底 + 下划线） / 下拉 ——
    style.configure(
        "Material.TEntry",
        fieldbackground=c["surface_variant"], background=c["surface_variant"],
        foreground=c["on_surface"], bordercolor=c["surface_variant"],
        lightcolor=c["surface_variant"], darkcolor=c["surface_variant"],
        insertcolor=c["primary"], borderwidth=0, relief="flat", padding=(8, 6),
    )
    style.configure(
        "Material.TCombobox",
        fieldbackground=c["surface_variant"], background=c["surface_variant"],
        foreground=c["on_surface"], bordercolor=c["surface_variant"],
        lightcolor=c["surface_variant"], darkcolor=c["surface_variant"],
        arrowcolor=c["secondary_text"], borderwidth=0, relief="flat", padding=(8, 5),
    )
    style.map(
        "Material.TCombobox",
        fieldbackground=[("readonly", c["surface_variant"]), ("disabled", c["surface_variant"])],
        foreground=[("readonly", c["on_surface"])],
        bordercolor=[("focus", c["primary"])],
    )

    # —— 数据表格：砍掉原生外框 / 网格感（只保留 treearea），表头做成 M3 浅色容器行 ——
    style.layout("Material.Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
    style.configure(
        "Material.Treeview",
        background=c["surface"], fieldbackground=c["surface"], foreground=c["on_surface"],
        borderwidth=0, relief="flat", rowheight=38, font=md_font(10), padding=0,
    )
    style.map(
        "Material.Treeview",
        background=[("selected", c["primary_container"])],
        foreground=[("selected", c["primary_dark"])],
    )
    style.configure(
        "Material.Treeview.Heading",
        background=c["surface_container_low"], foreground=c["on_surface_variant"],
        font=md_font(10, "bold"), relief="flat", borderwidth=0,
        bordercolor=c["surface_container_low"], lightcolor=c["surface_container_low"],
        darkcolor=c["surface_container_low"], padding=(14, 11),
    )
    style.map(
        "Material.Treeview.Heading",
        background=[("active", c["primary_container"]), ("pressed", c["primary_container"])],
        foreground=[("active", c["primary_dark"])],
    )

    # —— 滚动条：ttk 兜底样式（主界面统一改用 Canvas 自绘的细胶囊滚动条）——
    style.configure(
        "Material.Vertical.TScrollbar",
        background=c["scrollbar"], troughcolor=c["background"], bordercolor=c["background"],
        arrowcolor=c["secondary_text"], borderwidth=0, relief="flat", arrowsize=12,
    )
    style.map("Material.Vertical.TScrollbar", background=[("active", c["scrollbar_hover"])])


# ---------------------------------------------------------------------------
# 圆角 + 柔和投影：Canvas 自绘控件层
# ---------------------------------------------------------------------------
# 说明：tk/ttk 原生控件不支持圆角，这里用 Canvas + Pillow 预渲染「圆角矩形 + 高斯
# 模糊投影」贴图，再叠加子控件实现；对外接口尽量与 ttk 控件一致（text / state /
# command / config / invoke），因此调用点可以无感替换。
# —— 全局圆角刻度：按层级递减（卡片 > 输入框 = 按钮），避免同类控件圆角忽大忽小 ——
# 说明：圆角在贴图阶段会按矩形短边自动收敛，因此这里可以放心取偏大值。
RADIUS_CARD = 16
RADIUS_FIELD = 12
RADIUS_BUTTON = 12
CARD_OUTLINE = True  # 卡片加一道极浅描边：投影变轻后仍能读出卡片边界（M3 outlined 卡片）

# —— 全局间距刻度：区块内外边距统一走这套数值，避免 12/14/16/20 混用导致的错位感 ——
SPACE_XXS = 2
SPACE_XS = 4
SPACE_S = 8
SPACE_M = 12
SPACE_L = 16
SPACE_XL = 20

# 卡片内容内边距：全站卡片（条件卡 / 分页条 / 表格 / 记录卡 / 弹窗表单）统一取这一组值，
# 避免 12 / 14 / 16 / 18 混用带来的错位感；区块标题文字左边缘与之对齐。
CARD_PAD_X = SPACE_L
CARD_PAD_Y = SPACE_M

# —— 输入框（_field）统一规格：顶部 OpenID 框与登录弹窗输入框共用同一套内边距 / 圆角 / 字号，
# 仅宽度（字符数）不同，保证同类输入框高度、内边距完全一致。 ——
FIELD_PAD_X = 6            # 输入控件在圆角输入区内的水平内边距（px）
FIELD_PAD_Y = 5            # 输入控件在圆角输入区内的垂直内边距（px）
FIELD_INSET = SPACE_XS     # 圆角输入区与内部承载框的间距（px）

# —— Elevation 分级（M3）：同一层级共用同一档阴影，卡片轻、浮层重 ——
# 档位 -> (阴影留白 pad, 高斯模糊半径, 阴影不透明度 0-255, 垂直偏移 dy)
# 约定：blur ≈ 0.55 × pad，保证模糊范围落在留白内，投影不会被画布边缘裁出硬边。
ELEVATION_SPECS = {
    "flat": (0, 0, 0, 0),           # 平面：区块标题栏 / 分隔线 / 无投影按钮
    "card": (9, 5, 30, 2),          # 卡片：极轻，仅把边界从底色上托起来
    "raised": (12, 7, 44, 3),       # 抬升：主按钮等强调控件
    "overlay": (20, 11, 60, 6),     # 浮层：弹窗内的浮动面板
}


def elevation_shadow(level="card", pad=None):
    """按 Elevation 档位返回 (shadow_pad, shadow_blur, shadow_alpha, shadow_offset)。

    控件自带留白（如按钮）时传入 pad，模糊 / 透明度 / 偏移按比例收敛，
    既保持同一档位的手感，又避免留白不足导致投影被裁切。
    """
    base_pad, blur, alpha, dy = ELEVATION_SPECS.get(level, ELEVATION_SPECS["card"])
    if pad is None:
        pad = base_pad
    pad = int(pad)
    if pad <= 0 or base_pad <= 0:
        return pad, 0, 0, (0, 0)
    ratio = pad / float(base_pad)
    return (
        pad,
        max(1, int(round(blur * ratio))),
        max(0, min(72, int(round(alpha * ratio)))),
        (0, max(0, int(round(dy * ratio)))),
    )


CARD_SHADOW_PAD = elevation_shadow("card")[0]  # 卡片阴影留白（区块标题与卡片靠它对位）

_IMAGE_CACHE = {}
_IMAGE_CACHE_LIMIT = 900
_UI_CLASSES = {}

# 按钮样式表：键名沿用原 ttk 样式名，保证调用点替换后语义一致
BUTTON_SPECS = {
    "Primary.TButton": {
        "pad": (22, 7), "radius": RADIUS_BUTTON, "shadow": 5,
        "font": (FONT_FAMILY, 12, "bold"),
        "fill": "primary", "fg": "on_primary", "hover_fill": "primary_dark",
        "disabled_fill": "disabled", "disabled_fg": "on_disabled",
    },
    "Outlined.TButton": {
        "pad": (16, 8), "radius": RADIUS_BUTTON, "shadow": 2,
        "font": (FONT_FAMILY, 10, "normal"),
        "fill": "surface", "fg": "primary", "border": "outline", "border_width": 1,
        "hover_fill": "primary_light", "hover_border": "primary",
        "disabled_fill": "surface", "disabled_fg": "disabled",
        "disabled_border": "outline_variant",
    },
    "Text.TButton": {
        "pad": (12, 6), "radius": RADIUS_BUTTON, "shadow": 0,
        "font": (FONT_FAMILY, 10, "normal"),
        "fill": None, "fg": "primary", "hover_fill": "primary_light",
        "disabled_fill": None, "disabled_fg": "disabled",
    },
    "Appbar.TButton": {
        "pad": (14, 6), "radius": RADIUS_BUTTON, "shadow": 0,
        "font": (FONT_FAMILY, 10, "normal"),
        "fill": "primary", "fg": "on_primary", "hover_fill": "primary_dark",
        "disabled_fill": "primary", "disabled_fg": "on_primary_dim",
    },
}


def _hex_to_rgb(value):
    """#RRGGBB / #RGB → (r, g, b)。"""
    value = (value or "#000000").lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def _resample_filter():
    from PIL import Image
    return getattr(getattr(Image, "Resampling", Image), "LANCZOS", 1)


def _parent_bg(parent, default):
    """取父容器底色，供圆角贴图做背景合成（避免边缘出现色差方框）。"""
    try:
        value = parent.cget("background")
        if isinstance(value, str) and value.strip():
            return value
    except Exception:
        pass
    return default


def ui_rounded_image(width, height, radius, fill, background, border=None, border_width=1,
                     shadow_pad=0, shadow_offset=(0, 2), shadow_blur=5, shadow_alpha=52):
    """生成「圆角矩形 + 柔和投影」贴图（LRU 命中即复用），fill 为 None 时只画投影/描边。"""
    key = (width, height, radius, fill, background, border, border_width,
           shadow_pad, shadow_offset, shadow_blur, shadow_alpha)
    cached = _IMAGE_CACHE.get(key)
    if cached is not None:
        return cached
    if width < 2 or height < 2:
        return None

    from PIL import Image, ImageDraw, ImageFilter, ImageTk

    pad = max(0, int(shadow_pad))
    x0, y0 = pad, pad
    x1, y1 = width - pad - 1, height - pad - 1
    if x1 - x0 < 2 or y1 - y0 < 2:
        return None
    radius = max(0, min(int(radius), (x1 - x0) // 2, (y1 - y0) // 2))

    canvas_img = Image.new("RGB", (width, height), _hex_to_rgb(background))
    if pad > 0 and shadow_alpha > 0:
        shadow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        ImageDraw.Draw(shadow).rounded_rectangle(
            [x0 + shadow_offset[0], y0 + shadow_offset[1],
             x1 + shadow_offset[0], y1 + shadow_offset[1]],
            radius=radius, fill=(15, 23, 42, int(shadow_alpha)),
        )
        canvas_img = Image.alpha_composite(
            canvas_img.convert("RGBA"), shadow.filter(ImageFilter.GaussianBlur(shadow_blur))
        ).convert("RGB")

    if fill is not None or (border is not None and border_width):
        scale = 3
        shape = Image.new("RGBA", (width * scale, height * scale), (0, 0, 0, 0))
        draw = ImageDraw.Draw(shape)
        box = [x0 * scale, y0 * scale, x1 * scale + 2, y1 * scale + 2]
        if fill is not None:
            draw.rounded_rectangle(box, radius=radius * scale, fill=_hex_to_rgb(fill) + (255,))
        if border is not None and border_width:
            draw.rounded_rectangle(box, radius=radius * scale,
                                   outline=_hex_to_rgb(border) + (255,),
                                   width=max(1, int(border_width)) * scale)
        canvas_img = Image.alpha_composite(
            canvas_img.convert("RGBA"),
            shape.resize((width, height), _resample_filter()),
        ).convert("RGB")

    image = ImageTk.PhotoImage(canvas_img)
    if len(_IMAGE_CACHE) > _IMAGE_CACHE_LIMIT:
        _IMAGE_CACHE.clear()
    _IMAGE_CACHE[key] = image
    return image


def _ui_classes():
    """延迟构建 Canvas 自绘控件类（CLI 模式不会 import tkinter）。"""
    if _UI_CLASSES:
        return _UI_CLASSES

    import tkinter as tk
    import tkinter.font as tkfont

    _fonts = {}

    def _font_object(spec):
        obj = _fonts.get(spec)
        if obj is None:
            obj = tkfont.Font(family=spec[0], size=spec[1], weight=spec[2])
            _fonts[spec] = obj
        return obj

    def _measure(spec, text):
        """返回文字的（像素宽, 像素行高），与 Tk 实际渲染尺寸一致（含 DPI 缩放）。"""
        obj = _font_object(spec)
        return obj.measure(text), obj.metrics("linespace")

    class MDSurface(tk.Canvas):
        """圆角表面容器：支持承载单个子控件（自动撑高）或作为纯装饰色块。"""

        def __init__(self, parent, colors, radius=RADIUS_CARD, fill=None, bg=None, border=None,
                     border_width=0, shadow_pad=0, height=None, width=None, elevation=None,
                     shadow_blur=None, shadow_alpha=None, shadow_offset=None, **kw):
            self.colors = colors
            self._bg = bg or _parent_bg(parent, colors["background"])
            self._fill = fill if fill is not None else colors["surface"]
            self._border = border
            self._border_width = border_width
            self._radius = radius
            # Elevation 分级：给定档位即套用统一的留白 / 模糊 / 透明度 / 偏移，
            # 单项仍可显式覆盖（按钮等控件另有自己的档位微调）
            if elevation:
                pad, blur, alpha, offset = elevation_shadow(elevation)
                if not shadow_pad:
                    shadow_pad = pad
                if shadow_blur is None:
                    shadow_blur = blur
                if shadow_alpha is None:
                    shadow_alpha = alpha
                if shadow_offset is None:
                    shadow_offset = offset
            self._shadow_pad = shadow_pad
            self._shadow_blur = 5 if shadow_blur is None else shadow_blur
            self._shadow_alpha = 52 if shadow_alpha is None else shadow_alpha
            self._shadow_offset = (0, 2) if shadow_offset is None else shadow_offset
            self._fixed_height = height
            self._child = None
            self._child_window = None
            self._inset = 0
            self._mode = "x"
            self._syncing = False
            self._photo = None
            tk.Canvas.__init__(self, parent, bg=self._bg, bd=0, highlightthickness=0,
                               relief="flat", takefocus=0,
                               width=(width or 1), height=(height or 24), **kw)
            self._image_item = self.create_image(0, 0, anchor="nw")
            self.bind("<Configure>", self._on_configure)
            self._redraw()

        # -- 绘制 --------------------------------------------------------
        def _redraw(self):
            width = self.winfo_width()
            height = self.winfo_height()
            if width <= 1 or height <= 1:  # 尚未完成首次布局时用请求尺寸兜底
                width = max(1, self.winfo_reqwidth())
                height = max(1, self.winfo_reqheight())
            image = ui_rounded_image(width, height, self._radius, self._fill, self._bg,
                                     self._border, self._border_width, self._shadow_pad,
                                     self._shadow_offset, self._shadow_blur, self._shadow_alpha)
            self._photo = image
            self.itemconfigure(self._image_item, image=(image if image is not None else ""))

        def _on_configure(self, event=None):
            self._redraw()
            self._sync_child()

        # -- 子控件 ------------------------------------------------------
        def attach(self, child, inset=8, mode="x"):
            """把子控件放进圆角表面内部；mode="x" 时表面高度随内容增长。"""
            self._child = child
            self._inset = inset
            self._mode = mode
            self._child_window = self.create_window(inset, inset, anchor="nw", window=child)
            child.bind("<Configure>", self._on_child_configure, add="+")
            self._sync_child()
            return child

        def _on_child_configure(self, event=None):
            if self._syncing or self._mode != "x" or self._fixed_height is not None:
                return
            want = self._child.winfo_reqheight() + 2 * self._inset
            if abs(want - self.winfo_height()) > 1:
                self._syncing = True
                try:
                    self.configure(height=want)
                finally:
                    self._syncing = False

        def _sync_child(self):
            if self._child is None or self._child_window is None or self._syncing:
                return
            self._syncing = True
            try:
                self.itemconfigure(self._child_window,
                                   width=max(1, self.winfo_width() - 2 * self._inset))
                if self._mode == "fill":
                    self.itemconfigure(self._child_window,
                                       height=max(1, self.winfo_height() - 2 * self._inset))
                elif self._fixed_height is None:
                    want = self._child.winfo_reqheight() + 2 * self._inset
                    if abs(want - self.winfo_height()) > 1:
                        self.configure(height=want)
            finally:
                self._syncing = False

        # -- 样式 --------------------------------------------------------
        def set_style(self, fill=None, border=None, border_width=None, radius=None, bg=None):
            if fill is not None:
                self._fill = fill
            if border_width is not None:
                self._border_width = border_width
            elif border is not None and not self._border_width:
                self._border_width = 1
            if border is not None:
                self._border = border
            if radius is not None:
                self._radius = radius
            if bg is not None and bg != self._bg:
                self._bg = bg
                tk.Canvas.configure(self, bg=bg)
            self._redraw()

    class MDButton(tk.Canvas):
        """圆角按钮：接口兼容 ttk.Button（text / state / command / config / invoke）。"""

        def __init__(self, parent, colors, text="", style="Primary.TButton", command=None,
                     state="normal", **kw):
            self.colors = colors
            self._style = style if style in BUTTON_SPECS else "Text.TButton"
            self._spec = BUTTON_SPECS[self._style]
            self._bg = _parent_bg(parent, colors["background"])
            self._text = str(text)
            self._command = command
            self._state = str(state or "normal")
            self._hover = False
            self._pressed = False
            self._photo = None
            pad_x, pad_y = self._spec["pad"]
            self._pad_x, self._pad_y = pad_x, pad_y
            self._radius = self._spec["radius"]
            self._shadow_pad = self._spec["shadow"]
            # 按钮投影同样走 Elevation 分级：有投影的按钮统一 raised 档，
            # 无投影的（文字按钮 / 顶栏按钮）走 flat 档，避免同类控件各用一套模糊值
            self._elevation = self._spec.get("elevation") or (
                "raised" if self._shadow_pad else "flat")
            _, self._shadow_blur, self._shadow_alpha, self._shadow_offset = \
                elevation_shadow(self._elevation, self._shadow_pad)
            tk.Canvas.__init__(self, parent, bg=self._bg, bd=0, highlightthickness=0,
                               relief="flat", takefocus=0,
                               width=self._natural_width(), height=self._natural_height(), **kw)
            self._image_item = self.create_image(0, 0, anchor="nw")
            self._text_item = self.create_text(0, 0, text=self._text, anchor="center",
                                               font=self._spec["font"])
            self.bind("<Configure>", lambda e: self._redraw())
            self.bind("<Enter>", self._on_enter)
            self.bind("<Leave>", self._on_leave)
            self.bind("<Button-1>", self._on_press)
            self.bind("<ButtonRelease-1>", self._on_release)
            self._redraw()

        # -- 尺寸 --------------------------------------------------------
        def _natural_width(self):
            text_w, _ = _measure(self._spec["font"], self._text or " ")
            return int(text_w + 2 * self._pad_x + 2 * self._shadow_pad)

        def _natural_height(self):
            _, line_h = _measure(self._spec["font"], self._text or " ")
            return int(line_h + 2 * self._pad_y + 2 * self._shadow_pad)

        # -- 配色 --------------------------------------------------------
        def _tokens(self):
            spec, colors = self._spec, self.colors
            disabled = self._state == "disabled"
            if disabled:
                fill = spec.get("disabled_fill")
                fg = spec.get("disabled_fg") or spec.get("fg")
                border = spec.get("disabled_border", spec.get("border"))
            else:
                active = self._hover or self._pressed
                fill = spec.get("hover_fill") if active else spec.get("fill")
                fg = spec.get("fg")
                border = (spec.get("hover_border") if active else spec.get("border")) \
                    or spec.get("border")
            border_width = spec.get("border_width", 1) if border else 0
            return (colors.get(fill) if fill else None, colors.get(fg, colors["on_surface"]),
                    colors.get(border) if border else None, border_width)

        def _redraw(self):
            width = self.winfo_width() if self.winfo_width() > 1 else self._natural_width()
            height = self.winfo_height() if self.winfo_height() > 1 else self._natural_height()
            fill, fg, border, border_width = self._tokens()
            image = ui_rounded_image(width, height, self._radius, fill, self._bg, border,
                                     border_width, self._shadow_pad,
                                     self._shadow_offset, self._shadow_blur,
                                     self._shadow_alpha)
            self._photo = image
            self.itemconfigure(self._image_item, image=(image if image is not None else ""))
            self.itemconfigure(self._text_item, text=self._text, fill=fg, state="normal")
            self.coords(self._text_item, width / 2.0, height / 2.0)

        # -- 交互 --------------------------------------------------------
        def _on_enter(self, event=None):
            if self._state == "disabled":
                return
            self._hover = True
            try:
                self.configure(cursor="hand2")
            except Exception:
                pass
            self._redraw()

        def _on_leave(self, event=None):
            self._hover = False
            self._pressed = False
            self._redraw()

        def _on_press(self, event=None):
            if self._state == "disabled":
                return
            self._pressed = True
            self._redraw()

        def _on_release(self, event=None):
            if self._state == "disabled":
                return
            clicked = self._pressed
            self._pressed = False
            self._redraw()
            inside = True
            if event is not None:
                inside = 0 <= event.x <= self.winfo_width() and 0 <= event.y <= self.winfo_height()
            if clicked and inside and callable(self._command):
                self._command()

        def invoke(self):
            if self._state != "disabled" and callable(self._command):
                self._command()

        # -- 兼容 ttk.Button 的取值 / 赋值 -------------------------------
        def _apply_text(self):
            self.itemconfigure(self._text_item, text=self._text)

        def configure(self, cnf=None, **kw):
            if cnf:
                if isinstance(cnf, dict):
                    kw = dict(cnf, **kw)
                else:
                    return tk.Canvas.configure(self, cnf, **kw)
            rebuild = False
            for key in ("text", "state", "command"):
                if key in kw:
                    value = kw.pop(key)
                    if key == "text":
                        self._text = str(value)
                        self._apply_text()
                        rebuild = True
                    elif key == "state":
                        self._state = str(value)
                        self._hover = False
                        self._pressed = False
                        rebuild = True
                    else:
                        self._command = value
            if kw:
                tk.Canvas.configure(self, **kw)
            if rebuild:
                tk.Canvas.configure(self, width=self._natural_width(),
                                    height=self._natural_height())
                self._redraw()
            return None

        config = configure

        def cget(self, key):
            if key == "text":
                return self._text
            if key == "state":
                return self._state
            return tk.Canvas.cget(self, key)

        # tkinter 的 Misc 把 __getitem__ 直接绑定到 cget 函数，子类覆写 cget 不生效，
        # 因此这里显式转发，保证 widget["text"] / widget["state"] 可用。
        def __getitem__(self, key):
            return self.cget(key)

    class MDChip(tk.Canvas):
        """圆角标签片（chip）：浅色容器底 + 强调色文字，用于数据类型 / 可疑级别。"""

        def __init__(self, parent, colors, text, fg=None, bg=None, font=None, **kw):
            self.colors = colors
            self._bg = _parent_bg(parent, colors["surface"])
            self._fill = bg or colors["chip_bg"]
            self._fg = fg or colors["chip_fg"]
            self._text = str(text)
            self._font = font or (FONT_FAMILY, 9, "normal")
            text_w, line_h = _measure(self._font, self._text)
            pad_x, pad_y = 10, 3
            width = int(text_w + 2 * pad_x)
            height = int(line_h + 2 * pad_y)
            self._radius = max(2, height // 2)
            self._photo = None
            tk.Canvas.__init__(self, parent, bg=self._bg, bd=0, highlightthickness=0,
                               relief="flat", takefocus=0, width=width, height=height, **kw)
            self._image_item = self.create_image(0, 0, anchor="nw")
            self._text_item = self.create_text(0, 0, text=self._text, anchor="center",
                                               fill=self._fg, font=self._font)
            self.bind("<Configure>", lambda e: self._redraw())
            self._redraw()

        def _redraw(self):
            width = self.winfo_width() if self.winfo_width() > 1 else int(self["width"])
            height = self.winfo_height() if self.winfo_height() > 1 else int(self["height"])
            image = ui_rounded_image(width, height, self._radius, self._fill, self._bg)
            self._photo = image
            self.itemconfigure(self._image_item, image=(image if image is not None else ""))
            self.coords(self._text_item, width / 2.0, height / 2.0)

        def configure(self, cnf=None, **kw):
            text = None
            if isinstance(cnf, dict) and "text" in cnf:
                text = cnf.pop("text")
            if "text" in kw:
                text = kw.pop("text")
            if cnf:
                tk.Canvas.configure(self, cnf)
            if kw:
                tk.Canvas.configure(self, **kw)
            if text is not None:
                self._text = str(text)
                self.itemconfigure(self._text_item, text=self._text)
                _, line_h = _measure(self._font, self._text)
                text_w, _ = _measure(self._font, self._text)
                tk.Canvas.configure(self, width=int(text_w + 20), height=int(line_h + 6))
                self._redraw()
            return None

        config = configure

        def cget(self, key):
            if key == "text":
                return self._text
            if key == "fg":
                return self._fg
            return tk.Canvas.cget(self, key)

        # 同 MDButton：显式转发，保证 widget["text"] 可用
        def __getitem__(self, key):
            return self.cget(key)

    class MDScrollbar(tk.Canvas):
        """M3 细圆角胶囊滚动条：Canvas 自绘，悬停加粗 / 可拖动 / 可点击跳转，内容不满一屏时自动隐藏。"""

        def __init__(self, parent, colors, orient="vertical", command=None, thickness=12,
                     thumb_width=6, **kw):
            self.colors = colors
            self.orient = orient
            self._command = command
            self._thickness = int(thickness)
            self._thumb_width = int(thumb_width)
            self._first = 0.0
            self._last = 1.0
            self._hover = False
            self._dragging = False
            self._drag_offset = 0
            self._bg = _parent_bg(parent, colors["background"])
            self._photo = None
            vertical = orient == "vertical"
            tk.Canvas.__init__(
                self, parent, bg=self._bg, bd=0, highlightthickness=0, relief="flat",
                takefocus=0, width=(self._thickness if vertical else 1),
                height=(1 if vertical else self._thickness), **kw)
            self._image_item = self.create_image(0, 0, anchor="nw")
            self.bind("<Configure>", lambda _event: self._redraw())
            self.bind("<Enter>", lambda _event: self._set_hover(True))
            self.bind("<Leave>", lambda _event: self._set_hover(False))
            self.bind("<ButtonPress-1>", self._on_press)
            self.bind("<B1-Motion>", self._on_drag)
            self.bind("<ButtonRelease-1>", self._on_release)

        # -- 与滚动目标对接（作为 yscrollcommand / xscrollcommand 调用） ----
        def set(self, first, last):
            try:
                self._first, self._last = float(first), float(last)
            except (TypeError, ValueError):
                return
            self._redraw()

        # -- 几何计算 ------------------------------------------------------
        def _length(self):
            value = self.winfo_height() if self.orient == "vertical" else self.winfo_width()
            return max(0, int(value))

        def _axis(self, event):
            return event.y if self.orient == "vertical" else event.x

        def _span(self):
            """返回胶囊（起点, 终点, 粗细）；内容不足一屏或尚未布局时返回 None（隐藏）。"""
            length = self._length()
            if length <= 6 or (self._last - self._first) >= 0.999:
                return None
            thickness = self._thumb_width + (2 if (self._hover or self._dragging) else 0)
            start = int(self._first * length) + 2
            end = int(self._last * length) - 2
            if end - start < thickness:      # 内容极长时也保证拖得到
                center = (start + end) / 2.0
                start, end = int(center - thickness / 2.0), int(center + thickness / 2.0)
            start = max(2, min(start, length - thickness - 2))
            end = max(start + thickness, min(end, length - 2))
            return start, end, thickness

        def _redraw(self):
            span = self._span()
            if span is None:
                self._photo = None
                self.itemconfigure(self._image_item, image="")
                if self.cget("cursor"):
                    tk.Canvas.configure(self, cursor="")
                return
            start, end, thickness = span
            if self._dragging:
                fill = self.colors.get("scrollbar_active") or self.colors["scrollbar_hover"]
            elif self._hover:
                fill = self.colors["scrollbar_hover"]
            else:
                fill = self.colors["scrollbar"]
            if self.orient == "vertical":
                width, height = thickness, end - start
                x, y = (self._thickness - thickness) // 2, start
            else:
                width, height = end - start, thickness
                x, y = start, (self._thickness - thickness) // 2
            image = ui_rounded_image(width, height, max(2, min(width, height) // 2), fill, self._bg)
            self._photo = image
            self.itemconfigure(self._image_item, image=(image if image is not None else ""))
            self.coords(self._image_item, x, y)
            if not self.cget("cursor"):
                tk.Canvas.configure(self, cursor="hand2")

        def _set_hover(self, hover):
            if self._hover == hover:
                return
            self._hover = hover
            self._redraw()

        # -- 交互 ----------------------------------------------------------
        def _on_press(self, event):
            span = self._span()
            if span is None:
                return
            start, end, _ = span
            position = self._axis(event)
            if start <= position <= end:
                self._dragging = True
                self._drag_offset = position - start
            else:
                length = self._length()
                if length:
                    self._scroll_to(max(0.0, min(1.0, position / float(length))))
            self._redraw()

        def _on_drag(self, event):
            if not self._dragging:
                return
            span = self._span()
            length = self._length()
            if span is None or not length:
                return
            _, _, thickness = span
            top = self._axis(event) - self._drag_offset
            track = float(max(1, length - thickness - 4))
            self._scroll_to(max(0.0, min(1.0, (top - 2) / track)))

        def _on_release(self, _event=None):
            if self._dragging:
                self._dragging = False
                self._redraw()

        def _scroll_to(self, fraction):
            if self._command is not None:
                self._command("moveto", fraction)

    _UI_CLASSES.update({"tk": tk, "surface": MDSurface, "button": MDButton,
                        "chip": MDChip, "scrollbar": MDScrollbar,
                        "fonts": _font_object, "measure": _measure})
    return _UI_CLASSES


def ui_surface(parent, colors, **kwargs):
    """创建圆角表面容器。"""
    return _ui_classes()["surface"](parent, colors, **kwargs)


def ui_button(parent, colors, text="", style="Primary.TButton", command=None, state="normal",
              **kwargs):
    """创建圆角按钮（接口对齐 ttk.Button）。"""
    return _ui_classes()["button"](parent, colors, text=text, style=style, command=command,
                                   state=state, **kwargs)


def ui_chip(parent, colors, text, fg=None, bg=None, font=None, **kwargs):
    """创建圆角标签片。"""
    return _ui_classes()["chip"](parent, colors, text=text, fg=fg, bg=bg, font=font, **kwargs)


def ui_scrollbar(parent, colors, orient="vertical", command=None, **kwargs):
    """创建 M3 细胶囊滚动条（接口对齐 ttk.Scrollbar：set(first, last) + command）。"""
    return _ui_classes()["scrollbar"](parent, colors, orient=orient, command=command, **kwargs)


def ui_card(parent, colors, padx=0, pady=SPACE_S, mode="x", radius=RADIUS_CARD,
            elevation="card", shadow_pad=None, fill=None):
    """Material 卡片：圆角表面 + 柔和投影，返回内部内容 Frame。mode="fill" 时随父容器撑满。

    阴影统一走 elevation 档位（默认 card 档，极轻），不再由调用点各自传 shadow_pad，
    保证「卡片轻、浮层重」的层级关系全局一致。
    """
    classes = _ui_classes()
    if shadow_pad is None:
        shadow_pad = elevation_shadow(elevation)[0]
    host = classes["surface"](parent, colors, radius=radius, fill=fill or colors["surface"],
                              bg=_parent_bg(parent, colors["background"]),
                              elevation=elevation, shadow_pad=shadow_pad,
                              height=(None if mode == "x" else 60))
    host.pack(fill=("x" if mode == "x" else "both"), expand=(mode != "x"),
              padx=padx, pady=pady)
    body = classes["tk"].Frame(host, bg=fill or colors["surface"])
    host.attach(body, inset=shadow_pad, mode=mode)
    return body


# ---------------------------------------------------------------------------
# 图形界面（GUI）模式
# ---------------------------------------------------------------------------
class AceApp:
    """所有 Tk 操作都只在主线程执行；子线程通过 ui_queue 回传结果。"""

    def __init__(self, root, args):
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.root = root
        self.args = args
        self.autotest_mode = bool(
            getattr(args, "autotest", None)
            or getattr(args, "autotest_dq", None)
            or getattr(args, "autotest_batch", None)
        )

        # Material 配色与字体层级
        self.colors = dict(MATERIAL)
        self.fonts = {
            "appbar": md_font(22, "bold"),       # 页面主标题
            "appbar_sub": md_font(10),           # 顶栏副标题
            "tab": md_font(12),
            "tab_active": md_font(12, "bold"),
            "caption": md_font(9),               # 辅助说明
            "field": md_font(12),                # 输入框正文
            "body": md_font(10),                 # 正文
            "body_bold": md_font(12, "bold"),    # 卡片主信息
            "section": md_font(15, "bold"),      # 区块标题
            "chip": md_font(9),
        }
        install_material_styles(root)

        self.creds = None
        self.cookies = {}

        self.thumb_refs = []       # 保持缩略图引用，避免被回收
        self.image_windows = []
        self.first_shot = None     # (raw_bytes, title) 供自动验证复用
        self.pending_thumbs = 0
        self.render_done = False
        self.ui_queue = queue.Queue()   # 子线程 -> 主线程
        self.current_openid = ""        # 顶部入口最近一次查询的 OpenID
        self._query_pending = 0         # 一次点击后等待完成的子任务数

        # 「数据查询」区块状态
        self.dq_openid = ""
        self.dq_begin_time = ""
        self.dq_end_time = ""
        self.dq_page = 1
        self.dq_count = 0
        self.dq_render_done = False

        # 批量查询状态（无弹窗：清单 → 进度 → 汇总列表 → 双击行看明细，全部在主窗口内）
        self.batch_openids = []          # 本次批量查询的账号（去重，保持输入顺序）
        self.batch_results = []          # 与 batch_openids 等长；失败 / 被停止的条目也占位
        self._batch_row_index = {}       # 汇总列表 item id -> batch_openids 下标
        self.batch_mode = "single"       # single（单账号） / summary（批量汇总） / detail（批量明细）
        self.batch_running = False
        self.batch_finished = False
        self.batch_stop_requested = False
        self.batch_done_count = 0
        self.batch_success_count = 0
        self.batch_fail_count = 0
        self.batch_export_path = ""
        self.batch_queue = queue.Queue()  # 批量子线程 -> 主线程
        self.batch_worker = None
        self.batch_poll_job = None

        # 登录 / 登录态失效处理状态
        self.login_dialog = None        # 当前打开的登录窗口（同一时刻最多一个）
        self._pending_query_snapshot = None   # 因登录态失效而中断的查询参数
        self._auth_resume_used = False  # 自动续跑只做一次，避免死循环

        root.title("ACE 控制台 · OpenID 联合查询（可疑用户核查 / 数据查询）")
        root.geometry("1160x880")
        root.minsize(880, 620)
        root.configure(bg=self.colors["background"])

        self._build_ui()
        self._load_creds()
        self.root.after(60, self._poll_queue)

    # -- 弹窗（自动验证模式下改为打印，避免阻塞） --------------------------
    def _alert(self, title, message, kind="error"):
        if self.autotest_mode:
            print("[GUI-%s] %s: %s" % (kind, title, message.replace("\n", " ")))
            return
        from tkinter import messagebox
        if kind == "warn":
            messagebox.showwarning(title, message)
        else:
            messagebox.showerror(title, message)

    # -- Material 基础控件封装 --------------------------------------------
    def _field(self, parent, caption, var, width=14, values=None, bg=None):
        """Material 填充式输入框：标题层级 + 圆角浅色输入区（聚焦时描边转主色）。

        width 为字符数，会折算成像素显式落到圆角输入区上（见下方 area.configure），
        否则 Canvas 的默认请求宽度（1px）会让输入区被标题标签压扁、文本显示不全。

        返回 dict(box=input 容器, input=输入控件, line=底部指示线（兼容保留）, combo=下拉或 None)。
        """
        tk, ttk, c, f = self.tk, self.ttk, self.colors, self.fonts
        box_bg = bg or c["background"]

        box = tk.Frame(parent, bg=box_bg)
        tk.Label(box, text=caption, bg=box_bg, fg=c["secondary_text"],
                 font=f["caption"]).pack(anchor="w", pady=(0, 4))

        # 圆角输入区：外描边聚焦时切换为主色（原下划线指示线由底框描边取代）
        area = ui_surface(box, c, radius=RADIUS_FIELD, fill=c["surface_variant"],
                          bg=box_bg, shadow_pad=0)
        area.pack(fill="x")
        holder = tk.Frame(area, bg=c["surface_variant"])
        area.attach(holder, inset=FIELD_INSET, mode="x")

        line = tk.Frame(box, bg=c["divider"], height=0)  # 兼容旧接口，不再参与布局

        def _focus_in(_event=None):
            area.set_style(border=c["primary"], border_width=2)

        def _focus_out(_event=None):
            area.set_style(border=c["outline_variant"], border_width=1)

        combo = None
        if values is not None:
            pad_x, pad_y = SPACE_XXS, SPACE_XXS
            combo = ttk.Combobox(holder, textvariable=var, values=values, width=width,
                                 state="readonly", style="Material.TCombobox")
            combo.pack(fill="x", padx=pad_x, pady=pad_y)
            widget = combo
        else:
            pad_x, pad_y = FIELD_PAD_X, FIELD_PAD_Y
            widget = tk.Entry(holder, textvariable=var, width=width, relief="flat", bd=0,
                              bg=c["surface_variant"], fg=c["on_surface"],
                              insertbackground=c["primary"], font=f["field"],
                              highlightthickness=0, disabledbackground=c["surface_variant"])
            widget.pack(fill="x", padx=pad_x, pady=pad_y)

        # 圆角输入区是 Canvas（请求宽度恒为 1px）：若不显式给出宽度，整个输入区会被上方标题标签
        # 的宽度压窄（日期框只剩 34px，日期被裁成 “2026”）。这里按 width（字符数）折算像素定宽。
        want = widget.winfo_reqwidth()
        if want <= 1:   # 极端兜底：控件尚未算出请求宽度时用字体度量估算
            want = int(_ui_classes()["measure"](f["field"], "0")[0] * width + 12)
        area.configure(width=want + 2 * pad_x + 2 * FIELD_INSET)
        widget.bind("<FocusIn>", _focus_in, add="+")
        widget.bind("<FocusOut>", _focus_out, add="+")
        _focus_out()

        return {"box": box, "input": widget, "line": line, "combo": combo}

    def _card(self, parent, padx=CARD_PAD_X, pady=6):
        """Material 卡片：圆角表面 + 柔和投影（Canvas 自绘，统一走 card 档位）。返回内容 Frame。

        外层 padx 取 CARD_PAD_X：卡片表面正好落在区块内容左边界上，
        与「区块标题 / 条件卡内容」同一条竖线，列表不再比上方卡片内缩或外凸。
        """
        return ui_card(parent, self.colors, padx=padx, pady=pady, mode="x")

    def _chip(self, parent, text, fg=None, bg=None):
        """Material 标签片（chip）：圆角浅色底 + 强调色文字，用于数据类型 / 可疑级别。"""
        return ui_chip(parent, self.colors, text, fg=fg, bg=bg, font=self.fonts["chip"])

    # -- UI 构建 ----------------------------------------------------------
    def _build_ui(self):
        tk = self.tk

        self._build_query_bar(self.root)

        # 结果区：取消分栏与各自的独立滚动视口，改为「单一页面整体纵向滚动」。
        # 顶部查询栏固定；下方把「可疑用户核查」全部记录卡片、「数据查询」当前页表格
        # 与分页条自上而下平铺展开，整页共用一条纵向滚动条。
        c = self.colors
        outer = tk.Frame(self.root, bg=c["background"])
        outer.pack(fill="both", expand=True, padx=16, pady=(12, 14))

        self.page_canvas = tk.Canvas(outer, borderwidth=0, highlightthickness=0,
                                     bg=c["background"])
        self.page_vbar = ui_scrollbar(outer, c, orient="vertical",
                                      command=self.page_canvas.yview)
        self.page_canvas.configure(yscrollcommand=self.page_vbar.set)
        self.page_vbar.pack(side="right", fill="y", padx=(SPACE_XS, 0))
        self.page_canvas.pack(side="left", fill="both", expand=True)

        # 整页内容容器：两个区块都挂在这里，高度由内容决定（不裁剪、不内滚）
        self.page_frame = tk.Frame(self.page_canvas, bg=c["background"])
        self.page_window = self.page_canvas.create_window((0, 0), window=self.page_frame,
                                                          anchor="nw")

        # 内容变高时刷新滚动范围；画布变宽/变窄时把内容宽度对齐可视宽度（窄窗口不出现横向裁切）
        self.page_frame.bind(
            "<Configure>",
            lambda e: self.page_canvas.configure(scrollregion=self.page_canvas.bbox("all")),
        )
        self.page_canvas.bind(
            "<Configure>",
            lambda e: self.page_canvas.itemconfigure(self.page_window, width=e.width),
        )

        # 兼容旧属性名：原「上方区块画布」现在的角色就是整页画布
        self.results_canvas = self.page_canvas

        # 三个视图的容器：批量汇总（summary）/ 明细提示条（detail）/ 单账号两区块（single）。
        # 同一时刻只挂载其中一个视图；切换只做 pack / pack_forget，整页滚动条共用。
        self.batch_section = tk.Frame(self.page_frame, bg=c["background"])
        self.detail_bar = tk.Frame(self.page_frame, bg=c["background"])
        self.check_section = tk.Frame(self.page_frame, bg=c["background"])
        self.dq_section = tk.Frame(self.page_frame, bg=c["background"])

        self._build_batch_section(self.batch_section)
        self._build_detail_bar(self.detail_bar)
        self._build_check_section(self.check_section)
        self._build_dq_section(self.dq_section)

        # 初始视图：与单账号查询时一致（可疑用户核查 + 数据查询平铺）
        self._show_sections("single")

        self.page_canvas.bind_all("<MouseWheel>", self._on_mousewheel)

    def _section_header(self, parent, title, status_var):
        """区块标题行：主色指示条 + 区块标题 + 右侧状态（条数 / 分页信息）。

        标题栏与下方卡片使用同一份横向留白：标题文字左边缘 = 卡片表面左边缘
        （= CARD_SHADOW_PAD + CARD_PAD_X），标题栏 / 条件卡 / 记录卡 / 表格骨架
        因此同处一条竖线；卡片内部内容再按 CARD_PAD_X 内缩，层级一致；
        原来的 1px 通栏分隔线会与卡片边缘断开，已移除，改由留白区分层次。
        """
        tk, c, f = self.tk, self.colors, self.fonts

        head = tk.Frame(parent, bg=c["background"])
        head.pack(fill="x", padx=CARD_SHADOW_PAD)

        indicator = ui_surface(head, c, radius=2, fill=c["primary"], bg=c["background"],
                               width=4, height=18)
        indicator.pack(side="left", pady=1)

        tk.Label(head, text=title, bg=c["background"], fg=c["on_surface"],
                 font=f["section"]).pack(side="left", padx=(SPACE_M, 0))

        status = tk.Frame(head, bg=c["background"])
        status.pack(side="right")
        ui_surface(status, c, radius=3, fill=c["outline_variant"], bg=c["background"],
                   width=6, height=6).pack(side="left", padx=(0, 6), pady=1)
        tk.Label(status, textvariable=status_var, bg=c["background"], fg=c["secondary_text"],
                 font=f["body"]).pack(side="left")
        return head

    # -- 顶部单一查询入口（Material 顶栏 + 填充式输入框 + 主按钮） ---------
    def _build_query_bar(self, parent):
        tk, ttk, c, f = self.tk, self.ttk, self.colors, self.fonts

        # 顶部应用栏（M3 柔和处理：浅主色面 + 主色点缀，替代原来的实心主色横幅）
        appbar = tk.Frame(parent, bg=c["appbar_bg"])
        appbar.pack(fill="x")

        # 顶栏右侧：登录状态胶囊（主色容器色）+ 登录 / 重新登录入口（主色实心按钮）
        bar_right = tk.Frame(appbar, bg=c["appbar_bg"])
        bar_right.pack(side="right", padx=20, pady=16, anchor="ne")
        self.login_btn = ui_button(bar_right, c, text="登录", style="Appbar.TButton",
                                   command=self.on_login_button)
        self.login_btn.pack(side="right", pady=1)
        self.account_label = ui_chip(bar_right, c, "未登录", fg=c["primary_dark"],
                                     bg=c["primary_container"], font=f["caption"])
        self.account_label.pack(side="right", padx=(0, 14), pady=1)

        # 顶栏左侧：品牌徽标（主色实心方块）+ 标题层级（深色文字落在浅色面上）
        bar_left = tk.Frame(appbar, bg=c["appbar_bg"])
        bar_left.pack(side="left", padx=20, pady=16)
        badge = ui_surface(bar_left, c, radius=12, fill=c["primary"], bg=c["appbar_bg"],
                           width=40, height=40, shadow_pad=0)
        badge.pack(side="left")
        badge.create_text(20, 21, text="ACE", fill=c["on_primary"], font=md_font(11, "bold"))

        titles = tk.Frame(bar_left, bg=c["appbar_bg"])
        titles.pack(side="left", padx=(12, 0))
        tk.Label(titles, text="ACE 控制台 · OpenID 联合查询", bg=c["appbar_bg"],
                 fg=c["on_surface"], font=f["appbar"]).pack(anchor="w")
        tk.Label(titles, text="一次查询，同时获取「可疑用户核查」与「数据查询」两类数据",
                 bg=c["appbar_bg"], fg=c["on_surface_variant"], font=f["appbar_sub"]).pack(
            anchor="w", pady=(3, 0)
        )

        # 顶栏底部 1px 细线：让浅色顶栏与下方白色查询面形成连续的层次过渡
        tk.Frame(parent, bg=c["outline_variant"], height=1).pack(fill="x")

        # 查询条（表面色 + 单入口：一个 OpenID 输入框 + 一个查询按钮）
        sheet = tk.Frame(parent, bg=c["surface"])
        sheet.pack(fill="x")
        row = tk.Frame(sheet, bg=c["surface"])
        row.pack(fill="x", padx=20, pady=16)

        self.openid_var = tk.StringVar(value=clean_openid(self.args.openid))
        field = self._field(row, "OpenID（账号 ID，可多行 / 逗号 / 空格分隔批量粘贴）",
                            self.openid_var, width=34, bg=c["surface"])
        field["box"].pack(side="left")
        # 多行粘贴到单行输入框时换行会被保留：回车一律按「查询」处理，含多个账号时自动转入批量
        field["input"].bind("<Return>", lambda e: self.on_query())
        field["input"].bind("<<Paste>>", self._on_openid_paste)

        self.query_btn = ui_button(row, c, text="查询", style="Primary.TButton",
                                   command=self.on_query)
        # anchor="n" + pady 让按钮圆角面与输入区顶部对齐（pack 默认居中会偏高）
        self.query_btn.pack(side="left", padx=(18, 0), pady=(22, 0), anchor="n")

        self.batch_btn = ui_button(row, c, text="批量查询", style="Outlined.TButton",
                                   command=self.on_batch_trigger)
        self.batch_btn.pack(side="left", padx=(10, 0), pady=(24, 0), anchor="n")

        self.import_btn = ui_button(row, c, text="导入清单", style="Outlined.TButton",
                                    command=self.import_openid_file)
        self.import_btn.pack(side="left", padx=(10, 0), pady=(24, 0), anchor="n")

        self.export_btn = ui_button(row, c, text="导出汇总", style="Outlined.TButton",
                                    command=self.export_batch_csv, state="disabled")
        self.export_btn.pack(side="left", padx=(10, 0), pady=(24, 0), anchor="n")

        tk.Label(row, text="单个 OpenID 直接查询；多行 / 多账号自动批量，汇总列表直接显示在下方，"
                           "双击某行看明细。", bg=c["surface"], fg=c["secondary_text"],
                 font=f["caption"]).pack(side="left", padx=(18, 0), pady=(27, 0))

    # -- 固定时间范围：界面已移除时间选择控件，仅命令行 --begin/--end 可覆盖 --
    def fixed_check_dates(self):
        """「可疑用户核查」固定范围：近 30 天（含今天），返回 YYYY-MM-DD。"""
        return resolve_dates(self.args.begin, self.args.end)

    def fixed_dq_dates(self):
        """「数据查询」固定范围：当天回溯 10 年 ~ 当天，返回完整时间字符串。"""
        begin, end = dq_default_dates()
        return resolve_dq_dates(self.args.begin or begin, self.args.end or end)

    # -- 区块 1：可疑用户核查（保留卡片 / 缩略图 / 原图查看） --------------
    def _build_check_section(self, parent):
        tk, ttk, c, f = self.tk, self.ttk, self.colors, self.fonts

        self.status_var = tk.StringVar(value="就绪")
        self._section_header(parent, "可疑用户核查", self.status_var)

        # 条件区卡片：时间选择控件已整体移除（可疑级别此前已移除），仅保留一句范围说明。
        # 范围固定为近 30 天（含今天）；命令行显式传入 --begin/--end 时以其为准。
        card = ui_card(parent, c, padx=0, pady=(8, 0), mode="x")
        row = tk.Frame(card, bg=c["surface"])
        row.pack(fill="x", padx=CARD_PAD_X, pady=CARD_PAD_Y)

        check_begin, check_end = self.fixed_check_dates()
        self.begin_var = tk.StringVar(value=check_begin)
        self.end_var = tk.StringVar(value=check_end)

        tk.Label(
            row,
            text="时间范围固定为近 30 天（%s ~ %s，含今天）；「截图」记录显示缩略图，单击缩略图查看原大图。"
                 % (check_begin, check_end),
            bg=c["surface"], fg=c["secondary_text"], font=f["body"], anchor="w", justify="left",
        ).pack(side="left")

        # 记录卡片列表：不再自建滚动视口，全部卡片随整页一起纵向滚动、完整展开
        self.result_frame = tk.Frame(parent, bg=c["background"])
        self.result_frame.pack(fill="x", pady=(SPACE_S, 0))

    # -- 区块 2：数据查询（处罚数据 + 分页） ------------------------------
    def _build_dq_section(self, parent):
        tk, ttk, c, f = self.tk, self.ttk, self.colors, self.fonts

        self.dq_status_var = tk.StringVar(value="就绪")
        self._section_header(parent, "数据查询", self.dq_status_var)

        # 条件区卡片：时间选择控件已整体移除，仅保留一句范围说明。
        # 范围固定为当天回溯 10 年（含今天）；命令行显式传入 --begin/--end 时以其为准。
        card = ui_card(parent, c, padx=0, pady=(8, 0), mode="x")
        row = tk.Frame(card, bg=c["surface"])
        row.pack(fill="x", padx=CARD_PAD_X, pady=CARD_PAD_Y)

        dq_begin, dq_end = self.fixed_dq_dates()
        self.dq_begin_var = tk.StringVar(value=dq_begin)
        self.dq_end_var = tk.StringVar(value=dq_end)

        tk.Label(
            row,
            # 说明文字只取日期部分（YYYY-MM-DD），查询仍用含时分秒的完整范围
            text="时间范围固定为近 %d 年（%s ~ %s）；每页 %d 条；重新检索请点击窗口顶部「查询」。"
                 % (DQ_LOOKBACK_YEARS, dq_begin[:10], dq_end[:10], DQ_PAGE_SIZE),
            bg=c["surface"], fg=c["secondary_text"], font=f["body"], anchor="w", justify="left",
        ).pack(side="left")

        # 数据表格卡片：不再自建滚动视口，表格高度按当前页行数自适应，
        # 整页纵向滚动即可看到全部行（每页行数仍由 DQ_PAGE_SIZE 固定）
        table_wrap = ui_card(parent, c, padx=0, pady=(8, 0), mode="x")

        self.dq_tree = ttk.Treeview(
            table_wrap, columns=[col[1] for col in DQ_COLUMNS], show="headings",
            height=DQ_TREE_MIN_ROWS, style="Material.Treeview", selectmode="browse",
        )
        for head, field, width, anchor, stretch in DQ_COLUMNS:
            self.dq_tree.heading(field, text=head, anchor=anchor)
            # 最小宽度按列宽的 75% 兜底：窗口收窄时列不会被拖到把时间串挤断
            self.dq_tree.column(field, width=width, minwidth=max(96, int(width * 0.75)),
                                anchor=anchor, stretch=stretch)
        self.dq_tree.pack(side="left", fill="x", expand=True,
                          padx=(CARD_PAD_X, CARD_PAD_X), pady=SPACE_S)

        # 分页条：紧随表格下方的页脚卡片（与表格卡片形成 M3 层级）
        page_bar = ui_card(parent, c, padx=0, pady=(SPACE_S, SPACE_XS), mode="x")
        page_row = tk.Frame(page_bar, bg=c["surface"])
        page_row.pack(fill="x", padx=CARD_PAD_X, pady=SPACE_S)

        self.dq_prev_btn = ui_button(
            page_row, c, text="‹ 上一页", style="Outlined.TButton",
            command=self.on_dq_prev, state="disabled",
        )
        self.dq_prev_btn.pack(side="left")
        self.dq_next_btn = ui_button(
            page_row, c, text="下一页 ›", style="Outlined.TButton",
            command=self.on_dq_next, state="disabled",
        )
        self.dq_next_btn.pack(side="left", padx=(10, 0))

        self.dq_page_var = tk.StringVar(value="")
        tk.Label(page_row, textvariable=self.dq_page_var, bg=c["surface"],
                 fg=c["on_surface_variant"], font=f["body_bold"]).pack(side="left", padx=(16, 0))
        tk.Label(page_row, text="每页 %d 条，翻页后自动刷新表格" % DQ_PAGE_SIZE,
                 bg=c["surface"], fg=c["secondary_text"], font=f["caption"]).pack(
            side="right")

        # 斑马纹 + 行悬停高亮（M3：低对比隔行底 + 轻量悬停反馈）
        self.dq_tree.tag_configure("even", background=c["surface"])
        self.dq_tree.tag_configure("odd", background=c["row_alt"])
        self.dq_tree.tag_configure("hover", background=c["row_hover"])
        self._tree_row_tags = {}
        self._tree_hover_row = None
        self.dq_tree.bind("<Motion>", self._on_tree_motion)
        self.dq_tree.bind("<Leave>", lambda _event: self._set_tree_hover(None))


    def _on_mousewheel(self, event):
        """整页共用一条纵向滚动条：鼠标落在页面内时滚轮统一滚动整页。"""
        try:
            widget = self.root.winfo_containing(event.x_root, event.y_root)
        except Exception:
            widget = None
        if widget is None or not self._is_within(widget, self.page_canvas):
            return
        delta = int(-event.delta / 120) if event.delta else 0
        if delta:
            self.page_canvas.yview_scroll(delta, "units")

    @staticmethod
    def _is_within(widget, ancestor):
        """判断控件是否位于指定容器内部（含自身）。"""
        current = widget
        while current is not None:
            if current is ancestor:
                return True
            current = getattr(current, "master", None)
        return False

    # -- 登录窗口与登录态管理 ---------------------------------------------
    def _load_creds(self):
        """加载本地凭据；缺失 / 无效时在界面上给出明确状态，并弹出登录窗口。"""
        try:
            self.creds, self.cookies = load_credentials()
            name = (self.creds.get("account") or {}).get("user_name") or "未知"
            self.status_var.set("就绪 · 登录账号：%s" % name)
            self._set_login_state("重新登录", "已登录：%s" % name)
        except AceError as exc:
            self.creds, self.cookies = None, {}
            self.status_var.set("未登录 · 请在登录窗口完成登录后再查询")
            self._set_login_state("登录", "未登录")
            detail = _format_error(exc)
            if self.autotest_mode:
                print("[GUI-auth] 凭据加载失败: %s" % detail.replace("\n", " "))
            else:
                # 等主窗口布局完成后再弹出，避免窗口未映射时定位 / 抢占焦点异常
                self.root.after(150, lambda: self.open_login_dialog(reason=detail))

    def _set_login_state(self, button_text=None, account_text=None):
        try:
            if button_text and getattr(self, "login_btn", None) is not None:
                self.login_btn.config(text=button_text)
            if account_text and getattr(self, "account_label", None) is not None:
                self.account_label.config(text=account_text)
        except Exception:
            pass

    def on_login_button(self):
        """顶栏「登录 / 重新登录」按钮。"""
        if self.login_dialog is not None:
            try:
                self.login_dialog.top.lift()
                self.login_dialog.top.focus_set()
            except Exception:
                self.login_dialog = None
            else:
                return
        self.open_login_dialog(reason="")

    def open_login_dialog(self, reason="", prefill_phone="", prefill_password=""):
        """打开 Material 风格登录窗口（同一时刻只保留一个）。"""
        if self.login_dialog is not None:
            if reason:
                self.login_dialog.set_reason(reason)
            try:
                self.login_dialog.top.lift()
                self.login_dialog.top.focus_set()
            except Exception:
                self.login_dialog = None
            else:
                return self.login_dialog
        self.login_dialog = LoginDialog(
            self, reason=reason,
            prefill_phone=prefill_phone or "",
            prefill_password=prefill_password or "",
        )
        return self.login_dialog

    # -- 批量查询（多账号清单 → 汇总表 → 单击行看明细 / 导出 CSV） --------
    def _on_openid_paste(self, event=None):
        """粘贴账号清单时把换行 / 分号等分隔符归一为逗号，避免多行粘贴在单行框里显示错乱。"""
        widget = getattr(event, "widget", None)
        if widget is None:
            return None
        try:
            raw = self.root.clipboard_get()
        except Exception:
            return None  # 读取剪贴板失败则交回默认粘贴行为
        text = OPENID_SPLIT_RE.sub(",", clean_openid(raw)).strip(",")
        if not text:
            return None
        try:
            if widget.selection_present():
                widget.delete("sel.first", "sel.last")
            widget.insert("insert", text)
        except Exception:
            return None
        return "break"

    # -- 批量查询（主窗口内完成：清单 → 进度 → 汇总列表 → 双击行看明细） ----
    def _build_batch_section(self, parent):
        """批量汇总区块：进度（已完成 X / 总数 N）+ 停止 + 汇总列表，全部内嵌在主窗口结果区。"""
        tk, ttk, c, f = self.tk, self.ttk, self.colors, self.fonts

        self.batch_status_var = tk.StringVar(value="尚未开始")
        self._section_header(parent, "批量查询汇总", self.batch_status_var)

        # 进度卡：一行文字（已完成 X / 总数 N）+ 右侧操作（停止 / 导出 / 返回汇总列表）
        card = ui_card(parent, c, padx=0, pady=(8, 0), mode="x")
        row = tk.Frame(card, bg=c["surface"])
        row.pack(fill="x", padx=CARD_PAD_X, pady=CARD_PAD_Y)

        self.batch_running_var = tk.StringVar(value="尚未开始批量查询")
        tk.Label(row, textvariable=self.batch_running_var, bg=c["surface"],
                 fg=c["on_surface"], font=f["body_bold"]).pack(side="left")

        self.batch_back_btn = ui_button(row, c, text="返回汇总列表", style="Text.TButton",
                                        command=self.show_batch_summary, state="disabled")
        self.batch_back_btn.pack(side="right")
        self.batch_export_btn = ui_button(row, c, text="导出汇总 CSV", style="Outlined.TButton",
                                          command=self.export_batch_csv, state="disabled")
        self.batch_export_btn.pack(side="right", padx=(0, 10))
        self.batch_stop_btn = ui_button(row, c, text="停止", style="Outlined.TButton",
                                        command=self.stop_batch_query, state="disabled")
        self.batch_stop_btn.pack(side="right", padx=(0, 10))

        self.batch_progress = ttk.Progressbar(card, orient="horizontal", mode="determinate",
                                              maximum=1, value=0,
                                              style="Batch.Horizontal.TProgressbar")
        try:
            ttk.Style(self.root).configure(
                "Batch.Horizontal.TProgressbar",
                troughcolor=c["surface_variant"], background=c["primary"],
                bordercolor=c["surface_variant"], lightcolor=c["primary"],
                darkcolor=c["primary"], thickness=8,
            )
        except Exception:
            pass
        self.batch_progress.pack(fill="x", padx=CARD_PAD_X, pady=(0, CARD_PAD_Y))

        tk.Label(parent, text="汇总列表（每行一个账号：OpenID · 可疑用户核查 X 条 · 数据查询 X 条；"
                             "双击任意一行查看明细）", bg=c["background"], fg=c["secondary_text"],
                 font=f["caption"], anchor="w", justify="left").pack(
            fill="x", padx=CARD_SHADOW_PAD, pady=(SPACE_S, 0))

        # 汇总列表卡片：与「数据查询」表格同款骨架，随账号数自适应高度
        table_card = ui_card(parent, c, padx=0, pady=(SPACE_XS, 0), mode="x")
        wrap = tk.Frame(table_card, bg=c["surface"])
        wrap.pack(fill="x", padx=CARD_PAD_X, pady=CARD_PAD_Y)

        self.batch_tree = ttk.Treeview(
            wrap, columns=[col[1] for col in BATCH_COLUMNS], show="headings",
            height=BATCH_TREE_MIN_ROWS, style="Material.Treeview", selectmode="browse",
        )
        for head, field, width, anchor, stretch in BATCH_COLUMNS:
            self.batch_tree.heading(field, text=head, anchor=anchor)
            self.batch_tree.column(field, width=width, minwidth=max(96, int(width * 0.75)),
                                   anchor=anchor, stretch=stretch)
        batch_vbar = ui_scrollbar(wrap, c, orient="vertical", command=self.batch_tree.yview)
        self.batch_tree.configure(yscrollcommand=batch_vbar.set)
        self.batch_tree.pack(side="left", fill="x", expand=True)
        batch_vbar.pack(side="right", fill="y", padx=(6, 0))
        # 双击（或选中后回车）进入该账号明细
        self.batch_tree.bind("<Double-1>", self.on_batch_row_activate)
        self.batch_tree.bind("<Return>", self.on_batch_row_activate)

        self.batch_hint_var = tk.StringVar(value="")
        tk.Label(parent, textvariable=self.batch_hint_var, bg=c["background"],
                 fg=c["secondary_text"], font=f["caption"], anchor="w", justify="left").pack(
            fill="x", padx=CARD_SHADOW_PAD, pady=(SPACE_XS, 0))

    def _build_detail_bar(self, parent):
        """明细态提示条：显示当前查看的账号，并提供「返回汇总列表」入口。"""
        tk, c, f = self.tk, self.colors, self.fonts
        card = ui_card(parent, c, padx=0, pady=0, mode="x")
        row = tk.Frame(card, bg=c["surface"])
        row.pack(fill="x", padx=CARD_PAD_X, pady=SPACE_S)
        self.detail_bar_var = tk.StringVar(value="")
        tk.Label(row, textvariable=self.detail_bar_var, bg=c["surface"],
                 fg=c["on_surface"], font=f["body"], anchor="w").pack(side="left")
        self.detail_back_btn = ui_button(row, c, text="返回汇总列表", style="Outlined.TButton",
                                         command=self.show_batch_summary)
        self.detail_back_btn.pack(side="right")

    def _show_sections(self, mode):
        """切换结果区视图（三个视图共用同一条整页纵向滚动条）。

        - single ：单账号查询结果（可疑用户核查 + 数据查询），与原有布局一致；
        - summary：批量汇总列表（进度 + 汇总表格）；
        - detail ：批量某账号明细（提示条 + 两个区块），可返回汇总列表。
        """
        for widget in (self.batch_section, self.detail_bar,
                       self.check_section, self.dq_section):
            try:
                widget.pack_forget()
            except Exception:
                pass
        if mode == "summary":
            self.batch_section.pack(fill="x")
        else:
            # 单账号 / 批量明细共用两个区块；已有批量结果时额外挂一条「返回汇总列表」提示条
            if self.batch_openids:
                self.detail_bar.pack(fill="x")
            self.check_section.pack(fill="x")
            self.dq_section.pack(fill="x", pady=(SPACE_L, 0))
        self.batch_mode = mode
        try:
            self.page_canvas.yview_moveto(0.0)
        except Exception:
            pass

    # -- 清单入口：顶部输入框（多行 / 多分隔符）/ 导入 txt-csv -----------------
    def on_batch_trigger(self):
        """顶部「批量查询」按钮：把输入框清单直接在主窗口内批量查询（不再弹窗）。"""
        openids, ignored = parse_openid_list(self.openid_var.get())
        if not openids:
            self._alert("提示", "请输入需要查询的 OpenID（多个账号可多行粘贴或用逗号分隔）。",
                        kind="warn")
            return
        if ignored and hasattr(self, "batch_hint_var"):
            self.batch_hint_var.set("已忽略 %d 个无效账号片段" % ignored)
        if len(openids) == 1:
            self.on_query()          # 单个账号：保持原有单账号查询行为
            return
        self.start_batch_query(openids)

    def import_openid_file(self):
        """顶部「导入清单」：从 txt / csv 读入账号，写入顶部输入框后直接开跑。"""
        from tkinter import filedialog
        path = filedialog.askopenfilename(
            parent=self.root, title="选择 OpenID 清单文件",
            filetypes=[("账号清单", "*.txt *.csv"), ("文本文件", "*.txt"),
                       ("CSV 文件", "*.csv"), ("所有文件", "*.*")],
        )
        if not path:
            return
        suffix = os.path.splitext(path)[1].lower()
        if suffix not in (".txt", ".csv", ".tsv"):
            self._alert(
                "导入失败",
                "仅支持 txt / csv 清单文件（当前为 %s）。请另存为 txt 或 csv 后再导入。"
                % (suffix or "无扩展名"), kind="warn")
            return
        try:
            openids, ignored = load_openid_file(path)
        except AceError as exc:
            self._alert("导入失败", _format_error(exc), kind="warn")
            return
        if not openids:
            self._alert("导入失败", "未在 %s 中找到有效账号，请检查文件内容。"
                        % os.path.basename(path), kind="warn")
            return

        self.openid_var.set(", ".join(openids))
        tip = "已从 %s 导入 %d 个账号" % (os.path.basename(path), len(openids))
        if ignored:
            tip += "，忽略 %d 个无效片段" % ignored
        if len(openids) == 1:
            self.batch_hint_var.set(tip)
            self.on_query()          # 单个账号：与手工输入单个 OpenID 的行为一致
            return
        self.start_batch_query(openids)
        self.batch_hint_var.set(tip)

    # -- 执行：子线程逐个账号查询，主线程轮询回填 ---------------------------
    def start_batch_query(self, openids):
        """在主窗口内启动批量查询（结果直接平铺到下方汇总列表）。"""
        if self.batch_running:
            self._alert("提示", "批量查询正在进行中，请等待完成或点击「停止」。", kind="warn")
            return
        ordered = []
        for value in (openids or []):
            value = clean_openid(value)
            if value and value not in ordered:
                ordered.append(value)
        if not ordered:
            self._alert("提示", "未识别到有效 OpenID，请检查清单内容。", kind="warn")
            return

        try:
            cookies = dict(self.cookies)
            if not cookies:
                self.creds, cookies = load_credentials()
                self.cookies = dict(cookies)
        except AceError as exc:
            self.open_login_dialog(reason=_format_error(exc))
            return

        check_begin, check_end = self.fixed_check_dates()
        dq_begin, dq_end = self.fixed_dq_dates()

        self.batch_openids = list(ordered)
        self.batch_results = [None] * len(self.batch_openids)
        self._batch_row_index = {}
        self.batch_running = True
        self.batch_finished = False
        self.batch_stop_requested = False
        self.batch_done_count = self.batch_success_count = self.batch_fail_count = 0
        self.batch_export_path = ""
        self.current_openid = ""

        self.batch_tree.delete(*self.batch_tree.get_children())
        self.batch_tree.configure(
            height=max(BATCH_TREE_MIN_ROWS, min(len(self.batch_openids), BATCH_TREE_MAX_ROWS)))
        self.batch_progress.configure(maximum=len(self.batch_openids), value=0)
        self.batch_running_var.set("已完成 0 / %d（成功 0 · 失败 0）" % len(self.batch_openids))
        self.batch_status_var.set("批量查询进行中…")
        self.batch_hint_var.set("正在逐个查询 %d 个账号…" % len(self.batch_openids))
        self.batch_stop_btn.config(state="normal")
        self.batch_export_btn.config(state="disabled")
        self.batch_back_btn.config(state="disabled")
        try:
            self.export_btn.config(state="disabled")
        except Exception:
            pass

        self._show_sections("summary")
        self.batch_worker = threading.Thread(
            target=self._batch_worker_loop,
            args=(list(self.batch_openids), check_begin, check_end, dq_begin, dq_end, cookies),
            daemon=True,
        )
        self.batch_worker.start()
        self._batch_schedule_poll()

    def stop_batch_query(self):
        """请求停止：当前账号查完后不再继续，剩余账号不再发起请求。"""
        if not self.batch_running:
            return
        self.batch_stop_requested = True
        self.batch_stop_btn.config(state="disabled")
        self.batch_running_var.set(self.batch_running_var.get() + " · 正在停止…")

    def _batch_worker_loop(self, openids, check_begin, check_end, dq_begin, dq_end, cookies):
        """子线程：逐账号查询（可疑用户核查 + 数据查询首页），结果经队列回传主线程。"""
        for index, openid in enumerate(openids):
            if self.batch_stop_requested:
                break
            entry = {
                "openid": openid, "status": "ok", "error": "",
                "record_count": None, "top_level": "", "punish_count": None,
                "last_punish": "", "records": [], "dq_count": 0, "dq_items": [],
                "dq_begin": dq_begin, "dq_end": dq_end,
            }
            try:
                data = query_openid(openid, check_begin, check_end, "all", cookies)
                records = extract_records(data, openid)
                entry["records"] = records
                entry["record_count"] = len(records)
                entry["top_level"] = batch_top_level(records)
            except AceError as exc:
                entry["status"] = "failed"
                entry["error"] = _format_error(exc)
                if getattr(exc, "auth", False):
                    self.batch_queue.put(("auth", entry["error"]))
                    self.batch_queue.put(("row", index, entry))
                    break
            except Exception as exc:   # 兜底：任何异常都单账号隔离，不中断整批
                entry["status"] = "failed"
                entry["error"] = str(exc)

            if entry["status"] == "ok":
                try:
                    count, items = query_punish(
                        openid, 1, DQ_PAGE_SIZE, cookies, dq_begin, dq_end
                    )
                    entry["punish_count"] = int(count or 0)
                    entry["dq_count"] = int(count or 0)
                    entry["dq_items"] = list(items or [])
                    entry["last_punish"] = batch_last_punish(items)
                except AceError as exc:
                    entry["status"] = "failed"
                    entry["error"] = _format_error(exc)
                    if getattr(exc, "auth", False):
                        self.batch_queue.put(("auth", entry["error"]))
                        self.batch_queue.put(("row", index, entry))
                        break
                except Exception as exc:
                    entry["status"] = "failed"
                    entry["error"] = str(exc)

            self.batch_queue.put(("row", index, entry))
        self.batch_queue.put(("done", None, None))

    def _batch_schedule_poll(self):
        try:
            self.batch_poll_job = self.root.after(80, self._batch_poll_queue)
        except Exception:
            self.batch_poll_job = None

    def _batch_poll_queue(self):
        """主线程轮询：消费子线程结果，刷新进度与汇总列表。"""
        self.batch_poll_job = None
        try:
            while True:
                item = self.batch_queue.get_nowait()
                if item[0] == "row":
                    self._batch_apply_row(item[1], item[2])
                elif item[0] == "auth":
                    self.batch_hint_var.set("登录态失效，批量查询已中断：%s" % item[1])
                    if not self.autotest_mode:
                        self.open_login_dialog(reason=item[1])
                elif item[0] == "done":
                    self._batch_finish()
        except queue.Empty:
            pass
        except Exception as exc:
            print("[batch] %s" % exc, file=sys.stderr)
        if not self.batch_finished:
            self._batch_schedule_poll()

    def _batch_apply_row(self, index, entry):
        if index >= len(self.batch_results):
            return
        self.batch_results[index] = entry
        self.batch_done_count += 1
        if entry["status"] == "ok":
            self.batch_success_count += 1
        else:
            self.batch_fail_count += 1
        self._batch_insert_row(index, entry)
        self.batch_progress.configure(value=self.batch_done_count)
        self.batch_running_var.set("已完成 %d / %d（成功 %d · 失败 %d）" % (
            self.batch_done_count, len(self.batch_openids),
            self.batch_success_count, self.batch_fail_count))

    def _batch_finish(self):
        self.batch_running = False
        self.batch_finished = True
        if self.batch_stop_requested:
            self.batch_status_var.set("已停止")
            self.batch_running_var.set("已停止：已完成 %d / %d（成功 %d · 失败 %d）" % (
                self.batch_done_count, len(self.batch_openids),
                self.batch_success_count, self.batch_fail_count))
        else:
            self.batch_status_var.set("查询完成")
            self.batch_running_var.set("已完成 %d / %d（成功 %d · 失败 %d）" % (
                self.batch_done_count, len(self.batch_openids),
                self.batch_success_count, self.batch_fail_count))
        self.batch_stop_btn.config(state="disabled")
        has_rows = self.batch_done_count > 0
        self.batch_export_btn.config(state="normal" if has_rows else "disabled")
        self.batch_back_btn.config(state="normal" if has_rows else "disabled")
        try:
            self.export_btn.config(state="normal" if has_rows else "disabled")
        except Exception:
            pass
        self.batch_hint_var.set(
            "汇总完成，双击任意一行查看该账号明细（核查记录 + 处罚数据）。" if has_rows
            else "没有任何账号查询完成，请检查清单或网络后重试。")

    # -- 汇总列表：行渲染 / 双击进明细 --------------------------------------
    def _batch_row_values(self, entry):
        """汇总列表行文本：OpenID / 可疑用户核查 X 条 / 数据查询 X 条 / 最高级别 / 最近处罚。"""
        if entry is None:
            return ("", "", "", "", "")
        if entry["status"] == "ok":
            return (
                entry["openid"],
                "%d 条" % entry["record_count"],
                "%d 条" % entry["punish_count"],
                LEVEL_LABELS.get(entry["top_level"], entry["top_level"] or "—"),
                entry["last_punish"] or "—",
            )
        if entry["status"] == "stopped":
            return (entry["openid"], "已停止", "—", "—", "—")
        return (entry["openid"], "查询失败", "—", "—", "—")

    def _batch_insert_row(self, index, entry):
        values = self._batch_row_values(entry)
        item = self.batch_tree.insert("", "end", values=values)
        self._batch_row_index[item] = index

    def on_batch_row_activate(self, _event=None):
        """双击（或回车）汇总行 → 进入该账号明细；失败 / 未完成的行给出提示。"""
        selection = self.batch_tree.selection()
        if not selection:
            return "break"
        index = self._batch_row_index.get(selection[0])
        if index is None or index >= len(self.batch_results):
            return "break"
        entry = self.batch_results[index]
        if not entry:
            return "break"
        if entry["status"] != "ok":
            self._alert("该账号暂无可查看明细",
                        "OpenID %s：%s" % (entry["openid"], entry["error"] or entry["status"]),
                        kind="warn")
            return "break"
        self.show_batch_detail(
            entry["openid"], entry["records"], entry["dq_count"],
            entry["dq_items"], entry["dq_begin"], entry["dq_end"],
        )
        return "break"

    def show_batch_summary(self):
        """从明细返回批量汇总列表（保留已查询到的全部结果）。"""
        if not self.batch_openids:
            return
        self._show_sections("summary")

    def show_batch_detail(self, openid, records, count, items, dq_begin="", dq_end=""):
        """批量汇总双击某行：沿用下方两个区块展示该账号明细（不重新发起查询）。"""
        if not self.cookies:
            try:
                self.creds, self.cookies = load_credentials()
            except AceError as exc:
                self.open_login_dialog(reason=_format_error(exc))
                return

        self.current_openid = openid
        # 明细直接渲染，不参与「一次点击」的入口按钮计数
        self._query_pending = 0
        self.first_shot = None
        self.pending_thumbs = 0
        self.render_done = False

        self._clear_results()
        self._render_records(records, openid)
        self.status_var.set("查询成功，共 %d 条记录（OpenID %s · 批量查询明细）" % (
            len(records or []), openid))

        # 数据查询区块：沿用批量查询时使用的时间范围快照，翻页可继续
        self.dq_openid = openid
        self.dq_begin_time = dq_begin
        self.dq_end_time = dq_end
        self.dq_page = 1
        self.dq_count = int(count or 0)
        self.dq_page_var.set("")
        self.dq_render_done = False
        self._clear_dq_rows()
        self.dq_prev_btn.config(state="disabled")
        self.dq_next_btn.config(state="disabled")
        self._render_dq_page(count, items, 1, openid)

        # 切到明细视图：顶部提示条给出「返回汇总列表」入口
        self.detail_bar_var.set("正在查看 OpenID %s 的明细（批量查询结果，共 %d 条核查记录）"
                                % (openid, len(records or [])))
        self._show_sections("detail")

    # -- 导出汇总 CSV ------------------------------------------------------
    def export_batch_csv(self, path=None):
        """导出批量汇总结果为 CSV（utf-8-sig，Excel 直接可读）。返回实际写入路径。"""
        if not self.batch_done_count:
            return ""
        if not path:
            from tkinter import filedialog
            path = filedialog.asksaveasfilename(
                parent=self.root, title="导出批量汇总结果", defaultextension=".csv",
                initialfile=default_batch_csv_name(),
                initialdir=output_dir(),
                filetypes=[("CSV 文件", "*.csv"), ("所有文件", "*.*")],
            )
            if not path:
                return ""
        header = [col[0] for col in BATCH_COLUMNS] + ["状态", "备注"]
        with open(path, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(header)
            for entry in self.batch_results:
                if entry is None:
                    continue
                values = list(self._batch_row_values(entry))
                status = {"ok": "成功", "failed": "失败", "stopped": "已停止"}.get(
                    entry["status"], entry["status"])
                writer.writerow(values + [status, entry["error"] or ""])
        self.batch_export_path = path
        if hasattr(self, "batch_hint_var"):
            self.batch_hint_var.set("汇总结果已导出：%s" % path)
        if not self.autotest_mode:
            self._alert("导出完成", "汇总结果已导出到：\n%s" % path, kind="info")
        return path

    def _handle_auth_failure(self, exc):
        """查询阶段发现登录态失效：弹出登录窗口，登录成功后自动续跑一次。"""
        if getattr(exc, "manual", False):
            detail = _format_error(exc)
            if self.autotest_mode:
                print("[GUI-auth] 需要人工验证: %s" % detail.replace("\n", " "))
                return
            self.open_login_dialog(reason=detail)
            return
        if self.login_dialog is not None:
            return  # 登录窗口已打开，等用户操作
        if self._auth_resume_used:
            self._alert("登录态失效", _format_error(exc) + "\n\n请点击顶栏「重新登录」完成登录。")
            return
        self._pending_query_snapshot = True
        if self.autotest_mode:
            print("[GUI-auth] 登录态失效（自动验证模式不弹窗）: %s" % exc.message)
            return
        self.open_login_dialog(reason="登录态已失效：" + exc.message)

    def on_login_success(self, cookies, account, path):
        """登录窗口回调：更新内存 / 磁盘凭据，并自动继续被中断的查询。"""
        self.cookies = dict(cookies)
        try:
            self.creds, _ = load_credentials()
        except AceError:
            self.creds = {"account": account, "cookies": cookies}
        name = (account or {}).get("user_name") or cookies.get("user_name") or "未知"
        self._set_login_state("重新登录", "已登录：%s" % name)
        self.status_var.set("登录成功 · 凭据已更新（%s）" % os.path.basename(path))
        try:
            self.dq_status_var.set("登录成功 · 凭据已更新")
        except Exception:
            pass
        if self._pending_query_snapshot:
            self._pending_query_snapshot = None
            self._auth_resume_used = True   # 自动续跑只做一次
            self.root.after(400, lambda: self.on_query(resume=True))

    # -- 主线程事件泵（处理子线程回传） -----------------------------------
    def _poll_queue(self):
        try:
            while True:
                item = self.ui_queue.get_nowait()
                kind = item[0]
                if kind == "records":
                    self._render_records(item[1], item[2])
                elif kind == "error":
                    self._on_query_error(item[1])
                elif kind == "thumb":
                    self._set_thumb(item[1], item[2], item[3], item[4])
                elif kind == "thumb_fail":
                    self._thumb_failed(item[1], item[2], item[3])
                elif kind == "open_image":
                    self.show_full_image(item[1], item[2])
                elif kind == "open_image_error":
                    self._alert("下载失败", item[1])
                elif kind == "dq_page":
                    self._render_dq_page(item[1], item[2], item[3], item[4])
                elif kind == "dq_error":
                    self._on_dq_error(item[1])
        except queue.Empty:
            pass
        except Exception as exc:  # 避免事件泵被打断
            print("[poll_queue] %s" % exc, file=sys.stderr)
        try:
            self.root.after(60, self._poll_queue)
        except Exception:
            pass

    # -- 查询（单一入口：一次点击同时查两类数据） --------------------------
    def on_query(self, resume=False):
        if not resume:
            self._auth_resume_used = False
        raw = self.openid_var.get()
        # 顶部入口一次填入多个账号（多行粘贴或逗号 / 空格分隔）→ 直接在主窗口内批量查询
        batch_openids, _ignored = parse_openid_list(raw)
        if len(batch_openids) > 1:
            self.start_batch_query(batch_openids)
            return
        if self.batch_mode != "single":
            # 单账号查询回到「可疑用户核查 + 数据查询」视图（批量汇总结果仍保留，可随时返回）
            if self.batch_openids and hasattr(self, "detail_bar_var"):
                self.detail_bar_var.set("正在查看单个 OpenID 的查询结果（批量汇总仍保留，"
                                        "可点击右侧按钮返回）")
            self._show_sections("single")
        openid = clean_openid(raw)
        if not openid:
            self._alert("提示", "请输入需要查询的 OpenID。", kind="warn")
            return
        try:
            begin_time, end_time = resolve_dates(
                self.begin_var.get().strip(), self.end_var.get().strip()
            )
        except AceError as exc:
            self._alert("日期错误（可疑用户核查）", _format_error(exc), kind="warn")
            return
        try:
            dq_begin, dq_end = resolve_dq_dates(
                self.dq_begin_var.get().strip(), self.dq_end_var.get().strip()
            )
        except AceError as exc:
            self._alert("日期错误（数据查询）", _format_error(exc), kind="warn")
            return
        if not self.cookies:
            try:
                self.creds, self.cookies = load_credentials()
            except AceError as exc:
                self.open_login_dialog(reason=_format_error(exc))
                return

        self.current_openid = openid
        cookies = dict(self.cookies)
        # 可疑级别固定为 all：界面上已移除级别选择控件
        level = "all"

        # 区块 1（可疑用户核查）重置
        self._clear_results()
        self.render_done = False
        self.pending_thumbs = 0
        self.first_shot = None
        self.status_var.set("查询中…（OpenID %s，%s ~ %s）" % (openid, begin_time, end_time))

        # 区块 2（数据查询）重置（记录 OpenID 与时间范围快照，供翻页复用）
        self.dq_openid = openid
        self.dq_begin_time = dq_begin
        self.dq_end_time = dq_end
        self.dq_count = 0
        self.dq_page = 1
        self.dq_render_done = False
        self.dq_page_var.set("")
        self._clear_dq_rows()
        self.dq_prev_btn.config(state="disabled")
        self.dq_next_btn.config(state="disabled")
        self.dq_status_var.set("查询中…（OpenID %s，第 1 页，%s ~ %s）" % (openid, dq_begin, dq_end))

        # 两类查询并行发起，全部完成后再恢复入口按钮
        self._query_pending = 2
        self.query_btn.config(state="disabled")
        threading.Thread(
            target=self._query_worker,
            args=(openid, begin_time, end_time, level, cookies),
            daemon=True,
        ).start()
        threading.Thread(
            target=self._dq_worker,
            args=(openid, 1, DQ_PAGE_SIZE, cookies, dq_begin, dq_end),
            daemon=True,
        ).start()

    def _task_done(self):
        """一次点击派生的子任务逐个完成，全部结束后恢复入口按钮。"""
        self._query_pending = max(0, self._query_pending - 1)
        if self._query_pending == 0:
            self.query_btn.config(state="normal")

    def _query_worker(self, openid, begin_time, end_time, level, cookies):
        try:
            data = query_openid(openid, begin_time, end_time, level, cookies)
            records = extract_records(data, openid)
            self.ui_queue.put(("records", records, openid))
        except AceError as exc:
            self.ui_queue.put(("error", exc))
        except Exception as exc:
            self.ui_queue.put(("error", AceError("查询异常: %s" % exc)))

    def _on_query_error(self, exc):
        self.render_done = True
        self._task_done()
        self.status_var.set("查询失败：" + exc.message)
        if getattr(exc, "auth", False):
            self._handle_auth_failure(exc)
            return
        self._alert("可疑用户核查查询失败", _format_error(exc))

    # -- 渲染 -------------------------------------------------------------
    def _clear_results(self):
        for child in self.result_frame.winfo_children():
            child.destroy()
        self.thumb_refs.clear()

    def _render_records(self, records, openid):
        tk = self.tk
        ttk = self.ttk
        c = self.colors
        f = self.fonts
        self._task_done()

        if not records:
            self.status_var.set("查询完成：未查询到 OpenID %s 的数据记录" % openid)
            panel = tk.Frame(self.result_frame, bg=c["surface"])
            panel.pack(fill="x", padx=14, pady=14)
            tk.Label(
                panel, text="未查询到该 OpenID 在所选时间范围内的数据记录。",
                bg=c["surface"], fg=c["on_surface"], font=f["section"], anchor="w",
            ).pack(fill="x", padx=18, pady=(18, 4))
            tk.Label(
                panel, text="可尝试放宽时间范围，或将「可疑级别」设为 all。",
                bg=c["surface"], fg=c["secondary_text"], font=f["body"], anchor="w",
            ).pack(fill="x", padx=18, pady=(0, 18))
            self.render_done = True
            return

        self.status_var.set("查询成功，共 %d 条记录（OpenID %s）" % (len(records), openid))

        for rec in records:
            data_type = rec.get("data_type", "")
            dt_label = DATA_TYPE_LABELS.get(data_type, data_type or "-")
            level_key = rec.get("level", "")
            lv_label = LEVEL_LABELS.get(level_key, level_key or "-")

            card = self._card(self.result_frame)
            # 右侧缩略图容器：显式给出高度，避免被左侧文本压扁导致缩略图非正方形
            right = tk.Frame(card, bg=c["surface"], width=THUMB_SIZE[0] + 24,
                             height=THUMB_SIZE[1] + 44)
            right.pack(side="right", fill="y", padx=(0, CARD_PAD_X), pady=CARD_PAD_Y)
            right.pack_propagate(False)

            left = tk.Frame(card, bg=c["surface"])
            left.pack(side="left", fill="both", expand=True, padx=(CARD_PAD_X, SPACE_M),
                      pady=CARD_PAD_Y)

            head = tk.Frame(left, bg=c["surface"])
            head.pack(anchor="w", fill="x")
            tk.Label(
                head, text="上报日期：%s" % (rec.get("log_date") or "-"),
                bg=c["surface"], fg=c["on_surface"], font=f["body_bold"],
            ).pack(side="left")
            self._chip(head, dt_label).pack(side="left", padx=(12, 0))

            lv_colors = c["level"].get(str(level_key).lower())
            if lv_colors:
                self._chip(head, "级别 · %s" % lv_label, fg=lv_colors[1], bg=lv_colors[0]).pack(
                    side="left", padx=(6, 0)
                )
            else:
                self._chip(head, "级别 · %s" % lv_label).pack(side="left", padx=(6, 0))

            tk.Label(
                left, bg=c["surface"], anchor="w", justify="left", font=f["body"],
                fg=c["secondary_text"], text="记录时间：%s" % (rec.get("log_time") or "-"),
            ).pack(anchor="w", pady=(6, 0))

            if data_type == "screenshot":
                url = build_image_url(rec.get("des"))
                holder = tk.Label(right, bg=c["surface_variant"], text="加载中…", width=18,
                                  height=6, fg=c["secondary_text"], font=f["body"],
                                  cursor="hand2")
                holder.pack()
                btn = ui_button(right, c, text="查看原图", style="Text.TButton",
                                state="disabled")
                btn.pack(pady=(8, 2))
                self.pending_thumbs += 1
                threading.Thread(
                    target=self._thumb_worker,
                    args=(url, holder, btn),
                    daemon=True,
                ).start()
            else:
                right.pack_forget()
                tk.Label(
                    left, bg=c["surface"], anchor="w", justify="left", font=f["body"],
                    fg=c["secondary_text"], wraplength=920,
                    text="路径/描述：%s" % (rec.get("des") or "-"),
                ).pack(anchor="w", pady=(6, 0))

        self.render_done = True

    # -- 数据查询（区块 2） ------------------------------------------------
    def on_dq_prev(self):
        if self.dq_page > 1:
            self._dq_request(self.dq_page - 1)

    def on_dq_next(self):
        self._dq_request(self.dq_page + 1)

    def _dq_request(self, page):
        """翻页：复用顶部入口最近一次查询的 OpenID 与时间范围快照。"""
        if not self.dq_openid:
            self._alert("提示", "请先在窗口顶部输入 OpenID 并点击「查询」。", kind="warn")
            return
        self.dq_page = page
        self.dq_render_done = False
        self.dq_prev_btn.config(state="disabled")
        self.dq_next_btn.config(state="disabled")
        self.dq_status_var.set("查询中…（OpenID %s，第 %d 页）" % (self.dq_openid, page))
        cookies = dict(self.cookies)
        threading.Thread(
            target=self._dq_worker,
            args=(self.dq_openid, page, DQ_PAGE_SIZE, cookies,
                  self.dq_begin_time, self.dq_end_time),
            daemon=True,
        ).start()

    def _dq_worker(self, openid, page, page_size, cookies, begin_time, end_time):
        try:
            count, items = query_punish(openid, page, page_size, cookies, begin_time, end_time)
            self.ui_queue.put(("dq_page", count, items, page, openid))
        except AceError as exc:
            self.ui_queue.put(("dq_error", exc))
        except Exception as exc:
            self.ui_queue.put(("dq_error", AceError("查询异常: %s" % exc)))

    def _on_dq_error(self, exc):
        self.dq_render_done = True
        self._task_done()
        self.dq_prev_btn.config(state="normal" if self.dq_page > 1 else "disabled")
        self.dq_status_var.set("查询失败：" + exc.message)
        if getattr(exc, "auth", False):
            self._handle_auth_failure(exc)
            return
        self._alert("数据查询失败", _format_error(exc))

    def _clear_dq_rows(self):
        for item in self.dq_tree.get_children():
            self.dq_tree.delete(item)
        self._tree_row_tags.clear()
        self._tree_hover_row = None

    # -- 表格行悬停（M3 轻量反馈；选中态由 ttk 样式负责） -----------------
    def _set_tree_hover(self, row):
        if row == self._tree_hover_row:
            return
        previous = self._tree_hover_row
        self._tree_hover_row = row
        for iid, enabled in ((previous, False), (row, True)):
            if not iid or not self.dq_tree.exists(iid):
                continue
            base = tuple(self._tree_row_tags.get(iid, ()))
            self.dq_tree.item(iid, tags=(base + ("hover",)) if enabled else base)

    def _on_tree_motion(self, event):
        try:
            row = self.dq_tree.identify_row(event.y)
        except Exception:
            row = ""
        self._set_tree_hover(row or None)

    def _render_dq_page(self, count, items, page, openid):
        self.dq_render_done = True
        self._task_done()
        self.dq_count = count
        self._clear_dq_rows()

        if not items:
            # 无数据：表格收回最小占位高度，避免留下一大片空白
            self.dq_tree.configure(height=DQ_TREE_MIN_ROWS)
            if page > 1:
                self.dq_status_var.set(
                    "第 %d 页没有数据（OpenID %s，共 %d 条），请返回上一页。" % (page, openid, count)
                )
                self.dq_prev_btn.config(state="normal")
            else:
                self.dq_status_var.set("查询完成：OpenID %s 暂无处罚数据记录。" % openid)
                self.dq_prev_btn.config(state="disabled")
                self.dq_page_var.set("共 %d 条" % count)
            self.dq_next_btn.config(state="disabled")
            return

        for index, rec in enumerate(items):
            values = []
            for _, field, _, _, _ in DQ_COLUMNS:
                value = rec.get(field)
                values.append("-" if value in (None, "") else str(value))
            tag = "odd" if index % 2 else "even"
            row_id = self.dq_tree.insert("", "end", values=values, tags=(tag,))
            self._tree_row_tags[row_id] = (tag,)
        self._tree_hover_row = None

        # 表格高度按本页实际行数自适应（≤ 每页条数）：整页纵向滚动即可看全，表格自身不内滚
        self.dq_tree.configure(height=max(DQ_TREE_MIN_ROWS, min(len(items), DQ_PAGE_SIZE)))

        total_pages = max(1, (count + DQ_PAGE_SIZE - 1) // DQ_PAGE_SIZE)
        self.dq_status_var.set(
            "查询成功，共 %d 条记录（OpenID %s，第 %d/%d 页）" % (count, openid, page, total_pages)
        )
        self.dq_page_var.set("第 %d / %d 页 · 共 %d 条" % (page, total_pages, count))
        self.dq_prev_btn.config(state="normal" if page > 1 else "disabled")
        self.dq_next_btn.config(state="normal" if page < total_pages else "disabled")

    # -- 缩略图 -----------------------------------------------------------
    def _thumb_worker(self, url, holder, btn):
        try:
            raw = download_image(url, dict(self.cookies))
            self.ui_queue.put(("thumb", raw, holder, btn, url))
        except AceError as exc:
            self.ui_queue.put(("thumb_fail", holder, btn, exc.message))
        except Exception as exc:
            self.ui_queue.put(("thumb_fail", holder, btn, "缩略图加载失败: %s" % exc))

    def _set_thumb(self, raw, holder, btn, url):
        try:
            from PIL import Image, ImageTk
            img = Image.open(io.BytesIO(raw))
            img.load()
            thumb = make_square_thumb(img, THUMB_SIZE[0])  # 正方形：等比缩放 + 居中留白
            photo = ImageTk.PhotoImage(thumb)
        except Exception as exc:
            self._thumb_failed(holder, btn, "图片解析失败: %s" % exc)
            return

        self.thumb_refs.append(photo)
        holder.config(image=photo, text="", width=photo.width(), height=photo.height(),
                      bg=self.colors["surface_variant"], relief="flat", bd=0)
        holder.bind("<Button-1>", lambda e, r=raw: self.show_full_image(r, "截图原图"))
        btn.config(state="normal", command=lambda r=raw: self.show_full_image(r, "截图原图"))
        if self.first_shot is None:
            self.first_shot = (raw, "截图原图")
        self.pending_thumbs = max(0, self.pending_thumbs - 1)

    def _thumb_failed(self, holder, btn, msg):
        try:
            holder.config(text="[截图\n加载失败]", fg=self.colors["error"], width=18, height=6)
            btn.config(state="disabled")
        except Exception:
            pass
        self.pending_thumbs = max(0, self.pending_thumbs - 1)

    # -- 原图窗口 ---------------------------------------------------------
    def show_full_image(self, raw_bytes, title="查看原图"):
        tk = self.tk
        try:
            from PIL import Image, ImageTk
            img = Image.open(io.BytesIO(raw_bytes))
            img.load()
        except Exception as exc:
            self._alert("错误", "无法解析图片: %s" % exc)
            return None

        top = tk.Toplevel(self.root)
        top.title("%s  （%d × %d 像素）" % (title, img.width, img.height))
        top.configure(bg=self.colors["viewer_bg"])

        # 标准查看尺寸 1400×900：超出则等比缩放适配，小于则按原尺寸显示
        shown = img.copy()
        if shown.width > FULL_VIEW_SIZE[0] or shown.height > FULL_VIEW_SIZE[1]:
            shown.thumbnail(FULL_VIEW_SIZE)

        photo = ImageTk.PhotoImage(shown)
        panel = tk.Label(top, image=photo, bg=self.colors["viewer_bg"], bd=0)
        panel.pack(expand=True)
        panel.image = photo  # 保持引用
        top.image = photo
        self.image_windows.append(top)

        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        w = min(FULL_VIEW_SIZE[0], max(360, sw - 80))
        h = min(FULL_VIEW_SIZE[1], max(280, sh - 120))
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h) // 3)
        top.geometry("%dx%d+%d+%d" % (w, h, x, y))
        top.transient(self.root)
        top.lift()
        top.focus_set()
        return top


# ---------------------------------------------------------------------------
# 登录窗口（Material Design：与主界面同配色、同控件层级）
# ---------------------------------------------------------------------------
class LoginDialog:
    """手机号 + 密码登录窗口。

    - 视觉与主界面完全一致：主色顶栏（取 MATERIAL["primary"]）、填充式输入框（复用 AceApp._field）、卡片分层；
    - 网络请求在子线程执行，主线程轮询结果，期间按钮显示「登录中…」并给出阶段文案；
    - 遇到 TCaptcha 滑块 / 短信验证码等无法自动完成的环节，在窗口内明确提示需人工处理，
      并提供「打开登录页手动完成验证」入口，绝不静默失败。
    """

    POLL_MS = 120

    def __init__(self, app, reason="", prefill_phone="", prefill_password=""):
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.app = app
        self.root = app.root
        self.colors = app.colors
        self.fonts = app.fonts
        self.on_success = app.on_login_success

        self.result_queue = queue.Queue()
        self._closed = False
        self._busy = False

        c, f = self.colors, self.fonts
        top = tk.Toplevel(self.root)
        self.top = top
        top.title("登录 ACE 控制台")
        top.configure(bg=c["background"])
        top.resizable(False, False)
        top.protocol("WM_DELETE_WINDOW", self.close)

        width, height = 470, 486
        self.width, self.height = width, height
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        x = max(0, (sw - width) // 2)
        y = max(0, (sh - height) // 3)
        top.geometry("%dx%d+%d+%d" % (width, height, x, y))

        # ① 应用栏（与主界面同款浅色 M3 处理：浅主色面 + 深色标题 + 主色点缀）
        appbar = tk.Frame(top, bg=c["appbar_bg"])
        appbar.pack(fill="x")
        tk.Label(appbar, text="登录 ACE 控制台", bg=c["appbar_bg"], fg=c["on_surface"],
                 font=f["appbar"]).pack(anchor="w", padx=20, pady=(14, 0))
        tk.Label(appbar, text="登录成功后自动更新本地凭据，并继续刚才的查询",
                 bg=c["appbar_bg"], fg=c["on_surface_variant"], font=f["appbar_sub"]).pack(
            anchor="w", padx=20, pady=(2, 14))
        tk.Frame(top, bg=c["outline_variant"], height=1).pack(fill="x")

        # ② 说明条（登录态失效原因 / 人工验证提示）
        self.reason_var = tk.StringVar(value=reason or "")
        self.reason_label = tk.Label(
            top, textvariable=self.reason_var, bg=c["primary_light"], fg=c["chip_fg"],
            font=f["caption"], justify="left", anchor="w", wraplength=width - 74,
        )
        if reason:
            self.reason_label.pack(fill="x", padx=16, pady=(14, 0))

        # ③ 表单卡片：手机号（+86 固定区号）+ 密码
        # 弹窗内的浮动面板挂 overlay 档（比正文卡片更重的投影），
        # 阴影留白由该档位统一给出，故外层 padx 归零，卡片表面与上方说明条基本对齐。
        card = ui_card(top, c, padx=0, pady=(SPACE_M, 0), mode="x", elevation="overlay")
        body = tk.Frame(card, bg=c["surface"])
        body.pack(fill="x", padx=CARD_PAD_X, pady=SPACE_L)

        self.phone_var = tk.StringVar(
            value="".join(ch for ch in str(prefill_phone or "") if ch.isdigit())
        )
        self.password_var = tk.StringVar(value=prefill_password or "")

        phone_field = app._field(body, "手机号（区号固定 +86）", self.phone_var, width=22,
                                 bg=c["surface"])
        phone_field["box"].pack(fill="x")
        self.phone_entry = phone_field["input"]
        area = self.phone_entry.master
        self.phone_entry.pack_forget()
        tk.Label(area, text="+86", bg=c["surface_variant"], fg=c["chip_fg"],
                 font=f["body_bold"]).pack(side="left", padx=(10, 0), pady=6)
        self.phone_entry.pack(side="left", fill="x", expand=True, padx=(8, 10), pady=6)

        password_field = app._field(body, "登录密码", self.password_var, width=22,
                                    bg=c["surface"])
        password_field["box"].pack(fill="x", pady=(18, 0))
        self.password_entry = password_field["input"]
        self.password_entry.config(show="●")

        # ④ 错误 / 状态提示（Material 语义色）
        self.message_var = tk.StringVar(value="")
        self.message_label = tk.Label(body, textvariable=self.message_var, bg=c["surface"],
                                      fg=c["error"], font=f["caption"], justify="left",
                                      anchor="w", wraplength=width - 74)
        self.message_label.pack(fill="x", pady=(14, 0))

        # ⑤ 操作区：主按钮 + 次按钮（人工验证入口按需出现）
        actions = tk.Frame(top, bg=c["background"])
        actions.pack(fill="x", padx=16, pady=(16, 0))
        self.actions = actions
        self.login_btn = ui_button(actions, c, text="登录", style="Primary.TButton",
                                   command=self.submit)
        self.login_btn.pack(fill="x")
        self.manual_btn = ui_button(actions, c, text="打开登录页手动完成验证",
                                    style="Outlined.TButton", command=self.open_login_page)
        self.cancel_btn = ui_button(actions, c, text="取消", style="Text.TButton",
                                    command=self.close)
        self.cancel_btn.pack(pady=(10, 0))

        tk.Frame(top, bg=c["divider"], height=1).pack(fill="x", pady=(16, 0))
        tk.Label(top, text="凭据仅保存在本机 ace_session_credentials.json（写入前自动备份旧文件）。",
                 bg=c["background"], fg=c["secondary_text"], font=f["caption"],
                 justify="left", anchor="w", wraplength=width - 74).pack(
            fill="x", padx=20, pady=(10, 14))

        # 回车提交 / ESC 关闭
        for widget in (top, self.phone_entry, self.password_entry):
            widget.bind("<Return>", lambda event: self.submit())
        top.bind("<Escape>", lambda event: self.close())

        top.transient(self.root)
        top.lift()
        self._initial_focus()
        top.after_idle(self._fit_height)

    # -- 交互 -------------------------------------------------------------
    def _initial_focus(self):
        target = self.password_entry if self.password_var.get() else self.phone_entry
        try:
            target.focus_set()
            self.top.update_idletasks()
            self.top.grab_set()
        except Exception:
            pass

    def set_reason(self, text):
        self.reason_var.set(text or "")
        if text:
            self.reason_label.pack(fill="x", padx=16, pady=(14, 0))
        else:
            self.reason_label.pack_forget()
        self._fit_height()

    def _fit_height(self):
        """按内容自适应窗口高度：按钮 / 提示行数变化时不会被固定高度裁剪。"""
        try:
            self.top.update_idletasks()
            need_w = max(self.width, self.top.winfo_reqwidth())
            need_h = max(self.height, self.top.winfo_reqheight())
            x = self.top.winfo_x()
            y = self.top.winfo_y()
            limit = self.top.winfo_screenheight() - need_h - 40
            if limit > 0:
                y = min(y, limit)
            self.top.geometry("%dx%d+%d+%d" % (need_w, need_h, x, y))
            self.top.update_idletasks()
            return need_h
        except Exception:
            return None

    def open_login_page(self):
        """打开官网登录页，交由人工完成滑块 / 短信验证。"""
        try:
            webbrowser.open(LOGIN_REFERER)
            self._show_message(
                "已在浏览器打开 ACE 登录页，请完成滑块 / 短信验证后回到本窗口重试。", "info"
            )
        except Exception as exc:
            self._show_message("无法打开浏览器：%s" % exc, "error")

    def _show_message(self, text, kind="error"):
        palette = {
            "error": self.colors["error"], "warn": self.colors["warning"],
            "ok": self.colors["success"], "info": self.colors["info"],
        }
        self.message_label.config(fg=palette.get(kind, self.colors["error"]))
        self.message_var.set(text or "")
        self.top.after_idle(self._fit_height)

    def _set_busy(self, busy, button_text="登录", stage_text=None):
        self._busy = bool(busy)
        try:
            self.login_btn.config(state="disabled" if busy else "normal", text=button_text)
            self.cancel_btn.config(text="取消登录" if busy else "取消")
            self.top.update_idletasks()
        except Exception:
            pass
        if stage_text is not None:
            self._show_message(stage_text, "info")

    def submit(self):
        """回车 / 点击「登录」：先做前端校验，再在子线程里完成登录。"""
        if self._busy:
            return
        digits = "".join(ch for ch in self.phone_var.get() if ch.isdigit())
        password = self.password_var.get()
        if len(digits) < 6:
            self._show_message("请输入正确的手机号（至少 6 位数字，不含区号）。", "error")
            self.phone_entry.focus_set()
            return
        if not password:
            self._show_message("请输入登录密码。", "error")
            self.password_entry.focus_set()
            return
        self._set_busy(True, "登录中…", "正在获取 RSA 公钥…")
        threading.Thread(target=self._worker, args=(digits, password), daemon=True).start()
        self.top.after(self.POLL_MS, self._poll_result)

    # -- 子线程 -----------------------------------------------------------
    def _worker(self, phone, password):
        def progress(text):
            self.result_queue.put(("stage", text))

        try:
            cookies, account = login_by_phone(
                phone, password, region=LOGIN_REGION, timeout=30, progress=progress
            )
            self.result_queue.put(("ok", (cookies, account)))
        except AceError as exc:
            self.result_queue.put(("fail", exc))
        except Exception as exc:
            self.result_queue.put(("fail", AceError("登录异常: %s" % exc)))

    # -- 主线程轮询 -------------------------------------------------------
    def _poll_result(self):
        if self._closed:
            return
        try:
            while True:
                kind, payload = self.result_queue.get_nowait()
                if kind == "stage":
                    self._show_message(payload, "info")
                elif kind == "ok":
                    self._on_login_ok(*payload)
                    return
                elif kind == "fail":
                    self._on_login_fail(payload)
                    return
        except queue.Empty:
            pass
        try:
            self.top.after(self.POLL_MS, self._poll_result)
        except Exception:
            pass

    def _on_login_ok(self, cookies, account):
        try:
            path = write_credentials(cookies, account)
        except Exception as exc:
            self._set_busy(False, "重试登录")
            self._show_message("登录成功，但凭据写入失败：%s" % exc, "error")
            return
        self._set_busy(False, "登录成功")
        self._show_message("登录成功，凭据已更新：%s" % path, "ok")
        try:
            self.top.update()
        except Exception:
            pass
        try:
            self.on_success(cookies, account, path)
        except Exception as exc:
            print("[login] 登录成功回调失败: %s" % exc, file=sys.stderr)
        self.close()

    def _on_login_fail(self, exc):
        detail = exc.message
        if getattr(exc, "tip", None):
            detail += "\n" + exc.tip
        if getattr(exc, "manual", False):
            self._set_busy(False, "重试登录")
            self._show_message(detail, "warn")
            try:
                self.manual_btn.pack(fill="x", pady=(8, 0), before=self.cancel_btn)
            except Exception:
                pass
            self._fit_height()
        else:
            self._set_busy(False, "重试登录")
            self._show_message(detail, "error")

    def close(self):
        self._closed = True
        try:
            self.top.grab_release()
        except Exception:
            pass
        try:
            self.top.destroy()
        except Exception:
            pass
        if getattr(self.app, "login_dialog", None) is self:
            self.app.login_dialog = None


# ---------------------------------------------------------------------------
# 图形界面入口
# ---------------------------------------------------------------------------
def run_gui(args):
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    import tkinter as tk

    root = tk.Tk()
    app = AceApp(root, args)

    if args.autotest or args.autotest_dq or args.autotest_login or args.autotest_batch:
        out_dir = args.shot_out or SCRIPT_DIR
        # 含登录窗口验证时不销毁主窗口，便于在查询回归之后继续验证登录链路
        if args.autotest or args.autotest_dq:
            _run_autotest(root, app, _collect_autotest_openids(args), out_dir,
                          destroy=not args.autotest_login)
        if args.autotest_batch:
            _run_autotest_batch(root, app, _collect_autotest_batch_openids(args), out_dir)
        if args.autotest_login:
            _run_autotest_login(root, app, out_dir,
                                phone=args.login_phone or "",
                                password=args.login_password or "")
        try:
            root.destroy()
        except Exception:
            pass  # 单账号回归已在内部销毁主窗口时不再重复销毁
        return

    root.mainloop()


# ---------------------------------------------------------------------------
# 自动验证（无需人工点击即可确认缩略图 / 原图功能）
# ---------------------------------------------------------------------------
def _capture_hwnd(hwnd, out_path):
    """用 PrintWindow 做窗口级截图，不受其他窗口遮挡影响。成功返回 PIL Image，失败返回 None。"""
    import ctypes
    from ctypes import wintypes
    from PIL import Image

    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32

    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    if width <= 0 or height <= 0:
        return None

    hwindc = user32.GetWindowDC(hwnd)
    memdc = gdi32.CreateCompatibleDC(hwindc)
    bmp = gdi32.CreateCompatibleBitmap(hwindc, width, height)
    gdi32.SelectObject(memdc, bmp)
    user32.PrintWindow(hwnd, memdc, 2)  # PW_RENDERFULLCONTENT

    class _BMIH(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
            ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
            ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    class _BMI(ctypes.Structure):
        _fields_ = [("bmiHeader", _BMIH), ("bmiColors", wintypes.DWORD * 3)]

    bmi = _BMI()
    bmi.bmiHeader.biSize = ctypes.sizeof(_BMIH)
    bmi.bmiHeader.biWidth = width
    bmi.bmiHeader.biHeight = -height  # 负数 = 自顶向下
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = 0  # BI_RGB

    buf = ctypes.create_string_buffer(width * height * 4)
    gdi32.GetDIBits(memdc, bmp, 0, height, buf, ctypes.byref(bmi), 0)
    img = Image.frombuffer("RGBA", (width, height), buf, "raw", "BGRA", 0, 1).convert("RGB")
    img.save(out_path)

    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(memdc)
    user32.ReleaseDC(hwnd, hwindc)
    return img


def _grab_now(widget, out_path):
    """即时截屏（窗口级 PrintWindow，无等待 / 无重绘延迟），用于抓取「登录中…」这类瞬态状态。"""
    try:
        widget.update_idletasks()
        widget.lift()
    except Exception:
        pass
    hwnd = None
    try:
        hwnd = int(widget.wm_frame(), 16)
    except Exception:
        try:
            hwnd = widget.winfo_id()
        except Exception:
            hwnd = None
    if hwnd:
        try:
            if _capture_hwnd(hwnd, out_path) is not None:
                return out_path
        except Exception:
            pass
    from PIL import ImageGrab
    x, y = widget.winfo_rootx(), widget.winfo_rooty()
    w, h = widget.winfo_width(), widget.winfo_height()
    ImageGrab.grab(bbox=(x, y, x + w, y + h)).save(out_path)
    return out_path


def _grab_widget_image(widget, out_path):
    """截取某个 Tk 顶级窗口：优先窗口级 PrintWindow，失败退回屏幕区域抓取。"""
    for _ in range(8):
        widget.update_idletasks()
        time.sleep(0.05)
    widget.update()
    time.sleep(0.3)

    hwnd = None
    try:
        hwnd = int(widget.wm_frame(), 16)
    except Exception:
        try:
            hwnd = widget.winfo_id()
        except Exception:
            hwnd = None

    if hwnd:
        try:
            img = _capture_hwnd(hwnd, out_path)
            if img is not None:
                colors = img.getcolors(maxcolors=1 << 20)
                if colors is None or len(colors) > 1:  # 非纯色 = 抓到了真实内容
                    return out_path
        except Exception:
            pass

    # 兜底：置顶后按屏幕区域抓取
    try:
        widget.attributes("-topmost", True)
    except Exception:
        pass
    try:
        widget.lift()
        widget.update()
        time.sleep(0.4)
    except Exception:
        pass
    from PIL import ImageGrab
    x = widget.winfo_rootx()
    y = widget.winfo_rooty()
    w = widget.winfo_width()
    h = widget.winfo_height()
    try:
        ImageGrab.grab(bbox=(x, y, x + w, y + h)).save(out_path)
    except Exception as exc:  # 无交互桌面 / 远程会话下可能抓屏失败
        print("[screenshot] 屏幕抓取失败: %s" % exc, file=sys.stderr)
    return out_path


def _pump(root, seconds):
    deadline = time.time() + seconds
    while time.time() < deadline:
        root.update()
        time.sleep(0.05)


def _run_autotest(root, app, openids, out_dir, destroy=True):
    """自动验证单一入口：每个 OpenID 只在顶部点击一次「查询」，再核对同页上下两个区块是否都已填充。"""
    os.makedirs(out_dir, exist_ok=True)
    for index, openid in enumerate(openids, start=1):
        _autotest_one_click(root, app, openid, out_dir, index)
    if destroy:
        root.destroy()


def _collect_autotest_openids(args):
    """收集 --autotest / --autotest-dq 传入的 OpenID（去重，缺省 12376323）。"""
    openids = []
    for value in list(args.autotest or []) + list(args.autotest_dq or []):
        value = clean_openid(value)
        if value and value not in openids:
            openids.append(value)
    return openids or ["12376323"]


def _collect_autotest_batch_openids(args):
    """收集 --autotest-batch 传入的 OpenID（去重，缺省为空列表表示不跑批量验证）。"""
    openids = []
    for value in list(getattr(args, "autotest_batch", None) or []):
        value = clean_openid(value)
        if value and value not in openids:
            openids.append(value)
    return openids


def _run_autotest_batch(root, app, openids, out_dir, csv_path=""):
    """自动验证批量查询：真机实跑多个 OpenID，校验主窗口内进度 / 汇总列表 / 双击明细 / CSV 导出。"""
    os.makedirs(out_dir, exist_ok=True)

    # 顶部输入框填入多账号清单（等价于多行粘贴），直接触发批量查询（无弹窗）
    app.openid_var.set(", ".join(openids))
    _pump(root, 0.8)
    shot_open = os.path.join(out_dir, "gui_batch_01_open.png")
    _grab_widget_image(root, shot_open)
    print("[autotest-batch] 主窗口顶部入口截图: %s" % shot_open)
    print("[autotest-batch] 顶部清单: %s" % app.openid_var.get())

    app.on_batch_trigger()
    _pump(root, 0.5)
    shot_running = os.path.join(out_dir, "gui_batch_02_running.png")
    _grab_widget_image(root, shot_running)
    print("[autotest-batch] 主窗口内查询中（进度 / 停止）截图: %s" % shot_running)
    print("[autotest-batch] 视图: %s ｜ 进度: %s ｜ 停止按钮: %s" % (
        app.batch_mode, app.batch_running_var.get(), app.batch_stop_btn["state"]))

    deadline = time.time() + 300
    while time.time() < deadline:
        root.update()
        time.sleep(0.05)
        if app.batch_finished:
            break
    _pump(root, 0.8)
    print("[autotest-batch] 最终进度: %s" % app.batch_running_var.get())
    print("[autotest-batch] 汇总列表行数: %d（成功 %d · 失败 %d）" % (
        len(app.batch_tree.get_children()), app.batch_success_count, app.batch_fail_count))
    for iid in app.batch_tree.get_children():
        print("[autotest-batch] 汇总行: %s" % (app.batch_tree.item(iid, "values"),))

    shot_done = os.path.join(out_dir, "gui_batch_03_summary.png")
    _grab_widget_image(root, shot_done)
    print("[autotest-batch] 汇总列表截图: %s" % shot_done)

    # 双击汇总首行 → 主窗口两个区块展示该账号明细
    rows = app.batch_tree.get_children()
    if rows:
        app.batch_tree.selection_set(rows[0])
        app.batch_tree.focus(rows[0])
        _pump(root, 0.6)
        app.on_batch_row_activate()
        _pump(root, 1.8)
        index = app._batch_row_index.get(rows[0])
        entry = app.batch_results[index] if index is not None else None
        print("[autotest-batch] 双击首行明细: OpenID=%s ｜ 视图: %s ｜ 核查区块: %s ｜ 数据查询: %s" % (
            entry["openid"] if entry else "-", app.batch_mode, app.status_var.get(),
            app.dq_page_var.get() or app.dq_status_var.get()))
        print("[autotest-batch] 明细卡片数: %d ｜ 数据查询表格行数: %d" % (
            len(app.result_frame.winfo_children()), len(app.dq_tree.get_children())))
        shot_detail = os.path.join(out_dir, "gui_batch_04_detail.png")
        _grab_widget_image(root, shot_detail)
        print("[autotest-batch] 明细（主窗口）截图: %s" % shot_detail)

        # 明细页「返回汇总列表」
        app.show_batch_summary()
        _pump(root, 0.8)
        print("[autotest-batch] 返回汇总视图: %s ｜ 汇总行数: %d" % (
            app.batch_mode, len(app.batch_tree.get_children())))
        shot_back = os.path.join(out_dir, "gui_batch_05_back.png")
        _grab_widget_image(root, shot_back)
        print("[autotest-batch] 返回汇总截图: %s" % shot_back)
    else:
        print("[autotest-batch] 汇总列表为空，跳过明细验证")

    path = csv_path or os.path.join(out_dir, default_batch_csv_name())
    exported = app.export_batch_csv(path)
    print("[autotest-batch] 导出 CSV: %s" % exported)
    if exported:
        with open(exported, "r", encoding="utf-8-sig") as handle:
            lines = [line for line in handle.read().strip().splitlines() if line.strip()]
        print("[autotest-batch] CSV 行数: %d ｜ 表头: %s" % (
            len(lines), lines[0] if lines else ""))
        for line in lines[1:]:
            print("[autotest-batch] CSV 行: %s" % line)


def _run_autotest_login(root, app, out_dir, phone="", password="", timeout=45):
    """自动验证登录窗口外观与交互链路。

    覆盖点：窗口视觉、空表单校验提示、登录中状态、真实接口调用失败提示、人工验证入口。
    仅使用明显无效的测试输入，不提交任何真实凭据。
    """
    os.makedirs(out_dir, exist_ok=True)
    test_phone = phone or "10000000000"           # 明显无效测试号，不会命中真实账号
    test_password = password or "ace_autotest_invalid_pwd"

    dialog = app.open_login_dialog(reason="登录态已失效：本地凭据缺少可用的登录态，请重新登录。")
    _pump(root, 1.2)

    shot_open = os.path.join(out_dir, "gui_login_01_open.png")
    _grab_widget_image(dialog.top, shot_open)
    print("[autotest-login] 登录窗口截图: %s" % shot_open)
    print("[autotest-login] 窗口尺寸: %dx%d ｜ 登录按钮: %s" % (
        dialog.top.winfo_width(), dialog.top.winfo_height(), dialog.login_btn["text"]))

    # ① 空表单提交 → 前端校验提示（不发起网络请求）
    dialog.phone_var.set("")
    dialog.password_var.set("")
    dialog.submit()
    _pump(root, 0.4)
    print("[autotest-login] 空表单校验提示: %s" % dialog.message_var.get().replace("\n", " "))
    shot_validate = os.path.join(out_dir, "gui_login_02_validate.png")
    _grab_widget_image(dialog.top, shot_validate)
    print("[autotest-login] 校验提示截图: %s" % shot_validate)

    # ② 真实接口调用（明显无效的测试输入）：先抓「登录中」状态，再等结果
    dialog.phone_var.set(test_phone)
    dialog.password_var.set(test_password)
    dialog.submit()
    _pump(root, 0.05)
    shot_busy = os.path.join(out_dir, "gui_login_03_busy.png")
    _grab_now(dialog.top, shot_busy)
    print("[autotest-login] 登录中状态: 按钮=%s（state=%s）｜ 提示=%s" % (
        dialog.login_btn["text"], dialog.login_btn["state"],
        dialog.message_var.get().replace("\n", " ")))
    print("[autotest-login] 登录中截图: %s" % shot_busy)

    deadline = time.time() + timeout
    while time.time() < deadline:
        root.update()
        time.sleep(0.05)
        if not dialog._busy:
            break
    _pump(root, 0.8)

    print("[autotest-login] 提交结束: 窗口%s ｜ 按钮=%s ｜ 提示=%s" % (
        "已关闭（登录成功）" if dialog._closed else "仍打开",
        dialog.login_btn["text"], dialog.message_var.get().replace("\n", " ")))

    if not dialog._closed:
        print("[autotest-login] 人工验证入口是否出现: %s" % (
            "是" if dialog.manual_btn.winfo_ismapped() else "否"))
        shot_result = os.path.join(out_dir, "gui_login_04_result.png")
        _grab_widget_image(dialog.top, shot_result)
        print("[autotest-login] 结果截图: %s" % shot_result)
        dialog.close()
        _pump(root, 0.4)

    # ⑤ 人工验证分支（TCaptcha 滑块 / 短信验证码）：喂入该异常，验证提示与人工入口
    manual_dialog = app.open_login_dialog(
        reason="登录态已失效：本地凭据缺少可用的登录态，请重新登录。")
    _pump(root, 0.8)
    manual_dialog.phone_var.set(test_phone)
    manual_dialog.password_var.set(test_password)
    manual_dialog._on_login_fail(LoginManualRequired(
        "登录需要人工完成验证（滑块 / 短信验证码），程序无法自动继续。"))
    _pump(root, 0.6)
    print("[autotest-login] 人工验证提示: %s" % manual_dialog.message_var.get().replace("\n", " "))
    print("[autotest-login] 人工验证入口可见: %s" % (
        "是" if manual_dialog.manual_btn.winfo_ismapped() else "否"))
    shot_manual = os.path.join(out_dir, "gui_login_06_manual.png")
    _grab_widget_image(manual_dialog.top, shot_manual)
    print("[autotest-login] 人工验证提示截图: %s" % shot_manual)
    manual_dialog.close()
    _pump(root, 0.4)

    shot_main = os.path.join(out_dir, "gui_login_05_main_after.png")
    _grab_widget_image(root, shot_main)
    print("[autotest-login] 关闭登录窗口后的主界面截图: %s" % shot_main)
    print("[autotest-login] 顶栏登录入口文案: %s ｜ 账号状态: %s" % (
        app.login_btn["text"], app.account_label["text"]))


def _wait_query_done(root, app, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        root.update()
        time.sleep(0.05)
        if app.render_done and app.dq_render_done and app.pending_thumbs == 0:
            return True
    return False


def _print_sections_layout(root, app, openid):
    """核对结果区排版：已取消分栏与各自的独立滚动视口，整页共用一条纵向滚动条。"""
    cs, ds = app.check_section, app.dq_section
    canvas = app.page_canvas
    print("[autotest] [%s] 结果区组件: %s ｜ 整页滚动条: %s" % (
        openid,
        "已取消页签" if not hasattr(app, "notebook") else "页签仍存在",
        "已启用（整页共用）",
    ))
    print("[autotest] [%s] 分栏分隔条: %s" % (
        openid, "已移除" if not hasattr(app, "result_split") else "仍存在"))
    print("[autotest] [%s] 布局核对: 窗口 %dx%d ｜ 整页可视 %dx%d ｜ 内容高 %d ｜ scrollregion=%s" % (
        openid, root.winfo_width(), root.winfo_height(),
        canvas.winfo_width(), canvas.winfo_height(), app.page_frame.winfo_reqheight(),
        canvas.cget("scrollregion")))
    print("[autotest] [%s] 区块落点: 可疑用户核查 top=%d h=%d ｜ 数据查询 top=%d h=%d（同属一页，无重叠）" % (
        openid,
        cs.winfo_rooty() - root.winfo_rooty(), cs.winfo_height(),
        ds.winfo_rooty() - root.winfo_rooty(), ds.winfo_height()))
    print("[autotest] [%s] 「可疑用户核查」卡片区: 内容高 %d（全部展开，无内部滚动条）" % (
        openid, app.result_frame.winfo_reqheight()))
    rows = len(app.dq_tree.get_children())
    print("[autotest] [%s] 「数据查询」表格: 可视 %dx%d ｜ 行数 %d ｜ 表格配置行数 %s ｜ 表格内滚 yview=%s（无内滚应为满值）" % (
        openid, app.dq_tree.winfo_width(), app.dq_tree.winfo_height(), rows,
        app.dq_tree.cget("height"), tuple(round(v, 4) for v in app.dq_tree.yview())))


def _autotest_one_click(root, app, openid, out_dir, index=1):
    """顶部填 OpenID → 点击一次查询 → 单页内自上而下核对两个区块（含原图弹窗与翻页）。"""
    root.update()

    app.openid_var.set(openid)
    root.update()
    app.on_query()          # ← 单一入口：只点击一次
    root.update()

    ok = _wait_query_done(root, app)
    _pump(root, 1.5)
    print("[autotest] [%s] 一次点击后等待完成: %s" % (openid, "成功" if ok else "超时"))

    # ---------------- 布局：单页整体纵向滚动 ----------------
    _print_sections_layout(root, app, openid)

    # ---------------- 区块 1：可疑用户核查（页面顶部，全部卡片展开） ----------------
    _pump(root, 0.8)
    check_shot = os.path.join(out_dir, "gui_page_%02d_%s_top.png" % (index, openid))
    _grab_widget_image(root, check_shot)
    print("[autotest] [%s] 单页顶部截图(整页滚到顶): %s" % (openid, check_shot))
    print("[autotest] [%s] 上方区块状态: %s" % (openid, app.status_var.get()))
    print("[autotest] [%s] 下方区块状态: %s" % (openid, app.dq_status_var.get()))
    print("[autotest] [%s] 缩略图数量: %d, first_shot: %s" % (
        openid, len(app.thumb_refs), "有" if app.first_shot else "无"))
    print("[autotest] [%s] 缩略图尺寸: %s" % (openid, ", ".join(
        "%dx%d" % (p.width(), p.height()) for p in app.thumb_refs)))

    def _walk(w):
        out = []
        for c in w.winfo_children():
            out.append(c)
            out.extend(_walk(c))
        return out

    for w in _walk(app.result_frame):
        if w.winfo_class() == "Label" and w.cget("image"):
            print("[autotest] [%s] 缩略图控件 %dx%d（父容器 %dx%d）" % (
                openid, w.winfo_width(), w.winfo_height(),
                w.master.winfo_width(), w.master.winfo_height()))

    # 整页滚到底：验证单一滚动条能一路看到「数据查询」表格与分页条
    app.page_canvas.yview_moveto(1.0)
    _pump(root, 0.5)
    print("[autotest] [%s] 整页滚到底 yview=%s ｜ 表格 yview=%s" % (
        openid, tuple(round(v, 4) for v in app.page_canvas.yview()),
        tuple(round(v, 4) for v in app.dq_tree.yview())))
    bottom_shot = os.path.join(out_dir, "gui_page_%02d_%s_bottom.png" % (index, openid))
    _grab_widget_image(root, bottom_shot)
    print("[autotest] [%s] 整页滚到底截图: %s" % (openid, bottom_shot))
    app.page_canvas.yview_moveto(0.0)
    root.update()

    if app.first_shot:
        raw, title = app.first_shot
        top = app.show_full_image(raw, title)
        if top is not None:
            _pump(root, 1.2)
            full_path = os.path.join(out_dir, "gui_full_%02d_%s.png" % (index, openid))
            _grab_widget_image(top, full_path)
            print("[autotest] [%s] 原图查看窗口截图: %s" % (openid, full_path))
            try:
                top.destroy()
            except Exception:
                pass
        else:
            print("[autotest] [%s] 未能打开原图窗口" % openid)
    else:
        print("[autotest] [%s] 未获得可查看的截图记录" % openid)

    # ---------------- 区块 2：数据查询（下方，独立滚动，未再次点击查询） ----------------
    _pump(root, 0.8)
    dq_shot = os.path.join(out_dir, "gui_page_%02d_%s_dq.png" % (index, openid))
    _grab_widget_image(root, dq_shot)
    print("[autotest] [%s] 同页双区块截图(下方区块表格): %s" % (openid, dq_shot))
    print("[autotest] [%s] 状态: %s" % (openid, app.dq_status_var.get()))
    print("[autotest] [%s] 分页信息: %s" % (openid, app.dq_page_var.get()))
    print("[autotest] [%s] 表格行数: %d" % (openid, len(app.dq_tree.get_children())))
    if app.dq_tree.get_children():
        print("[autotest] [%s] 首行数据: %s" % (
            openid, app.dq_tree.item(app.dq_tree.get_children()[0], "values")))

    if str(app.dq_next_btn["state"]) == "normal":
        app.on_dq_next()
        deadline = time.time() + 60
        while time.time() < deadline:
            root.update()
            time.sleep(0.05)
            if app.dq_render_done:
                break
        _pump(root, 0.8)

        shot2 = os.path.join(out_dir, "gui_page_%02d_%s_p2.png" % (index, openid))
        _grab_widget_image(root, shot2)
        print("[autotest] [%s] 第 2 页截图: %s" % (openid, shot2))
        print("[autotest] [%s] 第 2 页分页信息: %s" % (openid, app.dq_page_var.get()))
        print("[autotest] [%s] 第 2 页行数: %d" % (openid, len(app.dq_tree.get_children())))

        app.on_dq_prev()
        deadline = time.time() + 60
        while time.time() < deadline:
            root.update()
            time.sleep(0.05)
            if app.dq_render_done:
                break
        _pump(root, 0.6)
        print("[autotest] [%s] 翻回上一页后的分页信息: %s" % (openid, app.dq_page_var.get()))
    else:
        print("[autotest] [%s] 数据不足一页，无需验证翻页" % openid)

    print("[autotest] [%s] 单入口联合查询结果 → 可疑用户核查: %s ｜ 数据查询: %s" % (
        openid,
        app.status_var.get(),
        app.dq_page_var.get() or app.dq_status_var.get(),
    ))


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def parse_args():
    parser = argparse.ArgumentParser(
        description="查询 ACE 控制台「账号核查」页面中某个 OpenID 的数据。默认启动图形界面。",
    )
    parser.add_argument("openid", nargs="?", default=None, help="要查询的 OpenID，如 12376323（GUI 模式下用于预填）")
    parser.add_argument("--cli", action="store_true", help="使用文本表格模式（旧用法），不启动图形界面")
    parser.add_argument("--begin", default=None,
                        help="覆盖固定时间范围的开始日期 YYYY-MM-DD（不传：核查近 30 天、数据查询回溯 10 年）")
    parser.add_argument("--end", default=None,
                        help="覆盖固定时间范围的结束日期 YYYY-MM-DD（不传：今天）")
    parser.add_argument(
        "--level", default="all", choices=["all", "high", "middle", "low"],
        help="可疑级别筛选，默认 all",
    )
    parser.add_argument("--json", action="store_true", dest="dump_json", help="CLI 模式下额外打印原始 JSON 响应")
    parser.add_argument("--des-max", type=int, default=60, help="CLI 模式下路径/描述列的截断字符数，0 表示不截断")
    parser.add_argument("--autotest", action="append", nargs="?", const="12376323", default=None,
                        metavar="OPENID",
                        help="自动化验证单一入口：可多次传入 OpenID，逐个「点击一次查询」并核对同页上下两个区块（不带值时默认 12376323），验证后退出")
    parser.add_argument("--autotest-dq", action="append", default=None, metavar="OPENID",
                        help="同 --autotest（兼容旧参数），可多次传入 OpenID")
    parser.add_argument("--autotest-batch", action="append", nargs="?", const="12376323", default=None,
                        metavar="OPENID",
                        help="自动化验证批量查询：可多次传入 OpenID，一次跑完批量流程"
                             "（清单识别 → 进度 / 停止 → 汇总表 → 单击明细 → 导出 CSV）后退出")
    parser.add_argument("--shot-out", default=None, help="自动化验证截图的输出目录（默认脚本所在目录）")
    parser.add_argument("--autotest-login", action="store_true",
                        help="自动验证登录窗口（截图 + 空表单校验 + 真实接口失败提示 + 人工验证入口），可与 --autotest 连用")
    parser.add_argument("--login-phone", default=None,
                        help="登录窗口自动验证时预填的手机号（仅测试用，建议留空使用明显无效的测试号）")
    parser.add_argument("--login-password", default=None,
                        help="登录窗口自动验证时预填的密码（仅测试用；请勿在命令行填写真实密码）")
    return parser.parse_args()


def main():
    setup_stdout()
    args = parse_args()

    # 授权校验（license-guard）：独立启动（GUI / --cli）时先校验，
    # 不通过则进程按后端策略结束，业务代码不会执行；通过后开启看门狗。
    if license_boot is None:
        raise SystemExit("授权组件缺失（license_boot.py）：%s\n"
                         "请确认程序目录下存在 license_boot.py、license_config.py "
                         "与 vendor/license_guard.py。" % _LICENSE_BOOT_ERROR)
    license_boot.ensure_license(component="ace_query")

    if args.cli:
        try:
            run_cli(args)
        except AceError as exc:
            print("\n[错误] " + exc.message, file=sys.stderr)
            if exc.tip:
                print("[提示] " + exc.tip, file=sys.stderr)
            sys.exit(1)
        return

    run_gui(args)


if __name__ == "__main__":
    main()
