"""股票池：抓「全市場」代號清單。

- 台股：證交所（上市）＋ 櫃買中心（上櫃）OpenAPI，只留 4 碼代號（普通股與 0050 這類 ETF）
- 美股：S&P 500 成分股
抓不到時依序退回：上次的快取 → config 裡的 fallback_symbols。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import requests

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
UA = {"User-Agent": "Mozilla/5.0 (stock-sim)"}

TWSE_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TPEX_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"
SP500_URL = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"


def _pick(row: dict, *needles: str) -> str | None:
    """欄位名稱偶爾會變，用關鍵字找。"""
    for k, v in row.items():
        if any(n.lower() in k.lower() for n in needles):
            return str(v).strip()
    return None


def _tw_list(url: str, suffix: str) -> dict[str, str]:
    rows = requests.get(url, headers=UA, timeout=30).json()
    out = {}
    for r in rows:
        code = _pick(r, "Code")
        name = _pick(r, "Name") or ""
        if code and re.fullmatch(r"\d{4}", code):
            out[f"{code}{suffix}"] = name
    return out


def fetch_tw() -> dict[str, str]:
    names = _tw_list(TWSE_URL, ".TW")
    names.update(_tw_list(TPEX_URL, ".TWO"))
    if len(names) < 500:
        raise RuntimeError(f"台股清單數量異常：{len(names)}")
    return names


def fetch_us() -> dict[str, str]:
    import io

    import pandas as pd
    text = requests.get(SP500_URL, headers=UA, timeout=30).text
    df = pd.read_csv(io.StringIO(text))
    sym_col = next(c for c in df.columns if c.lower().startswith("symbol"))
    name_col = next((c for c in df.columns if c.lower() in ("security", "name")), sym_col)
    names = {str(s).replace(".", "-"): str(n) for s, n in zip(df[sym_col], df[name_col])}
    if len(names) < 400:
        raise RuntimeError(f"S&P 500 清單數量異常：{len(names)}")
    return names


FETCHERS = {"twse_tpex": fetch_tw, "sp500": fetch_us}


def get(market: str, ucfg: dict, extra: list[str]) -> dict[str, str]:
    """回傳 {代號: 名稱}。extra（例如大盤基準）一定會被加入。"""
    DATA_DIR.mkdir(exist_ok=True)
    path = DATA_DIR / f"universe_{market}.json"
    try:
        names = FETCHERS[ucfg["source"]]()
        path.write_text(json.dumps(names, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        if path.exists():
            print(f"[警告] 股票清單下載失敗（{exc}），改用上次的清單")
            names = json.loads(path.read_text(encoding="utf-8"))
        else:
            print(f"[警告] 股票清單下載失敗（{exc}），改用 fallback_symbols")
            names = {s: s for s in ucfg.get("fallback_symbols", [])}
    for s in extra:
        names.setdefault(s, s)
    return names


def cached(market: str) -> dict[str, str]:
    path = DATA_DIR / f"universe_{market}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
