# QQQ_SQQQ_BOT — Progress & Handoff

**Last updated:** 2026-07-13 (evening, after market close)
**Purpose:** Paste this into a fresh Claude chat to resume without re-explaining. It captures the full state of the QQQ_SQQQ_BOT project as of tonight.

---

## 1. What this project is

A clean-slate trading bot that trades **only QQQ and SQQQ** on Alpaca **paper money**. Deliberately started fresh to leave behind the tangled MR/HR bot history. One ticker pair, one position at a time, heavy logging so performance can actually be measured and improved.

**Design intent:** all-in directional bet — go long QQQ when the market is bullish, long SQQQ (inverse) when bearish. Never both at once.

---

## 2. Repo & local paths

- **GitHub repo:** `sudhakaralandur/QQQ_SQQQ_BOT` (public)
- **GitHub URL:** https://github.com/sudhakaralandur/QQQ_SQQQ_BOT
- **Local folder:** `C:\Users\sudha\OneDrive\00-Codex Projects\QQQ_SQQQ_BOT`
- **GitHub PAT:** in user preferences / memory (same PAT used across all projects; repo + workflow scope). Claude pushes directly to GitHub; user pulls.
- **`.gitignore`** protects `config.py`, `*.db`, `logs/`, `*.csv`, `__pycache__/`. `config.py` was accidentally committed early on, then removed with `git rm --cached` — confirmed gone from remote.

