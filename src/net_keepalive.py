# -*- coding: utf-8 -*-
"""线程本地 keep-alive HTTP 客户端（零第三方依赖，只用标准库）

为什么需要它
------------
标准库 ``urllib.request.urlopen`` 每次调用都新建一条 TCP 连接，读完响应就关掉
（没有连接池、也不复用）。本工具一次批量查询是「编号数 × 2~3」次请求打向同两个
域名：每次都要重做 DNS + TCP + TLS 握手。公网往返 100~300ms 时，100 次请求就
白花十几秒，而这些开销对结果毫无贡献。

这里用 ``http.client`` 给**每个工作线程**持一条 keep-alive 长连接。用线程本地
存储是因为连接对象不是线程安全的，绝不能跨线程共用（会串包 / 读到别人的响应）。

设计取舍
--------
* **调用方写法不变**：返回对象支持 ``with ... as resp:`` / ``resp.read()`` /
  ``resp.status`` / ``resp.headers``，异常仍是 ``urllib.error.HTTPError`` /
  ``URLError``——两个业务模块的 ``except`` 分支一行都不用改。
* **支持系统代理**：按 ``getproxies()`` / ``proxy_bypass()`` 解析路由。走 HTTP
  代理时 http 目标用绝对 URI、https 目标用 CONNECT 隧道（``set_tunnel``），
  隧道同样复用；代理不支持 CONNECT / 需要认证 / 已退出等情况**自动回落 urllib**，
  并且把该路由标记为不可用，后续请求不再重试隧道——绝不因为代理怪异而让业务报错。
* **响应必须读干净**才能复用连接，否则下次在同一连接上读会串包。
* **只在「复用连接失败」时重试一次**：长连接空闲久了会被对端（或代理）单方面关掉，
  复用时可能失败；此时换新连接重发一次（查询类请求幂等，重发安全）。新连接就失败
  说明是真实网络故障，直接抛错，不把故障拖成两倍等待。
* 非 2xx 的响应按 ``HTTPError`` 抛出并丢弃连接（与 urllib 行为一致）。

自检会用本地 http.server 做一次真实往返，断言「两次请求只用了一条连接」。
"""

import base64
import http.client
import io
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request

__all__ = ["urlopen", "stats", "reset_stats", "close_thread_connections"]

_LOCAL = threading.local()                 # 每线程一份连接表：{key: (conn, timeout)}
_SSL_LOCK = threading.Lock()
_SSL_CONTEXT = None
_STATS_LOCK = threading.Lock()
_STATS = {"new": 0, "reused": 0, "retry": 0, "fallback": 0}
_DEFAULT_PORTS = {"http": 80, "https": 443}
_BROKEN_LOCK = threading.Lock()
_BROKEN_ROUTES = set()                     # 隧道建不起来的代理路由 → 一律交回 urllib
_FALLBACK = object()                       # 哨兵：这条路由我们不接管


def stats():
    """连接统计（供自检与日志核对优化是否真的生效）"""
    with _STATS_LOCK:
        return dict(_STATS)


def reset_stats():
    with _STATS_LOCK:
        for key in _STATS:
            _STATS[key] = 0


def _bump(key):
    with _STATS_LOCK:
        _STATS[key] = _STATS.get(key, 0) + 1


def _ssl_context():
    """进程内共用一个默认上下文（创建一次约几毫秒，别每请求建一个）

    默认上下文会校验证书链与主机名，并自动加载系统根证书（Windows 走系统证书库，
    与 urllib 的行为一致，不需要 certifi）。
    """
    global _SSL_CONTEXT
    with _SSL_LOCK:
        if _SSL_CONTEXT is None:
            _SSL_CONTEXT = ssl.create_default_context()
        return _SSL_CONTEXT


def _mark_proxy_broken(route):
    with _BROKEN_LOCK:
        _BROKEN_ROUTES.add(route)


