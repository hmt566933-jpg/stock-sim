# 策略模擬倉（台股＋美股）

每天收盤後自動抓資料、讓多個策略各自用模擬資金買賣，並在網頁儀表板比較哪個策略報酬最好。

- 股票池：台股＝上市＋上櫃全部（約 1,800 檔）；美股＝S&P 500
- 流動性篩選：近 20 日平均成交金額 台股 ≥ 5,000 萬、美股 ≥ 2,000 萬美元才會被選
- 每個策略每天掃描全部股票、依條件排名，最多持有 10 檔（每檔 10% 資金）
- 交易規則：收盤後產生訊號 → **下一個交易日開盤價**成交（避免偷看未來）
- 成本：台股手續費 0.1425%（預設 6 折、最低 20 元）、證交稅 0.3%（ETF 0.1%）、漲跌停買賣不到
- 價格：yfinance 還原權息日K
- 每個市場各自計算（台股用台幣、美股用美元），不混匯率

## 一次性設定（約 10 分鐘）

1. 在 GitHub 建一個新 repo（例如 `stock-sim`），把這個資料夾裡所有檔案上傳（包含 `.github` 資料夾）。
2. repo → **Settings → Actions → General → Workflow permissions** → 選 **Read and write permissions** → Save。
3. repo → **Actions** 分頁 → 左邊點「每日模擬更新」→ **Run workflow**（market 填 `all`）→ 等它跑完。
   第一次要下載全市場好幾年的資料，大約 15–40 分鐘；之後每天只補抓新資料，幾分鐘就好。
4. repo → **Settings → Pages** → Source 選 **Deploy from a branch**，Branch 選 `main`、資料夾選 `/docs` → Save。
5. 幾分鐘後，Pages 頁面上方會出現你的儀表板網址（`https://<帳號>.github.io/stock-sim/`）。

之後就全自動：
- 台股：週一到週五 台北時間 15:17 左右更新
- 美股：週二到週六 台北時間 06:23 左右更新
（GitHub 排程尖峰時可能延遲幾十分鐘。）

> 注意：repo 如果 60 天都沒有任何人推送變更，GitHub 會暫停排程。這個專案每天都會自動 commit，正常情況不會被暫停。

## 白話分析（選用 AI）

儀表板第一個分頁「白話分析」每天會用一般人看得懂的話，整理這週誰賺誰賠、長期誰有效、下週要注意什麼。

- **預設（免費）**：用內建規則自動產生。
- **改用 AI 撰寫**：到 Anthropic Console（console.anthropic.com）申請 API 金鑰（需付費，每天兩次分析約幾塊台幣），然後在 repo →
  **Settings → Secrets and variables → Actions → New repository secret**，Name 填 `ANTHROPIC_API_KEY`、Secret 貼上金鑰。
  之後每次執行就會改由 AI 撰寫；AI 失敗時會自動退回內建規則。

## 調整設定：`config.json`

- `universe.min_turnover`：流動性門檻；`max_positions`：每個策略最多持有幾檔
- `initial_cash`、`start_date`：起始資金與模擬起點
- `fee_discount`：你的券商手續費折數
- `strategies`：各策略參數（均線天數、RSI 門檻、動能回看天數…）

改完 push 上去，下一次排程就會用新設定從頭重算。

## 目前的策略（短線，持有 ≤ 1 週）

共同出場規則（`config.json` 的 `exits`）：最多持有 5 個交易日、跌 7% 停損；另外各策略有自己的出場條件。都以收盤價判斷，隔天開盤成交。

| 代碼 | 名稱 | 進場 | 出場（另加 5 天 / 停損） |
|---|---|---|---|
| benchmark | 大盤 | 100% 持有 0050 / SPY | — |
| ma_cross | 均線黃金交叉 5/20 | 5 日線剛穿越 20 日線，量增越多越優先 | 5 日線跌回 20 日線下 |
| rsi2 | RSI(2) 超賣反彈 | 收盤 > 200 日線且 RSI(2) < 10 | 收盤站上 5 日線 |
| breakout | 20日新高突破 | 收盤創 20 日新高、量 ≥ 1.5 倍均量 | 只靠時間 / 停損 |
| bollinger | 布林下軌反彈 | 收盤跌破布林下軌（20, 2） | 回到 20 日線 |
| volume_surge | 爆量長紅 | 漲 ≥ 4%、量 ≥ 2 倍、收在最高點附近 | 只靠時間 / 停損 |
| kd_cross | KD 低檔黃金交叉 | K 穿越 D 且 K < 30 | K > 80 |
| pullback | 多頭連3跌反彈 | 收盤 > 60 日線、連跌 3 天 | 第一根收漲 |
| momentum | 週動能輪動 | 每週五挑 20 日漲幅前 10 名 | 下週重新排名換股 |

| momentum_10 / momentum_60 | 週動能 10日 / 60日 | 同上，改看 10 / 60 日漲幅 | 同上 |
| momentum_raw | 週動能（無濾網） | 同週動能輪動，但不使用大盤濾網，用來比較濾網有沒有幫助 | 同上 |

**大盤濾網**（`config.json` 的 `market_filter`）：大盤（0050 / SPY）收盤跌破 20 日線時，所有策略都不開新倉（週動能類直接空手）；已持有的股票照原本規則出場。個別策略可設 `"market_filter": false` 關掉。

每個策略最多同時持有 10 檔。同一套策略可以用不同參數複製多份比較，例如在 `strategies` 加一個
`"breakout_60": {"type": "breakout", "label": "60日新高突破", "lookback": 60}`。

⚠️ 短線交易成本很高：台股一買一賣約 0.47%（手續費 6 折 ×2 + 證交稅 0.3%），每週換股一年下來可能吃掉 20% 以上報酬，排名表的「成本」欄可以看到。

## 新增策略

在 `simtrade/strategies.py` 寫一個 class，實作 `target_weights(close, eligible)`。最簡單的寫法是算出「進場分數」和「是否續抱」，交給 `select(entry_score, keep, self.k)` 處理排名與持股上限，然後加進 `REGISTRY` 和 `config.json` 的 `strategies`。只要用到當天以前的資料（例如 `rolling`、`shift`），引擎會自動處理成交、成本、績效。

## 本機執行

```bash
pip install -r requirements.txt
python run.py --market all      # 下載資料並產生 docs/index.html
python tests/test_engine.py     # 離線測試（用假資料）
```

## 已知簡化

- 使用還原權息價格，股利等同自動再投入；不另計股利所得稅與二代健保。
- 漲跌停以「開盤價貼近 ±10%」近似判斷。
- 股票池是「目前」的上市櫃／S&P 500 成分股，沒有包含已下市或被剔除的股票，歷史回測會偏高（倖存者偏差）。從開始每日自動模擬那天起的績效則沒有這個問題。
- 歷史股價存在 GitHub Actions 的快取（不放進 git）。平常只補抓最近幾天，每 7 天整批重抓一次以更新還原權息價格。
- yfinance 是免費的非官方資料源，偶爾會缺資料或被限流；失敗時會自動重試或沿用快取。
- 本專案為模擬結果，非投資建議。
