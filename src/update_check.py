# -*- coding: utf-8 -*-
"""自动更新：检查新版本 / 断点续传下载 / 校验 / 自替换安装（纯标准库）

============================ 一句话说明 ============================
启动后（或点「检查更新」）去 Release 里问一句「有没有新版？」，有就下载、
校验、把正在运行的 exe 换掉再重启。

============================ 关键设计 ============================
1) 版本源：优先读 Release 里的 ``latest.json``（体积小，不受 GitHub API 60 次/小时
   的限流影响），失败再退回 GitHub REST API。两条路都取不到 → 报「检查失败」，
   不影响程序正常使用。
2) 国内加速：所有请求都按「加速前缀 + 原始 URL」拼出多个候选地址**并发竞速**，
   谁先成功用谁；检查阶段探明的可用前缀会缓存下来，下载时优先复用。
   加速源可用环境变量 ``JOINT_UPDATE_MIRRORS``（逗号分隔）或状态目录下的
   ``mirrors.json`` 覆盖，见 ``candidate_prefixes()``。
3) 断点续传：下载写入 ``xxx.exe.part``，用 HTTP ``Range`` 从已下字节处接着下；
   服务器不支持 Range（回 200）时自动退回整包重下；换镜像时核对
   ``Content-Range`` 总长度，长度对不上说明远端文件已换 → 丢弃旧片段重下。
   任何一次中断都保留 ``.part``，下次（含下次启动）继续。
4) 完整性：下完先比 ``size``，再比 ``SHA256``（latest.json 里带的；缺失则从
   ``AccountCheck.exe.sha256`` 资产取）；只有校验通过才改名成正式文件。
5) 自替换：Windows 不允许覆盖正在运行的 exe，于是交给一个脱离本进程的外部程序：
   主方案是 `powershell -EncodedCommand`（脚本以 Base64/UTF-16LE 传入，路径里的中文、
   空格、引号都不需要转义），兜底方案是临时 `.bat`。替换策略是「同目录临时文件 +
   原子 Move」，失败就重试，所以目标文件不会出现半截；成功后自动启动新版。
   程序在启动替换程序之后立即退出。目标目录不可写（如装在 Program Files）会
   提前探测并明确提示，不白下一场。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import queue
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

# ---------------------------------------------------------------- 常量
REPO = "Serein1202/AccountCheck"
RELEASE_TAG = "latest"                       # CI 用滚动 tag：始终覆盖同一个 Release

ASSET_EXE = "AccountCheck.exe"
ASSET_SHA = "AccountCheck.exe.sha256"
ASSET_META = "latest.json"

GITHUB = "https://github.com/%s" % REPO
API_BASE = "https://api.github.com/repos/%s" % REPO
RELEASE_DOWNLOAD = GITHUB + "/releases/download/" + RELEASE_TAG

# GitHub 加速前缀（"前缀 + 原始 URL" 即代理地址）。
# 这些公共加速服务的可用性会变，因此在多个之间并发竞速；也可自行覆盖。
DEFAULT_PREFIXES = (
    "https://gh-proxy.org/",
    "https://ghfast.top/",
    "https://ghproxy.net/",
    "https://gh.llkk.cc/",
    "https://hub.gitmirror.com/",
    "https://ghproxy.cc/",
)

ENV_MIRRORS = "JOINT_UPDATE_MIRRORS"         # 逗号分隔；设置后完全替换内置列表
ENV_DISABLE = "JOINT_UPDATE_DISABLE"         # 1/on/true = 关闭更新检查
ENV_OFFLINE = "JOINT_UPDATE_BASE"            # 测试用：整体替换加速前缀（逗号分隔）

UA = "AccountCheck-Updater/1.1 (+%s)" % GITHUB

# 版本清单必须拿最新的：Release 资产走 GitHub CDN，会缓存几分钟，
# 不加这两个头就可能把「上一版」当成最新版。
NO_CACHE = {"Cache-Control": "no-cache", "Pragma": "no-cache"}

TIMEOUT_META = 10          # 元数据请求：并发竞速时整体等待上限（秒）
TIMEOUT_CONNECT = 12       # 下载：连接/首字节
TIMEOUT_READ = 60          # 下载：单次 read 间隔
CHUNK = 1 << 18            # 256 KB

STATE_DIR_NAME = "AccountCheck"


class UpdateError(Exception):
    """更新过程中的可预期错误（网络、校验、权限等）"""


class SourceMismatch(UpdateError):
    """某个镜像给的文件长度与版本清单不符

    latest 是滚动 tag，各镜像/CDN 的缓存刷新节奏并不一致：可能出现「清单已经是
    新版、镜像还缓存着上一版」。这种源再怎么重试也没用，只能换源。
    """


# ---------------------------------------------------------------- 文本/路径小工具
def state_dir():
    """状态目录（下载片段 / 缓存 / 日志）：%LOCALAPPDATA%\\AccountCheck"""
    base = (os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
            or tempfile.gettempdir())
    path = os.path.join(base, STATE_DIR_NAME)
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        path = tempfile.gettempdir()
    return path


def updates_dir():
    path = os.path.join(state_dir(), "updates")
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        pass
    return path


def log_path():
    return os.path.join(state_dir(), "update.log")


def _log(text):
    try:
        with open(log_path(), "a", encoding="utf-8", errors="replace") as f:
            f.write("%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), text))
    except Exception:
        pass


def current_exe():
    """当前运行的 exe 路径；源码运行返回空串（源码模式不做自动替换）

    注意必须只认 ``sys.frozen``：源码运行时 ``sys.executable`` 指向的是
    python.exe，照着它去「替换」会把解释器给换掉。
    """
    if getattr(sys, "frozen", False):
        return os.path.abspath(sys.executable)
    return ""


def read_text(path):
    """读一小段文本（用于记录下载片段的来源 URL）"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except Exception:
        return ""


def write_text(path, text):
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(str(text))
        return True
    except Exception:
        return False


def read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def write_json(path, data):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- 版本比较
_VER_RE = re.compile(r"(\d+(?:\.\d+)*)")


def parse_version(text):
    """从任意字符串里取出版本元组，如 "AccountCheck v1.0.12" → (1, 0, 12)"""
    m = _VER_RE.search(str(text or ""))
    if not m:
        return ()
    return tuple(int(x) for x in m.group(1).split("."))


def compare_versions(a, b):
    """a 比 b 新返回正数，相同返回 0，更旧返回负数（长度不同按 0 补齐）"""
    va, vb = parse_version(a), parse_version(b)
    if not va or not vb:
        return 0
    n = max(len(va), len(vb))
    va = va + (0,) * (n - len(va))
    vb = vb + (0,) * (n - len(vb))
    return (va > vb) - (va < vb)


def is_newer(remote, local):
    return compare_versions(remote, local) > 0


