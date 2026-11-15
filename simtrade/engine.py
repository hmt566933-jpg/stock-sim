"""引擎：把策略的目標權重，逐日交給模擬倉成交，記錄每天淨值。

每天自動更新時，會從起始日重跑到最新一天。因為規則固定、資料固定，
結果完全可重現，不需要另外保存「昨天的持倉狀態」。
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from .broker import Account, Costs
from .strategies import Strategy


def run(strategy: Strategy, wide: dict[str, pd.DataFrame], eligible: pd.DataFrame,
        initial_cash: float, costs: Costs, execution: str = "next_open", band: float = 0.02) -> dict:
    close = wide["close"]
    open_ = wide["open"]
    low = wide.get("low")
    dip = getattr(strategy, "entry_dip", None) or 0.0  # 限價進場：比前一天收盤低 dip 才買
    weights = strategy.target_weights(wide, eligible).reindex_like(close).fillna(0.0)
    w_arr = weights.to_numpy()
    cols = weights.columns
    mark = close.ffill()  # 停牌日用最後收盤價估值

    acct = Account(cash=float(initial_cash), costs=costs)
    equity = []
    # 引擎層停利停損（展望跟單用；一般策略的停損在 select() 裡處理）
    eng_exits = getattr(strategy, "engine_exits", False)
    sl, tp = getattr(strategy, "stop_loss", None), getattr(strategy, "take_profit", None)
    banned: set[str] = set()
    dates = close.index

    for i, dt in enumerate(dates):
        if execution == "next_open":
            if i == 0:
                equity.append(acct.cash)
                continue
            row = w_arr[i - 1]
            exec_px = open_.iloc[i]
        else:  # "close"：今天收盤決定，今天收盤價成交
            row = w_arr[i]
            exec_px = close.iloc[i]
        # 全市場有上千檔，只處理「想持有」或「已持有」的股票
        target = {cols[j]: float(row[j]) for j in np.flatnonzero(row > 0)}
        if eng_exits and i > 0:
            banned &= set(target)  # 跟隨的策略不再持有時解除封鎖
            prev = close.iloc[i - 1]
            for sym, q in acct.shares.items():
                c = prev[sym]
                if pd.isna(c):
                    continue
                r = float(c) * q / acct.cost_basis[sym] - 1
                if (sl and r <= -sl) or (tp and r >= tp):
                    banned.add(sym)
            for sym in banned:
                target.pop(sym, None)
        relevant = sorted(set(target) | set(acct.shares))  # 排序讓結果可重現
        mark_i = mark.iloc[i]
        prev_close = close.iloc[i - 1] if i > 0 else None

        px = {s: float(exec_px[s]) for s in relevant if pd.notna(exec_px[s]) and exec_px[s] > 0}
        value_px = {s: (px.get(s) if s in px else mark_i[s]) for s in acct.shares}
        total = acct.market_value(value_px)

        orders = []  # (symbol, 目標股數)
        for sym, p in px.items():
            w = target.get(sym, 0.0)
            held = acct.shares.get(sym, 0)
            want = math.floor(w * total / p) if w > 0 else 0
            if w <= 0 and held > 0:
                orders.append((sym, 0))
            elif w > 0 and held == 0:
                orders.append((sym, want))
            elif w > 0 and strategy.rebalance and abs(want - held) * p > band * total:
                orders.append((sym, want))

        pc = (lambda s: float(prev_close[s]) if prev_close is not None and pd.notna(prev_close[s]) else None)
        # 先賣後買，賣出的錢才能拿來買
        for sym, want in orders:
            held = acct.shares.get(sym, 0)
            if want < held:
                acct.sell(dt, sym, held - want, px[sym], pc(sym))
        for sym, want in orders:
            held = acct.shares.get(sym, 0)
            if want > held:
                price = px[sym]
                if dip > 0 and held == 0 and execution == "next_open":
                    ref = pc(sym)
                    if ref is None:
                        continue
                    limit = ref * (1 - dip)
                    day_low = float(low.iloc[i][sym]) if low is not None and pd.notna(low.iloc[i][sym]) else price
                    if price <= limit:
                        pass                      # 開盤就低於限價：用開盤價成交
                    elif day_low <= limit:
                        price = limit             # 盤中跌到限價：用限價成交
                    else:
                        continue                  # 沒跌到限價：今天買不到
                acct.buy(dt, sym, want - held, price, pc(sym))

        equity.append(acct.market_value({s: mark_i[s] for s in acct.shares}))

    eq = pd.Series(equity, index=dates, name=strategy.label)
    last = mark.iloc[-1]
    positions = [dict(symbol=s, qty=q, avg_cost=round(acct.cost_basis[s] / q, 4),
                      price=round(float(last[s]), 4),
                      value=round(q * float(last[s]), 2),
                      pnl_pct=round((q * float(last[s]) / acct.cost_basis[s] - 1) * 100, 2))
                 for s, q in sorted(acct.shares.items())]
    return dict(equity=eq, trades=acct.trades, positions=positions, cash=round(acct.cash, 2), weights=weights,
                pending=_pending(weights.iloc[-1], acct, strategy, last))


def _pending(target: pd.Series, acct: Account, strategy: Strategy, last: pd.Series) -> list[dict]:
    """最新收盤後的決定 → 下一個交易日預計要做的動作（給儀表板顯示）。
    股數以最新收盤價估算；實際成交用隔天開盤價，股數可能略有不同。"""
    out = []
    total = acct.market_value({s: float(last[s]) for s in acct.shares})
    for sym, w in target.items():
        held = acct.shares.get(sym, 0)
        px = float(last[sym]) if pd.notna(last[sym]) else 0.0
        if w <= 0 and held > 0:
            out.append(dict(symbol=sym, action="賣出", qty=held, price=round(px, 2)))
        elif w > 0 and held == 0 and px > 0:
            qty = int(w * total // px)
            if qty > 0:
                out.append(dict(symbol=sym, action="買進", qty=qty, price=round(px, 2)))
        elif w > 0 and held > 0 and strategy.rebalance and px > 0:
            want = int(w * total // px)
            if want != held and abs(want - held) * px > 0.02 * total:
                out.append(dict(symbol=sym, action="加碼" if want > held else "減碼", qty=abs(want - held), price=round(px, 2)))
    return out
