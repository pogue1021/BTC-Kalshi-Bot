"""
backtester.py — Standalone Kalshi 15-min BTC Strategy Backtester
=================================================================
Tests your current bot settings (and variations) against historical
Kalshi KXBTC15M markets. Uses Kraken's public API for BTC price history
and your existing Kalshi client for market data.

Run from the bot folder:
    python backtester.py

No changes are made to the live bot. Output is a performance report only.
"""

import os
import sys
import time
import yaml
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

# ─────────────────────────────────────────────────────────────
# CONFIG + CLIENT SETUP
# ─────────────────────────────────────────────────────────────

BOT_DIR = Path(__file__).resolve().parent


def load_config() -> dict:
    load_dotenv(BOT_DIR / ".env")
    with open(BOT_DIR / "config.yaml") as f:
        cfg = yaml.safe_load(f)
    kalshi = cfg.setdefault("kalshi", {})
    kalshi["api_key_id"]       = os.environ.get("KALSHI_API_KEY_ID", kalshi.get("api_key_id", ""))
    kalshi["private_key_path"] = os.environ.get("KALSHI_PRIVATE_KEY_PATH", kalshi.get("private_key_path", ""))
    return cfg


def make_kalshi_client(config: dict):
    from kalshi_client import load_client_from_config
    return load_client_from_config(config)


# ─────────────────────────────────────────────────────────────
# KALSHI DATA FETCHING
# ─────────────────────────────────────────────────────────────