# ---------------------------------------------------------------- 加速地址
def candidate_prefixes():
    """加速前缀候选列表（第一个为空串 = 直连 GitHub）

    覆盖顺序：环境变量 JOINT_UPDATE_MIRRORS → 状态目录 mirrors.json → 内置默认。
    环境变量设为 none / off / - 表示「不加任何加速前缀，只直连」。
    """
    raw = os.environ.get(ENV_OFFLINE)
    if raw is None:
        raw = os.environ.get(ENV_MIRRORS) or ""
    raw = raw.strip()
    bare = raw.lower() in ("none", "off", "-")
    items = [] if bare else [x.strip() for x in raw.split(",") if x.strip()]
    if not items and not bare:
        cfg = read_json(os.path.join(state_dir(), "mirrors.json"))
        if isinstance(cfg, dict):
            items = [str(x).strip() for x in (cfg.get("prefixes") or []) if str(x).strip()]
        elif isinstance(cfg, list):
            items = [str(x).strip() for x in cfg if str(x).strip()]
    prefixes = items or ([] if bare else list(DEFAULT_PREFIXES))
    out = []
    for p in [""] + prefixes:                     # 直连放第一位（在并发竞速里顺序不敏感）
        if p and not p.endswith("/"):
            p += "/"
        if p not in out:
            out.append(p)
    return out


def proxied(prefix, url):
    """按加速前缀拼出真实请求地址（prefix 为空串即直连）"""
    return (prefix + url) if prefix else url


def release_urls(base=None):
    """Release 内各资产的原始地址"""
    base = base or RELEASE_DOWNLOAD
    return {"exe": base + "/" + ASSET_EXE,
            "sha": base + "/" + ASSET_SHA,
            "meta": base + "/" + ASSET_META}


# ---------------------------------------------------------------- HTTP
def _is_ssl_error(exc):
    if isinstance(exc, ssl.SSLError):
        return True
    if isinstance(exc, urllib.error.URLError):
        return isinstance(getattr(exc, "reason", None), (ssl.SSLError, ssl.CertificateError))
    return False


def http_open(url, headers=None, timeout=10, insecure=False):
    """带自定义 UA 的 GET；重定向自动跟随"""
    hdrs = {"User-Agent": UA, "Accept": "*/*"}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, headers=hdrs)
    ctx = ssl._create_unverified_context() if insecure else None
    return urllib.request.urlopen(req, timeout=timeout, context=ctx)


def http_get(url, headers=None, timeout=10, notice=None):
    """GET：证书校验失败时降级重试一次（最终 exe 仍会做 SHA256 校验）"""
    try:
        return http_open(url, headers, timeout, insecure=False)
    except Exception as exc:
        if _is_ssl_error(exc) and notice:
            notice("SSL 证书校验失败，改用不校验证书的方式重试（下载完成后仍会核对 SHA256）")
        elif not _is_ssl_error(exc):
            raise
    return http_open(url, headers, timeout, insecure=True)


def http_text(url, timeout=10, notice=None, limit=1 << 20, headers=None):
    with http_get(url, headers=headers, timeout=timeout, notice=notice) as resp:
        data = resp.read(limit)
    return data.decode("utf-8", errors="replace")


def race(sources, worker, timeout, notice=None):
    """并发向多个候选源发同一个请求，返回最先成功者：(source, value)

    这是「国内直连 GitHub 大概率不通、公共加速站又说不准哪个能用」的应对：
    与其串行等超时，不如同时问，谁先答上就用谁。
    """
    q = queue.Queue()
    errors = []

    def run(src):
        try:
            q.put((True, src, worker(src)))
        except Exception as exc:                       # noqa: BLE001 - 逐个源记录原因
            q.put((False, src, exc))

    for src in sources:
        threading.Thread(target=run, args=(src,), daemon=True).start()

    deadline = time.time() + max(1.0, float(timeout))
    while True:
        remain = deadline - time.time()
        if remain <= 0:
            break
        try:
            ok, src, val = q.get(timeout=remain)
        except queue.Empty:
            break
        if ok:
            return src, val
        errors.append((src, val))

    brief = "；".join("%s → %s" % (s or "直连", _short(e)) for s, e in errors[:4])
    raise UpdateError("所有版本源都不可用（%s）%s" % (len(errors) or "超时",
                                                  ("：" + brief) if brief else ""))


def _short(exc):
    text = str(exc) or type(exc).__name__
    return text[:120]


# ---------------------------------------------------------------- 版本元数据
def _meta_from_json_obj(obj):
    """把 latest.json / GitHub API 的 JSON 归一成统一结构"""
    if not isinstance(obj, dict):
        return None
    version = obj.get("version") or obj.get("tag_name") or ""
    version = re.sub(r"^[vV]", "", str(version)).strip()
    if not parse_version(version):
        # tag_name 是 "latest" 这类非版本号时，从 name 里再抠一次（如 "AccountCheck v1.0.7"）
        version = ""
        m = _VER_RE.search(str(obj.get("name") or ""))
        if m:
            version = m.group(1)
    if not version:
        return None

    size = int(obj.get("size") or 0)
    sha = str(obj.get("sha256") or "").strip().lower()
    notes = obj.get("notes") or obj.get("body") or ""
    published = obj.get("published_at") or obj.get("built_at") or ""

    # 下载地址：latest.json 可自带 exe_url / sha256_url；
    # GitHub API 则要从 assets 里按资产名找，顺便补上 size。
    exe_url = str(obj.get("exe_url") or obj.get("exe_download_url") or "")
    sha_url = str(obj.get("sha256_url") or "")
    for asset in (obj.get("assets") or []):
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name") or "")
        url = str(asset.get("browser_download_url") or "")
        if name == ASSET_EXE:
            exe_url = exe_url or url
            size = size or int(asset.get("size") or 0)
        elif name == ASSET_SHA:
            sha_url = sha_url or url
    return {"version": version, "size": size, "sha256": sha,
            "notes": str(notes), "published_at": str(published),
            "exe_url": exe_url or release_urls()["exe"],
            "sha_url": sha_url or release_urls()["sha"]}


def _fetch_meta_from(prefix):
    """从某个前缀拉元数据：先 latest.json，失败再 GitHub API"""
    err = None
    try:
        text = http_text(proxied(prefix, release_urls()["meta"]), timeout=TIMEOUT_CONNECT,
                         headers=NO_CACHE)
        meta = _meta_from_json_obj(json.loads(text))
        if meta:
            meta["_source"] = (prefix or "直连") + " + latest.json"
            meta["_prefix"] = prefix
            return meta
        err = UpdateError("latest.json 内容不完整")
    except Exception as exc:                           # noqa: BLE001
        err = exc
    try:
        text = http_text(proxied(prefix, API_BASE + "/releases/tags/" + RELEASE_TAG),
                         timeout=TIMEOUT_CONNECT, notice=None, headers=NO_CACHE)
        meta = _meta_from_json_obj(json.loads(text))
        if meta:
            meta["_source"] = (prefix or "直连") + " + GitHub API"
            meta["_prefix"] = prefix
            return meta
        err = UpdateError("API 返回内容不含版本号")
    except Exception as exc:                           # noqa: BLE001
        err = exc
    raise err if isinstance(err, Exception) else UpdateError("取版本信息失败")


def fetch_latest_meta(timeout=TIMEOUT_META, notice=None, prefer_prefix=""):
    """并发竞速取「最新版本」信息，返回 dict（含 version / size / sha256 / exe_url）"""
    prefixes = candidate_prefixes()
    if prefer_prefix:
        prefixes = [prefer_prefix] + [p for p in prefixes if p != prefer_prefix]
    src, meta = race(prefixes, lambda p: _fetch_meta_from(p), timeout=timeout, notice=notice)
    if notice:
        notice("版本信息来自：%s" % meta.get("_source", src))
    log_state({"last_ok_prefix": meta.get("_prefix", ""), "last_check": time.time()})
    return meta


