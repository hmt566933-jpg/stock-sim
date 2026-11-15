"""資料層：下載日K（yfinance，已還原權息）並快取在 data/。

全市場股票很多，所以：
- 平常只補抓最近幾天（增量）
- 每 7 天整批重抓一次，讓除權息後的還原價格保持正確
- 新加入股票池的代號會單獨抓完整歷史
"""
from __future__ import annotations

import json
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
COLS = ["date", "symbol", "open", "high", "low", "close", "volume"]
FULL_REFRESH_DAYS = 7
CHUNK = 100


def _has_parquet() -> bool:
    try:
        import pyarrow  # noqa: F401
        return True
    except ImportError:
        return False


def _path(market: str) -> Path:
    return DATA_DIR / (f"{market}_daily.parquet" if _has_parquet() else f"{market}_daily.csv.gz")


def _meta_path(market: str) -> Path:
    return DATA_DIR / f"{market}_meta.json"


def save(market: str, df: pd.DataFrame) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    p = _path(market)
    df.to_parquet(p, index=False) if p.suffix == ".parquet" else df.to_csv(p, index=False)


def load(market: str) -> pd.DataFrame:
    p = _path(market)
    if not p.exists():
        raise FileNotFoundError(p)
    df = pd.read_parquet(p) if p.suffix == ".parquet" else pd.read_csv(p, parse_dates=["date"])
    df["date"] = pd.to_datetime(df["date"])
    return df


def _download_chunk(symbols: list[str], start: str) -> pd.DataFrame:
    import yfinance as yf

    raw = yf.download(symbols, start=start, auto_adjust=True, progress=False,
                      group_by="ticker", threads=True)
    if raw is None or raw.empty:
        return pd.DataFrame(columns=COLS)
    if not isinstance(raw.columns, pd.MultiIndex):  # 只有一檔時欄位不是兩層
        raw = pd.concat({symbols[0]: raw}, axis=1)
    frames = []
    for sym in raw.columns.get_level_values(0).unique():
        df = raw[sym].dropna(how="all")
        if df.empty:
            continue
        df = df.reset_index()
        df.columns = [str(c).lower() for c in df.columns]
        df["symbol"] = sym
        frames.append(df[COLS])
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=COLS)


def download(symbols: list[str], start: str) -> pd.DataFrame:
    frames = []
    for i in range(0, len(symbols), CHUNK):
        part = symbols[i:i + CHUNK]
        for attempt in range(3):
            try:
                frames.append(_download_chunk(part, start))
                break
            except Exception as exc:
                print(f"[重試] 第 {i // CHUNK + 1} 批失敗（{exc}）")
                time.sleep(5 * (attempt + 1))
        time.sleep(1)  # 對資料來源客氣一點
        print(f"  已下載 {min(i + CHUNK, len(symbols))}/{len(symbols)}")
    out = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=COLS)
    if out.empty:
        raise RuntimeError("沒有下載到任何資料")
    out["date"] = pd.to_datetime(out["date"]).dt.tz_localize(None).dt.normalize()
    return out.dropna(subset=["open", "close"])


def update(market: str, symbols: list[str], start: str) -> pd.DataFrame:
    meta_p = _meta_path(market)
    meta = json.loads(meta_p.read_text()) if meta_p.exists() else {}
    try:
        old = load(market)
    except FileNotFoundError:
        old = None

    last_full = date.fromisoformat(meta["last_full"]) if "last_full" in meta else None
    need_full = old is None or last_full is None or (date.today() - last_full).days >= FULL_REFRESH_DAYS

    try:
        if need_full:
            print(f"[{market}] 整批下載 {len(symbols)} 檔（{start} 起）")
            df = download(symbols, start)
            meta["last_full"] = date.today().isoformat()
        else:
            have = set(old["symbol"].unique())
            new_syms = [s for s in symbols if s not in have]
            since = (old["date"].max() - timedelta(days=7)).strftime("%Y-%m-%d")
            print(f"[{market}] 增量下載 {len(symbols)} 檔（{since} 起），新代號 {len(new_syms)} 檔")
            parts = [old, download(symbols, since)]
            if new_syms:
                parts.append(download(new_syms, start))
            df = pd.concat(parts, ignore_index=True)
            df = df.drop_duplicates(["date", "symbol"], keep="last")
    except Exception as exc:
        if old is not None:
            print(f"[警告] 下載失敗（{exc}），改用快取資料")
            return old
        raise

    df = df[df["symbol"].isin(symbols)].sort_values(["date", "symbol"]).reset_index(drop=True)
    save(market, df)
    meta_p.write_text(json.dumps(meta))
    return df


def to_wide(long: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """長表 → {'open': 寬表, 'close': 寬表, ...}，index=日期，columns=代號。"""
    wide = {}
    for field in ["open", "high", "low", "close", "volume"]:
        wide[field] = long.pivot_table(index="date", columns="symbol", values=field).sort_index()
    return wide
