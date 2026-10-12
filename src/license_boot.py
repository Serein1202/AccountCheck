# -*- coding: utf-8 -*-
"""license-guard 授权校验 · 客户端接入层

================ 接入方式（放在真正的业务入口，越早越好）================

    import license_boot
    license_boot.ensure_license()     # 校验不通过 → 本函数不返回，进程按后端策略结束
                                      # （内部已 start_watchdog，运行期到期也会自动退出）

    ...以下才是原有业务代码（授权无效时根本执行不到）

================ 组成与约定 ================

· SDK 在 ``src/vendor/license_guard.py``（零第三方依赖），接入常量在 ``license_config.py``。
· 策略（fail_mode / exit_code / offline_grace_days / heartbeat_sec）由**后端下发并覆盖**
  本地兜底值；本地那些只用于「连不上后端」的场景。客户端不写死策略。
· 授权码（license key）查找顺序：
      1. 环境变量 ``LICENSE_KEY`` / ``{APP_ID}_LICENSE_KEY`` / ``APP_LICENSE_KEY``
      2. 程序目录 ``license.key``（打包成 exe 时为 exe 所在目录）
      3. 程序目录 ``.license``
      4. 程序目录 ``vendor/license.key``
      5. **打包内置** ``license.key``（PyInstaller 单文件模式的 ``sys._MEIPASS``；
         由 CI 从 ``secrets.APP_LICENSE_KEY`` 注入后 ``--add-data`` 塞进 exe）
      6. ``%LOCALAPPDATA%\\{APP_ID}\\license.key``
· **组件缺失时拒绝启动（fail-closed）**：删掉 ``vendor/`` 或 ``license_config.py``
  不能绕过校验，程序会直接退出并给出提示。
· 授权失效时的表现（闪退 / 提示后退出、退出码）完全由后端产品的 ``fail_mode`` 决定。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_VENDOR = _HERE / "vendor"
if str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))

# ---- 加载 SDK 与配置常量（失败则 fail-closed，不让程序继续跑）----
_IMPORT_ERROR = None
try:
    from license_config import (APP_ID, EXIT_CODE, FAIL_MODE, PUBLIC_KEY,
                               SERVER_URL, SIGN_ALG)
    from license_guard import GuardConfig, LicenseError, LicenseGuard, machine_id
except Exception as _exc:                      # pragma: no cover - 仅组件缺失时发生
    _IMPORT_ERROR = _exc
    APP_ID = SERVER_URL = PUBLIC_KEY = SIGN_ALG = FAIL_MODE = ""
    EXIT_CODE = 0
    GuardConfig = LicenseGuard = LicenseError = None
    machine_id = None

# 保持对 guard 的引用，避免看门狗对象被回收
_GUARD = None


def is_available():
    """授权组件是否齐全（缺文件时为 False）"""
    return _IMPORT_ERROR is None


def load_error():
    """组件加载失败原因（正常时为空串）"""
    return "" if _IMPORT_ERROR is None else "%s: %s" % (type(_IMPORT_ERROR).__name__,
                                                       _IMPORT_ERROR)


def app_dir():
    """程序所在目录（打包后取 exe 所在目录；源码运行取入口脚本所在目录）"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    # 源码运行：license.key 放在入口脚本（joint_query.py / ace_query.py）旁边。
    # 本模块位于 src/ 子目录，_HERE 指向 src/，不是用户放授权码的地方。
    entry = Path(sys.argv[0] or ".").resolve().parent
    return entry if entry.is_dir() else _HERE


def license_key_candidates():
    """授权码文件的候选路径（按优先级）"""
    base = app_dir()
    out = [base / "license.key", base / ".license", base / "vendor" / "license.key"]
    # 打包时随 exe 一起塞进去的授权码（PyInstaller 单文件模式会解包到 sys._MEIPASS）。
    # 放在「exe 同目录」之后：用户若在 exe 旁边放了自己的 license.key，以用户那份为准。
    bundle = getattr(sys, "_MEIPASS", "")
    if bundle and Path(bundle) != base:
        out.append(Path(bundle) / "license.key")
    system = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if system:
        out.append(Path(system) / APP_ID / "license.key")
    return out


def _env_license_key():
    """从环境变量取授权码（多套命名都认，取第一个非空的）"""
    for env in ("LICENSE_KEY", ("%s_LICENSE_KEY" % APP_ID).upper(), "APP_LICENSE_KEY"):
        value = (os.environ.get(env) or "").strip()
        if value:
            return value
    return ""


