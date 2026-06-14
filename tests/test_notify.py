"""LINE 推播 / 命中彙整 / .env 載入的單元測試 (mock 網路，不需真的連線)。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import date, datetime, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stock_quant import notify as notify_mod
from stock_quant.notify import DailyDigestAlerter, LineNotifier, StableAlerter, load_dotenv


class _Row:
    """模擬 intraday.TickRow 的最小介面。"""
    def __init__(self, symbol, change_pct, close=10.0, stable=True, zh="上市"):
        self.symbol = symbol
        self.stable = stable
        self.market = type("M", (), {"zh": zh})()
        self.result = type("R", (), {"change_pct": change_pct, "close": close})()


class _FakeNotifier:
    def __init__(self, fail=False):
        self.fail = fail
        self.messages = []

    def push(self, text):
        if self.fail:
            raise RuntimeError("boom")
        self.messages.append(text)


# ---- .env 載入 ---------------------------------------------------------
def test_load_dotenv_reads_file():
    saved_u = os.environ.pop("LINE_USER_ID", None)
    saved_t = os.environ.pop("LINE_CHANNEL_TOKEN", None)
    p = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False, encoding="utf-8") as f:
            f.write('# 註解\nLINE_USER_ID="Uabc"\nexport LINE_CHANNEL_TOKEN=tok123\n')
            p = f.name
        loaded = load_dotenv(p, override=True)
        assert loaded["LINE_USER_ID"] == "Uabc"
        assert loaded["LINE_CHANNEL_TOKEN"] == "tok123"
        assert os.environ["LINE_USER_ID"] == "Uabc"        # 已寫進環境變數
    finally:
        if p:
            os.unlink(p)
        os.environ.pop("LINE_USER_ID", None)
        os.environ.pop("LINE_CHANNEL_TOKEN", None)
        if saved_u is not None:
            os.environ["LINE_USER_ID"] = saved_u
        if saved_t is not None:
            os.environ["LINE_CHANNEL_TOKEN"] = saved_t


def test_load_dotenv_does_not_override_existing():
    saved = os.environ.get("LINE_USER_ID")
    os.environ["LINE_USER_ID"] = "EXISTING"
    p = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False, encoding="utf-8") as f:
            f.write("LINE_USER_ID=FROMFILE\n")
            p = f.name
        load_dotenv(p)                                     # override=False
        assert os.environ["LINE_USER_ID"] == "EXISTING"    # 環境變數優先
    finally:
        if p:
            os.unlink(p)
        if saved is None:
            os.environ.pop("LINE_USER_ID", None)
        else:
            os.environ["LINE_USER_ID"] = saved


# ---- LineNotifier ------------------------------------------------------
def test_notifier_configured():
    assert LineNotifier(token="t", user_ids=["U1"]).configured
    assert not LineNotifier(token="", user_ids=["U1"]).configured
    assert not LineNotifier(token="t", user_ids=[]).configured


def test_notifier_user_ids_from_csv():
    n = LineNotifier(token="t", user_ids="U1, U2 ,U3")
    assert n.user_ids == ["U1", "U2", "U3"]


def test_notifier_push_builds_request(monkeypatch):
    sent = []

    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b"{}"

    def fake_urlopen(req, timeout=None):
        sent.append(req)
        return FakeResp()

    monkeypatch.setattr(notify_mod.urllib.request, "urlopen", fake_urlopen)
    LineNotifier(token="tok", user_ids=["U1", "U2"]).push("嗨 ✅")
    assert len(sent) == 2                                   # 兩個 userId -> 兩次 push
    req = sent[0]
    assert req.full_url == notify_mod.PUSH_URL
    assert req.get_header("Authorization") == "Bearer tok"
    payload = json.loads(req.data.decode("utf-8"))
    assert payload["to"] == "U1"
    assert payload["messages"][0]["text"] == "嗨 ✅"


def test_notifier_push_unconfigured_raises():
    try:
        LineNotifier(token="", user_ids=[]).push("x")
        assert False, "應該丟例外"
    except RuntimeError:
        pass


# ---- StableAlerter 即時模式 去重 / 失敗處理 ----------------------------
def test_alerter_dedupes_same_symbol():
    fn = _FakeNotifier()
    al = StableAlerter(fn, log=lambda m: None)
    now = datetime(2026, 6, 10, 10, 0)
    assert al.process(now, [_Row("2330", 5.0), _Row("1101", 3.5)]) == ["2330", "1101"]
    assert al.process(now, [_Row("2330", 6.0), _Row("1101", 3.6)]) == []      # 同檔不重推
    assert al.process(now, [_Row("2330", 6.0), _Row("2454", 4.0)]) == ["2454"]  # 只推新檔
    assert len(fn.messages) == 2


def test_alerter_ignores_unstable():
    fn = _FakeNotifier()
    al = StableAlerter(fn, log=lambda m: None)
    out = al.process(datetime(2026, 6, 10, 10, 0), [_Row("2330", 5.0, stable=False)])
    assert out == [] and fn.messages == []


def test_alerter_push_failure_not_marked():
    fn = _FakeNotifier(fail=True)
    logs = []
    al = StableAlerter(fn, log=logs.append)
    now = datetime(2026, 6, 10, 10, 0)
    assert al.process(now, [_Row("2330", 5.0)]) == []      # 失敗 -> 不算已推
    fn.fail = False
    assert al.process(now, [_Row("2330", 5.0)]) == ["2330"]  # 下次重試成功
    assert logs and "LINE" in logs[0]


def test_alerter_message_format():
    fn = _FakeNotifier()
    al = StableAlerter(fn, log=lambda m: None)
    al.process(datetime(2026, 6, 10, 10, 30, 5), [_Row("2330", 5.12, close=1005)])
    msg = fn.messages[0]
    assert "2330" in msg and "5.12" in msg and "非投資建議" in msg


# ---- DailyDigestAlerter 每日定時彙整 ----------------------------------
def test_digest_accumulates_then_fires_at_time():
    fn = _FakeNotifier()
    al = DailyDigestAlerter(fn, fire_time=time(12, 0), log=lambda m: None)
    # 13:00 之前: 只累積、不推
    assert al.process(datetime(2026, 6, 10, 10, 0), [_Row("2330", 5.0)]) == []
    assert al.process(datetime(2026, 6, 10, 11, 0), [_Row("1101", 3.5)]) == []
    assert fn.messages == []
    # 到 13:00: 一次推完當天累積 (含這個 tick 的 2454)
    out = al.process(datetime(2026, 6, 10, 12, 0), [_Row("2454", 4.0)])
    assert set(out) == {"2330", "1101", "2454"} and len(fn.messages) == 1
    # 13:00 之後: 當天已推，不再推
    assert al.process(datetime(2026, 6, 10, 12, 1), [_Row("3008", 6.0)]) == []
    assert len(fn.messages) == 1


def test_digest_empty_still_fires():
    fn = _FakeNotifier()
    al = DailyDigestAlerter(fn, fire_time=time(12, 0), log=lambda m: None)
    assert al.process(datetime(2026, 6, 10, 12, 0), []) == []     # 無符合 -> 回傳空
    assert len(fn.messages) == 1 and "尚無符合" in fn.messages[0]  # 但仍推一則


def test_digest_resets_next_day():
    fn = _FakeNotifier()
    al = DailyDigestAlerter(fn, fire_time=time(12, 0), log=lambda m: None)
    al.process(datetime(2026, 6, 10, 12, 0), [_Row("2330", 5.0)])
    assert len(fn.messages) == 1
    out = al.process(datetime(2026, 6, 11, 12, 0), [_Row("2317", 3.0)])  # 隔日重新累積可再推
    assert out == ["2317"] and len(fn.messages) == 2


def test_digest_failure_retries():
    fn = _FakeNotifier(fail=True)
    logs = []
    al = DailyDigestAlerter(fn, fire_time=time(12, 0), log=logs.append)
    assert al.process(datetime(2026, 6, 10, 12, 0), [_Row("2330", 5.0)]) == []  # 失敗 -> 不標記
    fn.fail = False
    assert al.process(datetime(2026, 6, 10, 12, 1), [_Row("2330", 5.0)]) == ["2330"]  # 重試成功
    assert logs and "日結" in logs[0]


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
