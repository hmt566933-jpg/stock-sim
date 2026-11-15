"""下週展望與準確度。

展望＝用「過去 N 週」的週報酬分布，估計下一週：上漲機率、中位數、常見範圍（10%～90%）。
準確度＝對過去每一週，都只用「那一週之前」的資料做同樣的估計，再跟那週的實際結果比對，
所以不會偷看未來，跟每週即時記錄的結果相同。
"""
from __future__ import annotations

import numpy as np

LOOKBACK = 52     # 用過去幾週估計
MIN_WEEKS = 26    # 至少要有幾週資料才估計
HISTORY = 26      # 準確度統計看最近幾週


def estimate(rets: list[float]) -> dict | None:
    a = np.asarray(rets[-LOOKBACK:], dtype=float)
    if len(a) < MIN_WEEKS:
        return None
    return dict(up=round(float((a > 0).mean() * 100), 1), med=round(float(np.median(a)), 2),
                lo=round(float(np.percentile(a, 10)), 2), hi=round(float(np.percentile(a, 90)), 2))


def predicted_up(f: dict) -> bool:
    return f["up"] > 50 or (f["up"] == 50 and f["med"] > 0)


def build(rets: list[float], weeks_done: list[bool]) -> dict:
    """rets：全部週報酬（%），weeks_done：該週是否已結束。"""
    done = [r for r, d in zip(rets, weeks_done) if d]
    history = []
    for i in range(max(0, len(done) - HISTORY), len(done)):
        f = estimate(done[:i])
        if f:
            actual = done[i]
            history.append(dict(**f, actual=actual, hit=predicted_up(f) == (actual > 0),
                                inside=f["lo"] <= actual <= f["hi"]))
    n = len(history)
    summary = dict(
        weeks=n,
        hit_rate=round(sum(h["hit"] for h in history) / n * 100, 1) if n else None,
        inside_rate=round(sum(h["inside"] for h in history) / n * 100, 1) if n else None,
        mae=round(float(np.mean([abs(h["actual"] - h["med"]) for h in history])), 2) if n else None,
    )
    return dict(next=estimate(done), history=history, accuracy=summary)