def _proxy_route(host, scheme):
    """解析该主机的代理路由

    返回 (proxy_host, proxy_port, auth_header) → 走代理
        None                                    → 直连
        _FALLBACK                               → 判断不了 / 不支持这类代理 → 交回 urllib
    """
    try:
        proxies = urllib.request.getproxies()
    except Exception:
        return _FALLBACK
    if not proxies:
        return None
    spec = proxies.get(scheme) or proxies.get("http") or ""
    if not spec:
        return None
    try:
        if urllib.request.proxy_bypass(host):
            return None
    except Exception:
        return _FALLBACK
    if "://" not in spec:
        spec = "http://" + spec
    parts = urllib.parse.urlsplit(spec)
    if not parts.hostname or parts.scheme not in ("http", ""):
        return _FALLBACK                       # socks:// 等不支持，交回 urllib
    auth = None
    if parts.username:
        raw = "%s:%s" % (urllib.parse.unquote(parts.username),
                         urllib.parse.unquote(parts.password or ""))
        auth = "Basic " + base64.b64encode(raw.encode("utf-8")).decode("ascii")
    route = (parts.hostname, parts.port or 80, auth)
    with _BROKEN_LOCK:
        if route in _BROKEN_ROUTES:            # 之前隧道没建起来 → 别再试
            return _FALLBACK
    return route


class _Response(object):
    """urlopen 返回值的轻量替身：只实现业务代码用到的接口"""

    def __init__(self, status, reason, headers, body):
        self.status = status
        self.code = status
        self.reason = reason
        self.headers = headers
        self._body = body
        self._pos = 0

    def read(self, amt=None):
        if amt is None:
            data = self._body[self._pos:]
            self._pos = len(self._body)
            return data
        data = self._body[self._pos:self._pos + amt]
        self._pos += len(data)
        return data

    def getcode(self):
        return self.status

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def getheaders(self):
        return list(self.headers.items())

    def info(self):
        return self.headers

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def _conn_table():
    table = getattr(_LOCAL, "conns", None)
    if table is None:
        table = {}
        _LOCAL.conns = table
    return table


def _pool_key(parts, route):
    """连接缓存键：目标、协议与代理路由都不同就不能共用连接"""
    port = parts.port or _DEFAULT_PORTS.get(parts.scheme, 0)
    if route is None:
        return ("direct", parts.scheme, parts.hostname, port)
    return ("proxy", route[0], route[1], route[2], parts.scheme, parts.hostname, port)


def _new_conn(key, timeout):
    kind = key[0]
    if kind == "direct":
        _, scheme, host, port = key
        if scheme == "https":
            return http.client.HTTPSConnection(host, port, timeout=timeout,
                                              context=_ssl_context())
        return http.client.HTTPConnection(host, port, timeout=timeout)
    _, phost, pport, auth, scheme, host, port = key
    if scheme == "https":                      # CONNECT 隧道 + 与目标做 TLS
        conn = http.client.HTTPSConnection(phost, pport, timeout=timeout,
                                          context=_ssl_context())
        conn.set_tunnel(host, port,
                        headers={"Proxy-Authorization": auth} if auth else None)
        return conn
    # 明文目标走代理：不隧道，直接向代理发绝对 URI（认证头在 urlopen 里加）
    return http.client.HTTPConnection(phost, pport, timeout=timeout)


def _conn_for(key, timeout):
    """取本线程的（或新建的）连接；请求超时值与现有连接不一致时重建

    返回 (conn, reused)：reused=True 表示这是复用已有连接，失败时才值得重试。
    """
    table = _conn_table()
    hit = table.get(key)
    if hit is not None:
        conn, conn_timeout = hit
        if conn_timeout == timeout:
            return conn, True
        try:
            conn.close()
        except Exception:
            pass
        table.pop(key, None)
    _bump("new")
    conn = _new_conn(key, timeout)
    table[key] = (conn, timeout)
    return conn, False


