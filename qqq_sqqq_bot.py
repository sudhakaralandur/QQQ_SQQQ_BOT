#!/usr/bin/env python3
"""
================================================================================
QQQ_SQQQ_BOT v1.0 — Directional Momentum Trader
================================================================================

OVERVIEW:
  This bot trades QQQ (Nasdaq ETF) and SQQQ (Inverse Nasdaq ETF) on paper money.
  It detects market direction and momentum, then goes all-in on ONE position.
  
CAPITAL ALLOCATION:
  - $2,000 per trade (entire capital deployed per signal)
  - Max daily loss: $100 (then hibernates until next day)
  - Paper trading only via Alpaca
  
TRADING LOGIC:
  1. Monitor 5-min bars on QQQ and SPY (market proxy)
  2. Detect regime: Is QQQ trending up or down?
  3. Wait for momentum confirmation (RSI breakout or price acceleration)
  4. Entry: Go LONG QQQ (if bullish) OR LONG SQQQ (if bearish)
  5. Exit: Hit 2% profit target OR 1% hard stop loss
  6. Position: Max 1 open position at a time
  7. Cooldown: 5-min wait after any exit before next entry allowed

LOGGING:
  - Every bar, signal, and trade logged to CSV
  - Nightly analysis dump for backtesting and optimization
  - Full order timestamps and P&L tracking

USAGE:
  python qqq_sqqq_bot.py
  (Runs until 3:50 PM ET, closes all positions, hibernates)

================================================================================
"""

import sys
import os
import logging
import json
import csv
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
from collections import deque
import threading
import queue
import signal
import ctypes

# ─────────────────────────────────────────────────────────────────────────────
# IMPORTS & SETUP
# ─────────────────────────────────────────────────────────────────────────────

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import config
except ImportError:
    print("ERROR: config.py not found. Create it with END_POINT, KEY, SECRET.")
    sys.exit(1)

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.live import StockDataStream
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
import pandas as pd
import numpy as np

# Logging setup
# Writes to BOTH console (when you're watching) and a daily log file
# (so headless/unattended runs via Task Scheduler leave a record).
_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(_LOG_DIR, exist_ok=True)
_log_filename = os.path.join(
    _LOG_DIR, f"bot_{datetime.now().strftime('%Y-%m-%d')}.log"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(),                 # console (visible when interactive)
        logging.FileHandler(_log_filename, encoding="utf-8"),  # file (visible when headless)
    ],
)
log = logging.getLogger("QQQ_SQQQ_BOT")

EST = ZoneInfo("America/New_York")


# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION & CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

CAPITAL_PER_TRADE = 2000.0          # Deploy this much on each trade
DAILY_LOSS_LIMIT = 100.0             # Stop trading if daily loss exceeds this
PROFIT_TARGET_PCT = 0.02             # Exit at +2% profit
HARD_STOP_PCT = 0.01                 # Exit at -1% loss (legacy/Option "current")

# ── STOP EXPERIMENT (shadow comparison of 3 stop rules) ──
# Which stop the bot ACTUALLY trades on. "ATR" = Option B (live during test).
#   "CURRENT" -> flat -1%   |   "OPTION_A" -> flat -2.5% for SQQQ, -1% for QQQ   |   "ATR" -> entry - ATR_STOP_MULT*ATR
STOP_MODE = "OPTION_A"
ATR_STOP_MULT = 1.5                  # Option B (5-min ATR): stop = entry_price - 1.5 * ATR5min(entry)
ATR_DAILY_MULT = 1.5                 # Option B-daily: stop = entry_price - 1.5 * ATR_daily(entry)
OPTION_A_STOP_PCT_SQQQ = 0.025       # Option A: -2.5% stop for SQQQ
OPTION_A_STOP_PCT_QQQ = 0.01         # Option A: -1% stop for QQQ (unchanged)
MIN_HOLD_MINUTES = 5                 # Minimum hold before exit allowed
COOLDOWN_MINUTES = 5                 # Wait X min after exit before next entry
ATR_PERIOD = 14                      # ATR lookback for volatility
RSI_PERIOD = 14                      # RSI lookback for momentum
EMA_FAST = 9                         # Fast EMA for trend
EMA_SLOW = 21                        # Slow EMA for trend

TICKERS = ["QQQ", "SQQQ"]            # Main positions
MARKET_PROXY = "SPY"                 # For regime confirmation

LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

TRADES_CSV = os.path.join(LOG_DIR, "trades.csv")
SIGNALS_CSV = os.path.join(LOG_DIR, "signals.csv")
BARS_CSV = os.path.join(LOG_DIR, "bars.csv")
NEAR_MISSES_CSV = os.path.join(LOG_DIR, "near_misses.csv")
STOP_COMPARISON_CSV = os.path.join(LOG_DIR, "stop_comparison.csv")

# Near-miss tuning: how close (in RSI points) a setup must get to the RSI>60
# threshold to be worth logging as a "near miss". 8 => log setups with RSI 52-60
# where SPY regime + EMA alignment were otherwise correct.
NEAR_MISS_RSI_WINDOW = 8.0


