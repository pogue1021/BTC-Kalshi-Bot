#!/usr/bin/env python3
"""
analyze_log.py — Mine bot_trades.log for market pricing patterns
=================================================================
Standalone script — does NOT import or modify any bot files.

Usage:
    python analyze_log.py                    # uses bot_trades.log in same folder
    python analyze_log.py path/to/file.log   # use a specific log file

What this does:
  - Parses every trade: entry price, side, time of entry, exit type, exit price
  - Shows how long positions lasted before stop-loss
  - Shows YES price movement in the minutes before each entry
  - Finds BTC distance at entry for each trade
  - Outputs a summary of what conditions lead to wins vs losses
  - Saves full dataset to analysis_output.json for further inspection
"""

import re
import sys
import json
from pathlib import Path
from collections import defaultdict

# ── Config ────────────────────────────────────────────────────
LOG_PATH = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "bot_trades.log"

# ── Regex patterns ────────────────────────────────────────────
RE_NEW_MARKET   = re.compile(r'New market: (KXBTC15M-\S+) \| Closes in: ([\d.]+) min')
RE_HOLD         = re.compile(r'HOLD \| YES=(\d+)c \| ([\d.]+)min left')
RE_DIST_CLOSE   = re.compile(r'CF within \$([\d,]+) of target \(\$([\d,]+\.?\d*)\)')
RE_DIST_FAR     = re.compile(r'CF \$([\d,]+) (above|below) target \$([\d,]+\.?\d*)')
RE_DIST_SIGNAL  = re.compile(r'CF \$([+-]?\d+) (above|below) target \$([\d,]+\.?\d*)')
RE_DELTA_SIGNAL = re.compile(r'Near target \(\$([\d,]+\.?\d*), Δ\$([+-]\d+)\)')
RE_SIGNAL       = re.compile(r'SIGNAL (YES|NO) \| (KXBTC15M-\S+) \| \S+ (\d+)x @ (\d+)c \| trade (\d+)/(\d+)')
RE_STOP_LOSS    = re.compile(r'STOP-LOSS: (YES|NO) entered @ (\d+)c, now (\d+)c — (.+?) — exiting')
RE_TAKE_PROFIT  = re.compile(r'TAKE-PROFIT: (YES|NO) entered @ (\d+)c, now (\d+)c \(\+(\d+)c')
RE_SETTLED      = re.compile(r'Market (KXBTC15M-\S+) settled: (YES|NO) \| P&L: \$([+-][\d.]+)')
RE_RM_LIVE      = re.compile(r'\[LIVE\] (Stop-loss|Take-profit) exit: entry (\d+)c → exit (\d+)c \(([+-]\d+)c × (\d+) = \$([+-][\d.]+)\) \| Daily P&L: \$([+-][\d.]+)')
RE_RM_LIVE_WIN  = re.compile(r'\[LIVE\] .+ \| Daily P&L: \$([+-][\d.]+)')
RE_BALANCE      = re.compile(r'Kalshi account balance: \$([\d.]+)')
RE_TIMESTAMP    = re.compile(r'^(\d{2}:\d{2}:\d{2})')


