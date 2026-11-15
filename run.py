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

from simtrade import data, engine, metrics, strategies, universe
from simtrade.broker import Costs

ROOT = Path(__file__).resolve().parent
DOCS = ROOT / "docs"
TPE = timezone(timedelta(hours=8))


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

    strats = [("benchmark", strategies.Benchmark(m["benchmark"], label=f"大盤 {m['benchmark']}"))]
    strats += [(name, strategies.build(name, scfg, defaults)) for name, scfg in cfg["strategies"].items()]
    print(f"[{m['name']}] 股票池 {wide['close'].shape[1]} 檔，最新一天可交易 {int(elig.iloc[-1].sum())} 檔")

    results = []
    for name, strat in strats:
        r = engine.run(strat, wide, elig, m["initial_cash"], costs,
                       execution=cfg.get("execution", "next_open"),
                       band=cfg.get("rebalance_band", 0.02))
        eq = r["equity"]
        results.append(dict(
            key=name, label=strat.label, stats=metrics.summarize(eq, r["trades"]),
            equity=[round(v / m["initial_cash"] * 100, 2) for v in eq],  # 起始 = 100
            positions=r["positions"], cash=r["cash"],
            pending=r["pending"] if cfg.get("execution", "next_open") == "next_open" else [],
            recent_trades=r["trades"][-15:][::-1],
        ))

    out = dict(
        market=key, name=m["name"], currency=m["currency"], initial_cash=m["initial_cash"],
        start=str(wide["close"].index[0].date()), last_date=str(wide["close"].index[-1].date()),
        updated_at=datetime.now(TPE).strftime("%Y-%m-%d %H:%M"),
        dates=[str(d.date()) for d in wide["close"].index],
        universe_size=int(wide["close"].shape[1]), eligible_today=int(elig.iloc[-1].sum()),
        max_positions=k, exits=cfg.get("exits", {}), strategies=results,
        names={s: names.get(s, "") for s in _used_symbols(results)},
    )
    (DOCS / "data").mkdir(parents=True, exist_ok=True)
    (DOCS / "data" / f"{key}.json").write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    best = max(results, key=lambda x: x["stats"]["total_return"])
    print(f"[{m['name']}] 資料到 {out['last_date']}，最佳：{best['label']} {best['stats']['total_return']}%")
    return out


def _used_symbols(results: list[dict]) -> set[str]:
    used = set()
    for r in results:
        used |= {p["symbol"] for p in r["positions"]}
        used |= {p["symbol"] for p in r["pending"]}
        used |= {t["symbol"] for t in r["recent_trades"]}
    return used


def build_dashboard() -> None:
    payload = {}
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