def _drop(key, conn):
    """丢弃连接（关闭并从连接表移除）；重复调用无副作用"""
    _conn_table().pop(key, None)
    try:
        conn.close()
    except Exception:
        pass


def close_thread_connections():
    """关闭本线程持有的全部长连接（线程收尾时调用，及时归还 socket）"""
    table = getattr(_LOCAL, "conns", None)
    if not table:
        return
    for key, (conn, _timeout) in list(table.items()):
        _drop(key, conn)


def _as_urlerror(exc):
    if isinstance(exc, urllib.error.URLError):
        return exc
    return urllib.error.URLError(exc)


def _merge_headers(req):
    """把 Request 上的普通头与 unredirected 头合并成一份（同名时后者优先）"""
    merged = {}
    for source in (getattr(req, "headers", None), getattr(req, "unredirected_hdrs", None)):
        if source:
            merged.update(source)
    return merged


def urlopen(req, timeout=None, force=False):
    """在 keep-alive 长连接上发一个请求；接口与 urllib.request.urlopen 兼容

    req     : urllib.request.Request
    timeout : 秒；连接按「超时值」缓存，值变了会重建连接
    force   : 仅供自检使用——忽略系统代理、强制直连走 keep-alive（生产不要传）
    """
    url = req.full_url if hasattr(req, "full_url") else req.get_full_url()
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        _bump("fallback")
        return urllib.request.urlopen(req, timeout=timeout)

    route = None if force else _proxy_route(parts.hostname, parts.scheme)
    if route is _FALLBACK:
        _bump("fallback")
        return urllib.request.urlopen(req, timeout=timeout)

    key = _pool_key(parts, route)
    proxied = route is not None
    if proxied and parts.scheme == "http":
        path = url                              # 走 HTTP 代理时用绝对 URI
    else:
        path = urllib.parse.urlunsplit(("", "", parts.path or "/", parts.query, ""))
    method = req.get_method()
    body = getattr(req, "data", None)
    headers = _merge_headers(req)
    if proxied:
        headers = dict(headers)
        # 走代理时请求头是自己拼的：Host 必须是**目标主机**（不是代理），
        # 明文代理的认证头也随请求发给代理
        headers["Host"] = parts.netloc
        if parts.scheme == "http" and route[2]:
            headers.setdefault("Proxy-Authorization", route[2])

    def fallback(reason_exc):
        """代理隧道走不通 → 记住并交回 urllib（与未做优化时的行为完全一致）"""
        if proxied:
            _mark_proxy_broken(route)
            _bump("fallback")
            return urllib.request.urlopen(req, timeout=timeout)
        raise _as_urlerror(reason_exc)

    conn, reused = _conn_for(key, timeout)
    resp = None
    for attempt in (1, 2):
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            break
        except Exception as exc:                 # 连接被对端 / 代理关掉，或隧道建不起来
            _drop(key, conn)
            if not reused or attempt == 2:       # 新建连接就失败 → 真实故障 / 代理不兼容
                return fallback(exc)
            _bump("retry")                       # 复用的连接失效 → 新连接重发一次
            conn, reused = _conn_for(key, timeout)

    status = resp.status
    reason = resp.reason
    resp_headers = resp.headers
    body_bytes = resp.read()                     # 必须读干净，否则连接不能复用
    if getattr(resp, "will_close", False) or status >= 300:
        _drop(key, conn)
    elif reused:                                 # 本次走的是已有连接 → keep-alive 生效
        _bump("reused")
    if 300 <= status < 400:
        # 跳转交给 urllib 处理（本模块不做重定向，保持与未优化时完全一致的行为）
        _bump("fallback")
        return urllib.request.urlopen(req, timeout=timeout)
    if status >= 400:                            # 与 urllib 一致：非 2xx 抛 HTTPError
        raise urllib.error.HTTPError(url, status, reason, resp_headers, io.BytesIO(body_bytes))
    return _Response(status, reason, resp_headers, body_bytes)
