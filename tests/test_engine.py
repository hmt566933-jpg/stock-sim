"""離線測試：用隨機產生的假股價跑整套流程（不需要網路）。
執行：python tests/test_engine.py
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from simtrade import engine, strategies  # noqa: E402
from simtrade.broker import Account, Costs  # noqa: E402


def fake_long(symbols, start="2020-01-01", days=1200, seed=1):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(start, periods=days)
    rows = []
    for k, s in enumerate(symbols):
        r = rng.normal(0.0004 * ((k % 7) - 2) / 3, 0.02, days)
        close = 100 * np.exp(np.cumsum(r))
        open_ = close * np.exp(rng.normal(0, 0.005, days))
        vol = (k % 5) * 3e5 * np.exp(rng.normal(0, 0.5, days))  # 第 k % 5 == 0 檔成交量為 0
        hi = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.005, days)))
        lo = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.005, days)))
        for row in zip(dates, [s] * days, open_, hi, lo, close, vol):
            rows.append(row)
    return pd.DataFrame(rows, columns=["date", "symbol", "open", "high", "low", "close", "volume"])


def test_costs():
    c = Costs(fee_rate=0.001425, fee_discount=0.6, min_fee=20, etfs=("0050.TW",))
    assert c.fee(10_000) == 20                      # 低於最低手續費
    assert abs(c.fee(1_000_000) - 855) < 1e-6       # 1,000,000 × 0.1425% × 6 折
    assert abs(c.tax("2330.TW", 100_000) - 300) < 1e-6
    assert abs(c.tax("0050.TW", 100_000) - 100) < 1e-6


def test_select_keeps_holdings():
    idx = pd.bdate_range("2024-01-01", periods=3)
    score = pd.DataFrame([[3, 2, 1], [1, 2, 3], [np.nan, np.nan, 9]], index=idx, columns=list("ABC"), dtype=float)
    keep = pd.DataFrame([[1, 1, 1], [1, 1, 1], [0, 1, 1]], index=idx, columns=list("ABC")).astype(bool)
    w = strategies.select(score, keep, k=2)
    assert list(w.iloc[0] > 0) == [True, True, False]   # 挑分數最高的 A、B
    assert list(w.iloc[1] > 0) == [True, True, False]   # 名次變了，但還沒出場就繼續抱
    assert list(w.iloc[2] > 0) == [False, True, True]   # A 出場，空位補 C
    assert abs(w.iloc[0].sum() - 1.0) < 1e-9


def test_time_exit_and_stop_loss():
    idx = pd.bdate_range("2024-01-01", periods=10)
    score = pd.DataFrame({"A": [1.0] * 10, "B": [np.nan] * 10}, index=idx)
    keep = pd.DataFrame(True, index=idx, columns=["A", "B"])
    w = strategies.select(score, keep, k=1, max_hold=5)
    held = (w["A"] > 0).tolist()
    assert held[:5] == [True] * 5 and held[5] is False   # 第 5 天後出場，且當天不立刻買回
    assert held[6] is True                                # 隔天條件仍成立可再進場
    close = pd.DataFrame({"A": [100, 99, 92, 95, 96, 97, 98, 99, 100, 101], "B": [1.0] * 10}, index=idx, dtype=float)
    w = strategies.select(score, keep, k=1, close=close, stop_loss=0.07)
    assert (w["A"] > 0).tolist()[:3] == [True, True, False]  # 跌 8% 觸發停損


def test_all_strategies_run():
    from simtrade.data import to_wide
    import json
    cfg = json.loads((Path(__file__).resolve().parent.parent / "config.json").read_text(encoding="utf-8"))
    syms = [f"S{i:03d}" for i in range(60)]
    wide = to_wide(fake_long(syms, days=400))
    wide["close"].iloc[300:303] *= 1.06  # 製造一次爆量長紅 / 突破
    wide["high"].iloc[300:303] = wide["close"].iloc[300:303]
    wide["volume"].iloc[300] *= 5
    elig = strategies.eligibility(wide, 0)
    defaults = {"max_positions": cfg["max_positions"], **cfg["exits"]}
    for name, scfg in cfg["strategies"].items():
        st = strategies.build(name, scfg, defaults)
        w = st.target_weights(wide, elig)
        assert (w.sum(axis=1) <= 1 + 1e-9).all(), name
        assert ((w > 0).sum(axis=1) <= st.k).all(), name
        assert (w > 0).to_numpy().any(), f"{name} 從來沒有進場"


def test_forecast_uses_only_past():
    from simtrade import forecast
    rets = [1.0] * 30 + [-5.0]
    out = forecast.build(rets, [True] * 31)
    last = out["history"][-1]
    assert last["med"] == 1.0 and last["actual"] == -5.0 and last["hit"] is False  # 預估時看不到 -5
    assert out["next"]["med"] == 1.0
    assert forecast.build([1.0] * 10, [True] * 10)["next"] is None             # 資料不足不估


def test_market_filter_blocks_new_entries():
    from simtrade.data import to_wide
    wide = to_wide(fake_long(["BENCH", "A", "B"], days=120))
    wide["close"]["BENCH"] = np.r_[np.linspace(100, 200, 60), np.linspace(200, 100, 60)]  # 先漲後跌
    elig = wide["close"].notna()
    st = strategies.build("breakout", {"type": "breakout", "lookback": 5, "min_vol_ratio": 0},
                          {"max_positions": 2, "market_filter": {"symbol": "BENCH", "ma": 20}})
    ok = st.market_ok(wide["close"])
    assert ok.iloc[40] and not ok.iloc[100]
    w = st.target_weights(wide, elig)
    new_buy = (w > 0) & (w.shift(1).fillna(0) == 0)
    assert not new_buy[~ok].to_numpy().any()          # 大盤弱勢的日子沒有新進場
    off = strategies.build("breakout", {"market_filter": False}, {"market_filter": {"symbol": "BENCH"}})
    assert off.market_filter is None


def test_ai_brief():
    import json
    import run as runner
    cfg = json.loads((runner.ROOT / "config.json").read_text(encoding="utf-8"))
    syms = ["0050.TW"] + [f"S{i:03d}" for i in range(80)]
    runner.DOCS = Path(__file__).resolve().parent / "_out"
    out = runner.run_market("tw", cfg, long=fake_long(syms, seed=3), names={s: s for s in syms})
    brief = out["ai_brief"]
    n = sum(runner._active(c, "tw") for c in cfg["strategies"].values()) + \
        sum(runner._active(c, "tw") for c in cfg.get("combos", {}).values())
    assert brief["market"] == "台股" and len(brief["strategies"]) == 2 + n
    assert out["market_filter"] is not None


def test_limit_entry():
    """限價進場：開盤沒跌到限價、盤中跌到 → 用限價成交；沒跌到 → 不成交。"""
    idx = pd.bdate_range("2024-01-01", periods=4)
    wide = {k: pd.DataFrame({"A": v}, index=idx, dtype=float) for k, v in {
        "open": [100, 100, 99, 97], "high": [101, 101, 100, 98], "low": [99, 99.5, 97.5, 96],
        "close": [100, 100, 99, 97], "volume": [1e6] * 4}.items()}
    class Always(strategies.Strategy):
        def target_weights(self, wide, eligible):
            return pd.DataFrame(1.0, index=wide["close"].index, columns=wide["close"].columns)
    st = Always(entry_dip=0.02)
    r = engine.run(st, wide, wide["close"].notna(), 10_000,
                   Costs(fee_rate=0, min_fee=0, sell_tax_stock=0, price_limit=None))
    buy = r["trades"][0]
    assert buy["date"] == "2024-01-03" and abs(buy["price"] - 98.0) < 1e-9   # 第 2 天沒跌到，第 3 天盤中到 98


def test_outlook_follow_picks_best():
    from simtrade import follow
    idx = pd.bdate_range("2023-01-02", periods=300)
    up = pd.Series(np.linspace(100, 200, 300), index=idx)
    flat = pd.Series(100.0, index=idx)
    wA = pd.DataFrame({"X": 0.5, "Y": 0.0}, index=idx)
    wB = pd.DataFrame({"X": 0.0, "Y": 0.5}, index=idx)
    w, log = follow.build_weights({"a": dict(label="A", equity=up, weights=wA),
                                   "b": dict(label="B", equity=flat, weights=wB)}, idx)
    assert log[-1]["key"] == "a" and w["X"].iloc[-10:].gt(0).all() and (w["Y"] == 0).all()


def test_resize_keeps_total_and_cap():
    idx = pd.bdate_range("2024-01-01", periods=3)
    w = pd.DataFrame([[0.1, 0.1, 0.1, 0], [0.1, 0.1, 0, 0], [0.1, 0, 0, 0]], index=idx, columns=list("ABCD"))
    sc = pd.DataFrame([[1, 1, 10, 5], [3, 1, 1, 1], [np.nan] * 4], index=idx, columns=list("ABCD"), dtype=float)
    r = strategies.resize(w, sc, 10)
    assert np.allclose(r.sum(axis=1), w.sum(axis=1))      # 總投入不變
    assert (r.max(axis=1) <= 0.2 + 1e-9).all()             # 單檔最多 2 倍
    assert r.loc[idx[0], "C"] == 0.2 and r.loc[idx[0], "D"] == 0  # 沒持有的不會被分到
    assert r.loc[idx[2], "A"] == 0.1                        # 沒分數時維持平均


def test_combo_switches_on_regime():
    import run as runner
    idx = pd.bdate_range("2024-01-01", periods=4)
    wa = pd.DataFrame({"X": 0.1, "Y": 0.0}, index=idx)
    wb = pd.DataFrame({"X": 0.0, "Y": 0.1}, index=idx)
    raw = {"a": dict(weights=wa), "b": dict(weights=wb)}
    bull = pd.Series([True, True, False, False], index=idx)
    w = runner._combo_weights(raw, {"bull": {"a": 1.0}, "bear": {"a": 0.5, "b": 0.5}}, bull)
    assert w.loc[idx[0], "X"] == 0.1 and w.loc[idx[0], "Y"] == 0
    assert w.loc[idx[3], "X"] == 0.05 and w.loc[idx[3], "Y"] == 0.05


def test_odd_lot_slippage():
    c = Costs(fee_rate=0, min_fee=0, sell_tax_stock=0, price_limit=None, lot_size=1000, odd_lot_slippage=0.003)
    assert c.fill_price("buy", 2000, 100) == 100                      # 整張：開盤價
    assert abs(c.fill_price("buy", 1500, 100) - (1000 * 100 + 500 * 100.3) / 1500) < 1e-9
    assert abs(c.fill_price("sell", 500, 100) - 99.7) < 1e-9          # 全部零股：賣便宜 0.3%
    a = Account(cash=100_000, costs=c)
    a.buy(pd.Timestamp("2024-01-02"), "X", 500, 100)
    assert abs(a.cash - (100_000 - 500 * 100.3)) < 1e-6


def test_round_trip_and_limits():
    c = Costs(fee_rate=0.001425, fee_discount=1, min_fee=20)
    a = Account(cash=100_000, costs=c)
    d = pd.Timestamp("2024-01-02")
    a.buy(d, "2330.TW", 100, 500)                   # 50,000 + 71.25 手續費
    assert a.shares["2330.TW"] == 100
    assert abs(a.cash - (100_000 - 50_000 - 71.25)) < 1e-6
    a.sell(d, "2330.TW", 100, 500, prev_close=560)  # 跌停附近 → 賣不掉
    assert a.shares["2330.TW"] == 100
    a.sell(d, "2330.TW", 100, 500, prev_close=500)
    assert "2330.TW" not in a.shares
    assert a.trades[-1]["pnl"] < 0                  # 平盤賣出，扣掉成本會小虧
    a.buy(d, "X", 10_000, 500)                      # 錢不夠 → 自動減量
    assert a.cash >= 0 and a.shares["X"] < 10_000


def test_no_lookahead():
    """next_open：第 t 天的訊號只能在 t+1 成交。"""
    from simtrade.data import to_wide
    wide = to_wide(fake_long(["A"], days=50))

    class FlipOnDay10(strategies.Strategy):
        def target_weights(self, wide, eligible):
            close = wide["close"]
            w = pd.DataFrame(0.0, index=close.index, columns=close.columns)
            w.iloc[10:] = 1.0
            return w

    elig = wide["close"].notna()
    r = engine.run(FlipOnDay10(), wide, elig, 100_000, Costs(fee_rate=0, min_fee=0, sell_tax_stock=0, price_limit=None))
    assert r["trades"][0]["date"] == str(wide["close"].index[11].date())


def test_full_pipeline(tmp=Path(__file__).resolve().parent / "_out"):
    import json
    import run as runner
    cfg = json.loads((runner.ROOT / "config.json").read_text(encoding="utf-8"))
    runner.DOCS = tmp  # 測試結果寫到 tests/_out，不碰正式的 docs/
    for key, m in cfg["markets"].items():
        syms = [m["benchmark"]] + [f"S{i:03d}" for i in range(150)]
        names = {s: f"假股{s}" for s in syms}
        out = runner.run_market(key, cfg, long=fake_long(syms, seed=len(key)), names=names)
        for s in out["strategies"]:
            if s["key"] != "benchmark":
                assert len(s["positions"]) <= cfg["max_positions"]
                # 成交量為 0 的股票（第 k 檔，k % 5 == 0）不能被買
                for t in s["recent_trades"]:
                    if t["side"] == "買" and t["symbol"].startswith("S"):
                        assert (int(t["symbol"][1:]) + 1) % 5 != 0, t
        n_active = sum(runner._active(c, key) for c in cfg["strategies"].values())
        n_combo = sum(runner._active(c, key) for c in cfg.get("combos", {}).values())
        assert len(out["strategies"]) == 2 + n_active + n_combo  # 大盤 + 策略 + 組合 + 展望跟單
        assert all(s["forecast"]["accuracy"]["weeks"] > 0 for s in out["strategies"])
        for s in out["strategies"]:
            assert s["equity"][0] == 100 and s["cash"] >= 0
            assert len(s["equity"]) == len(out["dates"])
    runner.build_dashboard()
    html = (tmp / "index.html").read_text(encoding="utf-8")
    assert "/*__DATA__*/null" not in html


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("通過", name)
