#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ACE 控制台查询引擎（由 joint_query.py 调用，不再提供独立入口）

职责:
    - 登录：手机号 + 密码走 ACE 登录接口，凭据写入程序目录 ace_session_credentials.json；
    - 「可疑用户核查」（page_type=user_check_v2）：query_openid / extract_records，
      对应「端游客户端安全 → 可疑用户核查 → 账号核查」，默认查近 30 天（含今天）；
    - 「数据查询」（page_type=query_by_openid）：query_punish，默认查近 10 年全部处罚记录，
      表格字段为 处罚方案 / 处罚描述 / 作弊时间 / 处罚时间 / 处罚时长；
    - 登录态失效识别：LoginManualRequired / AceError，由调用方决定是否弹登录窗；
    - 截图下载：build_image_url / download_image。

约定:
    - 登录凭据从同目录下的 ace_session_credentials.json 读取（可用环境变量
      ACE_PROJECT_DIR 指定额外回退目录），绝不硬编码。
    - 本模块只依赖 Python 标准库（urllib 等）；界面部分全部在 joint_query.py，
      因此这里不再引入 tkinter / Pillow。
    - 授权校验（license-guard）由程序入口 joint_query.py 统一完成，本模块不单独校验。
"""

import base64
import hashlib
import json
import os
import re
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

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
DQ_LOOKBACK_YEARS = 10  # 未限定日期时回溯的年数（与页面默认一致）


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
# 批量查询：汇总口径
# ---------------------------------------------------------------------------
# 可疑级别高低序（数值越大越可疑），用于汇总表取「最高可疑级别」
BATCH_LEVEL_ORDER = {"low": 1, "middle": 2, "high": 3}


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


def _post_json(url, payload, trace_header, cookies, timeout=30):
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
        with urllib.request.urlopen(request, timeout=timeout) as response:
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


def query_punish(openid, page, page_size, cookies, begin_time=None, end_time=None,
                 timeout=None):
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

    data = _post_json(url, payload, DQ_TRACE_HEADER, cookies, timeout=timeout or 30)
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
# 查询时间范围解析
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
