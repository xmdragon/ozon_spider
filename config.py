import json
import os
from pathlib import Path

_env = Path(__file__).parent / ".env"
if _env.exists():
    for _line in _env.read_text().splitlines():
        if _line.strip() and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

ACCOUNT_JSON_PATH = Path(__file__).parent / "account.json"
PROJECT_ROOT = Path(__file__).parent
TMP_ROOT = Path(os.getenv("APP_TMP_DIR", str(PROJECT_ROOT / "tmp"))).expanduser()

CHROME_BIN = os.getenv("CHROME_BIN", "/usr/bin/google-chrome-stable")
CDP_PORT = int(os.getenv("CDP_PORT", "9223"))
DISPLAY_SCREENSHOT_DEBUG = str(os.getenv("DISPLAY_SCREENSHOT_DEBUG", "")).strip().lower() in {
    "1", "true", "yes", "on",
}
DISPLAY_SCREENSHOT_INTERVAL_SECONDS = max(
    1,
    int(os.getenv("DISPLAY_SCREENSHOT_INTERVAL_SECONDS", "5")),
)
DISPLAY_SCREENSHOT_DIR = Path(
    os.getenv("DISPLAY_SCREENSHOT_DIR", str(PROJECT_ROOT / "screenshot"))
).expanduser()


def _normalize_display_name(value: str) -> str:
    raw = str(value).strip()
    if not raw:
        return ":99"
    if raw.startswith(":") or ":" in raw:
        return raw
    if raw.isdigit():
        return f":{raw}"
    return raw


def _load_browser_display() -> tuple[str, bool]:
    raw = str(os.getenv("BROWSER_DISPLAY", "")).strip()
    legacy = str(os.getenv("XVFB_DISPLAY", "")).strip()
    value = raw or (f"xvfb:{legacy}" if legacy else "xvfb:99")
    if value.lower().startswith("xvfb:"):
        return _normalize_display_name(value.split(":", 1)[1]), True
    return _normalize_display_name(value), False


BROWSER_DISPLAY, BROWSER_USE_XVFB = _load_browser_display()
XVFB_DISPLAY = BROWSER_DISPLAY  # Backwards-compatible alias for existing imports.


def apply_browser_display_env() -> str:
    os.environ["DISPLAY"] = BROWSER_DISPLAY
    return BROWSER_DISPLAY

# Timeouts (seconds)
PAGE_LOAD_TIMEOUT = 30
STABLE_POLL_INTERVAL = 3
STABLE_MAX_WAIT = 45
SLIDER_TIMEOUT = 20

SUCCESS_THRESHOLD = 3
OUTPUT_FILE = "results.json"

# Seller accounts: list of {email, app_password, client_id}
def _load_seller_accounts():
    if not ACCOUNT_JSON_PATH.exists():
        return []

    try:
        payload = json.loads(ACCOUNT_JSON_PATH.read_text(encoding="utf-8"))
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
            normalized = dict(item)
            normalized["email"] = email
            normalized["app_password"] = app_password
            normalized["client_id"] = client_id
            accounts.append(normalized)
    return accounts

SELLER_ACCOUNTS = _load_seller_accounts()
# Default (first account) for backwards compat
SELLER_EMAIL = SELLER_ACCOUNTS[0]["email"] if SELLER_ACCOUNTS else ""
SELLER_EMAIL_APP_PASSWORD = SELLER_ACCOUNTS[0]["app_password"] if SELLER_ACCOUNTS else ""
SELLER_CLIENT_ID = SELLER_ACCOUNTS[0]["client_id"] if SELLER_ACCOUNTS else ""
SELLER_STORAGE_STATE = "seller_state.json"
SELLER_LOGIN_URL = "https://seller.ozon.ru/"