def resolve_license_key():
    """按「环境变量 → exe 同目录 → 打包内置 → 数据目录」找出授权码；找不到返回空串"""
    value = _env_license_key()
    if value:
        return value
    for path in license_key_candidates():
        try:
            text = Path(path).read_text(encoding="utf-8", errors="ignore").strip()
        except Exception:
            continue
        if text:
            return text.splitlines()[0].strip()
    return ""


def _fatal(message):
    """组件缺失等致命问题：打印可读原因后直接终止（fail-closed）"""
    try:
        sys.stderr.write("\n[授权] %s\n" % message)
        sys.stderr.flush()
    except Exception:
        pass
    raise SystemExit(1)


def _degrade_transient_responses(guard):
    """把「边缘节点的临时错误」降级成「联不上」，避免误判成授权失效。

    背景：Cloudflare 边缘限流时返回的是它自己的错误页（非本系统的 JSON），
    SDK 的 ``_post`` 只能拼出一个 ``HTTP_429`` 之类的伪拒绝码。
    这个码既不属于 TERMINAL（真失效），也不属于 SOFT（限流白名单），
    于是会直接按 fail_mode 处理 —— 用户看到的就是「授权明明有效却打不开程序」。
    一次限流就挡人，代价太大，所以统一降级为「网络不可达」，
    交给离线容忍期（offline_grace_days）兜底；后台的吊销 / 改期
    仍会在下一次成功联网时立即生效。
    """
    if GuardConfig is None:                       # 组件缺失，无需处理
        return guard
    original = guard._post

    def _post(path, body):
        resp = original(path, body)
        if isinstance(resp, dict) and not resp.get("ok"):
            code = str(resp.get("code") or "").upper()
            # 真正的业务码是 LICENSE_* / KEY_* / APP_* 这类；
            # HTTP_* 说明拿到的不是本系统的应答（网关错误页 / 限流页）。
            if code.startswith("HTTP_"):
                return None
        return resp

    try:
        guard._post = _post
    except Exception:                             # pragma: no cover - 只读属性等极端情况
        pass
    return guard


def ensure_license(component=""):
    """启动时校验授权；不通过则**不返回**（进程按后端策略结束）

    component：可选，日志/排查用的入口名（如 "joint_query"）。
    返回 LicenseGuard 实例（已开启看门狗）。
    """
    global _GUARD
    if _IMPORT_ERROR is not None:
        _fatal("授权组件缺失或损坏（%s）。请确认 vendor/license_guard.py 与 "
               "license_config.py 与本程序在同一目录下。" % load_error())

    guard = LicenseGuard(GuardConfig(
        server_url=SERVER_URL,
        app_id=APP_ID,
        public_key=PUBLIC_KEY,
        sign_alg=SIGN_ALG,
        license_key=resolve_license_key(),
        fail_mode=FAIL_MODE,          # 兜底值：正常由后端下发覆盖
        exit_code=EXIT_CODE,
        watchdog_interval_sec=0,      # 0 = 复查间隔跟随后端下发的 heartbeat_sec
        debug=bool(os.environ.get("LICENSE_GUARD_DEBUG")),
    ))
    _degrade_transient_responses(guard)
    guard.authorize()                 # 不通过 → 进程在这里就结束了
    guard.start_watchdog()            # 运行期持续看守：到期那一刻立即退出
    _GUARD = guard
    return guard


def guard():
    """已校验通过时的 LicenseGuard 实例（未调用过 ensure_license 时为 None）"""
    return _GUARD


def summary():
    """接入信息摘要（供自检/排查打印，不含授权码明文）"""
    key = resolve_license_key()
    return {
        "available": is_available(),
        "load_error": load_error(),
        "server_url": SERVER_URL,
        "app_id": APP_ID,
        "sign_alg": SIGN_ALG,
        "key_found": bool(key),
        "key_masked": (key[:4] + "-****-" + key[-4:]) if len(key) >= 12 else ("****" if key else ""),
        "key_source": ("环境变量" if _env_license_key() else
                       next((str(p) for p in license_key_candidates() if p.exists()), "未找到")),
        "machine_id": machine_id() if machine_id else "",
        "state_dir": str(Path(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")) / APP_ID),
        "fallback_fail_mode": FAIL_MODE,
        "fallback_exit_code": EXIT_CODE,
    }
