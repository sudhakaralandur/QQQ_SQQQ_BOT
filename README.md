# QQQ_SQQQ_BOT v1.0

## Overview

A directional momentum trading bot for QQQ (Nasdaq ETF) and SQQQ (Inverse Nasdaq ETF) on Alpaca paper money.

**Key Features:**
- Trades QQQ when bullish, SQQQ when bearish
- $2,000 per trade (all-in, one position at a time)
- 5-min bar analysis with EMA + RSI signals
- Auto-exits at 2% profit or 1% stop loss
- Daily loss limit: $100 (hibernates if exceeded)
- Full CSV logging for analysis

## Setup

1. **Ensure you have `config.py` in this folder:**
   ```python
   END_POINT = "https://paper-api.alpaca.markets/v2"
   KEY = "your_alpaca_api_key"
   SECRET = "your_alpaca_secret_key"
   ```

2. **Run the bot:**
   ```bash
   python qqq_sqqq_bot.py
   ```

3. **It will:**
   - Connect to Alpaca
   - Subscribe to 5-min bars for QQQ, SQQQ, SPY
   - Log every trade, signal, and bar to `logs/` folder
   - Stop at 3:50 PM ET (close all positions)

## Output Files

After the bot runs, check the `logs/` folder:

- **`trades.csv`** — All completed trades with entry/exit prices and P&L
- **`signals.csv`** — All entry/exit signals with indicator values
- **`bars.csv`** — Every bar with computed EMA, RSI, ATR values

## Code Structure

The bot is modular:

- **`IndicatorEngine`** — Computes EMA, RSI, ATR
- **`TradeState`** — Manages position, P&L, cooldown
- **`SignalDetector`** — Decides entry/exit based on indicators
- **`TradeLogger`** — Writes CSV logs
- **`AlpacaBroker`** — Alpaca API wrapper (orders, positions, account)
- **`QQQSQQQBot`** — Main orchestrator (ties everything together)

## Strategy Explained

### Entry Signal
1. Check SPY trend (EMA fast > EMA slow = bullish)
2. If bullish: Look for QQQ RSI > 60 + QQQ price > EMA slow → BUY QQQ
3. If bearish: Look for SQQQ RSI < 40 + SQQQ price > EMA slow → BUY SQQQ

### Exit Conditions
- **Hard Stop:** Price falls 1% below entry (anytime)
- **Profit Target:** Price rises 2% above entry (after 5-min hold)
- **EOD Close:** 3:50 PM ET (market close)

### Position Sizing
- Entire $2,000 capital deployed per trade
- Qty = $2,000 / current_price (rounded down)
- Paper trading only

### Daily Risk Management
- If daily P&L < -$100, bot hibernates until next day
- Logs daily P&L after each trade

## Tuning

To adjust bot behavior, edit these constants in `qqq_sqqq_bot.py`:

```python
CAPITAL_PER_TRADE = 2000.0      # Change if you want different sizing
DAILY_LOSS_LIMIT = 100.0         # Daily loss cap
PROFIT_TARGET_PCT = 0.02         # Change to 0.03 for 3% target
HARD_STOP_PCT = 0.01             # Change to 0.02 for 2% stop
MIN_HOLD_MINUTES = 5             # Min hold before profit target allowed
COOLDOWN_MINUTES = 5             # Wait X min after exit before next entry
```

## Analysis

After running, open the CSV files in Excel or Python/pandas to analyze:

```python
import pandas as pd

# Load trades
trades = pd.read_csv("logs/trades.csv")
print(f"Total trades: {len(trades)}")
print(f"Win rate: {(trades['pnl'] > 0).sum() / len(trades) * 100:.1f}%")
print(f"Total P&L: ${trades['pnl'].sum():.2f}")
print(f"Avg trade: ${trades['pnl'].mean():.2f}")
```

## Debugging

The bot logs to console with timestamps. Check for:

- `✓ Order submitted` — Entry/exit order confirmed
- `🟢 ENTRY` — Position opened
- `🔴 EXIT` — Position closed with P&L
- `🚨 Daily loss limit hit` — Bot hibernated
- `✗ ERROR` — Something broke, check traceback

## Next Steps

1. Run the bot for 1-2 days
2. Review `logs/trades.csv` and `logs/signals.csv`
3. Calculate:
   - Win rate
   - Average win / average loss
   - Profit factor (gross profit / gross loss)
4. Decide: adjust parameters or strategy
5. Re-run and compare

---

**Questions?** Analyze the CSV logs. That's the whole point of this bot.
