"""白話分析：把當天的模擬結果整理成一般人看得懂的文字。

- 有設定 ANTHROPIC_API_KEY（GitHub → Settings → Secrets）時，請 Claude 寫分析
- 沒有設定或呼叫失敗時，用內建規則自動產生（免費、每天都有）
輸出格式：每行一段；"## " 開頭是小標題，"- " 開頭是條列。
"""
from __future__ import annotations

import json
import os

import requests

API_URL = "https://api.anthropic.com/v1/messages"
MODEL = os.environ.get("ANALYSIS_MODEL", "claude-sonnet-5-5")


def _money(v: float, cur: str) -> str:
    sign = "+" if v > 0 else "−" if v < 0 else ""
    return f"{sign}{abs(round(v)):,} {cur}"


def facts(out: dict) -> dict:
    """從結果中挑出分析要用的重點數字（也是給 AI 的資料）。"""
    cur = out["currency"]
    strategies = out["strategies"]
    bench = next((s for s in strategies if s["key"] == "benchmark"), None)
    weeks = out.get("weeks") or []
    wi = max((i for i, w in enumerate(weeks) if w["done"]), default=len(weeks) - 1)
    rows = []
    for s in strategies:
        st, wk = s["stats"], s["weekly"][wi] if weeks else None
        last12 = [w["ret"] for w in s["weekly"][-12:]]
        f = s.get("forecast") or {}
        rows.append(dict(
            name=s["label"], is_benchmark=s["key"] == "benchmark",
            week_ret=wk["ret"] if wk else None, week_pnl=wk["pnl"] if wk else None,
            total_ret=st["total_return"], max_drawdown=st["max_drawdown"], trading_cost=st["fees"],
            avg_week_12=round(sum(last12) / len(last12), 2) if last12 else None,
            win_weeks_12=sum(r > 0 for r in last12),
            next_up_prob=(f.get("next") or {}).get("up"), next_median=(f.get("next") or {}).get("med"),
            forecast_hit_rate=(f.get("accuracy") or {}).get("hit_rate"),
            monday_buys=sum(p["action"] == "買進" for p in s["pending"]),
            monday_sells=sum(p["action"] == "賣出" for p in s["pending"]),
        ))
    return dict(market=out["name"], currency=cur, data_until=out["last_date"],
                week=f'{weeks[wi]["start"]} ~ {weeks[wi]["end"]}' if weeks else None,
                initial_cash=out["initial_cash"], market_filter=out.get("market_filter"),
                benchmark=bench["label"] if bench else None, strategies=rows)


