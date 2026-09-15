"""富邦 eBrokerDJ 分點進出抓取器。"""
from __future__ import annotations

import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime
from html.parser import HTMLParser

FUBON_URL = "https://fubon-ebrokerdj.fbs.com.tw/z/zg/zgb/zgb0.djhtm"
DEFAULT_BRANCHES = ({"broker_code": "9200", "branch_code": "9275", "name": "凱基-三多"},)


@dataclass(frozen=True)
class BranchTrade:
    trade_date: date
    branch_name: str
    symbol: str
    stock_name: str
    buy_amount: int
    sell_amount: int
    net_amount: int
    inventory_cost: int | None = None
    inventory_value: int | None = None


class _TableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr": self._row = []
        elif tag in ("td", "th") and self._row is not None: self._cell = []

    def handle_data(self, data):
        if self._cell is not None: self._cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row: self.rows.append(self._row)
            self._row = None


def _number(value: str) -> int:
    value = value.replace(",", "").replace("－", "-").strip()
    m = re.search(r"-?\d+", value)
    return int(m.group()) if m else 0


def _javascript_html(text: str) -> str:
    """還原富邦放在 document.write/writeln 字串中的 table HTML。"""
    fragments: list[str] = []
    pattern = re.compile(
        r"document\.(?:write|writeln)\s*\(\s*([\"'])(.*?)\1\s*\)",
        re.IGNORECASE | re.DOTALL,
    )
    for match in pattern.finditer(text):
        fragment = match.group(2)
        fragment = (fragment.replace(r"\'", "'")
                            .replace(r'\"', '"')
                            .replace(r"\n", "\n")
                            .replace(r"\r", ""))
        fragments.append(fragment)
    return "\n".join(fragments)


def fetch_branch_trades(branch: dict[str, str], *, timeout: float = 30) -> list[BranchTrade]:
    query = urllib.parse.urlencode({"a": branch["broker_code"], "b": branch["branch_code"]})

    req = urllib.request.Request(FUBON_URL + "?" + query, headers={
        "User-Agent": "Mozilla/5.0 (compatible; StockQuant/1.0)",
        "Accept": "text/html,application/xhtml+xml",
    })
    with urllib.request.urlopen(req, timeout=timeout) as response:
        raw = response.read()
        charset = response.headers.get_content_charset() or "big5"
        text = raw.decode(charset, errors="replace")
    generated_html = _javascript_html(text)
    parse_text = text + "\n" + generated_html
    m = re.search(r"資料日期：\s*(\d{8})", parse_text)

        if not m:
        m = re.search(r"資料日期[^0-9]*(\d{8})", parse_text)



    if not m:
        raise ValueError("富邦頁面找不到資料日期")
        trade_date = datetime.strptime(m.group(1), "%Y%m%d").date()
    parser = _TableParser()
    parser.feed(parse_text)


    out: list[BranchTrade] = []
    for row in parser.rows:
        if len(row) < 4 or not re.match(r"^\d{4,6}[A-Za-z]?$", row[0].strip()):
            continue
        symbol_name = row[0].strip()
        match = re.match(r"^(\d{4,6}[A-Za-z]?)(.*)$", symbol_name)
        if not match: continue
        symbol, name = match.group(1), match.group(2).strip()
        buy, sell = _number(row[-3]), _number(row[-2])
        net = _number(row[-1])
        out.append(BranchTrade(trade_date, branch["name"], symbol, name, buy, sell, net))
    if not out:
        raise ValueError("富邦頁面沒有解析到分點交易資料")
    return out