def _sha_from_asset(meta, notice=None):
    """latest.json 没带 sha256 时，去下 ``AccountCheck.exe.sha256`` 资产"""
    urls = []
    if meta.get("sha_url"):
        urls.append(meta["sha_url"])
    base = release_urls()["sha"]
    for prefix in candidate_prefixes():
        u = proxied(prefix, base)
        if u not in urls:
            urls.append(u)
    for url in urls:
        try:
            text = http_text(url, timeout=TIMEOUT_META, notice=notice, limit=4096)
        except Exception:                               # noqa: BLE001
            continue
        m = re.search(r"\b([0-9a-fA-F]{64})\b", text)
        if m:
            return m.group(1).lower()
    return ""


def check_for_update(current_version, timeout=TIMEOUT_META, notice=None,
                     ignore_skipped=False):
    """检查是否有新版。

    返回 info dict（有新版本）或 None（已是最新 / 用户跳过 / 检查被关闭）；
    取不到版本信息时抛 ``UpdateError``。
    """
    if updates_disabled():
        return None
    if notice:
        notice("正在检查更新（当前 v%s）…" % current_version)
    meta = fetch_latest_meta(timeout=timeout, notice=notice)
    remote = meta.get("version") or ""
    if not is_newer(remote, current_version):
        if notice:
            notice("已是最新版本（v%s）" % current_version)
        return None
    state = load_state()
    if not ignore_skipped and str(state.get("skipped") or "") and \
            compare_versions(remote, state.get("skipped")) == 0:
        if notice:
            notice("v%s 已被选择跳过（可在 update_state.json 里清掉 skipped 重新提示）" % remote)
        return None
    info = dict(meta)
    info["current"] = current_version
    if not info.get("sha256"):
        info["sha256"] = _sha_from_asset(info, notice=notice)
    if not info.get("sha256") and notice:
        notice("未取到 SHA256 校验值：下载后将只核对文件大小")
    return info


def updates_disabled():
    return str(os.environ.get(ENV_DISABLE, "")).strip().lower() in ("1", "on", "true", "yes")


# ---------------------------------------------------------------- 状态缓存
def load_state():
    data = read_json(os.path.join(state_dir(), "update_state.json"))
    return data if isinstance(data, dict) else {}


def log_state(patch):
    """把少量状态合并写入 update_state.json（失败静默）"""
    data = load_state()
    data.update(patch or {})
    write_json(os.path.join(state_dir(), "update_state.json"), data)


# ---------------------------------------------------------------- 下载（断点续传）
def _parse_total(content_range):
    """从 ``bytes 100-999/1000`` 里取出总长度"""
    m = re.search(r"/(\d+)\s*$", str(content_range or ""))
    return int(m.group(1)) if m else 0


def sha256_file(path, progress=None):
    digest = hashlib.sha256()
    size = os.path.getsize(path)
    done = 0
    with open(path, "rb") as f:
        while True:
            block = f.read(CHUNK)
            if not block:
                break
            digest.update(block)
            done += len(block)
            if progress:
                progress(done, size)
    return digest.hexdigest()


def update_paths(version, folder=None):
    """新版安装包及其下载片段的路径（UI / 自检 / 下载共用同一套命名）"""
    folder = folder or updates_dir()
    dest = os.path.join(folder, "%s-%s.exe" % (ASSET_EXE[:-4], version or "unknown"))
    return dest, dest + ".part"


def _fetch_into(part, url, expected_size=0, size_hint=0, notice=None,
                progress=None, base=0, chunk=CHUNK):
    """把 url 的数据写进 part 文件（自动从 part 现有大小续传）

    size_hint：远端文件总长度的已知值（来自 latest.json，比 Content-Range 更早可用）。
    返回写入的总字节数；任何中断都会抛异常，但已落盘的数据保留。
    """
    have = os.path.getsize(part) if os.path.exists(part) else 0
    if have and expected_size and have > expected_size:
        have = 0                                     # 旧片段比目标还大 → 一定是坏的
    if have and size_hint and have > size_hint:
        have = 0

    headers = {}
    if have:
        headers["Range"] = "bytes=%d-" % have

    resp = http_get(url, headers=headers, timeout=TIMEOUT_CONNECT, notice=notice)
    try:
        status = resp.getcode()
        clen = int(resp.headers.get("Content-Length") or 0)
        if status == 206:
            total = _parse_total(resp.headers.get("Content-Range")) or \
                (have + clen if clen else 0)
        else:
            total = clen
            if have:
                if notice:
                    notice("该源不支持断点续传（已下 %s），改为重新下载整包" % _human(have))
                have = 0                             # 服务器忽略 Range → 只能整包重下
        if total and size_hint and total != size_hint:
            # 这个源拿到的不是我们要的版本（CDN 缓存还没刷新）：换源，别在这儿耗
            raise SourceMismatch("该镜像给的是另一个构建（长度 %s，期望 %s）"
                                 % (_human(total), _human(size_hint)))
        mode = "ab" if have else "wb"
        done = have
        # 记下这段数据是谁给的：换镜像续传时靠它判断能不能接着拼（见 download_update）
        write_text(part + ".src", url)
        if progress:
            progress(done, total or size_hint or 0, None)
        with open(part, mode) as f:
            while True:
                block = resp.read(chunk)
                if not block:
                    break
                f.write(block)
                done += len(block)
                if progress:
                    progress(done, total or size_hint or 0, None)
            f.flush()
            os.fsync(f.fileno())
        if total and done < total:
            # 连接被提前掐断（响应还没读完）——已落盘的片段留着，换个源接着下
            raise UpdateError("连接提前中断（%s / %s）" % (_human(done), _human(total)))
        return done
    finally:
        try:
            resp.close()
        except Exception:
            pass


def _truncate(path):
    try:
        with open(path, "wb"):
            pass
    except Exception:
        pass


def _remove_quiet(path):
    try:
        os.remove(path)
    except Exception:
        pass


