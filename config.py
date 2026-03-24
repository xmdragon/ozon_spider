import os
from pathlib import Path

_env = Path(__file__).parent / ".env"
if _env.exists():
    for _line in _env.read_text().splitlines():
        if _line.strip() and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

CHROME_BIN = os.getenv("CHROME_BIN", "/usr/bin/google-chrome-stable")
CDP_PORT = int(os.getenv("CDP_PORT", "9223"))
XVFB_DISPLAY = os.getenv("XVFB_DISPLAY", ":99")

# Timeouts (seconds)
PAGE_LOAD_TIMEOUT = 30
STABLE_POLL_INTERVAL = 3
STABLE_MAX_WAIT = 45
SLIDER_TIMEOUT = 20

SUCCESS_THRESHOLD = 3
OUTPUT_FILE = "results.json"

# Seller login config
SELLER_EMAIL = os.getenv("SELLER_EMAIL", "")
SELLER_EMAIL_APP_PASSWORD = os.getenv("SELLER_EMAIL_APP_PASSWORD", "")
SELLER_CLIENT_ID = os.getenv("SELLER_CLIENT_ID", "")
SELLER_STORAGE_STATE = os.getenv("SELLER_STORAGE_STATE", "seller_state.json")
SELLER_LOGIN_URL = "https://seller.ozon.ru/"