# ─────────────────────────────────────────────────────────────────────────────
# INDICATOR ENGINE — Computes EMA, RSI, ATR on incoming bars
# ─────────────────────────────────────────────────────────────────────────────

class IndicatorEngine:
    """
    Maintains rolling bar history and computes indicators on demand.
    
    Why separate class?
      - Keeps indicator logic testable and reusable
      - Prevents indicator state from polluting bot logic
      - Easy to swap or extend indicators later
    """
    
    def __init__(self, lookback=200):
        self.lookback = lookback
        self.bars = {}  # symbol -> deque of bar dicts
    
    def add_bar(self, symbol, bar_dict):
        """Add a new bar and trim history to lookback window."""
        if symbol not in self.bars:
            self.bars[symbol] = deque(maxlen=self.lookback)
        self.bars[symbol].append(bar_dict)
    
    def get_bars(self, symbol):
        """Return all bars for a symbol as list (for pandas)."""
        return list(self.bars.get(symbol, []))
    
    def compute(self, symbol):
        """
        Compute all indicators for a symbol.
        Returns dict: {ema_fast, ema_slow, rsi, atr, above_ema_fast, ...}
        """
        bars = self.get_bars(symbol)
        if len(bars) < 2:
            return {}
        
        df = pd.DataFrame(bars)
        closes = df["close"].astype(float)
        highs = df["high"].astype(float)
        lows = df["low"].astype(float)
        
        result = {}
        
        # EMA Fast (9-period)
        if len(closes) >= EMA_FAST:
            ema_f = closes.ewm(span=EMA_FAST, adjust=False).mean()
            result["ema_fast"] = float(ema_f.iloc[-1])
        
        # EMA Slow (21-period)
        if len(closes) >= EMA_SLOW:
            ema_s = closes.ewm(span=EMA_SLOW, adjust=False).mean()
            result["ema_slow"] = float(ema_s.iloc[-1])
        
        # RSI (14-period)
        if len(closes) >= RSI_PERIOD + 1:
            delta = closes.diff()
            gain = delta.clip(lower=0)
            loss = (-delta).clip(lower=0)
            avg_g = gain.ewm(span=RSI_PERIOD, adjust=False).mean()
            avg_l = loss.ewm(span=RSI_PERIOD, adjust=False).mean()
            rs = avg_g / avg_l.replace(0, np.nan)
            rsi = 100 - (100 / (1 + rs))
            result["rsi"] = float(rsi.iloc[-1])
        
        # ATR (14-period on high/low/close)
        if len(closes) >= ATR_PERIOD + 1:
            tr = pd.concat([
                highs - lows,
                (highs - closes.shift(1)).abs(),
                (lows - closes.shift(1)).abs()
            ], axis=1).max(axis=1)
            atr = tr.ewm(span=ATR_PERIOD, adjust=False).mean()
            result["atr"] = float(atr.iloc[-1])
        
        # Price position relative to EMAs
        price = closes.iloc[-1]
        if "ema_fast" in result and "ema_slow" in result:
            result["above_ema_fast"] = price > result["ema_fast"]
            result["above_ema_slow"] = price > result["ema_slow"]
            result["ema_bullish"] = result["ema_fast"] > result["ema_slow"]
        
        result["price"] = float(price)
        
        return result


# ─────────────────────────────────────────────────────────────────────────────
# TRADE STATE MANAGER
# ─────────────────────────────────────────────────────────────────────────────

class TradeState:
    """
    Tracks current position, P&L, and exit criteria.
    
    Why separate class?
      - Isolates position management from signal logic
      - Easy to query current state (am I in a position? what's my P&L?)
      - Thread-safe via locks (if we add threading later)
    """
    
    def __init__(self):
        self.open_position = None  # None or {"ticker": "QQQ", "qty": X, "entry_price": Y, "entry_time": Z}
        self.daily_pnl = 0.0
        self.last_exit_time = None
        self.hibernating = False
    
    def enter(self, ticker, qty, entry_price):
        entry_price = float(entry_price)
        """Record a new trade entry."""
        self.open_position = {
            "ticker": ticker,
            "qty": qty,
            "entry_price": entry_price,
            "entry_time": datetime.now(EST),
            "peak_price": entry_price,
            "entry_atr": getattr(self, "_pending_entry_atr", 0.0),  # 5-min ATR at entry
            "entry_atr_daily": getattr(self, "_pending_entry_atr_daily", 0.0),  # daily ATR at entry
            "worst_price": entry_price,   # lowest price seen while holding (for MAE)
            "worst_pct": 0.0,             # most-negative pnl_pct seen (Max Adverse Excursion)
        }
        log.info(f"🟢 ENTRY: {qty} shares of {ticker} @ ${entry_price:.2f}")
    
    def exit(self, exit_price, reason):
        """
        Close the current position and return exit details.
        Returns: {ticker, qty, entry_price, exit_price, pnl, pnl_pct, hold_minutes, reason}
        """
        if not self.open_position:
            return None
        
        pos = self.open_position
        pnl = (exit_price - pos["entry_price"]) * pos["qty"]
        pnl_pct = (exit_price - pos["entry_price"]) / pos["entry_price"] * 100
        hold_min = (datetime.now(EST) - pos["entry_time"]).total_seconds() / 60
        
        result = {
            "ticker": pos["ticker"],
            "qty": pos["qty"],
            "entry_price": pos["entry_price"],
            "entry_time": pos["entry_time"],
            "exit_price": exit_price,
            "exit_time": datetime.now(EST),
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "hold_minutes": hold_min,
            "reason": reason,
        }
        
        self.daily_pnl += pnl
        self.open_position = None
        self.last_exit_time = datetime.now(EST)
        
        log.info(f"🔴 EXIT: {pos['ticker']} @ ${exit_price:.2f} | P&L: ${pnl:+.2f} ({pnl_pct:+.2f}%) | {reason}")
        
        return result
    
    def in_cooldown(self):
        """Check if we're in cooldown period after last exit."""
        if not self.last_exit_time:
            return False
        elapsed = (datetime.now(EST) - self.last_exit_time).total_seconds() / 60
        return elapsed < COOLDOWN_MINUTES
    
    def check_daily_loss_limit(self):
        """Check if we've hit daily loss limit."""
        if self.daily_pnl <= -DAILY_LOSS_LIMIT:
            self.hibernating = True
            log.warning(f"🚨 Daily loss limit hit: ${abs(self.daily_pnl):.2f}. HIBERNATING.")
            return True
        return False


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL DETECTOR — Entry/Exit Decision Logic
# ─────────────────────────────────────────────────────────────────────────────

