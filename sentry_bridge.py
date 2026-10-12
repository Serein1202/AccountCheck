# -*- coding: utf-8 -*-
"""Sentry 错误上报桥接（纯标准库实现，不引入任何第三方依赖）

为什么不用 sentry-sdk
---------------------
本项目约定「纯 Python 标准库」（见 joint_query.py 头部说明），因此在标准库
urllib 之上直接对接 Sentry 官方 HTTP Store 协议，行为完全自控、无需 pip 安装。

设计原则
--------
1. 零依赖、零安装：只依赖 json / urllib / traceback / linecache 等标准库；
2. 绝不影响业务：所有上报都在守护线程中异步发送，超时短、失败静默，
   上报自身的任何异常都被吞掉；
3. 隐私优先：事件送出前统一脱敏——角色ID/无端ID/手机号等 5 位以上数字、
   token/密码/Cookie/密钥类字段、本机用户名路径（C:\\Users\\xxx）全部替换；
   不上报请求体、不上报用户身份；
4. 可随时关闭：环境变量 JOINT_SENTRY=0 或把 DSN 置空即完全停用（含初始化）。

用法
----
    import sentry_bridge
    sentry_bridge.init(release=VERSION, app_name=APP_NAME, app_root=SCRIPT_DIR)
    sentry_bridge.install_excepthooks()          # 捕获主线程/子线程未处理异常
    sentry_bridge.capture_exception(exc, tags={"stage": "query"})
    sentry_bridge.capture_message("批量查询失败", level="error", extra={"失败数": 3})
    sentry_bridge.add_breadcrumb("开始查询", category="query")

环境变量
--------
    JOINT_SENTRY_DSN   覆盖内置 DSN（设为空串即关闭上报）
    JOINT_SENTRY=0     强制关闭上报（优先级最高）
    JOINT_SENTRY_ENV   环境名，默认 production（例如 staging / local）
"""

from __future__ import annotations

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
                              "sentry_bridge.py", "license_boot.py", "license_config.py"))

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
