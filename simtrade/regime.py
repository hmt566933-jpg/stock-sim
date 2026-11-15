"""牛市／熊市分析：用大盤是否站上長期均線（台股年線 240 日、美股 200 日）判斷每天的市場狀態，
再把各策略的每日報酬分到牛市日、熊市日分別計算。

判斷用的是「前一天收盤」的狀態，避免用到當天才知道的資訊。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def classify(bench: pd.Series, ma: int) -> pd.Series:
    """回傳每天的狀態：True=牛市、False=熊市、NaN=資料不足。"""
    b = bench.ffill()
    line = b.rolling(ma, min_periods=ma).mean()
    state = (b > line).where(line.notna())
    return state.shift(1)


def periods(state: pd.Series) -> list[dict]:
    """把連續的熊市日合併成區段（給圖表畫底色）。"""
    out, start, prev = [], None, None
    for dt, v in state.items():
        if v is False or v == 0.0:
            start = start or dt
        elif start is not None:
            out.append(dict(start=str(start.date()), end=str(prev.date())))
            start = None
        prev = dt
    if start is not None:
        out.append(dict(start=str(start.date()), end=str(prev.date())))
    return out


def split_returns(equity: pd.Series, state: pd.Series) -> dict:
    r = equity.pct_change()
    res = {}
    for key, flag in (("bull", 1.0), ("bear", 0.0)):
        rr = r[state == flag].dropna()
        n = len(rr)
        cum = float((1 + rr).prod() - 1) if n else 0.0
        ann = float((1 + cum) ** (252 / n) - 1) if n else None
        vol = float(rr.std() * np.sqrt(252)) if n > 1 else None
        res[key] = dict(days=n, cum=round(cum * 100, 2),
                        ann=round(ann * 100, 2) if ann is not None else None,
                        win_days=round(float((rr > 0).mean() * 100), 1) if n else None,
                        vol=round(vol * 100, 2) if vol is not None else None)
    return res


def summary(bench: pd.Series, ma: int) -> tuple[pd.Series, dict]:
    state = classify(bench, ma)
    b = bench.ffill()
    line = b.rolling(ma, min_periods=ma).mean()
    now = bool(b.iloc[-1] > line.iloc[-1]) if pd.notna(line.iloc[-1]) else None
    info = dict(ma=ma, bull_days=int((state == 1.0).sum()), bear_days=int((state == 0.0).sum()),
                now="牛市" if now else "熊市" if now is not None else None,
                bench_price=round(float(b.iloc[-1]), 2),
                ma_value=round(float(line.iloc[-1]), 2) if pd.notna(line.iloc[-1]) else None,
                bear_periods=periods(state))
    return state, info