def fetch_settled_markets(client, days: int = 30) -> list:
    """
    Pull settled KXBTC15M markets from the last N days.
    Returns a list of dicts with ticker, floor_strike, close_time, result.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    markets = []
    cursor  = None

    print(f"  Fetching settled markets (last {days} days)...", end="", flush=True)

    while True:
        params = {
            "series_ticker": "KXBTC15M",
            "status":        "settled",
            "limit":         200,
        }
        if cursor:
            params["cursor"] = cursor

        try:
            data = client._get("/markets", params=params)
        except Exception as e:
            print(f"\n  WARNING: Could not fetch markets page: {e}")
            break

        page = data.get("markets", [])
        if not page:
            break

        for m in page:
            close_str = m.get("close_time", "")
            if not close_str:
                continue
            try:
                close_dt = datetime.fromisoformat(close_str.replace("Z", "+00:00"))
            except ValueError:
                continue

            if close_dt < cutoff:
                # Markets are returned newest-first; once we hit old ones we're done
                print(f" done. ({len(markets)} markets)")
                return markets

            # Extract strike price
            strike = m.get("floor_strike") or m.get("cap_strike")
            if strike is None:
                subtitle = m.get("subtitle", "") or m.get("title", "")
                match = re.search(r'\$([0-9,]+\.?\d*)', subtitle)
                if match:
                    strike = float(match.group(1).replace(",", ""))
            if strike is None:
                continue

            result = m.get("result", "")  # "yes" or "no"
            if result not in ("yes", "no"):
                continue

            markets.append({
                "ticker":     m.get("ticker", ""),
                "strike":     float(strike),
                "close_time": close_dt,
                "result":     result,
            })

        cursor = data.get("cursor")
        if not cursor:
            break
        time.sleep(0.2)  # be polite to the API

    print(f" done. ({len(markets)} markets)")
    return markets


def fetch_market_yes_prices(client, ticker: str) -> list:
    """
    Fetch historical trades for a market to reconstruct YES price over time.
    Returns list of (timestamp_utc, yes_price_cents) sorted by time.
    """
    try:
        data   = client._get("/historical/trades", params={"ticker": ticker, "limit": 1000})
        trades = data.get("trades", [])
        result = []
        for t in trades:
            ts_str = t.get("created_time", "") or t.get("ts", "")
            if not ts_str:
                continue
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            except ValueError:
                continue
            price = t.get("yes_price") or t.get("price")
            if price is None:
                continue
            price_cents = round(float(price) * 100) if float(price) < 1 else int(float(price))
            result.append((ts, price_cents))
        result.sort(key=lambda x: x[0])
        return result
    except Exception:
        return []


# ─────────────────────────────────────────────────────────────
# KRAKEN PRICE FETCHING
# ─────────────────────────────────────────────────────────────

def fetch_kraken_1min_candles(close_time: datetime, window_minutes: int = 20) -> list:
    """
    Fetch 1-minute BTC/USD OHLC candles from Kraken covering the market window.
    Returns list of dicts: {time, open, high, low, close}
    """
    since_ts = int((close_time - timedelta(minutes=window_minutes)).timestamp())

    try:
        resp = requests.get(
            "https://api.kraken.com/0/public/OHLC",
            params={"pair": "XBTUSD", "interval": 1, "since": since_ts},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("error"):
            return []

        result_data = data.get("result", {})
        pair_key    = next((k for k in result_data if k != "last"), None)
        if not pair_key:
            return []

        candles = []
        for c in result_data[pair_key]:
            # [time, open, high, low, close, vwap, volume, count]
            candle_time = datetime.fromtimestamp(c[0], tz=timezone.utc)
            if candle_time < close_time - timedelta(minutes=window_minutes):
                continue
            candles.append({
                "time":  candle_time,
                "open":  float(c[1]),
                "high":  float(c[2]),
                "low":   float(c[3]),
                "close": float(c[4]),
            })
        return candles

    except Exception:
        return []


# ─────────────────────────────────────────────────────────────
# SIGNAL SIMULATION
# ─────────────────────────────────────────────────────────────

def get_price_at(candles: list, target_time: datetime) -> Optional[float]:
    """Return the BTC close price at or just before target_time."""
    best = None
    for c in candles:
        if c["time"] <= target_time:
            best = c["close"]
        else:
            break
    return best


def get_yes_price_at(yes_prices: list, target_time: datetime) -> Optional[int]:
    """Return the YES price (cents) at or just before target_time."""
    best = None
    for ts, price in yes_prices:
        if ts <= target_time:
            best = price
        else:
            break
    return best


def simulate_market(market: dict, candles: list, yes_prices: list, settings: dict) -> dict:
    """
    Replay bot signal logic against one historical market.

    Returns a dict:
        entered      : bool — did the sim enter a trade?
        side         : "yes" or "no" or None
        entry_time   : datetime or None
        mins_before  : float — minutes before close at entry
        won          : bool or None
        reason       : str — why it entered or why it held
    """
    if not candles:
        return {"entered": False, "reason": "no_price_data"}

    close_time  = market["close_time"]
    strike      = market["strike"]
    result      = market["result"]  # "yes" or "no"

    # Settings
    mom_threshold     = settings["momentum_threshold_pct"] / 100      # convert % to fraction
    mom_window_secs   = settings["momentum_window_secs"]
    min_yes           = settings["min_yes_price_cents"]
    max_yes           = settings["max_yes_price_cents"]
    early_win_mins    = settings["early_entry_window_minutes"]
    early_dist        = settings["early_min_distance_pct"] / 100
    tw_start_mins     = settings["trade_window_start_minutes"]
    tw_end_mins       = settings["trade_window_end_minutes"]
    late_mins         = settings["late_window_fallback_minutes"]
    late_dist         = settings["late_window_min_distance_pct"] / 100
    late_max_yes      = settings["late_window_max_yes_cents"]

    # Walk through each minute of the trading window (earliest first)
    # Check from early_win_mins before close down to tw_end_mins before close
    for check_mins_before in [m / 10 for m in range(
        int(early_win_mins * 10), int(tw_end_mins * 10) - 1, -1
    )]:
        check_time = close_time - timedelta(minutes=check_mins_before)
        if check_time > datetime.now(timezone.utc):
            continue  # skip future candles

        btc_price = get_price_at(candles, check_time)
        if btc_price is None:
            continue

        # CF estimate ≈ BTC price (Kraken is a CF constituent)
        distance_pct = (btc_price - strike) / strike  # positive = above, negative = below

        # Momentum: price change over momentum window
        past_time  = check_time - timedelta(seconds=mom_window_secs)
        past_price = get_price_at(candles, past_time)
        if past_price is None or past_price == 0:
            continue
        momentum_pct = (btc_price - past_price) / past_price  # signed fraction

        # YES price at this moment
        yes_price = get_yes_price_at(yes_prices, check_time)
        # If no historical trade data, estimate from distance
        if yes_price is None:
            if abs(distance_pct) < 0.001:
                yes_price = 50
            elif distance_pct > 0:
                yes_price = min(95, int(50 + distance_pct * 10000))
            else:
                yes_price = max(5, int(50 + distance_pct * 10000))

        # YES price bounds check
        if not (min_yes <= yes_price <= max_yes):
            continue

        # Determine window type
        in_early_window   = check_mins_before > tw_start_mins
        in_regular_window = tw_end_mins <= check_mins_before <= tw_start_mins
        in_late_window    = check_mins_before <= late_mins

        side = None

        if in_late_window:
            # Late window fallback: take clearly-edged trade without momentum
            if abs(distance_pct) >= late_dist and yes_price <= late_max_yes:
                side = "yes" if distance_pct > 0 else "no"

        elif in_early_window:
            # Early entry: need strong distance AND momentum confirming
            if abs(distance_pct) >= early_dist:
                mom_confirms = (distance_pct > 0 and momentum_pct >= mom_threshold) or \
                               (distance_pct < 0 and momentum_pct <= -mom_threshold)
                if mom_confirms:
                    side = "yes" if distance_pct > 0 else "no"

        elif in_regular_window:
            # Regular window: distance + momentum
            mom_confirms = (distance_pct > 0 and momentum_pct >= mom_threshold) or \
                           (distance_pct < 0 and momentum_pct <= -mom_threshold)
            if mom_confirms:
                side = "yes" if distance_pct > 0 else "no"

        if side is not None:
            won = (side == result)
            return {
                "entered":     True,
                "side":        side,
                "entry_time":  check_time,
                "mins_before": check_mins_before,
                "yes_price":   yes_price,
                "distance_pct": round(distance_pct * 100, 4),
                "momentum_pct": round(momentum_pct * 100, 4),
                "won":         won,
                "reason":      f"{'early' if in_early_window else 'late' if in_late_window else 'regular'} window",
            }

    return {"entered": False, "reason": "no_signal"}


# ─────────────────────────────────────────────────────────────
# BACKTEST RUNNER
# ─────────────────────────────────────────────────────────────

def run_backtest(client, markets: list, settings: dict, label: str = "Current settings") -> dict:
    """
    Run the backtest over all markets with the given settings.
    Returns a performance summary dict.
    """
    trades     = []
    no_signal  = 0
    no_data    = 0
    max_bet    = settings.get("max_bet_dollars", 10.0)

    print(f"\n  Running: {label}")
    print(f"  Testing {len(markets)} markets...", end="", flush=True)

    for i, market in enumerate(markets):
        if (i + 1) % 20 == 0:
            print(f" {i+1}...", end="", flush=True)

        # Fetch Kraken candles for this market's window
        candles = fetch_kraken_1min_candles(market["close_time"], window_minutes=20)
        time.sleep(0.15)  # avoid rate limiting Kraken

        # Try to fetch historical YES prices (best effort)
        yes_prices = []
        if market.get("ticker"):
            yes_prices = fetch_market_yes_prices(client, market["ticker"])
            time.sleep(0.1)

        result = simulate_market(market, candles, yes_prices, settings)

        if not result["entered"]:
            if result["reason"] == "no_price_data":
                no_data += 1
            else:
                no_signal += 1
            continue

        # Simple P&L model: buy at yes_price, resolve at 100c (win) or 0c (loss)
        yes_price_cents = result.get("yes_price", 50)
        contracts       = max(1, int((max_bet * 100) / yes_price_cents))
        cost            = contracts * yes_price_cents / 100
        if result["won"]:
            pnl = contracts * (100 - yes_price_cents) / 100  # profit
        else:
            pnl = -cost  # full loss

        trades.append({
            **result,
            "ticker":   market["ticker"],
            "strike":   market["strike"],
            "contracts": contracts,
            "cost":     round(cost, 2),
            "pnl":      round(pnl, 2),
        })

    print(" done.")

    if not trades:
        return {
            "label":      label,
            "settings":   settings,
            "trades":     0,
            "wins":       0,
            "losses":     0,
            "win_rate":   0,
            "total_pnl":  0,
            "no_signal":  no_signal,
            "no_data":    no_data,
        }

    wins       = sum(1 for t in trades if t["won"])
    losses     = len(trades) - wins
    total_pnl  = sum(t["pnl"] for t in trades)
    win_rate   = wins / len(trades) * 100

    return {
        "label":     label,
        "settings":  settings,
        "trades":    len(trades),
        "wins":      wins,
        "losses":    losses,
        "win_rate":  round(win_rate, 1),
        "total_pnl": round(total_pnl, 2),
        "no_signal": no_signal,
        "no_data":   no_data,
        "trade_log": trades,
    }


# ─────────────────────────────────────────────────────────────
# REPORT PRINTING
# ─────────────────────────────────────────────────────────────

def print_report(results: list):
    print("\n" + "=" * 65)
    print("  BACKTEST RESULTS")
    print("=" * 65)
    print(f"  {'Settings':<38} {'Trades':>6} {'Win%':>6} {'P&L':>9}")
    print("-" * 65)

    best = max(results, key=lambda r: r["total_pnl"])

    for r in results:
        marker = " ◀ BEST" if r is best else ""
        pnl_str = f"${r['total_pnl']:+.2f}"
        print(f"  {r['label']:<38} {r['trades']:>6} {r['win_rate']:>5.1f}% {pnl_str:>9}{marker}")

    print("=" * 65)

    # Detail on best performer
    b = best
    print(f"\n  Best: {b['label']}")
    print(f"    Trades:     {b['trades']}  ({b['wins']} wins / {b['losses']} losses)")
    print(f"    Win rate:   {b['win_rate']}%")
    print(f"    Total P&L:  ${b['total_pnl']:+.2f}")
    print(f"    No-signal:  {b['no_signal']} markets skipped")

    if b.get("trade_log"):
        # Breakdown by entry window type
        by_window = {}
        for t in b["trade_log"]:
            w = t.get("reason", "unknown")
            if w not in by_window:
                by_window[w] = {"wins": 0, "losses": 0, "pnl": 0}
            by_window[w]["wins"   if t["won"] else "losses"] += 1
            by_window[w]["pnl"] += t["pnl"]

        print(f"\n  Win rate by entry type:")
        for window, stats in by_window.items():
            total = stats["wins"] + stats["losses"]
            wr    = stats["wins"] / total * 100 if total else 0
            print(f"    {window:<20} {stats['wins']}/{total} ({wr:.0f}%)   P&L: ${stats['pnl']:+.2f}")

    print()


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────

def main():
    print("\n" + "=" * 65)
    print("  KALSHI BTC 15-MIN STRATEGY BACKTESTER")
    print("=" * 65)

    config = load_config()
    print("\nConnecting to Kalshi...")
    try:
        client = make_kalshi_client(config)
        balance = client.get_balance()
        print(f"  Connected. Account balance: ${balance:.2f}")
    except Exception as e:
        print(f"  ERROR: Could not connect to Kalshi: {e}")
        sys.exit(1)

    # How many days to look back
    days = 30
    print(f"\nFetching market history ({days} days)...")
    markets = fetch_settled_markets(client, days=days)

    if not markets:
        print("  No settled markets found. Try reducing the lookback period.")
        sys.exit(1)

    print(f"  Found {len(markets)} settled KXBTC15M markets to test against.\n")

    # ── Current settings (from config.yaml) ──────────────────
    sig = config.get("signal", {})
    current = {
        "momentum_threshold_pct":    float(sig.get("momentum_threshold_pct", 0.025)),
        "momentum_window_secs":      int(sig.get("momentum_window_seconds", 30)),
        "min_yes_price_cents":       int(sig.get("min_yes_price_cents", 25)),
        "max_yes_price_cents":       int(sig.get("max_yes_price_cents", 75)),
        "max_bet_dollars":           float(config.get("trading", {}).get("max_bet_dollars", 10)),
        "early_entry_window_minutes":float(sig.get("early_entry_window_minutes", 10)),
        "early_min_distance_pct":    float(sig.get("early_min_distance_pct", 0.08)),
        "trade_window_start_minutes":float(sig.get("trade_window_start_minutes", 5)),
        "trade_window_end_minutes":  float(sig.get("trade_window_end_minutes", 1.5)),
        "late_window_fallback_minutes": float(sig.get("late_window_fallback_minutes", 3.0)),
        "late_window_min_distance_pct": float(sig.get("late_window_min_distance_pct", 0.05)),
        "late_window_max_yes_cents": int(sig.get("late_window_max_yes_cents", 75)),
    }

    # ── Parameter variations to test ─────────────────────────
    # Only vary the most impactful settings; keep everything else at current.
    variations = []

    for mom in [0.015, 0.020, 0.025, 0.030]:
        for late_dist in [0.03, 0.04, 0.05]:
            label = f"mom={mom:.3f}% late_dist={late_dist:.2f}%"
            s = {**current, "momentum_threshold_pct": mom, "late_window_min_distance_pct": late_dist}
            variations.append((label, s))

    # ── Run current settings first ────────────────────────────
    all_results = []

    print("Running backtests (this may take a few minutes)...")
    r = run_backtest(client, markets, current, label="Current settings")
    all_results.append(r)

    # ── Run variations ────────────────────────────────────────
    for label, settings in variations:
        r = run_backtest(client, markets, settings, label=label)
        all_results.append(r)

    # ── Print report ──────────────────────────────────────────
    print_report(all_results)

    # Save full results to file
    import json
    out_path = BOT_DIR / "backtest_results.json"
    with open(out_path, "w") as f:
        # Remove trade_log from JSON to keep it readable
        clean = [{k: v for k, v in r.items() if k != "trade_log"} for r in all_results]
        json.dump(clean, f, indent=2, default=str)
    print(f"  Full results saved to: backtest_results.json")
    print()


if __name__ == "__main__":
    main()
