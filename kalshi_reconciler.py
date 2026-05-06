"""
kalshi_reconciler.py - Background P&L reconciliation
"""

import logging
import threading
import time
from datetime import datetime
from typing import Optional

from bot_state import state

logger = logging.getLogger(__name__)

_reconciler_thread: Optional[threading.Thread] = None
_logged_sample_fill: bool = False
_logged_sample_settlement: bool = False


def _midnight_local_today() -> int:
    """Unix timestamp of local midnight — matches how risk_manager resets daily P&L."""
    now = datetime.now()  # local time, no timezone
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp())


def _per_contract_price_cents(fill: dict) -> int:
    def _to_cents(raw) -> int:
        if raw is None:
            return 0
        val = float(raw)
        if val <= 1.0:
            return int(round(val * 100))
        return int(val)

    side = (fill.get("side") or "").lower()
    if side == "yes":
        raw = fill.get("yes_price_dollars") or fill.get("yes_price")
        return _to_cents(raw)
    raw_no = fill.get("no_price_dollars") or fill.get("no_price")
    if raw_no is not None:
        return _to_cents(raw_no)
    raw_yes = fill.get("yes_price_dollars") or fill.get("yes_price")
    return 100 - _to_cents(raw_yes)


def _fee_dollars(fill: dict) -> float:
    raw = fill.get("fee_cost") or fill.get("fees") or fill.get("fee") or 0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _compute_pnl_from_fills(fills: list, settlements: list) -> dict:
    cost = 0.0
    proceeds = 0.0
    fees_paid = 0.0

    for fill in fills:
        per_contract = _per_contract_price_cents(fill)
        count = int(float(fill.get("count_fp") or fill.get("count") or 0))
        amount_dollars = (per_contract * count) / 100.0
        action = (fill.get("action") or "").lower()
        if action == "buy":
            cost += amount_dollars
        elif action == "sell":
            proceeds += amount_dollars
        fees_paid += _fee_dollars(fill)

    settlement_revenue = 0.0
    for s in settlements:
        rev = s.get("revenue") or 0
        try:
            rev = float(rev)
        except (TypeError, ValueError):
            rev = 0.0
        if rev > 1000:
            rev = rev / 100.0
        settlement_revenue += rev

    pnl = proceeds + settlement_revenue - cost - fees_paid
    return {
        "pnl":               round(pnl, 2),
        "cost":              round(cost, 2),
        "proceeds":          round(proceeds, 2),
        "settlements":       round(settlement_revenue, 2),
        "fees":              round(fees_paid, 2),
        "fills_count":       len(fills),
        "settlements_count": len(settlements),
    }


def compute_daily_pnl(client, ticker_prefix: Optional[str] = None) -> dict:
    midnight = _midnight_local_today()
    fills = client.get_fills(min_ts=midnight)
    settlements = client.get_settlements(min_ts=midnight)

    if ticker_prefix:
        fills = [f for f in fills if (f.get("ticker") or "").startswith(ticker_prefix)]
        settlements = [s for s in settlements if (s.get("ticker") or "").startswith(ticker_prefix)]

    global _logged_sample_fill, _logged_sample_settlement
    if not _logged_sample_fill and fills:
        logger.info(f"DIAGNOSTIC sample fill: {fills[0]}")
        _logged_sample_fill = True
    if not _logged_sample_settlement and settlements:
        logger.info(f"DIAGNOSTIC sample settlement: {settlements[0]}")
        _logged_sample_settlement = True

    return _compute_pnl_from_fills(fills, settlements)


def compute_lifetime_pnl(client, ticker_prefix: Optional[str] = None) -> dict:
    fills = client.get_fills()
    settlements = client.get_settlements()

    if ticker_prefix:
        fills = [f for f in fills if (f.get("ticker") or "").startswith(ticker_prefix)]
        settlements = [s for s in settlements if (s.get("ticker") or "").startswith(ticker_prefix)]

    return _compute_pnl_from_fills(fills, settlements)


