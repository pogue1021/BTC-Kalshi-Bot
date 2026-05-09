"""
backtester.py — Standalone Kalshi 15-min BTC Strategy Backtester
=================================================================
Tests your current bot settings (and variations) against historical
Kalshi KXBTC15M markets. Uses Kraken's public API for BTC price history
and your existing Kalshi client for market data.

Run from the bot folder:
    python backtester.py

No changes are made to the live bot. Output is a performance report only.

v2 improvements:
  - Data cached once per market — all parameter variations reuse cached data
    (runtime ~12 min instead of ~2.5 hrs)
  - Tests 4 variables: momentum_threshold, momentum_window, early_distance, late_distance
  - Detailed skip-reason breakdown shows why trades don't trigger
  - Top-10 leaderboard + win rate by entry window for best result
"""

import os
import sys
import json
import time
import yaml
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
from itertools import product

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
    cutoff  = datetime.now(timezone.utc) - timedelta(days=days)
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
                print(f" done. ({len(markets)} markets)")
                return markets

            strike = m.get("floor_strike") or m.get("cap_strike")
            if strike is None:
                subtitle = m.get("subtitle", "") or m.get("title", "")
                match    = re.search(r'\$([0-9,]+\.?\d*)', subtitle)
                if match:
                    strike = float(match.group(1).replace(",", ""))
            if strike is None:
                continue

            result = m.get("result", "")
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
        time.sleep(0.2)

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
# BTC PRICE FETCHING  (Coinbase Exchange — public, no auth needed)
# ─────────────────────────────────────────────────────────────
#
# WHY NOT KRAKEN?  Kraken's free 1-minute OHLC endpoint only keeps
# ~720 candles (~12 hours).  Markets older than that return empty
# data, which wrecks a 30-day backtest (79% of markets get skipped).
#
# Coinbase Exchange public candle API has years of 1-min history,
# requires no authentication, and is fully accessible from the US.
# Format: [[timestamp, low, high, open, close, volume], ...]

COINBASE_CANDLES_URL = "https://api.exchange.coinbase.com/products/BTC-USD/candles"


def fetch_btc_1min_candles(close_time: datetime, window_minutes: int = 25) -> list:
    """
    Fetch 1-minute BTC/USD OHLC candles from Coinbase Exchange.
    Returns list of dicts: {time, open, high, low, close}

    We fetch a wider window (25 min default) so momentum calculations
    that look back further than the trade window still have price data.
    Coinbase returns up to 300 candles per request; 25 min = 25 candles, fine.
    """
    start_time = close_time - timedelta(minutes=window_minutes)
    # Coinbase expects ISO 8601 strings
    start_iso  = start_time.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_iso    = close_time.strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        resp = requests.get(
            COINBASE_CANDLES_URL,
            params={"granularity": 60, "start": start_iso, "end": end_iso},
            headers={"Accept": "application/json"},
            timeout=10,
        )
        resp.raise_for_status()
        raw = resp.json()

        if not isinstance(raw, list) or not raw:
            return []

        # Coinbase returns newest-first: [timestamp, low, high, open, close, volume]
        candles = []
        for c in raw:
            candle_time = datetime.fromtimestamp(c[0], tz=timezone.utc)
            candles.append({
                "time":  candle_time,
                "open":  float(c[3]),
                "high":  float(c[2]),
                "low":   float(c[1]),
                "close": float(c[4]),
            })

        # Sort oldest-first so get_price_at() works correctly
        candles.sort(key=lambda x: x["time"])
        return candles

    except Exception:
        return []


# Keep old name as alias so nothing else breaks
fetch_kraken_1min_candles = fetch_btc_1min_candles


# ─────────────────────────────────────────────────────────────
# DATA CACHE — fetch once per market, reuse across all variations
# ─────────────────────────────────────────────────────────────

