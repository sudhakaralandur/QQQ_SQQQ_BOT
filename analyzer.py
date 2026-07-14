#!/usr/bin/env python3
"""
================================================================================
QQQ_SQQQ_BOT Analyzer — Phase 1 (Measurement)
================================================================================

WHAT THIS DOES:
  Reads the CSV logs produced by the bot and computes performance statistics,
  then writes an HTML report you can open in a browser.

  This is PURELY MEASUREMENT. It does NOT change any bot parameters.
  It tells you what happened; YOU decide what to change.

WHY MEASUREMENT-ONLY (read this):
  A bot that auto-tunes itself on a handful of recent trades will overfit to
  noise and confidently trade itself into losses. The MR bot already showed
  this — RSI was tuned to a range that only looked good because AMD was in a
  once-in-a-lifetime rally. The discipline that works is: measure rigorously,
  review on a real sample, change deliberately. This script is the "measure"
  step. Proposals (Phase 2) and any auto-adjust (Phase 3) come later, only
  after there's enough real data to trust.

WHAT IT REPORTS:
  - Core stats: total P&L, win rate, profit factor, avg win / avg loss
  - Best and worst trades
  - Breakdown by ticker (QQQ vs SQQQ)
  - Breakdown by exit reason (profit target vs hard stop vs EOD)
  - Breakdown by time of day (morning vs midday vs afternoon)
  - Entry RSI buckets — did high-RSI or low-RSI entries do better?
  - A plain-English "observations" section (NOT auto-applied changes)

HOW IT RUNS:
  - Automatically: the bot calls run_analysis() at EOD shutdown.
  - Manually:      python analyzer.py   (analyzes whatever is in logs/)

OUTPUT:
  logs/report_YYYY-MM-DD.html   (one per day)
  logs/report_latest.html       (always the most recent)

IMPORTANT ABOUT SAMPLE SIZE:
  Every number here is only as trustworthy as the number of trades behind it.
  Two trades tell you almost nothing. The report labels low-sample sections so
  you don't over-read them. Do not tune on a day or two of data.

================================================================================
"""

import os
import csv
from datetime import datetime
from zoneinfo import ZoneInfo
from collections import defaultdict

EST = ZoneInfo("America/New_York")

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
TRADES_CSV = os.path.join(LOG_DIR, "trades.csv")
SIGNALS_CSV = os.path.join(LOG_DIR, "signals.csv")
BARS_CSV = os.path.join(LOG_DIR, "bars.csv")

# Below this many trades, a statistic is flagged as low-confidence.
MIN_SAMPLE_FOR_CONFIDENCE = 20


# ─────────────────────────────────────────────────────────────────────────────
# CSV LOADING
# ─────────────────────────────────────────────────────────────────────────────

def _read_csv(filepath):
    """Read a CSV into a list of dicts. Returns [] if missing/empty."""
    if not os.path.exists(filepath):
        return []
    rows = []
    try:
        with open(filepath, "r", newline="") as f:
            for row in csv.DictReader(f):
                rows.append(row)
    except Exception as e:
        print(f"  ! Error reading {filepath}: {e}")
    return rows