def _sync_lifetime_pnl_once(client, ticker_prefix, fallback_pnl):
    time.sleep(10)
    scope = f"[{ticker_prefix}*]" if ticker_prefix else "[all markets]"
    try:
        result = compute_lifetime_pnl(client, ticker_prefix=ticker_prefix)
    except Exception as e:
        logger.warning(f"Lifetime P&L sync failed - keeping local fallback (${fallback_pnl:+.2f}): {e}")
        return

    kalshi_total = result["pnl"]
    logger.info(
        f"Lifetime P&L synced from Kalshi {scope}: "
        f"buys -${result['cost']:.2f} | "
        f"sells +${result['proceeds']:.2f} | "
        f"settled +${result['settlements']:.2f} | "
        f"fees -${result['fees']:.2f} | "
        f"NET = ${kalshi_total:+.2f} "
        f"({result['fills_count']} fills, {result['settlements_count']} settlements)"
    )

    state.live_total_pnl = kalshi_total
    state.total_pnl      = kalshi_total  # headline shows live only


def _reconcile_once(client, warn_threshold, ticker_prefix=None):
    try:
        result = compute_daily_pnl(client, ticker_prefix=ticker_prefix)
    except Exception as e:
        logger.warning(f"Reconciler: API error, will retry next interval: {e}")
        return

    kalshi_pnl = result["pnl"]
    local_live_pnl = float(getattr(state, "live_daily_pnl", 0.0) or 0.0)
    drift = kalshi_pnl - local_live_pnl

    scope = f"[{ticker_prefix}*]" if ticker_prefix else "[all markets]"
    msg = (
        f"Reconciled today {scope}: "
        f"buys -${result['cost']:.2f} | "
        f"sells +${result['proceeds']:.2f} | "
        f"settled +${result['settlements']:.2f} | "
        f"fees -${result['fees']:.2f} | "
        f"NET = ${kalshi_pnl:+.2f} | "
        f"(local was ${local_live_pnl:+.2f}, drift ${drift:+.2f}) | "
        f"{result['fills_count']} fills, {result['settlements_count']} settlements"
    )
    if abs(drift) >= warn_threshold:
        logger.warning(msg)
    else:
        logger.info(msg)

    state.live_daily_pnl = kalshi_pnl
    state.daily_pnl      = kalshi_pnl  # headline shows live only


def _reconcile_loop(client, interval_secs, warn_threshold, ticker_prefix=None):
    time.sleep(15)
    scope = f"[{ticker_prefix}*]" if ticker_prefix else "[all markets]"
    logger.info(
        f"Reconciler started {scope} - checking Kalshi every "
        f"{interval_secs/60:.1f} min (warn threshold ${warn_threshold:.2f})"
    )
    while True:
        _reconcile_once(client, warn_threshold, ticker_prefix=ticker_prefix)
        time.sleep(interval_secs)


def start_reconciler(client, config: dict) -> None:
    global _reconciler_thread

    rc_cfg = config.get("reconcile", {}) or {}
    if not bool(rc_cfg.get("enabled", False)):
        logger.info("Kalshi reconciler not enabled - skipping")
        return

    interval_min  = float(rc_cfg.get("interval_minutes", 5))
    warn_thresh   = float(rc_cfg.get("warn_threshold", 1.0))
    series_ticker = (config.get("markets", {}) or {}).get("series_ticker") or None

    # Daily P&L daemon - runs every interval_min minutes forever
    _reconciler_thread = threading.Thread(
        target=_reconcile_loop,
        args=(client, interval_min * 60, warn_thresh, series_ticker),
        daemon=True,
        name="kalshi-reconciler",
    )
    _reconciler_thread.start()

    # One-time startup thread: pull all-time BTC P&L from Kalshi and set
    # state.live_total_pnl / state.total_pnl to the real Kalshi number.
    # Falls back to the local trade-history value if the API call fails.
    fallback = float(getattr(state, "live_total_pnl", 0.0) or 0.0)
    threading.Thread(
        target=_sync_lifetime_pnl_once,
        args=(client, series_ticker, fallback),
        daemon=True,
        name="kalshi-lifetime-pnl",
    ).start()
