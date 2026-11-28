"""本地界面服务：只绑定 127.0.0.1，静态页 + JSON API。

用系统浏览器打开；装上 pywebview 后可以只替换「打开窗口」这一步，
页面与 API 都不需要改。
"""

from __future__ import annotations

import json
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import backend, jobs

STATIC = Path(__file__).resolve().parent / "static"
TOKEN = secrets.token_urlsafe(24)

CONTENT_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
}

ROUTES = {
    "/api/detect": backend.detect_all,
    "/api/plan": backend.plan_preview,
    "/api/nofade": backend.nofade_info,
    "/api/open": backend.open_path,
    "/api/open-url": backend.open_url,
    "/api/open-installer": backend.open_installer,
    "/api/channel-info": backend.channel_info,
    "/api/journals": backend.journal_list,
    "/api/regions": backend.regions_info,
    "/api/run": jobs.start,
    "/api/job": jobs.get,
    "/api/cancel": jobs.cancel,
}


class Handler(BaseHTTPRequestHandler):
    server_version = "azp-ui/0.1"

    def log_message(self, fmt, *args) -> None:
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, data: dict) -> None:
        self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def _token_ok(self, query: dict[str, list[str]]) -> bool:
        token = (query.get("token") or [""])[0] or self.headers.get("X-Azp-Token", "")
        return secrets.compare_digest(token, TOKEN)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path in ("/", "/index.html"):
            if not self._token_ok(query):
                self._send(403, b"forbidden", "text/plain; charset=utf-8")
                return
            html = (STATIC / "index.html").read_text("utf-8")
            self._send(200, html.replace("__AZP_TOKEN__", TOKEN).encode("utf-8"),
                       "text/html; charset=utf-8")
            return
        if parsed.path.startswith("/static/"):
            file = (STATIC / parsed.path[len("/static/"):]).resolve()
            if STATIC.resolve() not in file.parents or not file.is_file():
                self._send(404, b"not found", "text/plain; charset=utf-8")
                return
            ctype = CONTENT_TYPES.get(file.suffix.lower(), "application/octet-stream")
            self._send(200, file.read_bytes(), ctype)
            return
        self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._token_ok({}):
            self._json(403, {"ok": False, "error": "forbidden"})
            return
        handler = ROUTES.get(urlparse(self.path).path)
        if handler is None:
            self._json(404, {"ok": False, "error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
            if not isinstance(payload, dict):
                raise ValueError("请求体必须是对象")
            result = handler(payload)
        except Exception as exc:  # noqa: BLE001
            self._json(200, {"ok": False, "error": str(exc)})
            return
        data = {"ok": True}
        data.update(result or {})
        self._json(200, data)


def serve(out: str = "out", port: int = 0, open_browser: bool = True) -> int:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{httpd.server_address[1]}/?token={TOKEN}"
    print(f"界面地址：{url}")
    print(f"清单与计划输出目录：{Path(out).resolve()}")
    print("关闭浏览器不会结束服务；在本窗口按 Ctrl+C 停止。")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
    return 0