_HIST_CLIENT = None

def _get_hist_client():
    """Lazy singleton historical data client (paper keys work for data)."""
    global _HIST_CLIENT
    if _HIST_CLIENT is None:
        _HIST_CLIENT = StockHistoricalDataClient(config.KEY, config.SECRET)
    return _HIST_CLIENT


def fetch_daily_atr(ticker, period=14):
    """
    Fetch ~ (period+1) daily bars and compute a simple daily ATR.
    Returns float ATR in price units, or 0.0 on any failure (non-critical).
    Called once per entry for the stop experiment.
    """
    try:
        client = _get_hist_client()
        end = datetime.now(EST)
        start = end - timedelta(days=period * 3 + 10)  # calendar buffer for weekends/holidays
        req = StockBarsRequest(
            symbol_or_symbols=ticker,
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
            feed=DataFeed.IEX,
        )
        bars = client.get_stock_bars(req)
        df = bars.df
        if df is None or len(df) < 2:
            return 0.0
        # df may be multi-indexed by symbol; reduce to this ticker
        if "symbol" in df.index.names:
            df = df.xs(ticker, level="symbol")
        highs = df["high"].values
        lows = df["low"].values
        closes = df["close"].values
        trs = []
        for i in range(1, len(closes)):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
            trs.append(tr)
        if not trs:
            return 0.0
        use = trs[-period:] if len(trs) >= period else trs
        return float(sum(use) / len(use))
    except Exception as e:
        log.error(f"Daily ATR fetch failed for {ticker} (non-critical): {e}")
        return 0.0


def _live_stop_pct(pos):
    """
    Return the LIVE stop distance as a positive percent (e.g. 1.0 = -1%),
    based on STOP_MODE. Used by should_exit to actually trade one rule.
    """
    ticker = pos["ticker"]
    entry = pos["entry_price"]
    atr = pos.get("entry_atr", 0.0) or 0.0

    if STOP_MODE == "ATR" and atr > 0 and entry > 0:
        return (ATR_STOP_MULT * atr) / entry * 100.0
    if STOP_MODE == "OPTION_A":
        return (OPTION_A_STOP_PCT_SQQQ if ticker == "SQQQ" else OPTION_A_STOP_PCT_QQQ) * 100.0
    # default / CURRENT
    return HARD_STOP_PCT * 100.0


def _stop_pct_for_rule(rule, ticker, entry_price, entry_atr, entry_atr_daily=0.0):
    """Stop distance (positive %) for a named rule, for shadow comparison."""
    if rule == "CURRENT":
        return HARD_STOP_PCT * 100.0
    if rule == "OPTION_A":
        return (OPTION_A_STOP_PCT_SQQQ if ticker == "SQQQ" else OPTION_A_STOP_PCT_QQQ) * 100.0
    if rule == "ATR_5MIN":
        if entry_atr > 0 and entry_price > 0:
            return (ATR_STOP_MULT * entry_atr) / entry_price * 100.0
        return HARD_STOP_PCT * 100.0  # fallback if ATR missing
    if rule == "ATR_DAILY":
        if entry_atr_daily > 0 and entry_price > 0:
            return (ATR_DAILY_MULT * entry_atr_daily) / entry_price * 100.0
        return HARD_STOP_PCT * 100.0  # fallback if daily ATR missing
    return HARD_STOP_PCT * 100.0


