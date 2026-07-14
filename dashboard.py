#!/usr/bin/env python3
"""
================================================================================
QQQ_SQQQ_BOT Dashboard — Real-time monitoring
================================================================================

OVERVIEW:
  Flask web app that reads CSV logs and displays live bot status.
  
USAGE:
  python dashboard.py
  Then open: http://localhost:5000
  
  Auto-refreshes every 3 seconds.
  Keep it open while bot runs in another window.

================================================================================
"""

import os
import csv
from datetime import datetime
from zoneinfo import ZoneInfo
from flask import Flask, render_template_string

app = Flask(__name__)
app.jinja_env.globals.update(float=float, int=int)
EST = ZoneInfo("America/New_York")

# Paths to CSV files
LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")
TRADES_CSV = os.path.join(LOG_DIR, "trades.csv")
SIGNALS_CSV = os.path.join(LOG_DIR, "signals.csv")
BARS_CSV = os.path.join(LOG_DIR, "bars.csv")


# ─────────────────────────────────────────────────────────────────────────────
# DATA READERS
# ─────────────────────────────────────────────────────────────────────────────

def read_csv(filepath):
    """Read CSV file and return list of dicts."""
    if not os.path.exists(filepath):
        return []
    
    rows = []
    try:
        with open(filepath, "r", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(row)
    except Exception as e:
        print(f"Error reading {filepath}: {e}")
    
    return rows


def get_current_position():
    """
    Check if bot is currently in a position.
    Returns: {ticker, entry_price, entry_time, pnl, pnl_pct} or None
    """
    trades = read_csv(TRADES_CSV)
    if not trades:
        return None
    
    # Last trade (most recent)
    last_trade = trades[-1]
    
    # If last trade has exit_price, position is closed
    if last_trade.get("exit_price") and float(last_trade["exit_price"]) > 0:
        return None
    
    # Position is open (no exit yet)
    return last_trade


def get_daily_pnl():
    """Calculate total P&L for today."""
    trades = read_csv(TRADES_CSV)
    if not trades:
        return 0.0
    
    total = 0.0
    for trade in trades:
        try:
            pnl = float(trade.get("pnl", 0))
            total += pnl
        except:
            pass
    
    return total


def get_last_trades(n=5):
    """Get last N closed trades."""
    trades = read_csv(TRADES_CSV)
    if not trades:
        return []
    
    # Filter only closed trades (have exit_price)
    closed = [t for t in trades if t.get("exit_price") and float(t["exit_price"]) > 0]
    
    return closed[-n:]


def get_last_signals(n=10):
    """Get last N signals."""
    signals = read_csv(SIGNALS_CSV)
    if not signals:
        return []
    
    return signals[-n:]


def get_bar_count():
    """Count total bars logged."""
    bars = read_csv(BARS_CSV)
    return len(bars)


# ─────────────────────────────────────────────────────────────────────────────
# HTML TEMPLATE
# ─────────────────────────────────────────────────────────────────────────────

HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta http-equiv="refresh" content="3">
    <title>QQQ_SQQQ_BOT Dashboard</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: 'Monaco', 'Courier New', monospace;
            background: #0a0e27;
            color: #e0e0e0;
            padding: 20px;
            line-height: 1.6;
        }
        
        .container {
            max-width: 1400px;
            margin: 0 auto;
        }
        
        h1 {
            text-align: center;
            color: #00ff88;
            margin-bottom: 30px;
            font-size: 28px;
            text-shadow: 0 0 10px #00ff88;
        }
        
        .timestamp {
            text-align: center;
            color: #666;
            font-size: 12px;
            margin-bottom: 20px;
        }
        
        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(400px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }
        
        .card {
            background: #1a1f3a;
            border: 1px solid #00ff88;
            border-radius: 8px;
            padding: 20px;
            box-shadow: 0 0 10px rgba(0, 255, 136, 0.1);
        }
        
        .card h2 {
            color: #00ff88;
            font-size: 14px;
            text-transform: uppercase;
            margin-bottom: 15px;
            border-bottom: 1px solid #00ff88;
            padding-bottom: 10px;
        }
        
        .card-content {
            font-size: 13px;
        }
        
        .stat-row {
            display: flex;
            justify-content: space-between;
            margin-bottom: 8px;
            padding: 5px 0;
        }
        
        .stat-label {
            color: #888;
        }
        
        .stat-value {
            color: #fff;
            font-weight: bold;
        }
        
        .positive {
            color: #00ff88;
        }
        
        .negative {
            color: #ff4444;
        }
        
        .neutral {
            color: #ffaa00;
        }
        
        .position-open {
            background: #1a3a1a;
            border-color: #00ff88;
        }
        
        .position-closed {
            background: #1a1f3a;
            border-color: #666;
        }
        
        table {
            width: 100%;
            border-collapse: collapse;
            font-size: 12px;
            margin-top: 10px;
        }
        
        th {
            background: #0a0e27;
            color: #00ff88;
            padding: 10px;
            text-align: left;
            border-bottom: 1px solid #00ff88;
            font-weight: bold;
        }
        
        td {
            padding: 8px 10px;
            border-bottom: 1px solid #333;
        }
        
        tr:hover {
            background: #252a3a;
        }
        
        .no-data {
            color: #666;
            font-style: italic;
            text-align: center;
            padding: 20px;
        }
        
        .status-indicator {
            display: inline-block;
            width: 10px;
            height: 10px;
            border-radius: 50%;
            margin-right: 8px;
        }
        
        .status-trading {
            background: #00ff88;
            box-shadow: 0 0 5px #00ff88;
        }
        
        .status-idle {
            background: #666;
        }
        
        .footer {
            text-align: center;
            color: #666;
            font-size: 11px;
            margin-top: 30px;
            padding-top: 20px;
            border-top: 1px solid #333;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>🤖 QQQ_SQQQ_BOT Dashboard</h1>
        
        <div class="timestamp">
            Last updated: {{ now }} EST
            <br>
            <span class="status-indicator status-{{ 'trading' if current_position else 'idle' }}"></span>
            {{ 'IN POSITION' if current_position else 'IDLE' }}
        </div>
        
        <div class="grid">
            <!-- CURRENT POSITION CARD -->
            <div class="card {{ 'position-open' if current_position else 'position-closed' }}">
                <h2>📍 Current Position</h2>
                <div class="card-content">
                    {% if current_position %}
                        <div class="stat-row">
                            <span class="stat-label">Ticker:</span>
                            <span class="stat-value">{{ current_position.ticker }}</span>
                        </div>
                        <div class="stat-row">
                            <span class="stat-label">Entry Price:</span>
                            <span class="stat-value">${{ current_position.entry_price }}</span>
                        </div>
                        <div class="stat-row">
                            <span class="stat-label">Entry Time:</span>
                            <span class="stat-value">{{ current_position.entry_time }}</span>
                        </div>
                        <div class="stat-row">
                            <span class="stat-label">Current P&L:</span>
                            <span class="stat-value {{ 'positive' if float(current_position.pnl or 0) > 0 else 'negative' }}">
                                ${{ current_position.pnl or '0.00' }}
                            </span>
                        </div>
                        <div class="stat-row">
                            <span class="stat-label">Return:</span>
                            <span class="stat-value {{ 'positive' if float(current_position.pnl_pct or 0) > 0 else 'negative' }}">
                                {{ current_position.pnl_pct or '0.00' }}%
                            </span>
                        </div>
                    {% else %}
                        <div class="no-data">No open position</div>
                    {% endif %}
                </div>
            </div>
            
            <!-- DAILY P&L CARD -->
            <div class="card">
                <h2>💰 Daily P&L</h2>
                <div class="card-content">
                    <div class="stat-row">
                        <span class="stat-label">Total P&L:</span>
                        <span class="stat-value {{ 'positive' if daily_pnl > 0 else 'negative' if daily_pnl < 0 else 'neutral' }}">
                            ${{ '%.2f' % daily_pnl }}
                        </span>
                    </div>
                    <div class="stat-row">
                        <span class="stat-label">Trades Closed:</span>
                        <span class="stat-value">{{ last_trades|length }}</span>
                    </div>
                    <div class="stat-row">
                        <span class="stat-label">Bars Logged:</span>
                        <span class="stat-value">{{ bar_count }}</span>
                    </div>
                    <div class="stat-row">
                        <span class="stat-label">Capital Remaining:</span>
                        <span class="stat-value">$100,000</span>
                    </div>
                </div>
            </div>
        </div>
        
        <!-- LAST TRADES TABLE -->
        <div class="card">
            <h2>📊 Last 5 Trades</h2>
            {% if last_trades %}
                <table>
                    <thead>
                        <tr>
                            <th>Ticker</th>
                            <th>Entry</th>
                            <th>Exit</th>
                            <th>P&L</th>
                            <th>Return %</th>
                            <th>Hold (min)</th>
                            <th>Exit Reason</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for trade in last_trades|reverse %}
                            <tr>
                                <td><strong>{{ trade.ticker }}</strong></td>
                                <td>${{ trade.entry_price }}</td>
                                <td>${{ trade.exit_price }}</td>
                                <td class="{{ 'positive' if float(trade.pnl) > 0 else 'negative' }}">
                                    ${{ trade.pnl }}
                                </td>
                                <td class="{{ 'positive' if float(trade.pnl_pct) > 0 else 'negative' }}">
                                    {{ trade.pnl_pct }}%
                                </td>
                                <td>{{ '%.1f' % float(trade.hold_minutes) }}</td>
                                <td>{{ trade.exit_reason }}</td>
                            </tr>
                        {% endfor %}
                    </tbody>
                </table>
            {% else %}
                <div class="no-data">No trades yet</div>
            {% endif %}
        </div>
        
        <!-- SIGNAL HISTORY TABLE -->
        <div class="card">
            <h2>🔔 Last 10 Signals</h2>
            {% if last_signals %}
                <table>
                    <thead>
                        <tr>
                            <th>Time</th>
                            <th>Type</th>
                            <th>Ticker</th>
                            <th>Reason</th>
                            <th>QQQ RSI</th>
                            <th>SQQQ RSI</th>
                            <th>SPY Bullish?</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for signal in last_signals|reverse %}
                            <tr>
                                <td>{{ signal.timestamp[-8:] }}</td>
                                <td><strong>{{ signal.signal_type }}</strong></td>
                                <td>{{ signal.ticker }}</td>
                                <td>{{ signal.reason[:40] }}</td>
                                <td>{{ signal.qqq_rsi }}</td>
                                <td>{{ signal.sqqq_rsi }}</td>
                                <td>{{ signal.spy_ema_bullish }}</td>
                            </tr>
                        {% endfor %}
                    </tbody>
                </table>
            {% else %}
                <div class="no-data">No signals yet</div>
            {% endif %}
        </div>
        
        <div class="footer">
            <p>QQQ_SQQQ_BOT v1.0 — Auto-refresh every 3 seconds</p>
            <p>Keep this open while bot runs in another window</p>
        </div>
    </div>
</body>
</html>
"""


# ─────────────────────────────────────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/")
def dashboard():
    """Main dashboard route."""
    now = datetime.now(EST).strftime("%Y-%m-%d %H:%M:%S")
    
    current_position = get_current_position()
    daily_pnl = get_daily_pnl()
    last_trades = get_last_trades(5)
    last_signals = get_last_signals(10)
    bar_count = get_bar_count()
    
    return render_template_string(
        HTML_TEMPLATE,
        now=now,
        current_position=current_position,
        daily_pnl=daily_pnl,
        last_trades=last_trades,
        last_signals=last_signals,
        bar_count=bar_count,
    )


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("🚀 Starting QQQ_SQQQ_BOT Dashboard...")
    print("📱 Open browser: http://localhost:5000")
    print("⏱️  Auto-refreshes every 3 seconds")
    print("💾 Reading from: logs/ folder")
    print("\nPress Ctrl+C to stop\n")
    
    app.run(host="localhost", port=5000, debug=False)
