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
            if total and have and size_hint and total != size_hint:
                # 远端文件换了：旧片段作废
                _truncate(part)
                have, status, total = 0, 200, 0
                resp.close()
                resp = http_get(url, timeout=TIMEOUT_CONNECT, notice=notice)
                clen = int(resp.headers.get("Content-Length") or 0)
        else:
            total = clen
            if have:
                if notice:
                    notice("该源不支持断点续传（已下 %s），改为重新下载整包" % _human(have))
                have = 0                             # 服务器忽略 Range → 只能整包重下
        mode = "ab" if have else "wb"
        done = have
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


def _human(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.1f %s" % (n, unit) if unit != "B" else "%d B" % n
        n /= 1024.0


human_size = _human                        # 对外别名（CLI / GUI 都用它格式化字节数）


def probe_sources(urls, timeout=6, notice=None):
    """并发探测各下载源的「首字节」响应，返回可用的源（按快慢排序）

    国内直连 GitHub 下 Release 往往被限速到几十 KB/s，而公共加速站说不准哪个通；
    与其按固定顺序闷头等超时，不如先并发问一圈，挑最快的那个再真正开下。
    全部失败时返回空列表，由调用方退回原始顺序。
    """
    q = queue.Queue()

    def run(url):
        t0 = time.time()
        try:
            resp = http_get(url, headers={"Range": "bytes=0-0"}, timeout=timeout, notice=None)
            code = resp.getcode()
            try:
                resp.read(16)
            except Exception:
                pass
            try:
                resp.close()
            except Exception:
                pass
            q.put((code in (200, 206), url, time.time() - t0))
        except Exception as exc:                       # noqa: BLE001
            q.put((False, url, _short(exc)))

    for url in urls:
        threading.Thread(target=run, args=(url,), daemon=True).start()

    good, deadline = [], time.time() + max(1.0, float(timeout))
    for _ in range(len(urls)):
        remain = deadline - time.time()
        if remain <= 0:
            break
        try:
            ok, url, cost = q.get(timeout=remain)
        except queue.Empty:
            break
        if ok:
            good.append((cost, url))
    good.sort(key=lambda x: x[0])
    if good and notice:
        notice("已探测可用下载源：%s" % "、".join("%s（%.1fs）" % (_host(u), c)
                                                  for c, u in good[:3]))
    return [u for _c, u in good]


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


def download_update(info, progress=None, notice=None, stop=None, max_rounds=3,
                    dest_dir=None, probe=True):
    """下载新版 exe，支持断点续传；返回校验通过的本地文件路径

    progress(done, total, speed) 会被高频调用（UI 侧自行节流）；
    notice(text) 用于把「换源 / 续传 / 校验」等过程写进日志。
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
    if os.path.exists(dest) and want_sha:
        if sha256_file(dest) == want_sha:                # 上次已经下好了
            if notice:
                notice("本地已有校验通过的新版安装包：%s" % dest)
            return dest

    urls = _download_source_priority(info)
    if probe and len(urls) > 1 and not (os.path.exists(part) and os.path.getsize(part)):
        if notice:
            notice("正在探测最快的下载源（%d 个候选）…" % len(urls))
        fast = probe_sources(urls, notice=notice)
        if fast:
            urls = fast + [u for u in urls if u not in fast]
    _cleanup_old_parts(keep=part, folder=folder)
    last_err = None
    for round_no in range(1, max(1, max_rounds) + 1):
        for url in urls:
            if stop and stop():
                raise UpdateError("已取消")
            have = os.path.getsize(part) if os.path.exists(part) else 0
            if have and notice:
                notice("从 %s 处续传（第 %d 轮）：%s" % (_human(have), round_no, _host(url)))
            elif notice:
                notice("开始下载（第 %d 轮）：%s" % (round_no, _host(url)))
            t0 = time.time()
            try:
                _fetch_into(part, url, expected_size=size, size_hint=size,
                            notice=notice, progress=progress)
            except Exception as exc:                    # noqa: BLE001 - 换源重试
                last_err = exc
                got = os.path.getsize(part) if os.path.exists(part) else 0
                if notice:
                    notice("该源中断（%s，已下 %s），换下一个源继续：%s"
                           % (_short(exc), _human(got), url))
                continue
            got = os.path.getsize(part)
            if size and got != size:
                if notice:
                    notice("大小不符（%s / 应为 %s），保留片段继续下载"
                           % (_human(got), _human(size)))
                last_err = UpdateError("下载不完整")
                continue
            speed = got / max(0.05, time.time() - t0)
            if notice:
                notice("下载完成：%s（平均 %s/s），开始校验…" % (_human(got), _human(speed)))
            if want_sha:
                if notice:
                    notice("正在校验 SHA256…")
                digest = sha256_file(part)               # 18MB 约 0.1~0.3s，不必报进度
                if digest != want_sha:
                    if notice:
                        notice("SHA256 不符（%s…），丢弃已下内容重新下载" % digest[:12])
                    _truncate(part)
                    last_err = UpdateError("SHA256 校验失败")
                    continue
                if notice:
                    notice("SHA256 校验通过：%s…" % digest[:16])
            try:
                os.replace(part, dest)
            except Exception as exc:
                raise UpdateError("保存安装包失败：%s" % _short(exc))
            return dest
    raise UpdateError("下载失败：%s" % _short(last_err) if last_err else "下载失败")


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
            limit = 86400 if name.endswith(".part") else max_age_days * 86400
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
        'set "NEW=%s"' % new_exe,
        'set "TGT=%s"' % target,
        'set "LOG=%s"' % log_file,
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


def _detached_flags():
    flags = 0
    for name in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP", "CREATE_NO_WINDOW"):
        flags |= getattr(subprocess, name, 0)
    return flags


def launch_installer(new_exe, target=None, log_file="", notice=None):
    """启动替换程序（本进程退出后它才可能成功覆盖）；返回描述字符串

    优先用 PowerShell（路径编码无忧），失败再退回 .bat 脚本。
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

    exe = powershell_path()
    if exe:
        try:
            cmd = build_installer_command(new_exe, target, log_file)
            subprocess.Popen(cmd, close_fds=True, creationflags=_detached_flags())
            _log("install scheduled via powershell: %s -> %s" % (new_exe, target))
            if notice:
                notice("已启动替换程序（PowerShell），程序退出后自动覆盖并重启")
            return "powershell"
        except Exception as exc:                        # noqa: BLE001
            _log("powershell installer failed: %s" % _short(exc))
            if notice:
                notice("PowerShell 方式启动失败（%s），改用 .bat 兜底" % _short(exc))
    script = build_installer_script(new_exe, target, log_file)
    subprocess.Popen(["cmd", "/c", script], close_fds=True,
                     creationflags=_detached_flags(), cwd=os.path.dirname(script))
    _log("install scheduled via bat: %s -> %s (script %s)" % (new_exe, target, script))
    if notice:
        notice("已启动替换脚本（%s）" % script)
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
        need("替换命令生成",
             "powershell" in cmd[0].lower() and "Copy-Item" in decoded
             and "Move-Item" in decoded and target_name in decoded
             and "Start-Process" in decoded)
    except Exception as exc:                            # noqa: BLE001
        need("替换命令生成", False, _short(exc))

    try:
        script = build_installer_script(os.path.join(tempfile.gettempdir(), "new.exe"),
                                        os.path.join(tempfile.gettempdir(), "tgt.exe"))
        with open(script, "rb") as f:
            body = f.read().decode("utf-8-sig", "replace")
        need("替换脚本兜底生成",
             'copy /Y' in body and "tgt.exe" in body and "@echo off" in body)
        try:
            shutil.rmtree(os.path.dirname(script), ignore_errors=True)
        except Exception:
            pass
    except Exception as exc:                            # noqa: BLE001
        need("替换脚本兜底生成", False, _short(exc))

    return (not fails), ("; ".join(fails) if fails else
                         "版本比较 / 加速拼接 / 断点续传 / 校验 / 替换程序 全部通过")


def _selftest_resume(say):
    """用本地 HTTP 服务器验证断点续传：第一次故意截断，第二次带 Range 续传

    覆盖三条关键路径：截断后保留 .part、带 Range 续传、服务器不支持 Range 时整包重下。
    """
    import http.server
    import socket
    import socketserver

    payload = bytes((i * 7 + 13) % 251 for i in range(384 * 1024))
    want = hashlib.sha256(payload).hexdigest()
    state = {"mode": "truncate", "honor_range": True, "ranges": []}

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
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
                # 声明完整长度，却只发一半就断开 —— 客户端会读到 IncompleteRead，
                # 正是「下载中途掉线」的真实形态。用 SHUT_WR 而非 close()，
                # 免得 Windows 在缓冲未清空时发 RST 把已发数据一起丢掉。
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

    try:
        httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    except Exception as exc:                            # noqa: BLE001
        return False, "无法启动本地测试服务器：%s" % _short(exc)
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    tmpdir = tempfile.mkdtemp(prefix="ac_resume_")
    url = "http://127.0.0.1:%d/%s" % (port, ASSET_EXE)
    info = {"version": "9.9.9", "size": len(payload), "sha256": want, "exe_url": url}
    dest, part = update_paths("9.9.9", tmpdir)
    old_env = os.environ.get(ENV_OFFLINE)
    # 只留直连（= 本地测试服务器）：确保截断那一轮没有第二个源能顶上，
    # 从而真正验证「失败后保留片段」这条路径。
    os.environ[ENV_OFFLINE] = "none"
    try:
        # 第一轮：服务器截断 → 必须失败，且留下部分文件
        try:
            download_update(info, max_rounds=1, dest_dir=tmpdir)
            return False, "截断场景下不应成功"
        except UpdateError:
            pass
        part = dest + ".part"
        got = os.path.getsize(part) if os.path.exists(part) else 0
        if not (0 < got < len(payload)):
            return False, "截断后未保留可续传的片段（%s 字节）" % got
        if say:
            say("      截断后保留片段 %s / %s" % (_human(got), _human(len(payload))))

        # 第二轮：服务器恢复 → 必须带 Range 续传，且内容校验通过
        state["mode"] = "normal"
        state["ranges"] = []
        path = download_update(info, max_rounds=2, dest_dir=tmpdir)
        if sha256_file(path) != want:
            return False, "续传后内容校验不一致"
        if not any(r == "bytes=%d-" % got for r in state["ranges"]):
            return False, "第二轮没有按断点位置发起续传（收到 %r）" % (state["ranges"][:3],)

        # 第三轮：服务器不支持 Range（忽略 Range 头回 200）→ 应整包重下并成功
        os.remove(path)
        with open(dest + ".part", "wb") as f:           # 造一个「来源不同」的坏片段
            f.write(payload[:len(payload) // 3])
        state["honor_range"] = False
        path = download_update(info, max_rounds=2, dest_dir=tmpdir)
        if sha256_file(path) != want:
            return False, "不支持 Range 时重下结果不一致"
        if say:
            say("      续传 / 重下两条路径均校验通过")
        return True, ""
    except Exception as exc:                            # noqa: BLE001
        return False, "续传自检异常：%s" % _short(exc)
    finally:
        if old_env is None:
            os.environ.pop(ENV_OFFLINE, None)
        else:
            os.environ[ENV_OFFLINE] = old_env
        try:
            httpd.shutdown()
            httpd.server_close()
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
