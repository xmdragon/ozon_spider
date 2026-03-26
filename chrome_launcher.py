"""
Launch system Chrome under Xvfb and return CDP port.
"""
import subprocess
import time
import os
import signal
import requests
import logging

log = logging.getLogger(__name__)


def start_xvfb(display: str = ":99", resolution: str = "1920x1080x24") -> subprocess.Popen:
    proc = subprocess.Popen(
        ["Xvfb", display, "-screen", "0", resolution, "-ac", "+extension", "GLX"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(1)
    log.info(f"Xvfb started on {display} (pid {proc.pid})")
    return proc


def _make_user_data_dir() -> str:
    """Create a fresh temp user-data-dir per Chrome session."""
    import tempfile
    d = tempfile.mkdtemp(prefix="ozon_chrome_")
    log.info(f"user-data-dir: {d}")
    return d


def start_chrome(
    chrome_bin: str,
    cdp_port: int,
    display: str = None,
    user_data_dir: str = None,
) -> subprocess.Popen:
    env = os.environ.copy()
    if display:
        env["DISPLAY"] = display
    elif "DISPLAY" not in env:
        env["DISPLAY"] = ":0"
    if user_data_dir is None:
        user_data_dir = _make_user_data_dir()
    cmd = [
        chrome_bin,
        f"--remote-debugging-port={cdp_port}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-default-apps",
        "--disable-extensions",
        "--disable-sync",
        "--disable-translate",
        "--disable-background-networking",
        "--disable-client-side-phishing-detection",
        "--disable-hang-monitor",
        "--disable-prompt-on-repost",
        "--disable-domain-reliability",
        "--disable-features=TranslateUI",
        "--metrics-recording-only",
        "--safebrowsing-disable-auto-update",
        "--password-store=basic",
        "--use-mock-keychain",
        "--lang=ru-RU",
        "--accept-lang=ru-RU,ru;q=0.9",
        "--window-size=1920,1080",
        "about:blank",
    ]
    if user_data_dir is not None:
        cmd.insert(2, f"--user-data-dir={user_data_dir}")
    proc = subprocess.Popen(
        cmd,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # Wait for CDP to be ready
    for _ in range(20):
        time.sleep(0.5)
        try:
            r = requests.get(f"http://127.0.0.1:{cdp_port}/json/version", timeout=2)
            if r.status_code == 200:
                log.info(f"Chrome started (pid {proc.pid}), CDP ready on port {cdp_port}")
                return proc
        except Exception:
            pass
    raise RuntimeError("Chrome CDP did not become ready in time")


def kill(proc: subprocess.Popen):
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
