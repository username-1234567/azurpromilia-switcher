from __future__ import annotations

import json
import re
import threading
import urllib.request

from . import __version__

API_URL = "https://api.github.com/repos/username-1234567/azurpromilia-switcher/releases/latest"
RELEASE_URL = "https://github.com/username-1234567/azurpromilia-switcher/releases/latest"
TIMEOUT = 5

_state_lock = threading.Lock()
_check_lock = threading.Lock()
_state = {"status": "idle", "current": __version__}
_auto_started = False


def _version(text: str) -> tuple[int, ...]:
    match = re.fullmatch(r"\s*v?(\d+(?:\.\d+)*)(?:[-+].*)?\s*", text)
    if not match:
        raise ValueError("版本号格式不认识")
    return tuple(int(part) for part in match.group(1).split("."))


def status() -> dict:
    with _state_lock:
        return dict(_state)


def _latest_version() -> str:
    request = urllib.request.Request(
        API_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"azurpromilia-switcher/{__version__}",
        },
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        data = json.loads(response.read().decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("更新接口返回格式不正确")
    latest = str(data.get("tag_name") or "")
    _version(latest)
    return latest


def check(manual: bool = False) -> dict:
    global _state
    with _check_lock:
        with _state_lock:
            _state = {"status": "checking", "current": __version__}
        try:
            latest = _latest_version()
            available = _version(latest) > _version(__version__)
            result = {
                "status": "update" if available else "current",
                "current": __version__,
                "latest": latest,
                "url": RELEASE_URL,
            }
        except Exception:
            result = {"status": "error" if manual else "idle", "current": __version__}
            if manual:
                result["error"] = "暂时无法连接更新服务器，请稍后重试"
        with _state_lock:
            _state = result
        return dict(result)


def start_auto() -> None:
    global _auto_started
    with _state_lock:
        if _auto_started:
            return
        _auto_started = True
    threading.Thread(target=check, kwargs={"manual": False},
                     name="azp-update-check", daemon=True).start()