class SignalDetector:
    """
    Analyzes indicators and decides: should we enter? should we exit?
    
    Why separate class?
      - Isolates trading rules from bot plumbing
      - Easy to test and tweak signal logic
      - Clear, readable decision trees
    """
    
    @staticmethod
    def should_enter(indicators_qqq, indicators_sqqq, indicators_spy, trade_state):
        """
        Decide if we should enter a trade.
        Returns: (True/False, ticker, "reason")
        
        LOGIC:
          1. NOT in position already
          2. NOT in cooldown
          3. NOT hibernating
          4. SPY must be trending (EMA bullish/bearish)
          5. QQQ/SQQQ shows momentum (RSI > 60 or < 40)
          6. Price above/below appropriate EMA
        """
        
        if trade_state.open_position:
            return False, None, "Already in position"
        
        if trade_state.in_cooldown():
            return False, None, "In cooldown"
        
        if trade_state.hibernating:
            return False, None, "Hibernating (daily loss limit)"
        
        # Require indicators ready
        if not indicators_qqq or not indicators_sqqq or not indicators_spy:
            return False, None, "Indicators not ready"
        
        # SPY regime check (don't fight the overall market)
        spy_ema_bullish = indicators_spy.get("ema_bullish", False)
        spy_rsi = indicators_spy.get("rsi", 50)
        
        # QQQ momentum checks
        qqq_rsi = indicators_qqq.get("rsi", 50)
        qqq_above_slow = indicators_qqq.get("above_ema_slow", False)
        
        # SQQQ momentum checks (inverse)
        sqqq_rsi = indicators_sqqq.get("rsi", 50)
        sqqq_above_slow = indicators_sqqq.get("above_ema_slow", False)
        
        # BULLISH ENTRY: QQQ strong + SPY trending up
        if spy_ema_bullish and qqq_above_slow and qqq_rsi > 60:
            return True, "QQQ", f"QQQ bullish (RSI {qqq_rsi:.0f}, SPY bullish)"
        
        # BEARISH ENTRY: QQQ weak + SPY trending down
        if not spy_ema_bullish and sqqq_above_slow and sqqq_rsi > 60:
            return True, "SQQQ", f"SQQQ bullish (RSI {sqqq_rsi:.0f}, SPY bearish)"
        
        return False, None, "No signal"

    @staticmethod
    def detect_near_miss(indicators_qqq, indicators_sqqq, indicators_spy):
        """
        Detect a setup that ALMOST triggered but didn't, so we can measure
        how much the strict gate is passing on. Measurement only.

        Returns (side, reason) if it's a near miss, else (None, None).

        A "near miss" = SPY regime is correct AND price/EMA alignment is
        correct, but RSI fell just short of the 60 threshold (within
        NEAR_MISS_RSI_WINDOW points, i.e. RSI roughly 52-60). Also flags the
        inverse case: RSI cleared 60 but EMA alignment or SPY regime blocked it.
        """
        if not indicators_qqq or not indicators_sqqq or not indicators_spy:
            return None, None

        spy_ema_bullish = indicators_spy.get("ema_bullish", False)
        qqq_rsi = indicators_qqq.get("rsi", 50)
        qqq_above_slow = indicators_qqq.get("above_ema_slow", False)
        sqqq_rsi = indicators_sqqq.get("rsi", 50)
        sqqq_above_slow = indicators_sqqq.get("above_ema_slow", False)

        low = 60.0 - NEAR_MISS_RSI_WINDOW

        # --- BULLISH side (would have been a QQQ buy) ---
        if spy_ema_bullish and qqq_above_slow and low <= qqq_rsi <= 60.0:
            return "QQQ", f"RSI {qqq_rsi:.0f} just short of 60 (regime+EMA OK)"
        if spy_ema_bullish and qqq_rsi > 60.0 and not qqq_above_slow:
            return "QQQ", f"RSI {qqq_rsi:.0f} OK but price below EMA-slow"

        # --- BEARISH side (would have been an SQQQ buy) ---
        if (not spy_ema_bullish) and sqqq_above_slow and low <= sqqq_rsi <= 60.0:
            return "SQQQ", f"RSI {sqqq_rsi:.0f} just short of 60 (regime+EMA OK)"
        if (not spy_ema_bullish) and sqqq_rsi > 60.0 and not sqqq_above_slow:
            return "SQQQ", f"RSI {sqqq_rsi:.0f} OK but price below EMA-slow"

        return None, None

    @staticmethod
    def should_exit(current_price, trade_state, hold_minutes):
        """
        Decide if we should exit current position.
        Returns: (True/False, "reason")
        
        EXIT CONDITIONS:
          1. Hold >= 5 min AND price hit 2% profit target
          2. Price hit 1% hard stop loss (any time)
          3. EOD (3:50 PM ET)
        """
        
        if not trade_state.open_position:
            return False, ""
        
        pos = trade_state.open_position
        entry_price = pos["entry_price"]
        pnl_pct = (current_price - entry_price) / entry_price * 100

        # Track Max Adverse Excursion (worst dip) for the stop experiment
        if pnl_pct < pos.get("worst_pct", 0.0):
            pos["worst_pct"] = pnl_pct
            pos["worst_price"] = current_price

        # Hard stop — rule depends on STOP_MODE (live stop for the experiment)
        stop_pct = _live_stop_pct(pos)   # positive number, e.g. 1.0 means -1%
        if pnl_pct <= -stop_pct:
            return True, f"Hard stop ({pnl_pct:.2f}%)"
        
        # Profit target — only after min hold
        if hold_minutes >= MIN_HOLD_MINUTES and pnl_pct >= PROFIT_TARGET_PCT * 100:
            return True, f"Profit target ({pnl_pct:.2f}%)"
        
        # EOD close
        now = datetime.now(EST)
        if now.time() >= time(15, 50):  # 3:50 PM
            return True, "EOD close"
        
        return False, ""


