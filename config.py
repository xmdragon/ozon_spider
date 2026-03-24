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

# Seller accounts: list of {email, app_password, client_id}
# From SELLER_ACCOUNTS env: "email:pass:id,email2:pass2:id2"
def _parse_seller_accounts():
    accounts = []
    for entry in os.getenv("SELLER_ACCOUNTS", "").split(","):
        parts = entry.strip().split(":")
        if len(parts) == 3:
            accounts.append({"email": parts[0], "app_password": parts[1], "client_id": parts[2]})
    return accounts

SELLER_ACCOUNTS = _parse_seller_accounts()
# Default (first account) for backwards compat
SELLER_EMAIL = SELLER_ACCOUNTS[0]["email"] if SELLER_ACCOUNTS else ""
SELLER_EMAIL_APP_PASSWORD = SELLER_ACCOUNTS[0]["app_password"] if SELLER_ACCOUNTS else ""
SELLER_CLIENT_ID = SELLER_ACCOUNTS[0]["client_id"] if SELLER_ACCOUNTS else ""
SELLER_STORAGE_STATE = "seller_state.json"
SELLER_LOGIN_URL = "https://seller.ozon.ru/"
