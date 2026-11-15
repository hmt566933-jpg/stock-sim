"""每日執行入口。

用法：
  python run.py --market tw          # 下載最新台股資料並重跑全部策略
  python run.py --market us          # 美股
  python run.py --market all         # 兩個都跑
  python run.py --market tw --offline  # 不下載，用 data/ 裡的快取
結果寫到 docs/data/<market>.json，並重新產生 docs/index.html（儀表板）。
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd

from simtrade import analysis, data, engine, follow, forecast, metrics, regime, strategies, universe
from simtrade.broker import Costs

ROOT = Path(__file__).resolve().parent
DOCS = ROOT / "docs"
TPE = timezone(timedelta(hours=8))
TRADE_WEEKS = 12


def run_market(key: str, cfg: dict, offline: bool = False, long: pd.DataFrame | None = None,
               names: dict | None = None) -> dict:
    m = cfg["markets"][key]
    if names is None:
        names = (universe.cached(key) if offline else
                 universe.get(key, m["universe"], extra=[m["benchmark"]]))
    if long is None:
        long = data.load(key) if offline else data.update(key, sorted(names), m["start_date"])
    long = long[long["date"] >= m["start_date"]]
    if names:
        long = long[long["symbol"].isin(names)]
    wide = data.to_wide(long)
    elig = strategies.eligibility(wide, m["universe"].get("min_turnover", 0))
    costs = Costs(etf_prefix=m.get("etfs_prefix"), **m["costs"])
    k = cfg.get("max_positions", 10)
    defaults = {"max_positions": k, **cfg.get("exits", {})}
    mf = cfg.get("market_filter")
    if mf:
        defaults["market_filter"] = {"symbol": m["benchmark"], **mf}

    strats = [("benchmark", strategies.Benchmark(m["benchmark"], label=f"大盤 {m['benchmark']}"))]
    strats += [(name, strategies.build(name, scfg, defaults)) for name, scfg in cfg["strategies"].items()]
    print(f"[{m['name']}] 股票池 {wide['close'].shape[1]} 檔，最新一天可交易 {int(elig.iloc[-1].sum())} 檔")

    reg_state, reg_info = regime.summary(wide["close"][m["benchmark"]], m.get("regime_ma", 200))

    execution = cfg.get("execution", "next_open")

    def package(name, strat, r):
        eq = r["equity"]
        all_weeks, all_rows = weekly(eq, m["initial_cash"], n=10_000)
        fc = forecast.build([x["ret"] for x in all_rows], [w["done"] for w in all_weeks])
        weeks_, wrows = all_weeks[-52:], all_rows[-52:]
        # 保留最近 12 週的交易明細（再多會讓網頁太大、手機開很慢）
        since = weeks_[-TRADE_WEEKS]["start"] if len(weeks_) >= TRADE_WEEKS else (weeks_[0]["start"] if weeks_ else "")
        week_trades = [t for t in r["trades"] if weeks_ and t["date"] >= since]
        return weeks_, dict(
            weekly=wrows, week_trades=week_trades[::-1], forecast=fc,
            key=name, label=strat.label, stats=metrics.summarize(eq, r["trades"]),
            regime=regime.split_returns(eq, reg_state),
            equity=[round(v / m["initial_cash"] * 100, 2) for v in eq],  # 起始 = 100
            positions=r["positions"], cash=r["cash"],
            pending=r["pending"] if execution == "next_open" else [],
            recent_trades=r["trades"][-15:][::-1],
        )

    results, raw = [], {}
    for name, strat in strats:
        r = engine.run(strat, wide, elig, m["initial_cash"], costs, execution=execution,
                       band=cfg.get("rebalance_band", 0.02))
        raw[name] = dict(label=strat.label, equity=r["equity"], weights=r["weights"])
        weeks, pkg = package(name, strat, r)
        results.append(pkg)

    # 展望跟單：每週跟隨「下週展望」最好的策略
    follow_log = []
    fcfg = cfg.get("outlook_follow")
    if fcfg:
        fw, follow_log = follow.build_weights(raw, wide["close"].index, fcfg.get("pick", "med"),
                                              exclude=tuple(fcfg.get("exclude", [])))
        params = {k: v for k, v in fcfg.items() if k in ("stop_loss", "take_profit", "entry_dip")}
        fs = strategies.Fixed(fw, label=fcfg.get("label", "展望跟單"), **params)
        r = engine.run(fs, wide, elig, m["initial_cash"], costs, execution=execution)
        weeks, pkg = package("follow", fs, r)
        pkg["follow_log"] = follow_log[-12:]
        results.append(pkg)

    out = dict(
        market=key, name=m["name"], currency=m["currency"], initial_cash=m["initial_cash"],
        start=str(wide["close"].index[0].date()), last_date=str(wide["close"].index[-1].date()),
        updated_at=datetime.now(TPE).strftime("%Y-%m-%d %H:%M"),
        dates=[str(d.date()) for d in wide["close"].index],
        universe_size=int(wide["close"].shape[1]), eligible_today=int(elig.iloc[-1].sum()),
        regime=reg_info,
        # 我的模擬倉用：全部股票的最新收盤價與名稱、交易成本設定
        quotes=_quotes(wide["close"], names),
        costs=dict(m["costs"], etf_prefix=m.get("etfs_prefix")),
        follow_cfg=cfg.get("outlook_follow"),
        market_filter=_filter_state(wide["close"], m["benchmark"], mf),
        weeks=weeks, max_positions=k, exits=cfg.get("exits", {}), strategies=results,
        names={s: names.get(s, "") for s in _used_symbols(results)},
    )
    out["ai_brief"] = analysis.facts(out)
    out["analysis"] = analysis.generate(out, out["ai_brief"])
    (DOCS / "data").mkdir(parents=True, exist_ok=True)
    (DOCS / "data" / f"{key}.json").write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    best = max(results, key=lambda x: x["stats"]["total_return"])
    print(f"[{m['name']}] 資料到 {out['last_date']}，最佳：{best['label']} {best['stats']['total_return']}%")
    return out


def weekly(eq: pd.Series, initial: float, n: int = 52) -> tuple[list[dict], list[dict]]:
    """把每日淨值切成「週一到週五」，算每週損益金額與報酬率（最近 n 週）。"""
    groups = eq.groupby(eq.index.to_period("W-FRI"))
    weeks, rows, prev = [], [], initial
    for _, g in groups:
        last = float(g.iloc[-1])
        weeks.append(dict(start=str(g.index[0].date()), end=str(g.index[-1].date()), done=True))
        rows.append(dict(pnl=round(last - prev, 2), ret=round((last / prev - 1) * 100, 2) if prev else 0.0))
        prev = last
    if weeks:  # 最後一週如果還沒到週五，標示為進行中
        weeks[-1]["done"] = eq.index[-1].dayofweek == 4
    return weeks[-n:], rows[-n:]


def _quotes(close: pd.DataFrame, names: dict) -> dict:
    """最近 10 個交易日內有成交的股票：{代號: [名稱, 最新收盤價]}。"""
    recent = close.iloc[-10:].ffill().iloc[-1]
    return {s: [names.get(s, "") if names.get(s, "") != s else "", round(float(p), 2)]
            for s, p in recent.items() if pd.notna(p)}


def _filter_state(close: pd.DataFrame, bench: str, mf: dict | None) -> dict | None:
    """大盤濾網目前狀態（給儀表板與白話分析）。"""
    if not mf or bench not in close.columns:
        return None
    b = close[bench].ffill()
    ma = b.rolling(mf.get("ma", 20)).mean()
    return dict(ma=mf.get("ma", 20), price=round(float(b.iloc[-1]), 2), ma_value=round(float(ma.iloc[-1]), 2),
                on=bool(b.iloc[-1] > ma.iloc[-1]))


def _used_symbols(results: list[dict]) -> set[str]:
    used = set()
    for r in results:
        used |= {p["symbol"] for p in r["positions"]}
        used |= {p["symbol"] for p in r["pending"]}
        used |= {t["symbol"] for t in r["recent_trades"]}
        used |= {t["symbol"] for t in r.get("week_trades", [])}
    return used


def _previous_payload() -> dict:
    """沿用上一版儀表板裡的資料，避免某個市場這次沒跑就從頁面消失。"""
    try:
        html = (DOCS / "index.html").read_text(encoding="utf-8")
        start = html.index("const DATA = ") + len("const DATA = ")
        data, _ = json.JSONDecoder().raw_decode(html[start:])
        return data or {}
    except Exception:
        return {}


def build_dashboard() -> None:
    payload = _previous_payload()
    for f in sorted((DOCS / "data").glob("*.json")):
        payload[f.stem] = json.loads(f.read_text(encoding="utf-8"))
    template = (ROOT / "simtrade" / "dashboard.html").read_text(encoding="utf-8")
    html = template.replace("/*__DATA__*/null", json.dumps(payload, ensure_ascii=False))
    (DOCS / "index.html").write_text(html, encoding="utf-8")
    (DOCS / ".nojekyll").touch()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", default="all", choices=["tw", "us", "all"])
    ap.add_argument("--offline", action="store_true")
    args = ap.parse_args()
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    keys = list(cfg["markets"]) if args.market == "all" else [args.market]
    for k in keys:
        run_market(k, cfg, offline=args.offline)
    build_dashboard()


if __name__ == "__main__":
    main()