# ─────────────────────────────────────────────────────────────────────────────
# TRADE LOGGER — Logs to CSV for analysis
# ─────────────────────────────────────────────────────────────────────────────

class TradeLogger:
    """
    Writes trades, signals, and bars to CSV for later analysis.
    
    Why CSV?
      - Human-readable, easy to inspect in Excel
      - Can import into pandas for analysis
      - Simple to version control
      - No database setup required
    """
    
    @staticmethod
    def init_csv_files():
        """Create CSV headers if files don't exist."""
        
        # Trades CSV
        if not os.path.exists(TRADES_CSV):
            with open(TRADES_CSV, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "entry_time", "exit_time", "ticker", "qty", "entry_price",
                    "exit_price", "pnl", "pnl_pct", "hold_minutes", "exit_reason"
                ])
        
        # Signals CSV
        if not os.path.exists(SIGNALS_CSV):
            with open(SIGNALS_CSV, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "timestamp", "signal_type", "ticker", "reason",
                    "qqq_price", "qqq_rsi", "qqq_ema_bullish",
                    "sqqq_price", "sqqq_rsi", "sqqq_ema_bullish",
                    "spy_price", "spy_rsi", "spy_ema_bullish"
                ])
        
        # Bars CSV
        if not os.path.exists(BARS_CSV):
            with open(BARS_CSV, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "timestamp", "ticker", "open", "high", "low", "close",
                    "volume", "ema_fast", "ema_slow", "rsi", "atr"
                ])
        
        # Near-misses CSV (measurement only - setups that ALMOST fired)
        if not os.path.exists(NEAR_MISSES_CSV):
            with open(NEAR_MISSES_CSV, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "timestamp", "side", "miss_reason",
                    "qqq_price", "qqq_rsi", "qqq_above_slow", "qqq_ema_bullish",
                    "sqqq_price", "sqqq_rsi", "sqqq_above_slow", "sqqq_ema_bullish",
                    "spy_price", "spy_rsi", "spy_ema_bullish"
                ])

        # Stop-comparison CSV (shadow experiment: 3 stop rules scored per trade)
        if not os.path.exists(STOP_COMPARISON_CSV):
            with open(STOP_COMPARISON_CSV, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "entry_time", "exit_time", "ticker", "qty",
                    "entry_price", "actual_exit_price", "actual_exit_reason",
                    "actual_pnl", "actual_pnl_pct", "hold_minutes",
                    "live_stop_mode", "entry_atr_5min", "entry_atr_daily",
                    "worst_pnl_pct", "worst_price",
                    # For each rule: its stop %, and whether the trade's worst dip would have triggered it
                    "CURRENT_stop_pct", "CURRENT_would_fire",
                    "OPTION_A_stop_pct", "OPTION_A_would_fire",
                    "ATR_5MIN_stop_pct", "ATR_5MIN_would_fire",
                    "ATR_DAILY_stop_pct", "ATR_DAILY_would_fire",
                ])
    
    @staticmethod
    def log_trade(exit_details):
        """Log a completed trade."""
        with open(TRADES_CSV, "a", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                exit_details["entry_time"].isoformat(),
                exit_details["exit_time"].isoformat(),
                exit_details["ticker"],
                exit_details["qty"],
                f"{exit_details['entry_price']:.2f}",
                f"{exit_details['exit_price']:.2f}",
                f"{exit_details['pnl']:.2f}",
                f"{exit_details['pnl_pct']:.2f}",
                f"{exit_details['hold_minutes']:.1f}",
                exit_details["reason"],
            ])
    
    @staticmethod
    def log_signal(signal_type, ticker, reason, indicators_qqq, indicators_sqqq, indicators_spy):
        """Log a signal (entry or exit attempt)."""
        with open(SIGNALS_CSV, "a", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                datetime.now(EST).isoformat(),
                signal_type,
                ticker,
                reason,
                f"{indicators_qqq.get('price', 0):.2f}",
                f"{indicators_qqq.get('rsi', 0):.1f}",
                indicators_qqq.get("ema_bullish", False),
                f"{indicators_sqqq.get('price', 0):.2f}",
                f"{indicators_sqqq.get('rsi', 0):.1f}",
                indicators_sqqq.get("ema_bullish", False),
                f"{indicators_spy.get('price', 0):.2f}",
                f"{indicators_spy.get('rsi', 0):.1f}",
                indicators_spy.get("ema_bullish", False),
            ])
    
    @staticmethod
    def log_near_miss(side, miss_reason, ind_qqq, ind_sqqq, ind_spy):
        """
        Log a setup that ALMOST fired but didn't. Measurement only - this
        never affects trading. Writes to its own file so it can never
        interfere with signals.csv or the analyzer.
        """
        try:
            with open(NEAR_MISSES_CSV, "a", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    datetime.now(EST).isoformat(),
                    side,
                    miss_reason,
                    f"{ind_qqq.get('price', 0):.2f}",
                    f"{ind_qqq.get('rsi', 0):.1f}",
                    ind_qqq.get("above_ema_slow", False),
                    ind_qqq.get("ema_bullish", False),
                    f"{ind_sqqq.get('price', 0):.2f}",
                    f"{ind_sqqq.get('rsi', 0):.1f}",
                    ind_sqqq.get("above_ema_slow", False),
                    ind_sqqq.get("ema_bullish", False),
                    f"{ind_spy.get('price', 0):.2f}",
                    f"{ind_spy.get('rsi', 0):.1f}",
                    ind_spy.get("ema_bullish", False),
                ])
        except Exception as e:
            log.error(f"Near-miss log failed (non-critical): {e}")

    @staticmethod
    def log_stop_comparison(pos, exit_details):
        """
        Shadow experiment (measurement only): for the trade that just closed,
        record what EACH of the 3 stop rules would have done, using the trade's
        Max Adverse Excursion (worst_pnl_pct) captured while holding.

        A rule "would fire" if the worst dip reached its stop distance. Because
        the LIVE stop is whatever STOP_MODE is, the live rule's actual exit is
        also recorded, so analysis can compare fired/not-fired cleanly.
        """
        ticker = pos["ticker"]
        entry_price = pos["entry_price"]
        entry_atr = pos.get("entry_atr", 0.0) or 0.0
        entry_atr_daily = pos.get("entry_atr_daily", 0.0) or 0.0
        worst_pct = pos.get("worst_pct", 0.0)      # most-negative pnl_pct seen (<=0)
        worst_price = pos.get("worst_price", entry_price)

        rules = {}
        for rule in ("CURRENT", "OPTION_A", "ATR_5MIN", "ATR_DAILY"):
            stop_pct = _stop_pct_for_rule(rule, ticker, entry_price, entry_atr, entry_atr_daily)
            would_fire = worst_pct <= -stop_pct   # did worst dip reach this stop?
            rules[rule] = (stop_pct, would_fire)

        try:
            with open(STOP_COMPARISON_CSV, "a", newline="") as f:
                w = csv.writer(f)
                w.writerow([
                    pos["entry_time"].isoformat() if hasattr(pos["entry_time"], "isoformat") else pos["entry_time"],
                    exit_details["exit_time"].isoformat() if hasattr(exit_details["exit_time"], "isoformat") else exit_details["exit_time"],
                    ticker, pos["qty"],
                    f"{entry_price:.4f}",
                    f"{exit_details['exit_price']:.4f}",
                    exit_details["reason"],
                    f"{exit_details['pnl']:.2f}",
                    f"{exit_details['pnl_pct']:.4f}",
                    f"{exit_details['hold_minutes']:.1f}",
                    STOP_MODE,
                    f"{entry_atr:.4f}",
                    f"{entry_atr_daily:.4f}",
                    f"{worst_pct:.4f}",
                    f"{worst_price:.4f}",
                    f"{rules['CURRENT'][0]:.4f}", rules['CURRENT'][1],
                    f"{rules['OPTION_A'][0]:.4f}", rules['OPTION_A'][1],
                    f"{rules['ATR_5MIN'][0]:.4f}", rules['ATR_5MIN'][1],
                    f"{rules['ATR_DAILY'][0]:.4f}", rules['ATR_DAILY'][1],
                ])
        except Exception as e:
            log.error(f"Stop-comparison write failed (non-critical): {e}")

    @staticmethod
    def log_bar(ticker, bar_dict, indicators):
        """Log a bar with computed indicators."""
        with open(BARS_CSV, "a", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                datetime.now(EST).isoformat(),
                ticker,
                f"{bar_dict.get('open', 0):.2f}",
                f"{bar_dict.get('high', 0):.2f}",
                f"{bar_dict.get('low', 0):.2f}",
                f"{bar_dict.get('close', 0):.2f}",
                bar_dict.get("volume", 0),
                f"{indicators.get('ema_fast', 0):.2f}",
                f"{indicators.get('ema_slow', 0):.2f}",
                f"{indicators.get('rsi', 0):.1f}",
                f"{indicators.get('atr', 0):.4f}",
            ])


