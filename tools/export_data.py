"""把快取裡的股價資料壓縮、按年份拆檔，給 data 分支用（每個檔案遠小於 GitHub 100 MB 上限）。
用法：python tools/export_data.py <輸出資料夾>
"""
import shutil
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)

for p in sorted((ROOT / "data").glob("*_daily.parquet")):
    market = p.stem.replace("_daily", "")
    df = pd.read_parquet(p)
    for c in ("open", "high", "low", "close"):
        df[c] = df[c].astype("float32")
    df["volume"] = df["volume"].fillna(0).round().astype("int64")
    for year, g in df.groupby(df["date"].dt.year):
        f = out / f"{market}_{year}.parquet"
        g.reset_index(drop=True).to_parquet(f, compression="zstd", index=False)
        print(f"{f.name}: {len(g):,} 筆, {f.stat().st_size / 1e6:.1f} MB")

for f in (ROOT / "data").glob("universe_*.json"):
    shutil.copy(f, out / f.name)
(out / "README.md").write_text("每日排程自動產生的股價資料（還原權息日K），每次覆蓋，只保留最新一份。\n", encoding="utf-8")