def _human(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.1f %s" % (n, unit) if unit != "B" else "%d B" % n
        n /= 1024.0


human_size = _human                        # 对外别名（CLI / GUI 都用它格式化字节数）


def probe_sources(urls, timeout=6, notice=None, expect_size=0):
    """并发探测各下载源的「首字节」响应，返回可用的源（值得优先用的排前面）

    探三件事，都很便宜（每个源一个请求，只取 1 字节）：
      · 通不通 —— 公共加速站说不准哪个还活着，与其按固定顺序等超时不如并发问一圈；
      · 认不认 Range（回 206）—— 断线是常态，能续传的源才值得优先用；
      · **长度对不对** —— latest 是滚动 tag，可能清单已更新而某些镜像还在缓存上一版，
        这种源下到底也是错的，直接排到最后（全都对不上时才退回按速度用）。
    全部失败时返回空列表，由调用方退回原始顺序。
    """
    q = queue.Queue()

    def run(url):
        t0 = time.time()
        try:
            resp = http_get(url, headers={"Range": "bytes=0-0"}, timeout=timeout, notice=None)
            code = resp.getcode()
            if code == 206:
                total = _parse_total(resp.headers.get("Content-Range"))
            else:
                total = int(resp.headers.get("Content-Length") or 0)
            try:
                resp.read(16)
            except Exception:
                pass
            try:
                resp.close()
            except Exception:
                pass
            q.put((code in (200, 206), url, time.time() - t0, code == 206, total))
        except Exception as exc:                       # noqa: BLE001
            q.put((False, url, _short(exc), False, 0))

    for url in urls:
        threading.Thread(target=run, args=(url,), daemon=True).start()

    fresh, stale, deadline = [], [], time.time() + max(1.0, float(timeout))
    for _ in range(len(urls)):
        remain = deadline - time.time()
        if remain <= 0:
            break
        try:
            ok, url, cost, resumable, total = q.get(timeout=remain)
        except queue.Empty:
            break
        if not ok:
            continue
        item = (0 if resumable else 1, cost, url)
        if expect_size and total and total != expect_size:
            stale.append(item)
        else:
            fresh.append(item)

    pool = fresh or stale
    if not pool:
        return []
    if not fresh and notice:
        notice("注意：所有镜像的长度都与版本清单不符（CDN 缓存未同步），仍按速度尝试，"
               "最终以 SHA256 校验为准")
    pool.sort(key=lambda x: (x[0], x[1]))
    if notice:
        notice("已探测可用下载源：%s%s" % (
            "、".join("%s（%.1fs%s）" % (_host(u), c, "，可续传" if p == 0 else "")
                      for p, c, u in pool[:3]),
            ("；另有 %d 个镜像缓存的是旧版本，已排到最后" % len(stale)) if stale else ""))
    return [u for _p, _c, u in pool]


def _download_source_priority(info):
    """下载源顺序：检查阶段验证过的源 → 各加速镜像 → 直连兜底

    刻意把直连放最后：api.github.com 直连通常没问题（所以检查能成功），
    但 Release 资产在 GitHub 的 CDN 上，国内直连基本等于可望不可及。
    """
    prefixes = [p for p in candidate_prefixes() if p]
    prefixes.append("")                                # 直连兜底
    pref = info.get("_prefix")
    if pref is None:
        pref = load_state().get("last_ok_prefix") or ""
    if pref in prefixes:
        prefixes = [pref] + [p for p in prefixes if p != pref]
    base = info.get("exe_url") or release_urls()["exe"]
    urls = []
    for prefix in prefixes:
        u = proxied(prefix, base)
        if u not in urls:
            urls.append(u)
    return urls


def _same_content_at_breakpoint(part, have, url, window=1 << 17):
    """换源续传前的「拼接安全」校验：比对该源断点前的一段内容与本地片段

    latest 是滚动 tag：不同镜像完全可能缓存着不同时刻的构建，直接拼起来会得到
    「大小对得上、内容全错」的坏包。这里只取断点前最多 128 KB 做内容比对 ——
    一致说明两份数据在拼接处同源，可以安全接着下；不一致就丢弃重下。
    最终仍由 SHA256 兜底，这一步的职责是「别白下、也别拼坏」。
    """
    span = min(int(have), int(window))
    if span <= 0:
        return True
    start = int(have) - span
    try:
        resp = http_get(url, headers={"Range": "bytes=%d-%d" % (start, have - 1)},
                        timeout=TIMEOUT_CONNECT, notice=None)
    except Exception:
        return False
    try:
        if resp.getcode() not in (200, 206):
            return False
        data = resp.read(span + 32)
    except Exception:
        return False
    finally:
        try:
            resp.close()
        except Exception:
            pass
    if len(data) < span:
        return False
    try:
        with open(part, "rb") as f:
            f.seek(start)
            local = f.read(span)
    except Exception:
        return False
    return local == data[:span]


def _size(path):
    try:
        return os.path.getsize(path)
    except Exception:
        return 0


def download_update(info, progress=None, notice=None, stop=None, max_rounds=2,
                    dest_dir=None, probe=True, per_source_attempts=4,
                    max_seconds=300):
    """下载新版 exe，支持断点续传；返回校验通过的本地文件路径

    progress(done, total, speed) 会被高频调用（UI 侧自行节流）；
    notice(text) 用于把「换源 / 续传 / 校验」等过程写进日志。

    源的使用策略（针对「公共加速站经常下到一半就断」的现实）：
      1) 先并发探测各源，挑「认 Range（能续传）」且响应快的；
      2) 同一个源连着重试若干次 —— 断线多为偶发，同一份数据接着下最省流量；
      3) 该源连续没有进展才换源；换源前核对断点前一段内容，对得上才接着拼，
         对不上（可能是另一个构建 / 不支持 Range）就丢弃片段整包重下；
      4) 收尾一律 SHA256 校验，只有校验通过才算成功。
    """
    version = info.get("version") or "unknown"
    size = int(info.get("size") or 0)
    want_sha = str(info.get("sha256") or "").strip().lower()

    folder = dest_dir or updates_dir()
    try:
        os.makedirs(folder, exist_ok=True)
    except Exception:
        pass
    dest, part = update_paths(version, folder)
    if os.path.exists(dest) and want_sha and sha256_file(dest) == want_sha:
        if notice:                                   # 上次已经下好并校验过了
            notice("本地已有校验通过的新版安装包：%s" % dest)
        return dest

    urls = _download_source_priority(info)
    if probe and len(urls) > 1 and not _size(part):
        if notice:
            notice("正在探测最快的下载源（%d 个候选）…" % len(urls))
        fast = probe_sources(urls, notice=notice, expect_size=size)
        if fast:
            urls = fast + [u for u in urls if u not in fast]
    # 已经有半截文件时，优先回到当初给数据那个源：latest 是滚动 tag，不同镜像
    # 可能缓存着不同的构建，拼在一起就是「大小对得上、内容全错」的坏包。
    source_marker = part + ".src"
    origin = read_text(source_marker)
    if origin and _size(part):
        urls = [origin] + [u for u in urls if u != origin]
    _cleanup_old_parts(keep=part, folder=folder)

    last_err = None
    deadline = time.time() + max(60.0, float(max_seconds))
    for round_no in range(1, max(1, max_rounds) + 1):
        for url in urls:
            for attempt in range(1, max(1, per_source_attempts) + 1):
                if stop and stop():
                    raise UpdateError("已取消")
                if time.time() > deadline:
                    raise UpdateError(
                        "下载超时（已等 %d 秒）：镜像持续中断。已下载的片段已保留，"
                        "稍后再试会从断点继续。" % int(max_seconds))
                have = _size(part)
                if have:
                    origin = read_text(source_marker)
                    if origin and origin != url:
                        if _same_content_at_breakpoint(part, have, url):
                            if notice:
                                notice("换到 %s：断点处内容一致，从 %s 接着续传"
                                       % (_host(url), _human(have)))
                        else:
                            if notice:
                                notice("镜像 %s 接不上断点（内容不同或不支持 Range），"
                                       "丢弃片段整包重下" % _host(url))
                            _truncate(part)
                            _remove_quiet(source_marker)
                            have = 0
                if notice:
                    if have:
                        notice("从 %s 处续传（%s · 第 %d 轮第 %d 次）"
                               % (_human(have), _host(url), round_no, attempt))
                    else:
                        notice("开始下载（%s · 第 %d 轮第 %d 次）"
                               % (_host(url), round_no, attempt))

                t0 = time.time()
                try:
                    _fetch_into(part, url, expected_size=size, size_hint=size,
                                notice=notice, progress=progress)
                except SourceMismatch as exc:
                    if notice:                              # 该镜像缓存了别的版本
                        notice("%s，换下一个镜像" % exc)
                    _truncate(part)                         # 片段来自这个源，一并作废
                    _remove_quiet(source_marker)
                    last_err = exc
                    break                                   # 换源
                except Exception as exc:                # noqa: BLE001 - 断线属常态
                    last_err = exc
                    got = _size(part)
                    if got > have:
                        if notice:
                            notice("该源中断（%s），已下 %s；仍用同一个源接着下"
                                   % (_short(exc), _human(got)))
                        continue                        # 有进展 → 同一源继续续传
                    if notice:
                        notice("该源没有数据下发（%s），换下一个源" % _short(exc))
                    break                               # 毫无进展 → 换源

                got = _size(part)
                if size and got != size:
                    if notice:
                        notice("大小不符（%s / 应为 %s），保留片段继续下载"
                               % (_human(got), _human(size)))
                    last_err = UpdateError("下载不完整")
                    continue
                if want_sha:
                    if notice:
                        notice("正在校验 SHA256…")
                    digest = sha256_file(part)           # 18MB 约 0.1~0.3s，不必报进度
                    if digest != want_sha:
                        if notice:
                            notice("SHA256 不符（%s…），丢弃已下内容并从别的源重下"
                                   % digest[:12])
                        _truncate(part)
                        _remove_quiet(source_marker)
                        last_err = UpdateError("SHA256 校验失败")
                        break
                    if notice:
                        notice("SHA256 校验通过：%s…" % digest[:16])
                try:
                    os.replace(part, dest)
                except Exception as exc:
                    raise UpdateError("保存安装包失败：%s" % _short(exc))
                _remove_quiet(source_marker)
                if notice:
                    notice("下载完成：%s（本次平均 %s/s）"
                           % (_human(got), _human(got / max(0.05, time.time() - t0))))
                return dest
    raise UpdateError("下载失败：%s"
                      % (_short(last_err) if last_err else "所有镜像均不可用"))


def _host(url):
    m = re.match(r"https?://([^/]+)/", str(url))
    return m.group(1) if m else url


def _cleanup_old_parts(keep="", max_age_days=7, folder=None):
    """清理过期的下载片段 / 旧安装包，避免状态目录无限增长

    正在续传的那个文件（keep）与 1 天内的其它 .part 一律保留 ——
    否则会把还没下完、等着下次接着下的片段误删掉。
    """
    folder = folder or updates_dir()
    try:
        now = time.time()
        for name in os.listdir(folder):
            path = os.path.join(folder, name)
            if path == keep or not os.path.isfile(path):
                continue
            limit = 86400 if (name.endswith(".part") or name.endswith(".src")) \
                else max_age_days * 86400
            if (now - os.path.getmtime(path)) > limit:
                try:
                    os.remove(path)
                except Exception:
                    pass
    except Exception:
        pass


# ---------------------------------------------------------------- 安装（自替换）
def can_replace(target):
    """目标目录是否可写（决定「自动替换」还是「提示手动替换」）"""
    folder = os.path.dirname(os.path.abspath(target)) or "."
    try:
        fd, probe = tempfile.mkstemp(prefix=".ac_write_", dir=folder)
        os.close(fd)
        os.remove(probe)
        return True
    except Exception:
        return False


def _win_short_path(path):
    """取 Windows 8.3 短路径（纯 ASCII）；拿不到就原样返回"""
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(4096)
        if ctypes.windll.kernel32.GetShortPathNameW(str(path), buf, 4096):
            return buf.value or path
    except Exception:
        pass
    return path


def _is_ascii(text):
    try:
        str(text or "").encode("ascii")
        return True
    except Exception:
        return False


def _path_for_bat(path):
    """给 bat 用的路径：非 ASCII（中文用户名 / 中文目录）时尝试换成 8.3 短名

    bat 是按字节交给 cmd 解析的，中文路径一旦编码对不上就会被认成别的文件名，
    替换脚本会静默失败。8.3 短名可以绕开这个问题；万一对不上，脚本会退回
    「UTF-8 BOM + chcp 65001」的写法。**主路径其实是 PowerShell**（见
    build_installer_command），bat 只是它的兜底。
    """
    if not path or _is_ascii(path):
        return path
    return _win_short_path(path)


def build_installer_script(new_exe, target, log_file=""):
    """兜底方案：生成替换脚本 .bat（等旧进程退出 → 覆盖 → 启动新版 → 自删）

    Windows 不允许覆盖正在运行的 exe，只能借道外部程序。用 .bat 而不是 .ps1，
    是为了绕开 PowerShell 执行策略；覆盖用 copy 而非 move —— 跨盘时 move 是
    「复制 + 删除」，中途出错会留下半个文件，copy 不会破坏源文件。
    """
    log_file = log_file or log_path()
    new_exe = _path_for_bat(new_exe)
    target = _path_for_bat(target)
    log_file = _path_for_bat(log_file)
    ascii_only = all(_is_ascii(p) for p in (new_exe, target, log_file))

    lines = ["@echo off"]
    if not ascii_only:                        # 路径含非 ASCII：切到 UTF-8 代码页
        lines.append("chcp 65001 >nul")
    lines += [
        "setlocal enableextensions",
        # PyInstaller onefile：继承到的解包目录标记（_MEI* / _PYI*，6.x 用
        # _PYI_*）会让新 exe 跳过解包、去用旧进程即将删除的 _MEI 目录，
        # 启动即报「Failed to load Python DLL」。按前缀清空后再 start。
        'for /f "delims==" %%v in (\'set _MEI 2^>nul\') do set "%%v="',
        'for /f "delims==" %%v in (\'set _PYI 2^>nul\') do set "%%v="',
        'set "NEW=%s"' % new_exe,
        'set "TGT=%s"' % target,
        'set "LOG=%s"' % log_file,
        'echo %date% %time% installer started (bat)>> "%LOG%"',
        "set /a tries=0",
        ":retry",
        "set /a tries+=1",
        'copy /Y "%NEW%" "%TGT%" >nul 2>&1',
        "if not errorlevel 1 goto ok",
        "if %tries% GEQ 120 goto fail",
        "ping -n 2 127.0.0.1 >nul",
        "goto retry",
        ":ok",
        'del "%NEW%" >nul 2>&1',
        'echo %date% %time% update ok >> "%LOG%"',
        'start "" "%TGT%"',
        "goto quit",
        ":fail",
        'echo %date% %time% update FAILED after %tries% tries >> "%LOG%"',
        'start "" "%TGT%"',
        ":quit",
        '(goto) 2>nul & del "%~f0"',
    ]
    path = os.path.join(tempfile.mkdtemp(prefix="ac_update_"), "apply_update.bat")
    with open(path, "w", encoding="ascii" if ascii_only else "utf-8-sig",
              errors="replace", newline="\r\n") as f:
        f.write("\n".join(lines) + "\n")
    return path


# PowerShell 替换脚本（主方案）。
# 用 -EncodedCommand（Base64 + UTF-16LE）传进去，路径里的中文、空格、引号
# 全都不需要转义，也不受 bat 编码折腾 —— 这是相比 .bat 最大的好处。
# 覆盖策略：先复制到同目录临时名，再原子替换目标文件，任何一步失败都重试，
# 因此目标文件要么是完整的旧版、要么是完整的新版，不会出现半截文件。
_PS_TEMPLATE = r"""$ErrorActionPreference = 'Stop'
$new = '__NEW__'
$tgt = '__TGT__'
$log = '__LOG__'
try { Add-Content -LiteralPath $log -Value ((Get-Date).ToString('s') + ' installer started (powershell)') } catch {}
$ok = $false
for ($i = 1; $i -le 120; $i++) {
    try {
        $tmp = $tgt + '.acnew'
        Copy-Item -LiteralPath $new -Destination $tmp -Force
        Move-Item -LiteralPath $tmp -Destination $tgt -Force
        $ok = $true
        break
    } catch {
        Start-Sleep -Milliseconds 800
    }
}
$msg = (Get-Date).ToString('s') + ' update ' + $(if ($ok) { 'ok' } else { 'FAILED' })
try { Add-Content -LiteralPath $log -Value $msg -ErrorAction SilentlyContinue } catch {}
if ($ok) { Remove-Item -LiteralPath $new -Force -ErrorAction SilentlyContinue }
# PyInstaller onefile：继承到的解包目录标记（_MEI* / _PYI*，6.x 用 _PYI_*）
# 会让新 exe 跳过解包、去加载旧进程退出即删除的 _MEI 目录里的 python DLL。
# 按前缀整体清理后再启动新 exe。
Get-ChildItem Env:_MEI*, Env:_PYI* -ErrorAction SilentlyContinue | Remove-Item -ErrorAction SilentlyContinue
Start-Process -FilePath $tgt
"""


def _ps_quote(text):
    return str(text).replace("'", "''")


def powershell_path():
    """找 powershell.exe（Windows 自带 5.1，路径固定）"""
    exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                       "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    if os.path.isfile(exe):
        return exe
    return shutil.which("powershell") or ""


def build_installer_command(new_exe, target, log_file=""):
    """主方案：拼出 powershell -EncodedCommand 的命令行（不落地任何脚本文件）"""
    script = (_PS_TEMPLATE
              .replace("__NEW__", _ps_quote(new_exe))
              .replace("__TGT__", _ps_quote(target))
              .replace("__LOG__", _ps_quote(log_file or log_path())))
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return [powershell_path(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
            "Bypass", "-WindowStyle", "Hidden", "-EncodedCommand", encoded]


def _clean_runtime_env():
    """给「重启新版 exe」用的干净环境变量

    PyInstaller onefile 的 bootloader 会在进程环境里设置一组「解包目录」
    标记变量，子进程全部继承：旧 exe → 安装器 → 新 exe。新 exe 的
    bootloader 一旦看到这些变量已存在，就**跳过解包**、直接拿旧进程的
    _MEIxxxxx 目录当自己的库目录 —— 而旧进程退出时会删掉那个目录，于是
    新 exe 报「Failed to load Python DLL ...\\python3xx.dll，找不到指定的
    模块」。这正是自动更新重启后必现弹窗的根因。

    变量名随 PyInstaller 大版本变过：
      · 旧版（≤5.x）：`_MEIPASS2`、`_MEIPASS`
      · 6.x（含 CI 用的 6.18）：`_PYI_APPLICATION_HOME_DIR`、
        `_PYI_ARCHIVE_FILE`、`_PYI_PARENT_PROCESS_LEVEL`、`_PYI_SPLASH_IPC`
        等（first attempt 只剥了 _MEIPASS2，线上仍弹窗，教训）
    所以这里按 `_MEI*` / `_PYI*` 前缀整体剥离，并顺带清掉 CPython 的
    `PYTHONHOME` / `PYTHONPATH` / `__PYVENV_LAUNCHER__`，让新 exe 像正常
    双击启动一样重新解包，今后 PyInstaller 再改名也不受影响。
    """
    env = dict(os.environ)
    for key in list(env):
        if key.startswith("_MEI") or key.startswith("_PYI"):
            env.pop(key, None)
    for key in ("__PYVENV_LAUNCHER__", "PYTHONHOME", "PYTHONPATH"):
        env.pop(key, None)
    return env


def _detached_flags():
    """替换程序的启动标志：新进程组 + 不显示窗口

    特意**不用** DETACHED_PROCESS：它会让子进程完全没有控制台，而 PowerShell 5.1
    启动时依赖控制台主机，实测结果是进程被创建但脚本一行都不执行（静默失效，
    替换永远不会发生）。CREATE_NO_WINDOW 已经足够隐藏黑框，父进程退出也不会
    带走子进程。
    """
    flags = 0
    for name in ("CREATE_NEW_PROCESS_GROUP", "CREATE_NO_WINDOW"):
        flags |= getattr(subprocess, name, 0)
    return flags


def _log_size(path):
    try:
        return os.path.getsize(path)
    except Exception:
        return 0


def _wait_log_growth(path, before, timeout=3.0):
    """等外部替换程序「现身」：它会往同一个日志里写一行起始记录

    这是对「进程创建成功但脚本一行都没跑」（例如 DETACHED_PROCESS 那次）
    的兜底检测 —— 静默失效比报错更可怕，用户会以为已经更新了。
    """
    deadline = time.time() + max(0.5, float(timeout))
    while time.time() < deadline:
        if _log_size(path) > before:
            return True
        time.sleep(0.2)
    return False


def launch_installer(new_exe, target=None, log_file="", notice=None):
    """启动替换程序（本进程退出后它才可能成功覆盖）；返回所用方式的描述

    优先用 PowerShell（路径编码无忧），启动后确认它真的跑起来了，
    否则退回 .bat 脚本。
    """
    target = target or current_exe()
    if not target:
        raise UpdateError("源码模式不支持自动替换；新版安装包已下载到：\n%s" % new_exe)
    if not can_replace(target):
        raise UpdateError(
            "安装目录不可写，无法自动替换：\n%s\n\n"
            "请用管理员身份运行本程序，或手动把下面的文件覆盖过去：\n%s"
            % (os.path.dirname(target), new_exe))
    log_file = log_file or log_path()

    if powershell_path():
        try:
            cmd = build_installer_command(new_exe, target, log_file)
            _log("install scheduled via powershell: %s -> %s" % (new_exe, target))
            before = _log_size(log_file)
            subprocess.Popen(cmd, close_fds=True, creationflags=_detached_flags(),
                             env=_clean_runtime_env())
            if _wait_log_growth(log_file, before, 4.0):
                if notice:
                    notice("替换程序已接管：程序退出后会自动覆盖并重启到新版本")
                return "powershell"
            _log("powershell installer did not start; falling back to bat")
            if notice:
                notice("替换程序未按预期启动，改用 .bat 兜底")
        except Exception as exc:                        # noqa: BLE001
            _log("powershell installer failed: %s" % _short(exc))
            if notice:
                notice("PowerShell 方式启动失败（%s），改用 .bat 兜底" % _short(exc))

    script = build_installer_script(new_exe, target, log_file)
    _log("install scheduled via bat: %s -> %s (script %s)" % (new_exe, target, script))
    before = _log_size(log_file)
    subprocess.Popen(["cmd", "/c", script], close_fds=True,
                     creationflags=_detached_flags(), cwd=os.path.dirname(script),
                     env=_clean_runtime_env())
    if not _wait_log_growth(log_file, before, 4.0):
        raise UpdateError(
            "替换程序无法启动（PowerShell 与 .bat 都没跑起来），"
            "自动更新被安全软件拦截的可能性较大。\n\n"
            "请手动把下面的文件覆盖过去：\n%s\n→ %s" % (new_exe, target))
    if notice:
        notice("替换脚本已接管：程序退出后会自动覆盖并重启到新版本")
    return script


def install_update(new_exe, target=None, notice=None):
    """执行自替换：启动替换程序 → 调用方随后应退出程序

    返回所用方式的描述；目标目录不可写 / 源码模式时抛 UpdateError。
    """
    return launch_installer(new_exe, target=target, notice=notice)


# ---------------------------------------------------------------- 离线自检
def offline_selftest(printer=None):
    """不联网的自检：版本比较 / 加速地址拼接 / 断点续传 / 校验 / 替换脚本

    返回 (ok, detail)；detail 为失败原因或一句话摘要。
    """
    say = printer or (lambda *_a, **_k: None)
    fails = []

    def need(name, cond, extra=""):
        if not cond:
            fails.append("%s%s" % (name, ("（%s）" % extra) if extra else ""))
        return bool(cond)

    # 1) 版本比较
    need("版本比较", compare_versions("1.0.5", "1.0.4") > 0 and
         compare_versions("v1.0.4", "1.0.4") == 0 and
         compare_versions("1.10.0", "1.9.9") > 0 and
         compare_versions("AccountCheck v1.0.12", "1.0.9") > 0 and
         compare_versions("1.0.4", "1.0.4") == 0)

    # 2) 加速地址拼接 + 候选顺序
    prefixes = candidate_prefixes()
    need("加速前缀候选", prefixes and prefixes[0] == "" and
         proxied("https://gh-proxy.org/", GITHUB + "/x") ==
         "https://gh-proxy.org/" + GITHUB + "/x")

    # 3) Content-Range 解析
    need("Content-Range 解析", _parse_total("bytes 100-999/1000") == 1000 and
         _parse_total("") == 0)

    # 4) sha256 资产解析格式（"<hash>  AccountCheck.exe"）
    need("sha256 资产格式", bool(re.search(r"\b([0-9a-fA-F]{64})\b",
                                          "9f2c" * 16 + "  AccountCheck.exe")))

    # 5) 断点续传：本地假服务器先「截断」，再「续传」
    ok5, detail5 = _selftest_resume(say)
    need("断点续传 / 校验", ok5, detail5)

    # 6) 替换程序生成（不执行）：主方案 PowerShell 命令行 + 兜底 .bat
    try:
        target_name = "目 标 程序.exe"           # 故意用中文 + 空格，验证编码无忧
        cmd = build_installer_command(os.path.join(tempfile.gettempdir(), "新 版.exe"),
                                     os.path.join(tempfile.gettempdir(), target_name),
                                     os.path.join(tempfile.gettempdir(), "更新 日志.log"))
        decoded = base64.b64decode(cmd[-1]).decode("utf-16-le")
        # 自重启必须剥掉 PyInstaller 的解包目录标记（旧版 _MEI* / 6.x _PYI*），
        # 否则新 exe 跳过解包、去加载旧进程退出即删除的 _MEI 目录里的
        # python DLL，启动即弹窗。源码运行时环境里本没有这些变量，
        # 先注入再验证 _clean_runtime_env 按前缀剥离生效。
        inject = {"_MEIPASS2": r"C:\fake\_MEItest",
                  "_PYI_APPLICATION_HOME_DIR": r"C:\fake\_MEItest",
                  "_PYI_ARCHIVE_FILE": r"C:\fake\_MEItest"}
        os.environ.update(inject)
        try:
            clean = _clean_runtime_env()
            env_ok = not (set(inject) & set(clean))
        finally:
            for k in inject:
                os.environ.pop(k, None)
        need("替换命令生成",
             "powershell" in cmd[0].lower() and "Copy-Item" in decoded
             and "Move-Item" in decoded and target_name in decoded
             and "Start-Process" in decoded
             and "Env:_MEI*" in decoded and "Env:_PYI*" in decoded and env_ok)
    except Exception as exc:                            # noqa: BLE001
        need("替换命令生成", False, _short(exc))

    try:
        script = build_installer_script(os.path.join(tempfile.gettempdir(), "new.exe"),
                                        os.path.join(tempfile.gettempdir(), "tgt.exe"))
        with open(script, "rb") as f:
            body = f.read().decode("utf-8-sig", "replace")
        need("替换脚本兜底生成",
             'copy /Y' in body and "tgt.exe" in body and "@echo off" in body
             and "set _MEI" in body and "set _PYI" in body)
        try:
            shutil.rmtree(os.path.dirname(script), ignore_errors=True)
        except Exception:
            pass
    except Exception as exc:                            # noqa: BLE001
        need("替换脚本兜底生成", False, _short(exc))

    return (not fails), ("; ".join(fails) if fails else
                         "版本比较 / 加速拼接 / 断点续传 / 校验 / 替换程序 全部通过")


def _start_test_server(payload, mode="normal", honor_range=True):
    """起一个只服务本机自检的 HTTP 服务器，返回 (server, port, state)"""
    import http.server
    import socket
    import socketserver

    state = {"mode": mode, "honor_range": honor_range, "ranges": [], "hits": 0}

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            state["hits"] += 1
            if state["mode"] == "fail":                 # 模拟「这个镜像挂了」
                self.send_response(500)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            total = len(payload)
            start = 0
            rng = self.headers.get("Range") or ""
            if rng:
                state["ranges"].append(rng)
            m = re.match(r"bytes=(\d+)-", rng)
            if m and state["honor_range"]:
                start = int(m.group(1))
            body = payload[start:]
            self.send_response(206 if start else 200)
            if start:
                self.send_header("Content-Range",
                                 "bytes %d-%d/%d" % (start, total - 1, total))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if state["mode"] == "truncate" and start == 0:
                # 声明完整长度，却只发一半就断开 —— 正是「下到一半掉线」的真实形态。
                # 用 SHUT_WR 而非 close()，免得 Windows 在缓冲未清空时发 RST 把数据一起丢掉。
                self.wfile.write(body[:len(body) // 2])
                self.wfile.flush()
                time.sleep(0.05)
                try:
                    self.connection.shutdown(socket.SHUT_WR)
                except Exception:
                    pass
                self.close_connection = True
                return
            self.wfile.write(body)

        def log_message(self, *_a):
            pass

    class _QuietServer(socketserver.ThreadingTCPServer):
        daemon_threads = True
        allow_reuse_address = True

        def handle_error(self, request, client_address):
            """客户端提前断开（截断场景）是自检的预期行为，不必打印堆栈"""

    httpd = _QuietServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1], state


def _selftest_resume(say):
    """用本地 HTTP 服务器验证断点续传（全程离线，不碰外网）

    覆盖四条关键路径：
      ① 下到一半掉线 → 保留 .part 供下次续传；
      ② 恢复后按断点位置发 Range 接着下；
      ③ 服务器不支持 Range（回 200）→ 退回整包重下；
      ④ 换了镜像 → 丢弃旧片段重下（避免把两份不同的构建拼成「大小对、内容错」的坏包）。
    """
    payload = bytes((i * 7 + 13) % 251 for i in range(384 * 1024))
    payload_b = bytes((i * 11 + 5) % 253 for i in range(384 * 1024))   # 等长但内容不同
    want = hashlib.sha256(payload).hexdigest()
    want_b = hashlib.sha256(payload_b).hexdigest()

    servers = []
    tmpdir = tempfile.mkdtemp(prefix="ac_resume_")
    old_env = os.environ.get(ENV_OFFLINE)
    # 只留直连（= 本地测试服务器）：确保截断那一轮没有第二个源能顶上，
    # 从而真正验证「失败后保留片段」这条路径。
    os.environ[ENV_OFFLINE] = "none"
    try:
        srv, port, state = _start_test_server(payload, mode="truncate")
        servers.append(srv)
        url = "http://127.0.0.1:%d/%s" % (port, ASSET_EXE)
        info = {"version": "9.9.9", "size": len(payload), "sha256": want, "exe_url": url}
        dest, part = update_paths("9.9.9", tmpdir)

        # ① 截断 → 必须失败，且留下可直接续传的片段
        try:
            download_update(info, max_rounds=1, per_source_attempts=1, dest_dir=tmpdir)
            return False, "截断场景下不应成功"
        except UpdateError:
            pass
        got = os.path.getsize(part) if os.path.exists(part) else 0
        if not (0 < got < len(payload)):
            return False, "截断后未保留可续传的片段（%s 字节）" % got
        if say:
            say("      ① 掉线后保留片段 %s / %s" % (_human(got), _human(len(payload))))

        # ② 恢复后用 Range 从断点续传
        state["mode"] = "normal"
        state["ranges"] = []
        path = download_update(info, max_rounds=2, dest_dir=tmpdir)
        if sha256_file(path) != want:
            return False, "续传后内容校验不一致"
        if not any(r == "bytes=%d-" % got for r in state["ranges"]):
            return False, "第二轮没有按断点位置发起续传（收到 %r）" % (state["ranges"][:3],)
        if say:
            say("      ② 按断点 %s 续传并校验通过" % _human(got))

        # ③ 服务器不支持 Range（忽略 Range 头回 200）→ 整包重下
        os.remove(path)
        with open(part, "wb") as f:                     # 造一个来源不明的半截片段
            f.write(payload[:len(payload) // 3])
        state["honor_range"] = False
        path = download_update(info, max_rounds=2, dest_dir=tmpdir)
        if sha256_file(path) != want:
            return False, "不支持 Range 时重下结果不一致"
        if say:
            say("      ③ 服务器不支持 Range 时整包重下成功")

        # ④ 换到「内容相同」的镜像：应允许续传（公共镜像断线频繁，能接上就接）
        os.remove(path)
        srv_b, port_b, state_b = _start_test_server(payload)        # 同一份内容
        servers.append(srv_b)
        info_b = {"version": "9.9.9", "size": len(payload), "sha256": want,
                  "exe_url": "http://127.0.0.1:%d/%s" % (port_b, ASSET_EXE)}
        state["mode"] = "truncate"                      # 先用 A 做出半截片段
        state["honor_range"] = True
        try:
            download_update(info, max_rounds=1, per_source_attempts=1, dest_dir=tmpdir)
        except UpdateError:
            pass
        got = os.path.getsize(part) if os.path.exists(part) else 0
        if not (0 < got < len(payload)):
            return False, "换源场景的片段准备失败"
        state["mode"] = "fail"                          # A 随后失效，只能换到 B
        path = download_update(info_b, max_rounds=2, dest_dir=tmpdir)
        if sha256_file(path) != want:
            return False, "换到同内容镜像后校验不一致"
        if not any(r == "bytes=%d-" % got for r in state_b["ranges"]):
            return False, "换到同内容镜像后没有接着断点续传（收到 %r）" % (state_b["ranges"][:3],)
        if say:
            say("      ④ 换到内容相同的镜像：从 %s 处接着续传成功" % _human(got))

        # ⑤ 换到「内容不同」的镜像：绝不能拼接，必须丢弃片段从新源整包重下
        os.remove(path)
        srv_c, port_c, state_c = _start_test_server(payload_b)      # 等长但不同内容
        servers.append(srv_c)
        info_c = {"version": "9.9.9", "size": len(payload_b), "sha256": want_b,
                  "exe_url": "http://127.0.0.1:%d/%s" % (port_c, ASSET_EXE)}
        state["mode"] = "truncate"
        try:
            download_update(info, max_rounds=1, per_source_attempts=1, dest_dir=tmpdir)    # A 再给半截
        except UpdateError:
            pass
        got = os.path.getsize(part) if os.path.exists(part) else 0
        if not (0 < got < len(payload)):
            return False, "换源场景的片段准备失败（二）"
        state["mode"] = "fail"
        path = download_update(info_c, max_rounds=2, dest_dir=tmpdir)
        if sha256_file(path) != want_b:
            return False, "换源后拿到的不是新源的完整文件（很可能发生了跨源拼接）"
        bad = [r for r in state_c["ranges"] if r == "bytes=%d-" % got]
        if bad:
            return False, "换源后仍从旧断点续传（收到 %r），会拼出坏文件" % bad[:2]
        if say:
            say("      ⑤ 换到内容不同的镜像：丢弃旧片段重下，未发生跨源拼接")
        return True, ""
    except Exception as exc:                            # noqa: BLE001
        return False, "续传自检异常：%s" % _short(exc)
    finally:
        if old_env is None:
            os.environ.pop(ENV_OFFLINE, None)
        else:
            os.environ[ENV_OFFLINE] = old_env
        for srv in servers:
            try:
                srv.shutdown()
                srv.server_close()
            except Exception:
                pass
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------- 命令行辅助
def format_info(info):
    if not info:
        return "已是最新版本"
    return ("发现新版本 v%s（当前 v%s）\n"
            "  文件：%s\n"
            "  大小：%s\n"
            "  SHA256：%s\n"
            "  来源：%s\n"
            "  发布：%s"
            % (info.get("version"), info.get("current"),
               os.path.basename(info.get("exe_url") or ASSET_EXE),
               _human(info.get("size")), (info.get("sha256") or "")[:16] or "（未取得）",
               info.get("_source") or "?", info.get("published_at") or "?"))


def cmd_update(current_version, install=True, notice=print):
    """命令行入口：检查 → 下载 → （可选）替换安装；返回退出码"""
    try:
        info = check_for_update(current_version, notice=notice)
    except UpdateError as exc:
        notice("检查更新失败：%s" % exc)
        return 1
    if not info:
        return 0
    notice(format_info(info))

    def prog(done, total, _speed):
        if total:
            pct = int(done * 100.0 / total)
            if pct % 10 == 0:
                notice("  下载 %s / %s（%d%%）" % (_human(done), _human(total), pct))

    try:
        path = download_update(info, progress=prog, notice=notice)
    except UpdateError as exc:
        notice("下载失败：%s" % exc)
        return 1
    notice("安装包已就绪：%s" % path)
    if not install:
        return 0
    try:
        install_update(path, notice=notice)
    except UpdateError as exc:
        notice("%s" % exc)
        return 1
    notice("已启动替换脚本，程序将自动重启为新版本…")
    return 0