def build_market_cache(client, markets: list) -> dict:
    """
    Pre-fetch Coinbase candles and YES prices for every market.
    Returns dict keyed by ticker: {"candles": [...], "yes_prices": [...]}

    This is the key speedup: instead of re-fetching for each parameter combo,
    we fetch once and all 100+ variations run against the same in-memory data.
    """
    cache = {}
    total = len(markets)

    print(f"\n  Pre-fetching price data for {total} markets (Coinbase Exchange)...")
    print(f"  (This is the only slow part — all parameter variations will reuse this data)")
    print(f"  ", end="", flush=True)

    for i, market in enumerate(markets):
        ticker = market["ticker"]

        if (i + 1) % 50 == 0:
            pct = (i + 1) / total * 100
            print(f"{i+1}/{total} ({pct:.0f}%)... ", end="", flush=True)

        # Fetch Coinbase candles — 25 min window to cover all momentum lookbacks
        candles = fetch_btc_1min_candles(market["close_time"], window_minutes=25)
        time.sleep(0.15)  # ~6 req/sec — Coinbase public API allows 10/sec, be conservative

        # Fetch YES prices — best effort
        yes_prices = []
        if ticker:
            yes_prices = fetch_market_yes_prices(client, ticker)
            time.sleep(0.08)

        cache[ticker] = {
            "candles":    candles,
            "yes_prices": yes_prices,
        }

    print(f"\n  Data cache built. ({total} markets, {sum(len(v['candles']) for v in cache.values())} total candles)")
    return cache


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


# Skip reason codes — used for breakdown reporting
SKIP_NO_DATA       = "no_price_data"
SKIP_NO_MOMENTUM   = "no_momentum"
SKIP_DIST_TOO_SMAL = "distance_too_small"
SKIP_PRICE_RANGE   = "yes_price_out_of_range"
SKIP_LATE_PRICE    = "late_price_filter"
SKIP_NO_SIGNAL     = "no_signal"


def simulate_market(market: dict, candles: list, yes_prices: list, settings: dict) -> dict:
    """
    Replay bot signal logic against one historical market.

    Returns a dict:
        entered      : bool
        side         : "yes" / "no" / None
        entry_time   : datetime or None
        mins_before  : float
        yes_price    : int or None
        distance_pct : float
        momentum_pct : float
        won          : bool or None
        reason       : entry window type (early/regular/late) or skip code
        skip_reasons : dict counting each skip type encountered during scan
    """
    if not candles:
        return {"entered": False, "reason": SKIP_NO_DATA, "skip_reasons": {SKIP_NO_DATA: 1}}

    close_time = market["close_time"]
    strike     = market["strike"]
    result     = market["result"]

    # Unpack settings
    mom_threshold  = settings["momentum_threshold_pct"] / 100
    mom_window_secs= settings["momentum_window_secs"]
    min_yes        = settings["min_yes_price_cents"]
    max_yes        = settings["max_yes_price_cents"]
    early_win_mins = settings["early_entry_window_minutes"]
    early_dist     = settings["early_min_distance_pct"] / 100
    tw_start_mins  = settings["trade_window_start_minutes"]
    tw_end_mins    = settings["trade_window_end_minutes"]
    late_mins      = settings["late_window_fallback_minutes"]
    late_dist      = settings["late_window_min_distance_pct"] / 100
    late_max_yes   = settings["late_window_max_yes_cents"]

    # Track why each minute-slot was skipped (for debugging/reporting)
    skip_counts = {
        SKIP_NO_MOMENTUM:   0,
        SKIP_DIST_TOO_SMAL: 0,
        SKIP_PRICE_RANGE:   0,
        SKIP_LATE_PRICE:    0,
    }

    # Walk from earliest possible entry to latest, check each 0.1-min step
    for check_mins_before in [m / 10 for m in range(
        int(early_win_mins * 10), int(tw_end_mins * 10) - 1, -1
    )]:
        check_time = close_time - timedelta(minutes=check_mins_before)
        if check_time > datetime.now(timezone.utc):
            continue

        btc_price = get_price_at(candles, check_time)
        if btc_price is None:
            continue

        distance_pct = (btc_price - strike) / strike

        # Momentum
        past_time  = check_time - timedelta(seconds=mom_window_secs)
        past_price = get_price_at(candles, past_time)
        if past_price is None or past_price == 0:
            continue
        momentum_pct = (btc_price - past_price) / past_price

        # YES price (live trade data or estimated from distance)
        yes_price = get_yes_price_at(yes_prices, check_time)
        if yes_price is None:
            if abs(distance_pct) < 0.001:
                yes_price = 50
            elif distance_pct > 0:
                yes_price = min(95, int(50 + distance_pct * 10000))
            else:
                yes_price = max(5, int(50 + distance_pct * 10000))

        if not (min_yes <= yes_price <= max_yes):
            skip_counts[SKIP_PRICE_RANGE] += 1
            continue

        in_early_window   = check_mins_before > tw_start_mins
        in_regular_window = tw_end_mins <= check_mins_before <= tw_start_mins
        in_late_window    = check_mins_before <= late_mins

        side = None

        if in_late_window:
            if abs(distance_pct) >= late_dist:
                if yes_price <= late_max_yes:
                    side = "yes" if distance_pct > 0 else "no"
                else:
                    skip_counts[SKIP_LATE_PRICE] += 1
            else:
                skip_counts[SKIP_DIST_TOO_SMAL] += 1

        elif in_early_window:
            if abs(distance_pct) >= early_dist:
                mom_confirms = (
                    (distance_pct > 0 and momentum_pct >= mom_threshold) or
                    (distance_pct < 0 and momentum_pct <= -mom_threshold)
                )
                if mom_confirms:
                    side = "yes" if distance_pct > 0 else "no"
                else:
                    skip_counts[SKIP_NO_MOMENTUM] += 1
            else:
                skip_counts[SKIP_DIST_TOO_SMAL] += 1

        elif in_regular_window:
            mom_confirms = (
                (distance_pct > 0 and momentum_pct >= mom_threshold) or
                (distance_pct < 0 and momentum_pct <= -mom_threshold)
            )
            if mom_confirms:
                side = "yes" if distance_pct > 0 else "no"
            else:
                skip_counts[SKIP_NO_MOMENTUM] += 1

        if side is not None:
            window_type = (
                "early"   if in_early_window else
                "late"    if in_late_window  else
                "regular"
            )
            return {
                "entered":      True,
                "side":         side,
                "entry_time":   check_time,
                "mins_before":  check_mins_before,
                "yes_price":    yes_price,
                "distance_pct": round(distance_pct * 100, 4),
                "momentum_pct": round(momentum_pct * 100, 4),
                "won":          (side == result),
                "reason":       window_type,
                "skip_reasons": skip_counts,
            }

    return {"entered": False, "reason": SKIP_NO_SIGNAL, "skip_reasons": skip_counts}


