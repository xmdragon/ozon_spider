import json
import os
from pathlib import Path

_env = Path(__file__).parent / ".env"
if _env.exists():
    for _line in _env.read_text().splitlines():
        if _line.strip() and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

_account_file = Path(__file__).parent / "account.json"

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
def _load_seller_accounts():
    if not _account_file.exists():
        return []

    try:
        payload = json.loads(_account_file.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[config] failed to read account.json: {e}")
        return []

    if isinstance(payload, dict):
        raw_accounts = payload.get("seller_accounts", [])
    elif isinstance(payload, list):
        raw_accounts = payload
    else:
        print("[config] account.json must be an array or {\"seller_accounts\": [...]} format")
        return []

    accounts = []
    for item in raw_accounts:
        if not isinstance(item, dict):
            continue
        email = str(item.get("email", "")).strip()
        app_password = str(item.get("app_password", "")).strip()
        client_id = str(item.get("client_id", "")).strip()
        if email and app_password and client_id:
            accounts.append({
                "email": email,
                "app_password": app_password,
                "client_id": client_id,
            })
    return accounts

SELLER_ACCOUNTS = _load_seller_accounts()
# Default (first account) for backwards compat
SELLER_EMAIL = SELLER_ACCOUNTS[0]["email"] if SELLER_ACCOUNTS else ""
SELLER_EMAIL_APP_PASSWORD = SELLER_ACCOUNTS[0]["app_password"] if SELLER_ACCOUNTS else ""
SELLER_CLIENT_ID = SELLER_ACCOUNTS[0]["client_id"] if SELLER_ACCOUNTS else ""
SELLER_STORAGE_STATE = "seller_state.json"
SELLER_LOGIN_URL = "https://seller.ozon.ru/"
