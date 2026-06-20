"""開機時取得 GCP VM 對外 IP 並透過 LINE 推播。

VM 每天開機 (IP 會變) -> app 啟動 -> 推一則「今日對外 IP」。
沿用 notify.py 的 LineNotifier (同一組 LINE_CHANNEL_TOKEN / LINE_USER_ID)。

對外 IP 來源是 GCE metadata server，VM/容器內部免任何權限即可查詢。
用數字 IP 169.254.169.254 (而非 metadata.google.internal) 以免容器內 DNS 解析不到。
零第三方依賴，只用標準函式庫 urllib。
"""
from __future__ import annotations

import urllib.request
from datetime import datetime
from typing import Callable, Optional

# GCE metadata server: 第一張網卡第一個 access-config 的對外 IP
_EXTERNAL_IP_URL = (
    "http://169.254.169.254/computeMetadata/v1/"
    "instance/network-interfaces/0/access-configs/0/external-ip"
)
_HEADERS = {"Metadata-Flavor": "Google"}


def gcp_external_ip(timeout: float = 5.0) -> str:
    """回傳本機 (GCE VM) 的對外 IP；非 GCE 環境或無外網 IP 會丟出例外。"""
    req = urllib.request.Request(_EXTERNAL_IP_URL, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8").strip()


def push_startup_ip(notifier, log: Callable[[str], None] = print,
                    now: Optional[datetime] = None) -> Optional[str]:
    """開機時推一則對外 IP。成功回傳 IP，失敗 (取不到 IP / 推播失敗 / 未設定) 回傳 None。

    刻意吞掉所有例外：開機通知不該讓主程式 (選股) 起不來。
    """
    if not getattr(notifier, "configured", False):
        log("ℹ️ 未設定 LINE，略過開機 IP 推播")
        return None
    try:
        ip = gcp_external_ip()
    except Exception as exc:  # noqa: BLE001 — 非 GCE / 查不到 IP 時不可中斷主程式
        log(f"⚠️ 取得對外 IP 失敗 (可能非 GCE 環境): {exc}")
        return None
    when = now or datetime.now()
    # 帶 http:// 前綴，LINE 會自動轉成可點擊連結；前端 Vite 預設埠 5173。
    text = f"🟢 VM 已開機 {when:%Y-%m-%d %H:%M}\n今日前端網址：http://{ip}:5173"
    try:
        notifier.push(text)
    except Exception as exc:  # noqa: BLE001
        log(f"⚠️ LINE 開機 IP 推播失敗: {exc}")
        return None
    log(f"📨 已推開機對外 IP: {ip}")
    return ip