# ─────────────────────────────────────────────────────────────
# BACKTEST RUNNER  (uses pre-built cache — no API calls here)
# ─────────────────────────────────────────────────────────────

def run_backtest_cached(markets: list, cache: dict, settings: dict, label: str) -> dict:
    """
    Run backtest over all markets using cached price data.
    No API calls — runs in seconds per variation.
    """
    trades    = []
    no_signal = 0
    no_data   = 0
    max_bet   = settings.get("max_bet_dollars", 10.0)

    # Aggregate skip reasons across all markets
    total_skips = {
        SKIP_NO_MOMENTUM:   0,
        SKIP_DIST_TOO_SMAL: 0,
        SKIP_PRICE_RANGE:   0,
        SKIP_LATE_PRICE:    0,
    }

    for market in markets:
        ticker     = market["ticker"]
        cached     = cache.get(ticker, {})
        candles    = cached.get("candles", [])
        yes_prices = cached.get("yes_prices", [])

        result = simulate_market(market, candles, yes_prices, settings)

        # Accumulate skip reasons
        for k, v in result.get("skip_reasons", {}).items():
            if k in total_skips:
                total_skips[k] += v

        if not result["entered"]:
            if result["reason"] == SKIP_NO_DATA:
                no_data += 1
            else:
                no_signal += 1
            continue

        yes_price_cents = result.get("yes_price", 50)
        contracts       = max(1, int((max_bet * 100) / yes_price_cents))
        cost            = contracts * yes_price_cents / 100
        if result["won"]:
            pnl = contracts * (100 - yes_price_cents) / 100
        else:
            pnl = -cost

        trades.append({
            **result,
            "ticker":    market["ticker"],
            "strike":    market["strike"],
            "contracts": contracts,
            "cost":      round(cost, 2),
            "pnl":       round(pnl, 2),
        })

    if not trades:
        return {
            "label":       label,
            "settings":    settings,
            "trades":      0,
            "wins":        0,
            "losses":      0,
            "win_rate":    0.0,
            "total_pnl":   0.0,
            "no_signal":   no_signal,
            "no_data":     no_data,
            "skip_detail": total_skips,
        }

    wins      = sum(1 for t in trades if t["won"])
    losses    = len(trades) - wins
    total_pnl = sum(t["pnl"] for t in trades)
    win_rate  = wins / len(trades) * 100

    return {
        "label":       label,
        "settings":    settings,
        "trades":      len(trades),
        "wins":        wins,
        "losses":      losses,
        "win_rate":    round(win_rate, 1),
        "total_pnl":   round(total_pnl, 2),
        "no_signal":   no_signal,
        "no_data":     no_data,
        "skip_detail": total_skips,
        "trade_log":   trades,
    }


