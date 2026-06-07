"""共用的 HTTP 取得工具: 含 timeout、重試與指數退避。

刻意只依賴標準函式庫 urllib，讓專案零第三方依賴也能跑起來;
若已安裝 requests，可自行替換為 requests 版本。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; stock-quant/0.1; +https://example.local)",
    "Accept": "application/json",
}


def get_json(
    url: str,
    *,
    timeout: float = 30.0,
    retries: int = 3,
    backoff: float = 1.5,
    headers: dict[str, str] | None = None,
) -> Any:
    """GET 一個 JSON 端點，失敗時以指數退避重試。

    回傳已 parse 的 Python 物件 (通常是 list[dict])。
    全部重試失敗才丟出最後一個例外。
    """
    last_err: Exception | None = None
    req_headers = {**DEFAULT_HEADERS, **(headers or {})}

    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=req_headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            return json.loads(raw.decode("utf-8-sig"))  # utf-8-sig 去除 BOM
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as exc:
            last_err = exc
            if attempt < retries:
                sleep_s = backoff ** attempt
                time.sleep(sleep_s)
            continue

    raise RuntimeError(f"GET {url} 連續 {retries} 次失敗") from last_err
