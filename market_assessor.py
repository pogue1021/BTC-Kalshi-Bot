"""
market_assessor.py — Daily BTC Market Assessment
=================================================
Runs twice daily (9 AM and 3 PM local time) to analyze current Bitcoin
market conditions and suggest bot settings. Sends results via Telegram
and updates the dashboard notification banner.

Uses Kraken's free public REST API for historical candle data.
Kraken is one of the four CF Benchmarks constituent exchanges that
Kalshi uses for settlement — same price source as the bot's live feed.
No API key or authentication required.
"""

import logging
import threading
import time
from datetime import datetime
from typing import Optional

import requests

logger = logging.getLogger(__name__)

KRAKEN_OHLC_URL = "https://api.kraken.com/0/public/OHLC"

# Hours (24h local time) when assessments run automatically
ASSESSMENT_HOURS = {8, 10, 12, 14, 16, 18, 20, 22}  # every 2 hours, 8 AM – 10 PM


# ─────────────────────────────────────────────────────────────
# DATA FETCHING
# ─────────────────────────────────────────────────────────────

def fetch_btc_candles(interval: str = "15m", limit: int = 16) -> list:
    """
    Pull recent XBTUSD candlestick data from Kraken's public OHLC API.
    Default: 16 × 15-minute candles = 4 hours of data. No auth needed.
    Kraken is a CF Benchmarks constituent — same source as the bot's live feed.

    Kraken returns candles as:
      [time, open, high, low, close, vwap, volume, count]
    We normalise these into the same positional format calculate_metrics expects:
      [_, open, high, low, close, ...]
    """
    try:
        resp = requests.get(
            KRAKEN_OHLC_URL,
            params={"pair": "XBTUSD", "interval": 15},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("error"):
            logger.warning(f"market_assessor: Kraken API error: {data['error']}")
            return []

        # Kraken nests candles under the pair key in result
        result = data.get("result", {})
        pair_key = next((k for k in result if k != "last"), None)
        if not pair_key:
            logger.warning("market_assessor: No candle data in Kraken response")
            return []

        candles = result[pair_key]
        # Return the most recent `limit` candles (Kraken returns up to 720)
        return candles[-limit:]

    except Exception as e:
        logger.warning(f"market_assessor: Could not fetch Kraken candles: {e}")
        return []


# ─────────────────────────────────────────────────────────────
# METRIC CALCULATION
# ─────────────────────────────────────────────────────────────

def calculate_metrics(candles: list) -> Optional[dict]:
    """
    Compute volatility and trend metrics from raw candle data.
    Returns None if there's not enough data to work with.
    """
    if not candles or len(candles) < 4:
        return None

    try:
        highs  = [float(c[2]) for c in candles]
        lows   = [float(c[3]) for c in candles]
        opens  = [float(c[1]) for c in candles]
        closes = [float(c[4]) for c in candles]

        four_hr_range = max(highs) - min(lows)
        candle_ranges = [h - l for h, l in zip(highs, lows)]
        candle_bodies = [abs(c - o) for c, o in zip(closes, opens)]
        avg_range     = sum(candle_ranges) / len(candle_ranges)
        avg_body      = sum(candle_bodies) / len(candle_bodies)
        current_price = closes[-1]

        # Direction of each candle close vs previous close
        directions = []
        for i in range(1, len(closes)):
            if closes[i] > closes[i - 1]:
                directions.append(1)
            elif closes[i] < closes[i - 1]:
                directions.append(-1)
            else:
                directions.append(0)

        # Choppiness: fraction of candles that reversed the prior direction
        reversals = sum(
            1 for i in range(1, len(directions))
            if directions[i] != 0 and directions[i] != directions[i - 1]
        )
        choppiness = reversals / max(len(directions) - 1, 1)

        # Net price move over the full window
        net_move = closes[-1] - closes[0]

        return {
            "current_price":   current_price,
            "four_hr_range":   four_hr_range,
            "avg_candle_range": avg_range,
            "avg_candle_body":  avg_body,
            "choppiness":      choppiness,   # 0.0 = trending, 1.0 = perfectly choppy
            "net_move":        net_move,     # positive = up, negative = down
        }

    except Exception as e:
        logger.warning(f"market_assessor: Error computing metrics: {e}")
        return None


def categorize_volatility(metrics: dict) -> str:
    """Classify current market conditions into LOW / MODERATE / HIGH / EXTREME."""
    r = metrics["four_hr_range"]
    a = metrics["avg_candle_range"]

    if   r > 600 or a > 120: return "EXTREME"
    elif r > 350 or a > 70:  return "HIGH"
    elif r > 150 or a > 30:  return "MODERATE"
    else:                    return "LOW"


# ─────────────────────────────────────────────────────────────
# REPORT GENERATION
# ─────────────────────────────────────────────────────────────

# Setting suggestions keyed by volatility category.
# These are starting points — the user always has final say via the dashboard.
SUGGESTIONS = {
    "LOW": {
        "momentum_threshold_pct": 0.020,
        "momentum_window_secs":   25,
        "early_min_distance_pct": 0.05,
        "max_trades_per_cycle":   2,
        "sl_min_hold_secs":       90,
        "note": (
            "BTC is calm and range-bound. You can afford slightly looser filters "
            "since noise is low. Late-window fallback works especially well in "
            "stable conditions — obvious outcomes get priced in clearly."
        ),
    },
    "MODERATE": {
        "momentum_threshold_pct": 0.025,
        "momentum_window_secs":   20,
        "early_min_distance_pct": 0.06,
        "max_trades_per_cycle":   2,
        "sl_min_hold_secs":       120,
        "note": (
            "Normal conditions. Your default settings should be close to optimal. "
            "A slightly faster momentum window (20s) will help catch moves "
            "without adding much noise."
        ),
    },
    "HIGH": {
        "momentum_threshold_pct": 0.035,
        "momentum_window_secs":   15,
        "early_min_distance_pct": 0.08,
        "max_trades_per_cycle":   1,
        "sl_min_hold_secs":       150,
        "note": (
            "BTC is moving fast. Raise your momentum threshold to avoid chasing "
            "fakeouts. Limit to 1 trade per cycle — re-entries after stop-losses "
            "are especially risky when price is swinging this hard."
        ),
    },
    "EXTREME": {
        "momentum_threshold_pct": 0.050,
        "momentum_window_secs":   15,
        "early_min_distance_pct": 0.10,
        "max_trades_per_cycle":   1,
        "sl_min_hold_secs":       180,
        "note": (
            "⚠️ Very high volatility. BTC is making large, fast moves. "
            "Consider pausing the bot or trading very conservatively. "
            "Losses can compound quickly in these conditions."
        ),
    },
}

VOLATILITY_EMOJI = {
    "LOW":      "🟢",
    "MODERATE": "🟡",
    "HIGH":     "🟠",
    "EXTREME":  "🔴",
}


def generate_report(metrics: dict) -> tuple[str, dict]:
    """
    Build the plain-text assessment report and return it alongside
    the raw suggestions dict (for the dashboard to display separately
    if needed).

    Returns: (report_text, suggestions_dict)
    """
    category = categorize_volatility(metrics)
    s        = SUGGESTIONS[category]
    emoji    = VOLATILITY_EMOJI[category]

    price = metrics["current_price"]
    r4h   = metrics["four_hr_range"]
    avg_r = metrics["avg_candle_range"]
    chop  = metrics["choppiness"]
    net   = metrics["net_move"]

    # Human-readable market character
    if chop > 0.65:
        character = "choppy / reversing frequently"
    elif abs(net) > r4h * 0.4:
        direction = "trending UP 📈" if net > 0 else "trending DOWN 📉"
        character = direction
    else:
        character = "ranging / no clear direction"

    timestamp = datetime.now().strftime("%I:%M %p")

    lines = [
        f"📊 BTC Market Assessment — {timestamp}",
        f"",
        f"BTC Price:     ${price:,.2f}",
        f"Volatility:    {emoji} {category}",
        f"4hr range:     ${r4h:.0f}   |   Avg 15min candle: ${avg_r:.0f}",
        f"Character:     {character}",
        f"",
        f"💡 {s['note']}",
        f"",
        f"Suggested Settings:",
        f"  • Momentum threshold:    {s['momentum_threshold_pct']:.3f}%",
        f"  • Momentum window:       {s['momentum_window_secs']}s",
        f"  • Early entry distance:  {s['early_min_distance_pct']:.2f}%",
        f"  • Max trades/cycle:      {s['max_trades_per_cycle']}",
        f"  • Stop-loss min hold:    {s['sl_min_hold_secs']}s",
        f"",
        f"These are suggestions only — adjust via the dashboard as you see fit.",
    ]

    return "\n".join(lines), s


# ─────────────────────────────────────────────────────────────
# ASSESSMENT RUNNER
# ─────────────────────────────────────────────────────────────

def run_assessment(notify_fn=None, state=None) -> str:
    """
    Fetch data, compute metrics, generate the report, send it via
    Telegram (if configured), and update the dashboard state.

    Returns the report text so it can be logged or used by callers.
    """
    logger.info("market_assessor: Running BTC market assessment...")

    candles = fetch_btc_candles(interval="15m", limit=16)
    if not candles:
        msg = "⚠️ Market assessment failed — could not reach Binance API."
        logger.warning(msg)
        if notify_fn:
            try:
                notify_fn(msg)
            except Exception:
                pass
        return msg

    metrics = calculate_metrics(candles)
    if not metrics:
        msg = "⚠️ Market assessment failed — not enough price data."
        logger.warning(msg)
        if notify_fn:
            try:
                notify_fn(msg)
            except Exception:
                pass
        return msg

    report, suggestions = generate_report(metrics)

    logger.info(f"market_assessor: Assessment complete.\n{report}")

    # Send via Telegram
    if notify_fn:
        try:
            notify_fn(report)
        except Exception as e:
            logger.warning(f"market_assessor: Telegram send failed: {e}")

    # Update dashboard state
    if state is not None:
        state.last_assessment             = report
        state.last_assessment_time        = time.time()
        state.last_assessment_suggestions = suggestions

    return report


# ─────────────────────────────────────────────────────────────
# BACKGROUND SCHEDULER
# ─────────────────────────────────────────────────────────────

def start_assessor(notify_fn=None, state=None):
    """
    Start the assessment scheduler as a background daemon thread.
    Checks the time every 60 seconds and fires at 9 AM and 3 PM local time.
    Safe to call from main() alongside the reconciler.
    """
    def _loop():
        fired_today = set()   # tracks (date, hour) pairs already fired
        logger.info(
            "market_assessor: Scheduler started — "
            "assessments will run every 2 hours from 8 AM to 10 PM local time."
        )
        while True:
            try:
                now  = datetime.now()
                key  = (now.date(), now.hour)

                if now.hour in ASSESSMENT_HOURS and key not in fired_today:
                    fired_today.add(key)
                    # Prune old dates so the set doesn't grow indefinitely
                    today = now.date()
                    fired_today = {k for k in fired_today if k[0] >= today}
                    run_assessment(notify_fn=notify_fn, state=state)

            except Exception as e:
                logger.error(f"market_assessor: Unexpected scheduler error: {e}")

            time.sleep(60)   # wake once per minute to check the time

    t = threading.Thread(target=_loop, daemon=True, name="market-assessor")
    t.start()
    return t