# ─────────────────────────────────────────────────────────────
# REPORTING
# ─────────────────────────────────────────────────────────────

def print_report(results: list, current_label: str = "Current settings"):
    """Print ranked leaderboard + detail for the best result."""

    # Sort by P&L descending
    ranked = sorted(results, key=lambda r: r["total_pnl"], reverse=True)

    print("\n" + "=" * 72)
    print("  BACKTEST RESULTS  —  Top 10 by P&L")
    print("=" * 72)
    print(f"  {'#':<3}  {'Settings':<42} {'Trades':>6} {'Win%':>6} {'P&L':>10}")
    print("-" * 72)

    current_result = next((r for r in results if r["label"] == current_label), None)

    for rank, r in enumerate(ranked[:10], 1):
        marker = ""
        if r["label"] == current_label:
            marker = " ◀ CURRENT"
        elif rank == 1:
            marker = " ◀ BEST"
        pnl_str = f"${r['total_pnl']:+.2f}"
        print(f"  {rank:<3}  {r['label']:<42} {r['trades']:>6} {r['win_rate']:>5.1f}% {pnl_str:>10}{marker}")

    # Show where current settings ranks overall
    if current_result:
        current_rank = ranked.index(current_result) + 1
        print(f"\n  Current settings rank: #{current_rank} of {len(results)}")

    print("=" * 72)

    # Detail on best performer
    best = ranked[0]
    print(f"\n  ── Best result: {best['label']}")
    print(f"     Trades:    {best['trades']}  ({best['wins']} wins / {best['losses']} losses)")
    print(f"     Win rate:  {best['win_rate']}%")
    print(f"     Total P&L: ${best['total_pnl']:+.2f}")
    print(f"     No-signal: {best['no_signal']} markets skipped")

    # Skip reason breakdown for best
    sd = best.get("skip_detail", {})
    total_skipped = best["no_signal"] + best["no_data"]
    if sd and total_skipped > 0:
        print(f"\n  Why markets didn't trade (best result):")
        print(f"    No price data:       {best['no_data']:>5}")
        print(f"    No momentum signal:  {sd.get(SKIP_NO_MOMENTUM, 0):>5}  (threshold or window too strict)")
        print(f"    Distance too small:  {sd.get(SKIP_DIST_TOO_SMAL, 0):>5}  (price too close to strike)")
        print(f"    YES price OOB:       {sd.get(SKIP_PRICE_RANGE, 0):>5}  (outside min/max price filter)")
        print(f"    Late price filter:   {sd.get(SKIP_LATE_PRICE, 0):>5}  (late window YES price too high)")

    # Win rate by entry window for best
    if best.get("trade_log"):
        by_window = {}
        for t in best["trade_log"]:
            w = t.get("reason", "unknown")
            if w not in by_window:
                by_window[w] = {"wins": 0, "losses": 0, "pnl": 0.0}
            by_window[w]["wins" if t["won"] else "losses"] += 1
            by_window[w]["pnl"] += t["pnl"]

        print(f"\n  Win rate by entry type (best result):")
        for window, stats in sorted(by_window.items()):
            total = stats["wins"] + stats["losses"]
            wr    = stats["wins"] / total * 100 if total else 0
            print(f"    {window:<12}  {stats['wins']}/{total} trades  ({wr:.0f}% win)   P&L: ${stats['pnl']:+.2f}")

    # Show best settings in copy-paste format
    bs = best["settings"]
    print(f"\n  ── Best settings (copy into config.yaml / dashboard sliders):")
    print(f"     momentum_threshold_pct:      {bs['momentum_threshold_pct']:.3f}")
    print(f"     momentum_window_secs:        {bs['momentum_window_secs']}")
    print(f"     early_min_distance_pct:      {bs['early_min_distance_pct']:.2f}")
    print(f"     late_window_min_distance_pct:{bs['late_window_min_distance_pct']:.2f}")
    print()


# ─────────────────────────────────────────────────────────────
# PARAMETER GRID
# ─────────────────────────────────────────────────────────────

