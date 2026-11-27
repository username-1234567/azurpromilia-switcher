"""启动器接口的带签名请求。"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from .channels import Channel

# 配置只有几 KB，清单也就几十 MB；响应无限读会把内存拖垮（gzip 解压后同样限量）
MAX_RESPONSE_BYTES = 512 * 1024 * 1024


class ApiError(RuntimeError):
    pass


def _read_limited(resp, limit: int = MAX_RESPONSE_BYTES) -> bytes:
    """分块读响应，超过上限就中止。urllib/gzip 的 read() 都会一次读到底。"""
    chunks: list[bytes] = []
    total = 0
    while True:
        block = resp.read(1024 * 1024)
        if not block:
            return b"".join(chunks)
        total += len(block)
        if total > limit:
            raise ApiError(f"响应超过 {limit // 2**20} MiB 上限，已中止")
        chunks.append(block)


def _device_id() -> str:
    seed = os.environ.get("AZP_DEVICE_ID") or hashlib.md5(
        f"{os.environ.get('COMPUTERNAME', 'pc')}-azp".encode()
    ).hexdigest()
    return hashlib.md5(seed.encode()).hexdigest()


def user_agent(version: str) -> str:
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"AzurPromilia/{version} Chrome/120.0.0.0 Safari/537.36"
    )


def _client_info(lang: str, version: str) -> str:
    head = lang.split("-")[0].lower()
    device_lang, country, tz = {
        "zh": ("zh", "CN", "UTC+08:00"),
        "ja": ("ja", "JP", "UTC+09:00"),
    }.get(head, ("en", "CN", "UTC+08:00"))
    return json.dumps(
        {
            "UA": user_agent(version),
            "device_model": "Launcher",
            "device_os": "Windows 10",
            "device_os_version": "10.0.19045",
            "device_network": "Ethernet",
            "device_language": device_lang,
            "device_timezone": tz,
            "device_country": country,
            "app_version": version,
            "grp_trace_id": "",
            "spec_device_info": {},
            "device_uuid": "",
            "sub_channel_trace_id": "",
            "init_trace_id": "",
            "previous_trace_id": "",
            "current_trace_id": "",
        },
        separators=(",", ":"),
        ensure_ascii=False,
    )


class Api:
    def __init__(self, channel: Channel):
        self.ch = channel
        self.version = channel.launcher_version or "1.0.0.0"
        self.device_id = _device_id()

    def _sign(self, canonical: str, ts_ms: int) -> str:
        raw = f"{ts_ms}{canonical}{self.ch.app_id}{self.ch.channel_code}{self.ch.secret}"
        return hashlib.md5(raw.encode()).hexdigest()

    def _headers(self, canonical: str, ts_ms: int) -> dict[str, str]:
        return {
            "User-Agent": user_agent(self.version),
            "Accept": "application/json, text/plain, */*",
            "Accept-Encoding": "gzip",
            "device-id": self.device_id,
            "grp-trace-id": "",
            "grp-app-id": self.ch.app_id,
            "grp-platform": "Launcher",
            "grp-channel-code": self.ch.channel_code,
            "grp-sub-channel-code": "SC000",
            "sdk-version": self.version,
            "channel-sdk-version": self.version,
            "grp-launcher-version-code": self.version,
            "lang": self.ch.lang,
            "grp-client-info": _client_info(self.ch.lang, self.version),
            "utoken": "",
            "timestamp": str(ts_ms),
            "sign": self._sign(canonical, ts_ms),
        }

    def get(self, path: str, params: dict[str, str], retries: int = 3, timeout: int = 30) -> dict:
        if not self.ch.domains:
            raise ApiError(
                f"{self.ch.label} 没有配置接口域名。"
                "在 regions.json 里给这个区补 domains，再重试。"
            )
        canonical = "&".join(f"{k}={params[k]}" for k in sorted(params))
        query = urllib.parse.urlencode(params)
        last = ""
        for domain in self.ch.domains:
            url = f"{domain}{path}" + (f"?{query}" if query else "")
            for attempt in range(retries):
                ts = int(time.time() * 1000)
                req = urllib.request.Request(url, headers=self._headers(canonical, ts))
                try:
                    with urllib.request.urlopen(req, timeout=timeout) as resp:
                        body = _read_limited(resp)
                        if resp.headers.get("Content-Encoding") == "gzip":
                            with gzip.GzipFile(fileobj=io.BytesIO(body)) as gz:
                                body = _read_limited(gz)
                    data = json.loads(body.decode("utf-8", "replace"))
                    if data.get("code") not in (0, None):
                        raise ApiError(f"接口返回 code={data.get('code')} msg={data.get('message') or data.get('msg')}")
                    return data
                except urllib.error.HTTPError as exc:
                    detail = ""
                    try:
                        detail = exc.read()[:200].decode("utf-8", "replace")
                    except Exception:  # noqa: BLE001
                        pass
                    last = f"HTTP {exc.code} {url} {detail}"
                except Exception as exc:  # noqa: BLE001
                    last = f"{type(exc).__name__}: {exc} ({url})"
                time.sleep(1.2 * (attempt + 1))
        raise ApiError(f"所有域名均失败: {last}")


def http_get_bytes(url: str, retries: int = 3, timeout: int = 120, ua: str = "") -> bytes:
    # url 来自接口返回的 downloadUrl：只接受 http(s)，挡住 file:// 读本地文件、
    # 以及把请求引到别的协议上去的写法
    scheme = urllib.parse.urlsplit(url).scheme.lower()
    if scheme not in ("http", "https"):
        raise ApiError(f"不支持的下载地址协议：{url[:80]}")
    last = ""
    for attempt in range(retries):
        req = urllib.request.Request(
            url, headers={"User-Agent": ua or user_agent("1.0.0.0"), "Accept-Encoding": "identity"}
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return _read_limited(resp)
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}: {exc}"
            time.sleep(1.5 * (attempt + 1))
    raise ApiError(f"下载失败 {url}: {last}")