# ─────────────────────────────────────────────────────────────────────────────
# ALPACA BROKER INTERFACE
# ─────────────────────────────────────────────────────────────────────────────

class AlpacaBroker:
    """
    Handles all Alpaca API interactions: orders, positions, account.
    
    Why separate class?
      - Isolates Alpaca-specific logic
      - Easy to swap for another broker later
      - Keeps API error handling centralized
    """
    
    def __init__(self):
        self.client = TradingClient(
            api_key=config.KEY,
            secret_key=config.SECRET
        )
    
    def get_account(self):
        """Get current account info (cash, portfolio value, etc.)."""
        try:
            return self.client.get_account()
        except Exception as e:
            log.error(f"Failed to get account: {e}")
            return None
    
    def submit_order(self, ticker, qty, side):
        """
        Submit a market order.
        side: "buy" or "sell"
        Returns: order object or None on failure
        """
        try:
            order_req = MarketOrderRequest(
                symbol=ticker,
                qty=qty,
                side=OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL,
                time_in_force=TimeInForce.DAY
            )
            order = self.client.submit_order(order_req)
            log.info(f"✓ Order submitted: {qty} {ticker} {side} | Order ID: {order.id}")
            return order
        except Exception as e:
            log.error(f"Failed to submit order: {e}")
            return None
    
    def get_positions(self):
        """Get all open positions."""
        try:
            return self.client.get_all_positions()
        except Exception as e:
            log.error(f"Failed to get positions: {e}")
            return []
    
    def close_position(self, ticker):
        """Close position in a ticker."""
        try:
            self.client.close_position(ticker)
            log.info(f"✓ Position closed: {ticker}")
            return True
        except Exception as e:
            log.error(f"Failed to close {ticker}: {e}")
            return False


