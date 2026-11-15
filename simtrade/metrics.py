"""績效層：把淨值曲線和交易紀錄算成可比較的數字。"""
from __future__ import annotations

import numpy as np
import pandas as pd


def summarize(equity: pd.Series, trades: list[dict]) -> dict:
    eq = equity.dropna()
    eq = eq[eq > 0]
    days = (eq.index[-1] - eq.index[0]).days or 1
    total = eq.iloc[-1] / eq.iloc[0] - 1
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (365.25 / days) - 1
    rets = eq.pct_change().dropna()
    vol = rets.std() * np.sqrt(252)
    sharpe = rets.mean() / rets.std() * np.sqrt(252) if rets.std() > 0 else 0.0
    mdd = (eq / eq.cummax() - 1).min()
    sells = [t for t in trades if t["side"] == "賣"]
    wins = sum(1 for t in sells if t["pnl"] > 0)
    return dict(
        final=round(float(eq.iloc[-1]), 2),
        total_return=round(float(total) * 100, 2),
        cagr=round(float(cagr) * 100, 2),
        max_drawdown=round(float(mdd) * 100, 2),
        volatility=round(float(vol) * 100, 2),
        sharpe=round(float(sharpe), 2),
        trades=len(trades),
        win_rate=round(wins / len(sells) * 100, 1) if sells else None,
        fees=round(sum(t["fee"] for t in trades), 2),
    )
