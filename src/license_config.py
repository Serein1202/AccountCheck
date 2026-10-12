"""license-guard 接入配置 —— 由 Skill 自动生成，直接 import 即可。"""

SERVER_URL = "https://license-guard.xhd19981202.workers.dev"
APP_ID     = "748291635"
SIGN_ALG   = "ed25519"

# ed25519 用 raw base64url 公钥
PUBLIC_KEY = "LjZLPnhcuJ5qJEtNi1axHbc54lvk-wfN5wcCTfZzIEk"

# 兜底策略：后端连不上时才生效，正常情况下后端会下发并覆盖它
FAIL_MODE  = "hard"      # "hard" 静默退出 / "message" 提示后退出
EXIT_CODE  = 0
