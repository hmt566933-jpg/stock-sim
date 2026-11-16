"""策略層：每個策略只負責「看完今天收盤後，明天想持有哪些股票、各多少比例」。

共同規則（短線版，持有期 ≤ 1 週）：
- 每天掃描整個股票池，只考慮「可交易」(eligible) 的股票（流動性篩選）
- 符合進場條件的股票依強度排名，最多持有 max_positions 檔，每檔 1/max_positions 資金
- 出場：策略自己的出場條件、持有滿 max_hold_days 天、或跌破停損（以收盤價判斷，隔天開盤賣）
- 第 t 列 = 第 t 天收盤後的決定；只能用到當天以前的資料，成交由引擎在 t+1 處理

新增策略：繼承 Strategy、實作 target_weights，加到 REGISTRY 和 config.json。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


# ───────────────────────── 通用選股器 ─────────────────────────

def select(entry_score: pd.DataFrame, keep: pd.DataFrame, k: int, close: pd.DataFrame | None = None,
           max_hold: int | None = None, stop_loss: float | None = None,
           take_profit: float | None = None) -> pd.DataFrame:
    """entry_score：可進場的股票給分數（越大越優先），不可進場為 NaN
    keep：已持有的股票是否繼續持有（False = 出場）
    max_hold / stop_loss / take_profit：時間出場、停損、停利（以進場當天收盤價為成本估算）
    """
    score = entry_score.to_numpy(dtype=float)
    keep_a = keep.reindex_like(entry_score).fillna(False).to_numpy(dtype=bool)
    px = close.reindex_like(entry_score).to_numpy(dtype=float) if close is not None else None
    w = np.zeros_like(score)
    held: dict[int, tuple[int, float]] = {}  # 欄位 -> (進場日 index, 進場價)
    for i in range(score.shape[0]):
        exited = set()
        for j, (i0, p0) in list(held.items()):
            ok = keep_a[i, j]
            if max_hold and i - i0 >= max_hold:
                ok = False
            if px is not None and p0 > 0 and not np.isnan(px[i, j]):
                r = px[i, j] / p0 - 1
                if stop_loss and r <= -stop_loss:
                    ok = False
                if take_profit and r >= take_profit:
                    ok = False
            if not ok:
                del held[j]
                exited.add(j)  # 今天剛出場的不立刻買回
        slots = k - len(held)
        if slots > 0:
            row = score[i]
            cand = np.where(~np.isnan(row))[0]
            if cand.size:
                cand = cand[np.argsort(-row[cand], kind="stable")]
                for j in cand:
                    if j in held or j in exited:
                        continue
                    held[j] = (i, px[i, j] if px is not None else np.nan)
                    slots -= 1
                    if slots == 0:
                        break
        if held:
            w[i, list(held)] = 1.0 / k
    return pd.DataFrame(w, index=entry_score.index, columns=entry_score.columns)


# ───────────────────────── 資金分配 ─────────────────────────

def resize(w: pd.DataFrame, score: pd.DataFrame, k: int, cap: float = 2.0) -> pd.DataFrame:
    """把每天「持有的股票」按 score 比例重新分配資金（總投入比例不變）。
    單檔最多 cap / k（預設 2 倍平均），超出的部分分給其他檔。score 缺值時用平均。"""
    held = w > 0
    total = w.sum(axis=1)
    sc = score.reindex_like(w).where(held)
    sc = sc.where(sc > 0)
    sc = sc.fillna(sc.mean(axis=1).to_frame().to_numpy() * held.astype(float)).where(held)
    out = sc.div(sc.sum(axis=1), axis=0).mul(total, axis=0).fillna(0.0)
    hi = cap / k
    for _ in range(3):  # 封頂後把多出來的資金按比例分給沒封頂的
        over = (out - hi).clip(lower=0)
        if over.to_numpy().sum() < 1e-12:
            break
        out = out.clip(upper=hi)
        room = (out < hi - 1e-12) & held
        share = out.where(room, 0)
        out = out + share.div(share.sum(axis=1).replace(0, np.nan), axis=0).mul(over.sum(axis=1), axis=0).fillna(0)
    out = out.where(held, 0.0)
    no_score = ~(sc.sum(axis=1) > 0)          # 整列都沒有分數時維持平均分配
    out.loc[no_score] = w.loc[no_score]
    return out


def inv_vol(close: pd.DataFrame, n: int = 20) -> pd.DataFrame:
    """波動度倒數：近 n 日日報酬標準差越小，分數越高。"""
    return 1 / close.pct_change().rolling(n, min_periods=n).std().replace(0, np.nan)


# ───────────────────────── 指標 ─────────────────────────

def sma(df, n):
    return df.rolling(n, min_periods=n).mean()


def rsi(close: pd.DataFrame, period: int) -> pd.DataFrame:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = gain / loss.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.where(loss != 0, 100.0).where(gain.notna())


def kd(high, low, close, n=9):
    """台灣常用 KD(9,3,3)。"""
    lo, hi = low.rolling(n, min_periods=n).min(), high.rolling(n, min_periods=n).max()
    rsv = ((close - lo) / (hi - lo).replace(0, np.nan) * 100).clip(0, 100)
    k = rsv.ewm(alpha=1 / 3, adjust=False).mean()
    d = k.ewm(alpha=1 / 3, adjust=False).mean()
    return k, d


def vol_ratio(volume, n=20):
    """今天成交量 / 前 n 日平均量。"""
    return volume / volume.shift(1).rolling(n, min_periods=n).mean()


# ───────────────────────── 策略 ─────────────────────────

class Strategy:
    # rebalance=False：只在「進場 / 出場」時交易，不會為了比例偏移而調整
    rebalance = False

    def __init__(self, label: str = "", max_positions: int = 10, max_hold_days: int | None = None,
                 stop_loss: float | None = None, take_profit: float | None = None,
                 market_filter: dict | None = None, entry_dip: float | None = None,
                 sizing: str = "equal", **params):
        self.label = label or self.__class__.__name__
        self.k = max_positions
        self.max_hold, self.stop_loss, self.take_profit = max_hold_days, stop_loss, take_profit
        self.market_filter = market_filter  # {"symbol": "0050.TW", "ma": 20} 或 None（不使用）
        self.entry_dip = entry_dip          # 限價進場：比前一天收盤低多少才買（0.02 = 2%），None = 開盤直接買
        self.sizing = sizing                # 資金分配："equal" 平均、"vol" 依波動度、"signal" 依訊號強弱
        self.params = params

    def p(self, name, default):
        return self.params.get(name, default)

    def market_ok(self, close: pd.DataFrame) -> pd.Series:
        """大盤濾網：大盤收盤在 N 日均線之上才允許開新倉。沒設定時永遠允許。"""
        f = self.market_filter
        if not f or f.get("symbol") not in close.columns:
            return pd.Series(True, index=close.index)
        b = close[f["symbol"]].ffill()
        return (b > sma(b, f.get("ma", 20))).fillna(False)

    def pick(self, entry, keep, close):
        entry = entry.where(self.market_ok(close), axis=0)  # 大盤弱勢時不開新倉（已持有的照原規則出場）
        w = select(entry, keep, self.k, close, self.max_hold, self.stop_loss, self.take_profit)
        if self.sizing == "vol":
            w = resize(w, inv_vol(close), self.k)
        elif self.sizing == "signal":
            # 進場當天在所有候選股中的排名（越強越高），持有期間沿用進場時的分數
            w = resize(w, entry.rank(axis=1, pct=True).ffill(), self.k)
        return w

    def target_weights(self, wide: dict, eligible: pd.DataFrame) -> pd.DataFrame:
        raise NotImplementedError


class Benchmark(Strategy):
    """100% 持有大盤指標（0050 / SPY）。"""

    def __init__(self, symbol: str, **kw):
        super().__init__(**kw)
        self.symbol = symbol

    def target_weights(self, wide, eligible):
        close = wide["close"]
        w = pd.DataFrame(0.0, index=close.index, columns=close.columns)
        if self.symbol in w.columns:
            w[self.symbol] = close[self.symbol].notna().astype(float)
        return w


class MaCross(Strategy):
    """短均線剛向上穿越長均線（黃金交叉）就買，量增越多越優先；短均線跌回長均線下就賣。"""

    def target_weights(self, wide, eligible):
        c = wide["close"]
        f, s = sma(c, self.p("fast", 5)), sma(c, self.p("slow", 20))
        cross = (f > s) & (f.shift(1) <= s.shift(1))
        entry = vol_ratio(wide["volume"]).where(cross & eligible)
        return self.pick(entry, f > s, c)


class Rsi2(Strategy):
    """Connors RSI(2)：長期多頭（收盤 > 200 日線）中，RSI(2) 極度超賣就買；收盤站回 5 日線就賣。"""

    def target_weights(self, wide, eligible):
        c = wide["close"]
        r = rsi(c, self.p("period", 2))
        trend = c > sma(c, self.p("trend", 200))
        entry = (-r).where((r < self.p("buy_below", 10)) & trend & eligible)
        keep = ~(c > sma(c, self.p("exit_ma", 5)))
        return self.pick(entry, keep, c)


class Breakout(Strategy):
    """收盤創 N 日新高且成交量放大就買，量越大越優先；靠持有天數 / 停損出場。"""

    def target_weights(self, wide, eligible):
        c = wide["close"]
        n = self.p("lookback", 20)
        vr = vol_ratio(wide["volume"])
        brk = (c > c.shift(1).rolling(n, min_periods=n).max()) & (vr > self.p("min_vol_ratio", 1.5))
        entry = vr.where(brk & eligible)
        return self.pick(entry, c.notna(), c)


class Bollinger(Strategy):
    """收盤跌破布林通道下軌就買（跌越深越優先）；回到中軌（20 日線）就賣。"""

    def target_weights(self, wide, eligible):
        c = wide["close"]
        n, m = self.p("period", 20), self.p("width", 2.0)
        mid = sma(c, n)
        lower = mid - m * c.rolling(n, min_periods=n).std()
        entry = ((lower - c) / c).where((c < lower) & eligible)
        return self.pick(entry, ~(c >= mid), c)


class VolumeSurge(Strategy):
    """爆量長紅：單日漲幅 ≥ X%、成交量 ≥ 前 20 日均量 N 倍、收在當日高點附近；量越大越優先。"""

    def target_weights(self, wide, eligible):
        c, h = wide["close"], wide["high"]
        vr = vol_ratio(wide["volume"])
        cond = ((c / c.shift(1) - 1) >= self.p("min_gain", 0.04)) \
            & (vr >= self.p("min_vol_ratio", 2.0)) & (c >= h * (1 - self.p("near_high", 0.01)))
        entry = vr.where(cond & eligible)
        return self.pick(entry, c.notna(), c)


class KdCross(Strategy):
    """KD 低檔黃金交叉：K 向上穿越 D 且 K < 低檔門檻就買（K 越低越優先）；K > 高檔門檻就賣。"""

    def target_weights(self, wide, eligible):
        c = wide["close"]
        k, d = kd(wide["high"], wide["low"], c, self.p("period", 9))
        cross = (k > d) & (k.shift(1) <= d.shift(1)) & (k < self.p("low", 30))
        entry = (-k).where(cross & eligible)
        return self.pick(entry, ~(k > self.p("high", 80)), c)


class PullbackRebound(Strategy):
    """多頭回檔：收盤在 60 日線上、但連續 N 天下跌就買（跌越多越優先）；第一根收紅就賣。"""

    def target_weights(self, wide, eligible):
        c = wide["close"]
        n = self.p("down_days", 3)
        down = (c < c.shift(1)).astype(float).rolling(n, min_periods=n).sum() == n
        trend = c > sma(c, self.p("trend", 60))
        drop = 1 - c / c.shift(n)
        entry = drop.where(down & trend & eligible)
        keep = ~(c > c.shift(1))
        return self.pick(entry, keep, c)


class Momentum(Strategy):
    """每週（或每月）最後一個交易日，挑過去 N 天漲最多的前 K 檔（報酬需 > 0），持有到下次換股。"""

    rebalance = True

    def target_weights(self, wide, eligible):
        c = wide["close"]
        ret = (c / c.shift(self.p("lookback", 20)) - 1).where(eligible)
        ok = self.market_ok(c)
        period = pd.Series(c.index.to_period(self.p("freq", "W")), index=c.index)
        last_day = (period != period.shift(-1)).to_numpy()
        w = pd.DataFrame(np.nan, index=c.index, columns=c.columns)
        for dt in c.index[last_day]:
            r = ret.loc[dt].dropna()
            row = pd.Series(0.0, index=c.columns)
            if ok.loc[dt]:  # 大盤弱勢時整週空手
                picks = r[r > 0].nlargest(self.k).index
                row[picks] = 1.0 / self.k
            w.loc[dt] = row
        w = w.ffill().fillna(0.0)
        if self.sizing == "signal":   # 漲越多分越多
            w = resize(w, (c / c.shift(self.p("lookback", 20)) - 1), self.k)
        elif self.sizing == "vol":    # 波動越小分越多
            w = resize(w, inv_vol(c), self.k)
        return w


class Fixed(Strategy):
    """直接使用外部算好的權重（展望跟單用）。"""

    engine_exits = True  # 停利停損由引擎處理

    def __init__(self, weights: pd.DataFrame, **kw):
        super().__init__(**kw)
        self.weights = weights

    def target_weights(self, wide, eligible):
        return self.weights.reindex_like(wide["close"]).fillna(0.0)


REGISTRY = {
    "ma_cross": MaCross,
    "rsi2": Rsi2,
    "breakout": Breakout,
    "bollinger": Bollinger,
    "volume_surge": VolumeSurge,
    "kd_cross": KdCross,
    "pullback": PullbackRebound,
    "momentum": Momentum,
}


def build(name: str, cfg: dict, defaults: dict) -> Strategy:
    """defaults：config 裡的共同設定（max_positions、exits）；個別策略可覆寫。"""
    params = {**defaults, **{k: v for k, v in cfg.items() if k not in ("label", "type", "markets")}}
    if params.get("market_filter") is False:  # 個別策略可用 "market_filter": false 關掉濾網
        params["market_filter"] = None
    cls = REGISTRY[cfg.get("type", name)]
    return cls(label=cfg.get("label", name), **params)


def eligibility(wide: dict[str, pd.DataFrame], min_turnover: float, window: int = 20,
                min_history: int = 60) -> pd.DataFrame:
    """可交易條件：近 window 日平均成交金額 ≥ 門檻，且至少有 min_history 天資料。"""
    close, vol = wide["close"], wide["volume"]
    turnover = (close * vol).rolling(window, min_periods=window).mean()
    history = close.notna().cumsum() >= min_history
    return (turnover >= min_turnover) & history & close.notna()
