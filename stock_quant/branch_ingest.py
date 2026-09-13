"""每日 19:30 抓取主力分點並上傳/推播。"""
from __future__ import annotations
import json, urllib.request
from datetime import date, datetime, time
from .datasource.fubon_branch import DEFAULT_BRANCHES, fetch_branch_trades
from .notify import LineNotifier

class BranchIngestor:
    def __init__(self, base_url: str, token: str, branches=None, fire_time: time=time(19,30), log=print):
        self.base_url, self.token = base_url.rstrip('/'), token
        self.branches = list(branches or DEFAULT_BRANCHES)
        self.fire_time, self.log, self.sent = fire_time, log, set()
    def process(self, now: datetime):
        if now.time() < self.fire_time: return
        for branch in self.branches:
            key = (branch['branch_code'], now.date())
            if key in self.sent: continue
            try:
                rows = fetch_branch_trades(branch)
                payload = {'trade_date': rows[0].trade_date.isoformat(), 'branch_code': branch['branch_code'], 'branch_name': branch['name'],
                           'items': [r.__dict__ | {'trade_date': r.trade_date.isoformat()} for r in rows]}
                req = urllib.request.Request(self.base_url + '/userapi/branches/ingest', data=json.dumps(payload).encode(), method='POST', headers={'Content-Type':'application/json','X-Ingest-Token':self.token})
                with urllib.request.urlopen(req, timeout=30) as response: response.read()
                notifier = LineNotifier()
                if notifier.configured:
                    top_buy = sorted(rows, key=lambda r: r.net_amount, reverse=True)[:5]
                    top_sell = sorted(rows, key=lambda r: r.net_amount)[:5]
                    lines = [f"分點進出｜{branch['name']}｜{rows[0].trade_date}", "買超："]
                    lines += [f"{r.symbol} {r.stock_name} +{r.net_amount:,}" for r in top_buy]
                    lines.append("賣超：")
                    lines += [f"{r.symbol} {r.stock_name} {r.net_amount:,}" for r in top_sell]
                    lines.append("單位：仟元；僅供參考，非投資建議")
                    notifier.push('\n'.join(lines))
                self.sent.add(key); self.log(f"分點進出已完成：{branch['name']} {len(rows)} 筆")
            except Exception as exc:
                self.log(f"分點進出失敗：{branch['name']} {exc}")