**Workflow that works:** Claude commits/pushes to GitHub → user runs `git pull` locally. Early on there was an "unrelated histories" mess (user had `git init`'d locally while Claude also created a repo); resolved by deleting the local folder and doing a clean `git clone`, then recreating `config.py`. If history diverges again, clean clone is the fast fix.

---

## 3. Files in the repo

| File | Purpose |
|---|---|
| `qqq_sqqq_bot.py` | Main bot. Websocket → indicators → signal → order → log. Heavily commented. |
| `dashboard.py` | Flask dashboard on `localhost:5000`, auto-refresh every 3s, reads the CSV logs. |
| `analyzer.py` | Phase-1 measurement-only analyzer. Writes `logs/report_latest.html`. |
| `README.md` | Setup + strategy explanation + tuning knobs. |
| `.gitignore` | Protects secrets and logs. |
| `PROGRESS.md` | This file. |
| `config.py` | **Local only, NOT in git.** Holds `END_POINT`, `KEY`, `SECRET`. |
| `test.py` | **Local only.** Simple Alpaca connection test. Confirmed working. |

`logs/` (created at first run, gitignored) contains `trades.csv`, `signals.csv`, `bars.csv`, and the generated `report_*.html`.

---

## 4. config.py format (local only)

```python
END_POINT = "https://paper-api.alpaca.markets/v2"
KEY = "…"      # Alpaca paper key (starts PKN…)
SECRET = "…"   # Alpaca paper secret
```

Note: this SDK version's `TradingClient(api_key=…, secret_key=…)` does **not** accept `base_url`. `END_POINT` is kept in config for reference but isn't passed to the client.

---

## 5. Environment

- Windows, Python **3.14.4**, Notepad++ for editing, multiple CMD windows.
- `alpaca-py 0.43.4` installed. Key quirks already hit:
  - Websocket bar handler **must be `async def`** (`on_bar` is a coroutine). A plain `def` throws `handler must be a coroutine function`.
  - Use `feed=DataFeed.IEX` on `StockDataStream` (paper/free tier; SIP would error).
- Connection test passed: account `PA32VMK5BG46`, $100,000 paper cash.

---

## 6. Bot strategy as currently coded

**Timeframe:** 5-minute bars on QQQ, SQQQ, and SPY (SPY = market-regime proxy).

**Indicators** (computed in `IndicatorEngine`): EMA fast 9, EMA slow 21, RSI 14, ATR 14.

**Entry (`SignalDetector.should_enter`):**
- Bullish: SPY EMA bullish **and** QQQ price > EMA-slow **and** QQQ RSI > 60 → **buy QQQ**.
- Bearish: SPY EMA bearish **and** SQQQ price > EMA-slow **and** SQQQ RSI < 40 → **buy SQQQ**.
- Gated by: not already in a position, not in cooldown, not hibernating, indicators ready.

**Exit (`SignalDetector.should_exit`):**
- Hard stop at **−1%** (any time).
- Profit target at **+2%** (only after 5-min min hold).
- EOD close at **3:50 PM ET**.

**Sizing / risk constants (top of `qqq_sqqq_bot.py`):**
- `CAPITAL_PER_TRADE = 2000.0` (entire capital per trade; qty = 2000 / price, floored)
- `DAILY_LOSS_LIMIT = 100.0` (hibernate for the day if breached)
- `PROFIT_TARGET_PCT = 0.02`, `HARD_STOP_PCT = 0.01`
- `MIN_HOLD_MINUTES = 5`, `COOLDOWN_MINUTES = 5`

**Class structure (modular on purpose):** `IndicatorEngine`, `TradeState`, `SignalDetector`, `TradeLogger`, `AlpacaBroker`, `QQQSQQQBot` (orchestrator).

---

## 7. Logging & the learning plan

User wants a bot that "analyzes, learns, and adjusts." Agreed approach — phased, because auto-tuning on tiny samples is exactly how the old MR bot overfit (it tuned RSI to 45–90 to fit AMD's once-in-a-lifetime +86% rally, then bled).

- **Phase 1 — measurement (DONE, shipped tonight).** `analyzer.py` reads the CSVs and computes P&L, win rate, profit factor, expectancy, avg win/loss, best/worst, and breakdowns by ticker / exit reason / time-of-day / entry-RSI bucket. Writes `logs/report_latest.html` + a dated copy, with a plain-English observations section. It **changes nothing**; it loudly flags low sample size. Wired into the bot's EOD shutdown (`run()`'s `finally`), wrapped in try/except so a report error can't crash trading. Tested against empty logs (clean "empty report") and against seeded sample trades (stats + HTML verified via screenshot).
- **Phase 2 — proposal engine (NOT built).** After *weeks* of real trades, surface patterns and propose specific parameter changes **with evidence**, for human approval. Not automatic.
- **Phase 3 — controlled auto-adjust (NOT built, maybe never).** Only after Phase 2 earns trust; tiny nudges, large-sample gate, hard rollback. Explicitly deferred.

**Guiding rule agreed with user:** review on real drift over a meaningful sample, not on single good/bad days. Measurement + guardrails is the point, not auto-tuning.

---

## 8. Current status

- ✅ Repo created, code pushed, `config.py` protected and out of git.
- ✅ Connection test passes.
- ✅ Bot runs and connects to the Alpaca websocket after hours (idle, no bars — expected). No crashes.
- ✅ `logs/` created with the three CSVs (headers only, **no real trades yet**).
- ✅ Dashboard runs and renders.
- ✅ `analyzer.py` shipped and hooked into EOD.
- ⛔ **Zero live/paper trades so far.** The bot has never traded during market hours. Every stat is empty until it runs a real session.

---

## 9. Open items / next steps

- [ ] **Run the bot during market hours** (9:30 AM–3:50 PM ET) to generate the first real trades. Two CMD windows: `python qqq_sqqq_bot.py` and `python dashboard.py` (browser at http://localhost:5000).
- [ ] After the first real session, open `logs/report_latest.html` (auto-generated at EOD) and review.
- [ ] **Do not tune parameters on the first day or two of data.** Wait for a real sample.
- [ ] **Analyzer RSI-bucket matching is approximate** — it links trades to entry signals by ticker, not a hard trade ID. Fine for a directional read; tighten this (exact trade↔signal link) before building Phase 2 proposals.
- [ ] Phase 2 proposal engine — only after weeks of trades.
- [ ] Consider whether the +2% target / −1% stop is right for QQQ/SQQQ's actual daily range once there's data (the analyzer's "by exit reason" table will show if stops dominate or trades die at EOD).

---

## 10. Lessons carried over from MR/HR (don't repeat)

- Ticker/regime quality matters more than parameter tuning. An edge measured during an abnormal rally isn't a real edge.
- Hair-trigger exits kill winners (the old MR "VWAP break the instant price dips below VWAP after 10 min" shook out trades that then recovered). The new bot uses %-based target/stop + min-hold instead.
- Small sample + auto-tuning = confident overfitting. Measure first.
- Real-money trading: **not on the table.** Paper only until there's a proven, validated profitable stretch.

---

## 11. How to resume in a fresh chat

1. Paste this file.
2. Tell Claude whether the bot has traded yet and, if so, paste or upload `logs/report_latest.html` or the CSVs.
3. Pick up at the relevant open item in section 9.
