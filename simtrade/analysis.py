"""整理給外部 AI 分析用的重點數據（儀表板「複製數據給 AI」按鈕使用）。"""
from __future__ import annotations

def facts(out: dict) -> dict:
    """從結果中挑出重點數字。"""
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


# ───────────────────────── 外部 AI 分析（選用） ─────────────────────────
# 在 GitHub repo 的 Secrets 設定其中一個金鑰即可：
#   GEMINI_API_KEY → Google Gemini（模型預設 gemini-flash-latest，可用 GEMINI_MODEL 變數改）
#   XAI_API_KEY    → xAI Grok（模型預設 grok-4.7，可用 XAI_MODEL 改）
#   OPENAI_API_KEY → OpenAI（模型預設 chat-latest，可用 OPENAI_MODEL 改；
#                    設 OPENAI_BASE_URL 可接其他相容 OpenAI 格式的服務）
# 都沒設定時不產生分析。
import json
import os

import requests

PROMPT = """以下是台美股短線策略模擬倉（{market}）最新一次的回測與模擬數據（JSON）。
讀者有投資經驗，不需要解釋基本名詞。請用繁體中文寫一份精簡的分析（約 300 字）：
1. 本週表現：各策略相對大盤的強弱，值得注意的差異
2. 長期有效性：哪些策略在扣除交易成本後仍有優勢，哪些應考慮淘汰或調整參數（具體指出）
3. 下週觀察重點：大盤濾網狀態、部位變動，以及預估的可信度
格式：每段一行；「## 」開頭為小標題（最多 3 個）；條列用「- 」；不要表格或粗體。
只能使用資料中的數字，不要捏造。不要對個股給買賣建議。

資料：
{data}"""


def _gemini(prompt: str, key: str) -> tuple[str, str]:
    model = os.environ.get("GEMINI_MODEL") or "gemini-flash-latest"
    r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                      headers={"x-goog-api-key": key, "content-type": "application/json"},
                      json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=120)
    r.raise_for_status()
    parts = r.json()["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in parts).strip(), model


def _chat(prompt: str, key: str, base: str, model: str) -> tuple[str, str]:
    """OpenAI 相容格式（OpenAI、Grok、DeepSeek…都適用）。"""
    base = base.rstrip("/")
    r = requests.post(f"{base}/chat/completions", timeout=120,
                      headers={"authorization": f"Bearer {key}", "content-type": "application/json"},
                      json={"model": model, "messages": [{"role": "user", "content": prompt}]})
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip(), model


def _grok(prompt: str, key: str) -> tuple[str, str]:
    return _chat(prompt, key, os.environ.get("XAI_BASE_URL") or "https://api.x.ai/v1",
                 os.environ.get("XAI_MODEL") or "grok-4.7")


def _openai(prompt: str, key: str) -> tuple[str, str]:
    return _chat(prompt, key, os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1",
                 os.environ.get("OPENAI_MODEL") or "chat-latest")


def generate(out: dict, brief: dict) -> dict | None:
    providers = [("Grok", "XAI_API_KEY", _grok), ("Gemini", "GEMINI_API_KEY", _gemini),
                 ("OpenAI", "OPENAI_API_KEY", _openai)]
    prompt = PROMPT.format(market=out["name"], data=json.dumps(brief, ensure_ascii=False))
    for name, env, fn in providers:
        key = os.environ.get(env, "").strip()
        if not key:
            continue
        try:
            text, model = fn(prompt, key)
            if text:
                return dict(provider=name, model=model, text=text)
        except Exception as exc:
            print(f"[警告] {name} 分析失敗：{exc}")
    return None