def parse_log(path: Path):
    """
    Single-pass log parser. Returns markets dict and trades list.
    """
    markets = {}        # ticker -> {observations: [], trades: []}
    trades  = []        # flat list of all trades
    balances = []       # (time, balance) tuples

    current_ticker = None
    pending_entry  = None   # signal seen, waiting for RM confirmation
    current_time   = "00:00:00"

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    print(f"Parsing {len(lines):,} lines from {path.name}...")

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # ── Timestamp ─────────────────────────────────────────
        ts_m = RE_TIMESTAMP.match(line)
        if ts_m:
            current_time = ts_m.group(1)

        # ── Account balance ───────────────────────────────────
        bal_m = RE_BALANCE.search(line)
        if bal_m:
            balances.append((current_time, float(bal_m.group(1))))

        # ── New market ────────────────────────────────────────
        nm_m = RE_NEW_MARKET.search(line)
        if nm_m:
            current_ticker = nm_m.group(1)
            closes_in      = float(nm_m.group(2))
            if current_ticker not in markets:
                markets[current_ticker] = {
                    "ticker":       current_ticker,
                    "observations": [],
                    "trades":       [],
                    "first_seen_at": current_time,
                }
            pending_entry = None
            continue

        if current_ticker is None:
            continue

        mkt = markets[current_ticker]

        # ── HOLD observation ──────────────────────────────────
        hold_m = RE_HOLD.search(line)
        if hold_m:
            yes_c  = int(hold_m.group(1))
            mins   = float(hold_m.group(2))
            rest   = line[hold_m.end():]

            # Try to extract BTC distance
            distance = None
            dist_far_m = RE_DIST_FAR.search(rest)
            dist_close_m = RE_DIST_CLOSE.search(rest)

            if dist_far_m:
                dollars = float(dist_far_m.group(1).replace(",", ""))
                sign    = 1 if dist_far_m.group(2) == "above" else -1
                distance = sign * dollars
            elif dist_close_m:
                dollars  = float(dist_close_m.group(1).replace(",", ""))
                distance = dollars   # within distance, treat as positive (close to strike)

            mkt["observations"].append({
                "time":     current_time,
                "mins_left": mins,
                "yes_c":    yes_c,
                "distance": distance,
            })
            continue

        # ── Signal / entry ─────────────────────────────────────
        sig_m = RE_SIGNAL.search(line)
        if sig_m:
            direction  = sig_m.group(1)   # YES or NO
            ticker     = sig_m.group(2)
            qty        = int(sig_m.group(3))
            price_c    = int(sig_m.group(4))
            trade_num  = int(sig_m.group(5))

            rest = line[sig_m.end():]

            # Extract BTC distance from signal line
            distance = None
            dist_m  = RE_DIST_SIGNAL.search(rest)
            delta_m = RE_DELTA_SIGNAL.search(rest)
            if dist_m:
                dollars  = float(dist_m.group(1).replace(",", ""))
                sign     = 1 if dist_m.group(2) == "above" else -1
                distance = sign * dollars
            elif delta_m:
                # Near-target: Δ$+3 means BTC is 3 above strike
                distance = float(delta_m.group(2))

            pending_entry = {
                "ticker":    ticker,
                "side":      direction,
                "entry_c":   price_c,
                "qty":       qty,
                "entry_time": current_time,
                "distance":  distance,
                "trade_num": trade_num,
                "exit_type": None,
                "exit_c":    None,
                "exit_time": None,
                "pnl":       None,
                "duration_approx": None,
            }
            continue

        # ── Stop-loss ──────────────────────────────────────────
        sl_m = RE_STOP_LOSS.search(line)
        if sl_m:
            side    = sl_m.group(1)
            entry_c = int(sl_m.group(2))
            exit_c  = int(sl_m.group(3))
            reason  = sl_m.group(4)

            # Find matching pending entry
            trade = None
            if pending_entry and pending_entry["entry_c"] == entry_c:
                trade = pending_entry
            else:
                # Search recent trades for this position
                for t in reversed(trades[-10:]):
                    if t.get("exit_type") is None and t.get("entry_c") == entry_c:
                        trade = t
                        break

            if trade is None:
                # Create from what we know
                trade = {
                    "ticker":    current_ticker,
                    "side":      side,
                    "entry_c":   entry_c,
                    "qty":       None,
                    "entry_time": None,
                    "distance":  None,
                    "trade_num": None,
                }

            trade["exit_type"] = "stop_loss"
            trade["exit_c"]    = exit_c
            trade["exit_time"] = current_time
            trade["sl_reason"] = reason
            trade["loss_c"]    = entry_c - exit_c

            if trade not in trades:
                trades.append(trade)
                mkt["trades"].append(trade)
            pending_entry = None
            continue

        # ── Take-profit ────────────────────────────────────────
        tp_m = RE_TAKE_PROFIT.search(line)
        if tp_m:
            side    = tp_m.group(1)
            entry_c = int(tp_m.group(2))
            exit_c  = int(tp_m.group(3))
            gain_c  = int(tp_m.group(4))

            trade = None
            if pending_entry and pending_entry["entry_c"] == entry_c:
                trade = pending_entry
            else:
                for t in reversed(trades[-10:]):
                    if t.get("exit_type") is None and t.get("entry_c") == entry_c:
                        trade = t
                        break

            if trade is None:
                trade = {
                    "ticker":    current_ticker,
                    "side":      side,
                    "entry_c":   entry_c,
                    "qty":       None,
                    "entry_time": None,
                    "distance":  None,
                    "trade_num": None,
                }

            trade["exit_type"] = "take_profit"
            trade["exit_c"]    = exit_c
            trade["exit_time"] = current_time
            trade["gain_c"]    = gain_c

            if trade not in trades:
                trades.append(trade)
                mkt["trades"].append(trade)
            pending_entry = None
            continue

        # ── Settlement ─────────────────────────────────────────
        set_m = RE_SETTLED.search(line)
        if set_m:
            ticker = set_m.group(1)
            result = set_m.group(2)
            pnl    = float(set_m.group(3))
            if ticker in markets:
                markets[ticker]["settled"] = result
                markets[ticker]["settlement_pnl"] = pnl
            continue

        # ── Risk manager P&L confirmation ─────────────────────
        rm_m = RE_RM_LIVE.search(line)
        if rm_m:
            exit_type = rm_m.group(1).lower().replace("-", "_")
            entry_c   = int(rm_m.group(2))
            exit_c    = int(rm_m.group(3))
            delta_c   = int(rm_m.group(4))
            qty       = int(rm_m.group(5))
            trade_pnl = float(rm_m.group(6))
            daily_pnl = float(rm_m.group(7))

            # Attach P&L to most recent matching trade
            for t in reversed(trades[-5:]):
                if t.get("entry_c") == entry_c and t.get("pnl") is None:
                    t["pnl"]      = trade_pnl
                    t["daily_pnl"] = daily_pnl
                    if t.get("qty") is None:
                        t["qty"] = qty
                    break
            continue

    return markets, trades, balances