# ─────────────────────────────────────────────────────────────────────────────
# MAIN BOT CLASS
# ─────────────────────────────────────────────────────────────────────────────

class QQQSQQQBot:
    """
    Main bot orchestrator.
    
    Flow:
      1. Subscribe to 5-min bars on QQQ, SQQQ, SPY
      2. On each bar:
         - Compute indicators
         - Check for entry signal
         - If in position, check for exit signal
         - Log everything to CSV
      3. At 3:50 PM ET, close all positions and stop
    """
    
    def __init__(self):
        self.broker = AlpacaBroker()
        self.indicators = IndicatorEngine()
        self.state = TradeState()
        self.data_queue = queue.Queue()
        self.running = False
        self._shutdown_sent = False  # guards against sending SIGINT more than once
        
        TradeLogger.init_csv_files()
        log.info("QQQ_SQQQ_BOT initialized")
    
    async def on_bar(self, bar_data):
        """
        Callback from Alpaca websocket when a new 5-min bar arrives.
        
        This is the heartbeat of the bot — everything happens here.
        """
        try:
            symbol = bar_data.symbol
            bar_dict = {
                "open": bar_data.open,
                "high": bar_data.high,
                "low": bar_data.low,
                "close": bar_data.close,
                "volume": bar_data.volume,
            }
            
            # Add bar to history
            self.indicators.add_bar(symbol, bar_dict)
            
            # Compute indicators
            indicators = self.indicators.compute(symbol)
            
            # Log the bar
            TradeLogger.log_bar(symbol, bar_dict, indicators)
            
            # Check market hours (before 3:50 PM ET)
            now = datetime.now(EST)

            # Market close (4:00 PM ET): trigger a clean, safe shutdown of the
            # whole process. Reuses the exact same shutdown path as Ctrl+C
            # (KeyboardInterrupt -> finally: close position, stop stream,
            # write EOD report) by sending SIGINT to ourselves, instead of
            # hand-rolling a second, less-tested shutdown path.
            if now.time() >= time(16, 0) and not self._shutdown_sent:
                self._shutdown_sent = True
                log.info("Market closed (4:00 PM ET). Shutting down bot...")
                os.kill(os.getpid(), signal.SIGINT)
                return

            if now.time() >= time(15, 50):
                if self.state.open_position:
                    log.info("📍 Market close approaching, EOD closing all positions")
                    self._close_position_eod()
                self.running = False
                return
            
            # Get all indicators
            ind_qqq = self.indicators.compute("QQQ")
            ind_sqqq = self.indicators.compute("SQQQ")
            ind_spy = self.indicators.compute("SPY")
            
            # CHECK EXITS FIRST
            if self.state.open_position:
                pos = self.state.open_position
                hold_min = (now - pos["entry_time"]).total_seconds() / 60
                # CRITICAL: use the position ticker's price, not the arriving bar's price
                pos_ticker = pos["ticker"]
                current_price = self.indicators.compute(pos_ticker).get("price", 0)
                
                should_exit, exit_reason = SignalDetector.should_exit(
                    current_price, self.state, hold_min
                )
                
                if should_exit:
                    self._execute_exit(current_price, exit_reason)
            
            # CHECK ENTRIES
            else:
                should_enter, ticker, reason = SignalDetector.should_enter(
                    ind_qqq, ind_sqqq, ind_spy, self.state
                )
                
                if should_enter:
                    TradeLogger.log_signal("entry_signal", ticker, reason, ind_qqq, ind_sqqq, ind_spy)
                    self._execute_entry(ticker, ind_qqq if ticker == "QQQ" else ind_sqqq)
                else:
                    # Measurement only: record setups that ALMOST fired.
                    miss_side, miss_reason = SignalDetector.detect_near_miss(
                        ind_qqq, ind_sqqq, ind_spy
                    )
                    if miss_side:
                        TradeLogger.log_near_miss(
                            miss_side, miss_reason, ind_qqq, ind_sqqq, ind_spy
                        )
        
        except Exception as e:
            log.error(f"Error in on_bar: {e}", exc_info=True)
    
    def _execute_entry(self, ticker, indicators):
        """Execute a trade entry."""
        current_price = indicators.get("price", 0)
        if current_price <= 0:
            log.warning(f"Invalid price for {ticker}: {current_price}")
            return
        
        # Calculate position size: $2000 capital / current_price
        qty = int(CAPITAL_PER_TRADE / current_price)
        if qty <= 0:
            log.warning(f"Invalid qty for {ticker}: {qty}")
            return
        
        # Submit order
        order = self.broker.submit_order(ticker, qty, "buy")
        if order:
            # Stash entry ATRs so TradeState.enter() can record them (stop experiment)
            self.state._pending_entry_atr = float(indicators.get("atr", 0.0) or 0.0)
            self.state._pending_entry_atr_daily = fetch_daily_atr(ticker)  # 1 REST call, wrapped
            self.state.enter(ticker, qty, current_price)
            TradeLogger.log_signal("entry_executed", ticker, f"Bought {qty} @ ${current_price:.2f}", 
                                   self.indicators.compute("QQQ"), 
                                   self.indicators.compute("SQQQ"),
                                   self.indicators.compute("SPY"))
    
    def _execute_exit(self, current_price, exit_reason):
        """Execute a trade exit."""
        order = self.broker.submit_order(
            self.state.open_position["ticker"],
            self.state.open_position["qty"],
            "sell"
        )
        if order:
            # Snapshot position BEFORE exit() clears it (needed for stop experiment)
            pos_snapshot = dict(self.state.open_position)
            exit_details = self.state.exit(current_price, exit_reason)
            if exit_details:
                try:
                    TradeLogger.log_trade(exit_details)
                    log.info(f"Daily P&L: ${self.state.daily_pnl:+.2f}")
                except Exception as log_err:
                    log.error(f"TRADE LOG FAILED: {log_err} | details={exit_details}", exc_info=True)
                # Shadow experiment: score all 3 stop rules on this trade (measurement only)
                try:
                    TradeLogger.log_stop_comparison(pos_snapshot, exit_details)
                except Exception as cmp_err:
                    log.error(f"Stop-comparison log failed (non-critical): {cmp_err}", exc_info=True)
    
    def _close_position_eod(self):
        """Close position at end of day."""
        if self.state.open_position:
            current_price = self.indicators.compute(
                self.state.open_position["ticker"]
            ).get("price", 0)
            self._execute_exit(current_price, "EOD close")
    
    def run(self):
        """
        Main bot loop: connect to Alpaca stream and listen for bars.
        """
        log.info("Starting bot...")
        
        # Create websocket stream
        stream = StockDataStream(
            api_key=config.KEY,
            secret_key=config.SECRET,
            feed=DataFeed.IEX  # IEX is faster than SIP
        )
        
        # Subscribe to 5-min bars
        for ticker in TICKERS + [MARKET_PROXY]:
            stream.subscribe_bars(self.on_bar, ticker)
            log.info(f"Subscribed to {ticker} bars")
        
        self.running = True
        
        try:
            stream.run()
        except KeyboardInterrupt:
            log.info("Bot interrupted by user")
        except Exception as e:
            log.error(f"Stream error: {e}", exc_info=True)
        finally:
            self._close_position_eod()
            stream.close()
            log.info("Bot stopped. Check logs/ folder for analysis CSV files.")
            # ── EOD auto-analysis ─────────────────────────────────────────
            # Runs the Phase-1 analyzer against today's logs and writes an
            # HTML report to logs/report_latest.html. Wrapped in try/except so
            # a reporting error can never take down the trading process.
            try:
                import analyzer
                report_path = analyzer.run_analysis(verbose=True)
                log.info(f"📊 EOD report written: {report_path}")
                log.info("   Open logs/report_latest.html in a browser to review.")
            except Exception as e:
                log.error(f"EOD analysis failed (bot itself is fine): {e}", exc_info=True)


