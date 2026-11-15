"""展望跟單：每週最後一個交易日收盤後，看各策略的「下週展望」（只用當時已知的週報酬），
挑出展望最好的策略，下一週就照著那個策略的持股做。

挑選標準（config 的 pick）：
  "med" → 過去 52 週週報酬的中位數最高（預設）
  "up"  → 過去 52 週賺錢週數比例最高
"""
from __future__ import annotations

import pandas as pd

from . import forecast


def build_weights(results: dict[str, dict], index: pd.DatetimeIndex, pick: str = "med",
                  exclude: tuple = ()) -> tuple[pd.DataFrame, list[dict]]:
    """results：{策略代碼: {"label", "equity", "weights"}}。回傳（每日權重, 每週選擇紀錄）。"""
    periods = pd.Series(index.to_period("W-FRI"), index=index)
    week_ends = index[(periods != periods.shift(-1)).to_numpy()]
    # 各策略每週報酬
    weekly = {}
    for k, r in results.items():
        if k in exclude:
            continue
        eq = r["equity"]
        last = eq.groupby(eq.index.to_period("W-FRI")).last()
        weekly[k] = (last.pct_change() * 100).fillna(last.iloc[0] / eq.iloc[0] * 100 - 100)
    cols = next(iter(results.values()))["weights"].columns
    out = pd.DataFrame(0.0, index=index, columns=cols)
    log = []
    for wi, end in enumerate(week_ends[:-1]):
        scores = {}
        for k, w in weekly.items():
            f = forecast.estimate(list(w.iloc[: wi + 1]))
            if f:
                scores[k] = (f["med"], f["up"]) if pick == "med" else (f["up"], f["med"])
        if not scores:
            continue
        best = max(scores, key=lambda k: scores[k])
        nxt_start, nxt_end = week_ends[wi] + pd.Timedelta(days=1), week_ends[wi + 1]
        days = index[(index >= nxt_start) & (index <= nxt_end)]
        # 下一週每天收盤後的決定＝跟隨策略同一天的決定；週五收盤後就先換成新策略的持股
        out.loc[days] = results[best]["weights"].loc[days].to_numpy()
        out.loc[end] = results[best]["weights"].loc[end].to_numpy()
        log.append(dict(week=str(days[0].date()) if len(days) else str(nxt_start.date()), pick=results[best]["label"], key=best,
                        score=round(scores[best][0], 2)))
    # 最新一週（還沒有下一週）：以最新展望決定下週要跟誰
    scores = {}
    for k, w in weekly.items():
        f = forecast.estimate(list(w))
        if f:
            scores[k] = (f["med"], f["up"]) if pick == "med" else (f["up"], f["med"])
    if scores:
        best = max(scores, key=lambda k: scores[k])
        last_end = week_ends[-1]
        if last_end.dayofweek == 4:  # 這週已結束（週五收盤）才換策略
            out.loc[last_end] = results[best]["weights"].loc[last_end].to_numpy()
        log.append(dict(week="下週", pick=results[best]["label"], key=best, score=round(scores[best][0], 2)))
    return out, log
