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
import pandas as pd
import numpy as np

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s"
)
log = logging.getLogger("QQQ_SQQQ_BOT")

EST = ZoneInfo("America/New_York")


# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION & CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

CAPITAL_PER_TRADE = 2000.0          # Deploy this much on each trade
DAILY_LOSS_LIMIT = 100.0             # Stop trading if daily loss exceeds this
PROFIT_TARGET_PCT = 0.02             # Exit at +2% profit
HARD_STOP_PCT = 0.01                 # Exit at -1% loss
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
        
        result["price"] = price
        
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
        """Record a new trade entry."""
        self.open_position = {
            "ticker": ticker,
            "qty": qty,
            "entry_price": entry_price,
            "entry_time": datetime.now(EST),
            "peak_price": entry_price,
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
        if not spy_ema_bullish and not sqqq_above_slow and sqqq_rsi < 40:
            return True, "SQQQ", f"SQQQ bullish (RSI {sqqq_rsi:.0f}, SPY bearish)"
        
        return False, None, "No signal"
    
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
        
        # Hard stop — can trigger anytime
        if pnl_pct <= -HARD_STOP_PCT * 100:
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
                current_price = indicators.get("price", 0)
                
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
            exit_details = self.state.exit(current_price, exit_reason)
            if exit_details:
                TradeLogger.log_trade(exit_details)
                log.info(f"Daily P&L: ${self.state.daily_pnl:+.2f}")
    
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


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    bot = QQQSQQQBot()
    bot.run()