def rule_based(fx: dict) -> str:
    cur = fx["currency"]
    rows = [r for r in fx["strategies"] if r["week_ret"] is not None]
    strat = [r for r in rows if not r["is_benchmark"]]
    bench = next((r for r in rows if r["is_benchmark"]), None)
    if not strat:
        return "資料不足，還無法分析。"
    best = max(strat, key=lambda r: r["week_ret"])
    worst = min(strat, key=lambda r: r["week_ret"])
    lines = [f"## {fx['market']}：{fx['week']} 這週"]
    if bench:
        beat = [r for r in strat if r["week_ret"] > bench["week_ret"]]
        mood = "大漲" if bench["week_ret"] >= 3 else "上漲" if bench["week_ret"] > 0.5 else \
               "大跌" if bench["week_ret"] <= -3 else "下跌" if bench["week_ret"] < -0.5 else "差不多持平"
        bname = bench["name"].replace("大盤", "").strip()
        lines.append(f"大盤（{bname}）這週{mood}，{bench['week_ret']:+.2f}%。"
                     f"{len(strat)} 個策略裡有 {len(beat)} 個贏過大盤。")
    lines.append(f"表現最好的是「{best['name']}」{best['week_ret']:+.2f}%（{_money(best['week_pnl'], cur)}）；"
                 f"最差的是「{worst['name']}」{worst['week_ret']:+.2f}%（{_money(worst['week_pnl'], cur)}）。")

    lines.append("## 長期來看")
    lt = max(strat, key=lambda r: r["total_ret"])
    msg = f"從頭到現在累積最多的是「{lt['name']}」{lt['total_ret']:+.1f}%"
    if bench:
        n_beat = sum(r["total_ret"] > bench["total_ret"] for r in strat)
        msg += f"，大盤是 {bench['total_ret']:+.1f}%。沒有任何策略長期贏過直接買大盤。" if n_beat == 0 else \
            f"，大盤是 {bench['total_ret']:+.1f}%。只有 {n_beat} 個策略長期贏過大盤。" if n_beat <= len(strat) / 2 \
            else f"，大盤是 {bench['total_ret']:+.1f}%，有 {n_beat} 個策略長期贏過大盤。"
    lines.append(msg)
    heavy = [r for r in strat if r["trading_cost"] > fx["initial_cash"] * 0.5]
    if heavy:
        lines.append(f"- 注意：有 {len(heavy)} 個策略付出的手續費和稅超過本金的一半，"
                     "代表買賣太頻繁，賺的錢大多被成本吃掉了。")
    steady = max(strat, key=lambda r: (r["win_weeks_12"], r["avg_week_12"] or 0))
    lines.append(f"- 最近 12 週最穩定的是「{steady['name']}」，有 {steady['win_weeks_12']} 週是賺錢的。")

    lines.append("## 下週要注意")
    mf = fx.get("market_filter")
    if mf:
        if mf["on"]:
            lines.append(f"大盤目前在 {mf['ma']} 日均線之上（{mf['price']} > {mf['ma_value']}），"
                         "代表趨勢還算健康，策略可以照常找股票進場。")
        else:
            lines.append(f"大盤目前跌破 {mf['ma']} 日均線（{mf['price']} < {mf['ma_value']}），"
                         "代表市場偏弱，有開大盤濾網的策略下週不會買新股票，先保守觀望。")
    buys = sum(r["monday_buys"] for r in strat)
    sells = sum(r["monday_sells"] for r in strat)
    lines.append(f"週一開盤，所有策略合計預計買進 {buys} 筆、賣出 {sells} 筆。")
    cand = [r for r in strat if r["next_up_prob"] is not None]
    if cand:
        top = max(cand, key=lambda r: (r["next_up_prob"], r["next_median"]))
        hits = [r["forecast_hit_rate"] for r in cand if r["forecast_hit_rate"] is not None]
        avg_hit = sum(hits) / len(hits) if hits else None
        lines.append(f"照過去一年的紀錄，「{top['name']}」比較常賺錢（約 {top['next_up_prob']:.0f}% 的週是賺的）。"
                     + ("" if avg_hit is None else
                        f"不過這類預估過去猜對方向的比例平均只有 {avg_hit:.0f}%，跟丟銅板差不多，請當參考就好。"
                        if avg_hit < 60 else
                        f"這類預估過去猜對方向的比例平均約 {avg_hit:.0f}%，還算有參考價值，但一樣可能猜錯。"))
    lines.append("- 以上是程式模擬的結果，不是投資建議。")
    return "\n".join(lines)


PROMPT = """你是一位擅長把投資數據講給一般人聽的分析師。以下是一套「股票策略模擬倉」{market}的最新結果（JSON）。
請用繁體中文、口語白話、像朋友解釋一樣，寫一段 250～400 字的分析，讓完全不懂技術指標的人也看得懂：

1. 這週大盤和各策略表現如何？誰最好、誰最差，可能的原因（白話講，不要用術語；必要的術語用一句話解釋）
2. 長期來看哪些策略真的有效、哪些在虧錢，交易成本的影響
3. 下週要注意什麼（大盤濾網狀態、週一要買賣多少），並提醒預估準確度有限

格式規則：每段一行；用「## 」開頭當小標題（最多 3 個）；條列用「- 」開頭；不要用表格、粗體或其他 Markdown。
不要叫讀者買賣任何股票，最後一行寫「以上是程式模擬的結果，不是投資建議。」
數字只能用資料裡有的，不要自己編。

資料：
{data}"""


def ai(fx: dict, key: str) -> str:
    body = dict(model=MODEL, max_tokens=1200,
                messages=[dict(role="user", content=PROMPT.format(
                    market=fx["market"], data=json.dumps(fx, ensure_ascii=False)))])
    r = requests.post(API_URL, json=body, timeout=90, headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    r.raise_for_status()
    text = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text").strip()
    if not text:
        raise RuntimeError("AI 沒有回傳內容")
    return text


def generate(out: dict) -> dict:
    fx = facts(out)
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key:
        try:
            return dict(source="ai", model=MODEL, text=ai(fx, key))
        except Exception as exc:
            print(f"[警告] AI 分析失敗（{exc}），改用內建規則")
    return dict(source="rule", text=rule_based(fx))