def print_report(markets, trades, balances):
    print("\n" + "="*65)
    print("  LOG ANALYSIS REPORT")
    print("="*65)

    # ── Account balance journey ────────────────────────────────
    if balances:
        print(f"\n📊 ACCOUNT BALANCE OVER SESSION")
        for t, b in balances:
            print(f"  {t}  →  ${b:.2f}")
        if len(balances) > 1:
            change = balances[-1][1] - balances[0][1]
            print(f"  Net change: ${change:+.2f}")

    # ── High-level trade stats ─────────────────────────────────
    live_trades = [t for t in trades if t.get("pnl") is not None]
    stops  = [t for t in live_trades if t.get("exit_type") == "stop_loss"]
    tps    = [t for t in live_trades if t.get("exit_type") == "take_profit"]

    print(f"\n📈 TRADE SUMMARY")
    print(f"  Total recorded trades:  {len(trades)}")
    print(f"  Trades with P&L data:   {len(live_trades)}")
    print(f"  Stop-losses:            {len(stops)}")
    print(f"  Take-profits:           {len(tps)}")

    if live_trades:
        total_pnl = sum(t["pnl"] for t in live_trades)
        sl_pnl    = sum(t["pnl"] for t in stops)
        tp_pnl    = sum(t["pnl"] for t in tps)
        print(f"  Total P&L:              ${total_pnl:+.2f}")
        print(f"  Stop-loss P&L:          ${sl_pnl:+.2f}  (avg ${sl_pnl/len(stops):+.2f}/trade)" if stops else "")
        print(f"  Take-profit P&L:        ${tp_pnl:+.2f}  (avg ${tp_pnl/len(tps):+.2f}/trade)" if tps else "")

    # ── Stop-loss breakdown ────────────────────────────────────
    if stops:
        print(f"\n🛑 STOP-LOSS BREAKDOWN")

        # By reason
        reasons = defaultdict(list)
        for t in stops:
            r = t.get("sl_reason", "unknown")
            if "price collapse" in r:
                reasons["price_collapse"].append(t)
            elif "signal broken" in r:
                reasons["signal_broken"].append(t)
            else:
                reasons["other"].append(t)

        for reason, group in sorted(reasons.items(), key=lambda x: -len(x[1])):
            avg_loss = sum(t["pnl"] for t in group if t.get("pnl")) / len(group)
            print(f"  {reason:20s}  {len(group):3d} trades  avg ${avg_loss:+.2f}")

        # Entry price distribution
        print(f"\n  Entry price distribution (stop-losses only):")
        buckets = defaultdict(list)
        for t in stops:
            bucket = (t["entry_c"] // 10) * 10
            buckets[f"{bucket}-{bucket+9}c"].append(t)
        for label in sorted(buckets.keys()):
            group    = buckets[label]
            avg_loss = sum(t["pnl"] for t in group if t.get("pnl")) / len(group)
            avg_loss_c = sum(t.get("loss_c", 0) for t in group) / len(group)
            print(f"    {label:10s}  {len(group):3d} trades  avg loss {avg_loss_c:.0f}c  (${avg_loss:+.2f})")

        # Loss magnitude
        loss_c_list = [t.get("loss_c", 0) for t in stops if t.get("loss_c")]
        if loss_c_list:
            print(f"\n  Stop-loss exit price collapse:")
            print(f"    Avg:  {sum(loss_c_list)/len(loss_c_list):.1f}c per contract")
            print(f"    Min:  {min(loss_c_list)}c")
            print(f"    Max:  {max(loss_c_list)}c")
            print(f"    >30c: {sum(1 for x in loss_c_list if x > 30)} trades ({sum(1 for x in loss_c_list if x > 30)/len(loss_c_list)*100:.0f}%)")
            print(f"    >40c: {sum(1 for x in loss_c_list if x > 40)} trades ({sum(1 for x in loss_c_list if x > 40)/len(loss_c_list)*100:.0f}%)")

    # ── Take-profit breakdown ──────────────────────────────────
    if tps:
        print(f"\n✅ TAKE-PROFIT BREAKDOWN")
        avg_gain = sum(t.get("gain_c", 0) for t in tps) / len(tps)
        print(f"  Avg gain per take-profit:  {avg_gain:.1f}c")

        entry_dist = defaultdict(list)
        for t in tps:
            bucket = (t["entry_c"] // 10) * 10
            entry_dist[f"{bucket}-{bucket+9}c"].append(t)
        print(f"  Entry price distribution:")
        for label in sorted(entry_dist.keys()):
            group    = entry_dist[label]
            avg_gain_c = sum(t.get("gain_c", 0) for t in group) / len(group)
            avg_pnl  = sum(t["pnl"] for t in group if t.get("pnl")) / max(len(group),1)
            print(f"    {label:10s}  {len(group):3d} trades  avg gain {avg_gain_c:.0f}c  (${avg_pnl:+.2f})")

    # ── BTC distance analysis ──────────────────────────────────
    trades_with_dist = [t for t in trades if t.get("distance") is not None]
    if trades_with_dist:
        print(f"\n📏 BTC DISTANCE FROM STRIKE AT ENTRY  ({len(trades_with_dist)} trades with distance data)")

        # Near-strike trades (distance < $50 — essentially coin flips)
        near   = [t for t in trades_with_dist if abs(t["distance"]) < 50]
        medium = [t for t in trades_with_dist if 50 <= abs(t["distance"]) < 200]
        far    = [t for t in trades_with_dist if abs(t["distance"]) >= 200]

        def group_stats(group, label):
            if not group: return
            sl_count = sum(1 for t in group if t.get("exit_type") == "stop_loss")
            tp_count = sum(1 for t in group if t.get("exit_type") == "take_profit")
            pnl_list = [t["pnl"] for t in group if t.get("pnl") is not None]
            avg_pnl  = sum(pnl_list) / len(pnl_list) if pnl_list else 0
            print(f"  {label:30s}  {len(group):3d} trades  SL={sl_count}  TP={tp_count}  avg P&L ${avg_pnl:+.2f}")

        group_stats(near,   "Near strike   (<$50)")
        group_stats(medium, "Medium        ($50–$200)")
        group_stats(far,    "Far from strike (>$200)")

    # ── Market observations summary ────────────────────────────
    total_obs = sum(len(m["observations"]) for m in markets.values())
    markets_with_trades = sum(1 for m in markets.values() if m["trades"])

    print(f"\n🔍 OBSERVATION DATA")
    print(f"  Total markets observed:        {len(markets)}")
    print(f"  Markets where we traded:       {markets_with_trades}")
    print(f"  Total YES price observations:  {total_obs:,}")
    print(f"  Avg observations per market:   {total_obs/max(len(markets),1):.0f}")

    # YES price distribution across all observations
    all_yes = [o["yes_c"] for m in markets.values() for o in m["observations"]]
    if all_yes:
        print(f"\n  YES price distribution (all observations):")
        buckets = defaultdict(int)
        for y in all_yes:
            b = (y // 10) * 10
            buckets[b] += 1
        for b in sorted(buckets.keys()):
            bar = "█" * (buckets[b] * 40 // max(buckets.values()))
            print(f"    {b:3d}-{b+9:3d}c  {buckets[b]:6,}  {bar}")

    # ── Risk:reward reality check ──────────────────────────────
    if stops and tps:
        print(f"\n⚖️  RISK:REWARD REALITY CHECK")
        avg_sl_loss = abs(sum(t["pnl"] for t in stops if t.get("pnl")) / len(stops))
        avg_tp_gain = sum(t["pnl"] for t in tps if t.get("pnl")) / len(tps)
        rr_ratio    = avg_sl_loss / avg_tp_gain if avg_tp_gain else float("inf")
        breakeven   = rr_ratio / (rr_ratio + 1) * 100
        actual_tp_rate = len(tps) / (len(stops) + len(tps)) * 100

        print(f"  Avg stop-loss loss:     ${avg_sl_loss:.2f}")
        print(f"  Avg take-profit gain:   ${avg_tp_gain:.2f}")
        print(f"  Risk:reward ratio:      {rr_ratio:.1f}:1  (you risk {rr_ratio:.1f}x what you make)")
        print(f"  Win rate needed to break even: {breakeven:.0f}%")
        print(f"  Actual take-profit rate:       {actual_tp_rate:.0f}%")
        verdict = "LOSING" if actual_tp_rate < breakeven else "WINNING"
        print(f"  Verdict: {verdict} STRATEGY  (need {breakeven:.0f}%, getting {actual_tp_rate:.0f}%)")

    print("\n" + "="*65)
    print(f"  Full data saved to: analysis_output.json")
    print("="*65 + "\n")


def main():
    if not LOG_PATH.exists():
        print(f"ERROR: Log file not found: {LOG_PATH}")
        sys.exit(1)

    markets, trades, balances = parse_log(LOG_PATH)

    print_report(markets, trades, balances)

    # Save full dataset for further analysis
    output = {
        "total_markets":      len(markets),
        "total_trades":       len(trades),
        "total_observations": sum(len(m["observations"]) for m in markets.values()),
        "balances":           balances,
        "trades":             trades,
        "markets_summary": [
            {
                "ticker":       k,
                "trade_count":  len(v["trades"]),
                "obs_count":    len(v["observations"]),
                "first_seen":   v.get("first_seen_at"),
            }
            for k, v in markets.items()
        ],
    }

    out_path = LOG_PATH.parent / "analysis_output.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)


if __name__ == "__main__":
    main()