def build_parameter_grid(base: dict) -> list:
    """
    Build all (label, settings) combinations to test.
    We vary the four most impactful settings; everything else stays at current.

    Grid: 4 x 3 x 3 x 3 = 108 variations
    """
    momentum_thresholds  = [0.015, 0.020, 0.025, 0.030]
    momentum_windows     = [15, 25, 40]          # seconds
    early_distances      = [0.05, 0.07, 0.10]    # % from strike for early entry
    late_distances       = [0.03, 0.04, 0.05]    # % from strike for late fallback

    variations = []
    for mom, win, edist, ldist in product(
        momentum_thresholds, momentum_windows, early_distances, late_distances
    ):
        label = f"mom={mom:.3f} win={win}s e={edist:.2f} l={ldist:.2f}"
        s = {
            **base,
            "momentum_threshold_pct":       mom,
            "momentum_window_secs":         win,
            "early_min_distance_pct":       edist,
            "late_window_min_distance_pct": ldist,
        }
        variations.append((label, s))

    return variations


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────

def main():
    print("\n" + "=" * 72)
    print("  KALSHI BTC 15-MIN STRATEGY BACKTESTER  v2")
    print("=" * 72)

    config = load_config()
    print("\nConnecting to Kalshi...")
    try:
        client  = make_kalshi_client(config)
        balance = client.get_balance()
        print(f"  Connected. Account balance: ${balance:.2f}")
    except Exception as e:
        print(f"  ERROR: Could not connect to Kalshi: {e}")
        sys.exit(1)

    # ── Fetch historical markets ───────────────────────────────────────
    days = 30
    print(f"\nFetching market history ({days} days)...")
    markets = fetch_settled_markets(client, days=days)

    if not markets:
        print("  No settled markets found.")
        sys.exit(1)

    print(f"  Found {len(markets)} settled KXBTC15M markets.\n")

    # ── Build cache (the slow step — fetch once per market) ────
    t_start = time.time()
    cache   = build_market_cache(client, markets)
    t_fetch = time.time() - t_start
    print(f"  Cache built in {t_fetch/60:.1f} min.\n")

    # ── Build base settings from config.yaml ──────────────
    sig     = config.get("signal", {})
    current = {
        "momentum_threshold_pct":        float(sig.get("momentum_threshold_pct",        0.025)),
        "momentum_window_secs":          int(  sig.get("momentum_window_seconds",       30)),
        "min_yes_price_cents":           int(  sig.get("min_yes_price_cents",           25)),
        "max_yes_price_cents":           int(  sig.get("max_yes_price_cents",           75)),
        "max_bet_dollars":               float(config.get("trading", {}).get("max_bet_dollars", 10)),
        "early_entry_window_minutes":    float(sig.get("early_entry_window_minutes",    10)),
        "early_min_distance_pct":        float(sig.get("early_min_distance_pct",        0.08)),
        "trade_window_start_minutes":    float(sig.get("trade_window_start_minutes",    5)),
        "trade_window_end_minutes":      float(sig.get("trade_window_end_minutes",      1.5)),
        "late_window_fallback_minutes":  float(sig.get("late_window_fallback_minutes",  3.0)),
        "late_window_min_distance_pct":  float(sig.get("late_window_min_distance_pct",  0.05)),
        "late_window_max_yes_cents":     int(  sig.get("late_window_max_yes_cents",     75)),
    }

    current_label = "Current settings"

    # ── Build parameter grid ─────────────────────────────────────
    grid = build_parameter_grid(current)
    print(f"Running {len(grid) + 1} parameter combinations against cached data...")
    print(f"(This should take under a minute)\n")

    # ── Run current settings ───────────────────────────────────────
    all_results = []
    r = run_backtest_cached(markets, cache, current, label=current_label)
    all_results.append(r)
    print(f"  [  1/{len(grid)+1}]  {current_label:<48}  trades={r['trades']}  win={r['win_rate']}%  P&L=${r['total_pnl']:+.2f}")

    # ── Run variations ──────────────────────────────────────────────
    for i, (label, settings) in enumerate(grid, 2):
        r = run_backtest_cached(markets, cache, settings, label=label)
        all_results.append(r)
        if i % 10 == 0 or i == len(grid) + 1:
            print(f"  [{i:>3}/{len(grid)+1}]  {label:<48}  trades={r['trades']}  win={r['win_rate']}%  P&L=${r['total_pnl']:+.2f}")

    # ── Print report ──────────────────────────────────────────────────
    print_report(all_results, current_label=current_label)

    # ── Save results ──────────────────────────────────────────────────────────
    out_path = BOT_DIR / "backtest_results.json"
    with open(out_path, "w") as f:
        clean = [{k: v for k, v in r.items() if k != "trade_log"} for r in all_results]
        json.dump(clean, f, indent=2, default=str)
    print(f"  Full results saved to: backtest_results.json")
    print()


if __name__ == "__main__":
    main()