def _to_float(value, default=0.0):
    """Safely convert a CSV string to float."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def load_trades():
    """
    Load closed trades only. A trade is 'closed' if it has a real exit_price.
    Returns a list of normalized dicts with numeric fields already converted.
    """
    raw = _read_csv(TRADES_CSV)
    trades = []
    for r in raw:
        exit_price = _to_float(r.get("exit_price"))
        if exit_price <= 0:
            continue  # still open, skip
        trades.append({
            "entry_time": r.get("entry_time", ""),
            "exit_time": r.get("exit_time", ""),
            "ticker": r.get("ticker", ""),
            "qty": int(_to_float(r.get("qty"))),
            "entry_price": _to_float(r.get("entry_price")),
            "exit_price": exit_price,
            "pnl": _to_float(r.get("pnl")),
            "pnl_pct": _to_float(r.get("pnl_pct")),
            "hold_minutes": _to_float(r.get("hold_minutes")),
            "exit_reason": r.get("exit_reason", ""),
        })
    return trades


# ─────────────────────────────────────────────────────────────────────────────
# CORE STATISTICS
# ─────────────────────────────────────────────────────────────────────────────

def core_stats(trades):
    """
    Compute the headline numbers.

    profit_factor = gross profit / gross loss. Above 1.0 means the winners
    outweigh the losers in dollar terms. It's a better single number than
    win rate, because a bot can win 70% of the time and still lose money if
    the losses are big.
    """
    if not trades:
        return None

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] < 0]
    scratches = [t for t in trades if t["pnl"] == 0]

    gross_profit = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))

    total_pnl = sum(t["pnl"] for t in trades)
    win_rate = (len(wins) / len(trades) * 100) if trades else 0.0
    avg_win = (gross_profit / len(wins)) if wins else 0.0
    avg_loss = (gross_loss / len(losses)) if losses else 0.0

    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    elif gross_profit > 0:
        profit_factor = float("inf")  # winners, no losers yet
    else:
        profit_factor = 0.0

    # Expectancy: average dollars per trade. The number that actually compounds.
    expectancy = total_pnl / len(trades) if trades else 0.0

    best = max(trades, key=lambda t: t["pnl"])
    worst = min(trades, key=lambda t: t["pnl"])

    avg_hold = sum(t["hold_minutes"] for t in trades) / len(trades)

    return {
        "num_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "scratches": len(scratches),
        "total_pnl": total_pnl,
        "win_rate": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "profit_factor": profit_factor,
        "expectancy": expectancy,
        "avg_hold_minutes": avg_hold,
        "best": best,
        "worst": worst,
        "low_confidence": len(trades) < MIN_SAMPLE_FOR_CONFIDENCE,
    }


def group_pnl(trades, key_func):
    """
    Generic grouping: bucket trades by key_func(trade) and summarize each bucket.
    Returns {bucket_name: {count, total_pnl, win_rate, avg_pnl}}.
    """
    buckets = defaultdict(list)
    for t in trades:
        buckets[key_func(t)].append(t)

    out = {}
    for name, group in buckets.items():
        wins = sum(1 for t in group if t["pnl"] > 0)
        total = sum(t["pnl"] for t in group)
        out[name] = {
            "count": len(group),
            "total_pnl": total,
            "win_rate": (wins / len(group) * 100) if group else 0.0,
            "avg_pnl": total / len(group) if group else 0.0,
        }
    return out


def _time_of_day_bucket(trade):
    """Classify a trade by when it was entered."""
    ts = trade["entry_time"]
    try:
        hour = datetime.fromisoformat(ts).astimezone(EST).hour
    except Exception:
        return "unknown"
    if hour < 11:
        return "morning (9:30-11)"
    elif hour < 14:
        return "midday (11-2)"
    else:
        return "afternoon (2-4)"


def entry_rsi_buckets(trades):
    """
    Match each trade to the entry signal that preceded it, then bucket by the
    RSI of the traded ticker at entry. This is how we'll eventually learn
    whether high-RSI or low-RSI entries pay off — but only with enough trades.

    We match on ticker + nearest timestamp in signals.csv where an entry was
    executed. It's approximate; good enough for a directional read.
    """
    signals = _read_csv(SIGNALS_CSV)
    entry_signals = [s for s in signals if "entry" in s.get("signal_type", "").lower()]

    def rsi_for(trade):
        tkr = trade["ticker"]
        # find the entry signal for this ticker closest in time before exit
        best = None
        for s in entry_signals:
            if s.get("ticker") != tkr:
                continue
            best = s  # signals are chronological; last matching wins
        if not best:
            return None
        col = "qqq_rsi" if tkr == "QQQ" else "sqqq_rsi"
        return _to_float(best.get(col))

    buckets = defaultdict(list)
    for t in trades:
        rsi = rsi_for(t)
        if rsi is None:
            buckets["unknown"].append(t)
        elif rsi < 40:
            buckets["RSI <40"].append(t)
        elif rsi < 55:
            buckets["RSI 40-55"].append(t)
        elif rsi < 70:
            buckets["RSI 55-70"].append(t)
        else:
            buckets["RSI 70+"].append(t)

    out = {}
    for name, group in buckets.items():
        wins = sum(1 for x in group if x["pnl"] > 0)
        total = sum(x["pnl"] for x in group)
        out[name] = {
            "count": len(group),
            "total_pnl": total,
            "win_rate": (wins / len(group) * 100) if group else 0.0,
            "avg_pnl": total / len(group) if group else 0.0,
        }
    return out


# ─────────────────────────────────────────────────────────────────────────────
# OBSERVATIONS (plain-English, NOT auto-applied)
# ─────────────────────────────────────────────────────────────────────────────

def build_observations(stats, by_ticker, by_reason, by_rsi):
    """
    Turn the numbers into human-readable observations. These are prompts for a
    human to think about — deliberately NOT changes the bot makes on its own.
    Everything here is hedged by sample size.
    """
    notes = []

    if stats is None:
        return ["No closed trades yet. Run the bot during market hours, then "
                "come back. Nothing to learn from an empty log."]

    n = stats["num_trades"]

    if stats["low_confidence"]:
        notes.append(
            f"⚠ Only {n} closed trade(s). This is far too few to conclude "
            f"anything. Treat every number below as a rough sketch, not a "
            f"signal. Do not change parameters on this sample."
        )

    # Profit factor read
    pf = stats["profit_factor"]
    if pf == float("inf"):
        notes.append("All trades so far are winners — no losses to weigh against. "
                     "Nice, but wait for losing trades before believing it.")
    elif pf >= 1.5:
        notes.append(f"Profit factor {pf:.2f}: winners outweigh losers comfortably "
                     f"(on this sample).")
    elif pf >= 1.0:
        notes.append(f"Profit factor {pf:.2f}: marginally profitable. Thin edge.")
    else:
        notes.append(f"Profit factor {pf:.2f}: losing money — losses outweigh wins.")

    # Win rate vs avg win/loss shape
    if stats["avg_loss"] > 0 and stats["avg_win"] > 0:
        ratio = stats["avg_win"] / stats["avg_loss"]
        notes.append(
            f"Average win ${stats['avg_win']:.2f} vs average loss "
            f"${stats['avg_loss']:.2f} (ratio {ratio:.2f}). "
            + ("Winners bigger than losers — the exit structure is working."
               if ratio >= 1
               else "Losers bigger than winners — the 2%/1% target/stop may be "
                    "cutting winners short or letting losers run to the stop too "
                    "often.")
        )

    # Exit-reason read — is the profit target ever actually hit?
    if by_reason:
        target_hits = sum(v["count"] for k, v in by_reason.items()
                          if "profit" in k.lower())
        stop_hits = sum(v["count"] for k, v in by_reason.items()
                        if "stop" in k.lower())
        eod_hits = sum(v["count"] for k, v in by_reason.items()
                       if "eod" in k.lower())
        if stop_hits > target_hits and n >= 5:
            notes.append(
                f"Hard stops ({stop_hits}) are firing more than profit targets "
                f"({target_hits}). Either entries are poorly timed or the 1% stop "
                f"is too tight for QQQ/SQQQ's normal wiggle. Worth examining once "
                f"the sample is bigger."
            )
        if eod_hits and eod_hits >= target_hits and n >= 5:
            notes.append(
                f"{eod_hits} trade(s) closed at EOD rather than hitting a target — "
                f"positions are drifting sideways. The 2% target may be too far for "
                f"a single session, or entries are late."
            )

    # Ticker skew
    if by_ticker and len(by_ticker) >= 2:
        qqq = by_ticker.get("QQQ", {})
        sqqq = by_ticker.get("SQQQ", {})
        if qqq and sqqq:
            notes.append(
                f"QQQ: {qqq['count']} trades, ${qqq['total_pnl']:.2f} total. "
                f"SQQQ: {sqqq['count']} trades, ${sqqq['total_pnl']:.2f} total. "
                + ("Both directions contributing." if qqq['total_pnl'] > 0 and sqqq['total_pnl'] > 0
                   else "One side is dragging — but confirm with more trades before "
                        "disabling a direction.")
            )

    # RSI read — explicitly hedged, this is the MR-bot overfitting trap
    if by_rsi:
        known = {k: v for k, v in by_rsi.items() if k != "unknown" and v["count"] > 0}
        if known and n >= MIN_SAMPLE_FOR_CONFIDENCE:
            best_bucket = max(known.items(), key=lambda kv: kv[1]["avg_pnl"])
            notes.append(
                f"Entries in {best_bucket[0]} have the best average P&L so far "
                f"(${best_bucket[1]['avg_pnl']:.2f} over {best_bucket[1]['count']} "
                f"trades). Candidate to investigate — NOT a reason to hard-code the "
                f"RSI filter yet. Remember the MR bot overfit exactly this way."
            )
        elif known:
            notes.append(
                "RSI-bucket P&L is shown below, but there aren't enough trades to "
                "read it. Do not tune the RSI filter on this."
            )

    notes.append(
        "None of the above has been applied to the bot. These are observations "
        "for you to review. Parameter changes stay manual until there's a real "
        "sample and a human decision."
    )
    return notes


# ─────────────────────────────────────────────────────────────────────────────
# HTML REPORT
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_money(x):
    return f"${x:,.2f}"


def _pf_display(pf):
    return "∞" if pf == float("inf") else f"{pf:.2f}"


def _table_from_groups(title, groups, name_header):
    """Render a grouping dict as an HTML table sorted by total P&L."""
    if not groups:
        return f"<h2>{title}</h2><p class='nodata'>No data.</p>"
    rows = ""
    for name, v in sorted(groups.items(), key=lambda kv: kv[1]["total_pnl"], reverse=True):
        pnl_cls = "pos" if v["total_pnl"] > 0 else "neg" if v["total_pnl"] < 0 else "neu"
        rows += (
            f"<tr><td>{name}</td>"
            f"<td>{v['count']}</td>"
            f"<td>{v['win_rate']:.0f}%</td>"
            f"<td class='{pnl_cls}'>{_fmt_money(v['total_pnl'])}</td>"
            f"<td class='{pnl_cls}'>{_fmt_money(v['avg_pnl'])}</td></tr>"
        )
    return f"""
    <h2>{title}</h2>
    <table>
      <thead><tr><th>{name_header}</th><th>Trades</th><th>Win %</th>
             <th>Total P&amp;L</th><th>Avg P&amp;L</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
    """


def render_html(stats, by_ticker, by_reason, by_tod, by_rsi, observations):
    """Assemble the full HTML report as a string."""
    generated = datetime.now(EST).strftime("%Y-%m-%d %H:%M:%S")

    if stats is None:
        headline = "<p class='nodata'>No closed trades yet.</p>"
    else:
        pnl_cls = "pos" if stats["total_pnl"] > 0 else "neg" if stats["total_pnl"] < 0 else "neu"
        conf = ("<span class='badge warn'>LOW SAMPLE</span>"
                if stats["low_confidence"] else
                "<span class='badge ok'>SAMPLE OK</span>")
        headline = f"""
        <div class="stat-grid">
          <div class="stat"><div class="k">Total P&amp;L</div>
               <div class="v {pnl_cls}">{_fmt_money(stats['total_pnl'])}</div></div>
          <div class="stat"><div class="k">Trades</div>
               <div class="v">{stats['num_trades']} {conf}</div></div>
          <div class="stat"><div class="k">Win Rate</div>
               <div class="v">{stats['win_rate']:.0f}%</div></div>
          <div class="stat"><div class="k">Profit Factor</div>
               <div class="v">{_pf_display(stats['profit_factor'])}</div></div>
          <div class="stat"><div class="k">Expectancy / trade</div>
               <div class="v {pnl_cls}">{_fmt_money(stats['expectancy'])}</div></div>
          <div class="stat"><div class="k">Avg Win</div>
               <div class="v pos">{_fmt_money(stats['avg_win'])}</div></div>
          <div class="stat"><div class="k">Avg Loss</div>
               <div class="v neg">{_fmt_money(stats['avg_loss'])}</div></div>
          <div class="stat"><div class="k">Avg Hold</div>
               <div class="v">{stats['avg_hold_minutes']:.0f} min</div></div>
        </div>
        <div class="extremes">
          <div><strong>Best:</strong> {stats['best']['ticker']}
               {_fmt_money(stats['best']['pnl'])}
               ({stats['best']['pnl_pct']:.2f}%, {stats['best']['exit_reason']})</div>
          <div><strong>Worst:</strong> {stats['worst']['ticker']}
               {_fmt_money(stats['worst']['pnl'])}
               ({stats['worst']['pnl_pct']:.2f}%, {stats['worst']['exit_reason']})</div>
        </div>
        """

    obs_html = "".join(f"<li>{o}</li>" for o in observations)

    tables = ""
    if stats is not None:
        tables += _table_from_groups("By Ticker", by_ticker, "Ticker")
        tables += _table_from_groups("By Exit Reason", by_reason, "Exit reason")
        tables += _table_from_groups("By Time of Day", by_tod, "Session")
        tables += _table_from_groups("By Entry RSI (approximate)", by_rsi, "RSI bucket")

    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<title>QQQ_SQQQ_BOT — Performance Report</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif;
          background:#0f1226; color:#e6e6e6; margin:0; padding:32px; }}
  h1 {{ color:#00e08a; margin:0 0 4px; }}
  .sub {{ color:#8a8fb0; font-size:13px; margin-bottom:24px; }}
  h2 {{ color:#00e08a; font-size:15px; text-transform:uppercase;
        letter-spacing:.5px; margin:28px 0 10px; border-bottom:1px solid #2a2f52;
        padding-bottom:6px; }}
  .stat-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr));
                gap:14px; margin-bottom:18px; }}
  .stat {{ background:#181c3a; border:1px solid #2a2f52; border-radius:10px;
           padding:14px; }}
  .stat .k {{ color:#8a8fb0; font-size:12px; }}
  .stat .v {{ font-size:22px; font-weight:700; margin-top:4px; }}
  .pos {{ color:#00e08a; }} .neg {{ color:#ff5c6c; }} .neu {{ color:#ffb020; }}
  .extremes {{ display:flex; gap:24px; flex-wrap:wrap; color:#c9ccea;
               font-size:13px; margin-bottom:8px; }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; margin-bottom:8px; }}
  th {{ text-align:left; color:#00e08a; border-bottom:1px solid #2a2f52; padding:8px; }}
  td {{ padding:8px; border-bottom:1px solid #20244a; }}
  .obs {{ background:#181c3a; border:1px solid #2a2f52; border-radius:10px;
          padding:16px 16px 16px 34px; }}
  .obs li {{ margin-bottom:10px; line-height:1.5; }}
  .badge {{ font-size:10px; padding:2px 8px; border-radius:10px; vertical-align:middle; }}
  .badge.warn {{ background:#4a2b00; color:#ffb020; }}
  .badge.ok {{ background:#08351f; color:#00e08a; }}
  .nodata {{ color:#8a8fb0; font-style:italic; }}
  .disclaimer {{ margin-top:28px; color:#8a8fb0; font-size:12px;
                 border-top:1px solid #2a2f52; padding-top:14px; }}
</style></head>
<body>
  <h1>QQQ_SQQQ_BOT — Performance Report</h1>
  <div class="sub">Generated {generated} EST · measurement only, no parameters were changed</div>
  {headline}
  <h2>Observations</h2>
  <ul class="obs">{obs_html}</ul>
  {tables}
  <div class="disclaimer">
    This report measures past logged trades. It does not predict future results
    and it does not modify the bot. Small samples are unreliable — wait for a
    meaningful number of trades before drawing conclusions or changing anything.
  </div>
</body></html>"""


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def run_analysis(verbose=True):
    """
    Run the full analysis and write HTML reports. Safe to call anytime — if
    there are no trades yet it writes an 'empty' report and returns cleanly.
    Returns the path to the latest report.
    """
    if verbose:
        print("Analyzing logs...")

    trades = load_trades()
    stats = core_stats(trades)

    by_ticker = group_pnl(trades, lambda t: t["ticker"]) if trades else {}
    by_reason = group_pnl(trades, lambda t: t["exit_reason"] or "unknown") if trades else {}
    by_tod = group_pnl(trades, _time_of_day_bucket) if trades else {}
    by_rsi = entry_rsi_buckets(trades) if trades else {}

    observations = build_observations(stats, by_ticker, by_reason, by_rsi)
    html = render_html(stats, by_ticker, by_reason, by_tod, by_rsi, observations)

    os.makedirs(LOG_DIR, exist_ok=True)
    dated = os.path.join(LOG_DIR, f"report_{datetime.now(EST):%Y-%m-%d}.html")
    latest = os.path.join(LOG_DIR, "report_latest.html")
    for path in (dated, latest):
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)

    if verbose:
        if stats:
            print(f"  {stats['num_trades']} trades | "
                  f"P&L {_fmt_money(stats['total_pnl'])} | "
                  f"win {stats['win_rate']:.0f}% | "
                  f"PF {_pf_display(stats['profit_factor'])}")
            if stats["low_confidence"]:
                print("  ⚠ Low sample — do not tune on this.")
        else:
            print("  No closed trades yet — wrote an empty report.")
        print(f"  Report: {latest}")

    return latest


if __name__ == "__main__":
    run_analysis(verbose=True)