# ─────────────────────────────────────────────────────────────────────────────
# POST-SHUTDOWN: OPTIONAL PC SHUTDOWN (Windows only)
# ─────────────────────────────────────────────────────────────────────────────

IDLE_THRESHOLD_SECONDS = 300  # 5 minutes idle = "not actively using the PC"


def get_idle_seconds():
    """Return how many seconds since the last keyboard/mouse input (Windows)."""
    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
    ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii))
    millis_since_boot = ctypes.windll.kernel32.GetTickCount()
    idle_ms = millis_since_boot - lii.dwTime
    return idle_ms / 1000.0


def maybe_shutdown_pc():
    """
    Called only after the bot has fully and safely shut down (position
    closed, logs flushed, EOD report written). Shuts down the PC ONLY if
    the user hasn't touched the mouse/keyboard recently. If the user is
    actively at the machine, skip shutdown entirely and just log it.
    """
    try:
        idle = get_idle_seconds()
    except Exception as e:
        log.error(f"Could not determine idle time, skipping PC shutdown: {e}")
        return

    if idle < IDLE_THRESHOLD_SECONDS:
        log.info(
            f"PC active (idle {idle:.0f}s < {IDLE_THRESHOLD_SECONDS}s threshold). "
            f"Skipping shutdown — you're at the machine."
        )
        return

    log.info(
        f"PC idle {idle:.0f}s >= {IDLE_THRESHOLD_SECONDS}s threshold. "
        f"Shutting down PC in 60s (run 'shutdown /a' to cancel)."
    )
    os.system("shutdown /s /t 60")


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    bot = QQQSQQQBot()
    bot.run()          # blocks until bot fully, safely shuts down (EOD)
    maybe_shutdown_pc()  # only reached after clean shutdown above


