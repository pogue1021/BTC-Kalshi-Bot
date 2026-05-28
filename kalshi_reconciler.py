"""
kalshi_reconciler.py - Background P&L reconciliation

Strategy: call get_balance() every minute and compute
    daily_pnl = current_balance - balance_at_day_start

This is always exact — no fill math, no fee edge cases, no format guessing.
"""

import logging
import threading
import time
from typing import Optional

from bot_state import state

logger = logging.getLogger(__name__)


def _reconcile_once(client, warn_threshold):
    try:
        current_balance = client.get_balance()
    except Exception as e:
        logger.warning(f"Reconciler: balance fetch failed, will retry: {e}")
        return

    state.balance_dollars = current_balance

    start = float(getattr(state, "balance_at_day_start", 0.0) or 0.0)
    if start <= 0:
        # Day-start not set yet (very early startup) — nothing to report
        return

    daily_pnl = round(current_balance - start, 2)
    prev      = float(getattr(state, "live_daily_pnl", 0.0) or 0.0)
    drift     = daily_pnl - prev

    msg = (
        f"Reconciled: balance ${current_balance:.2f} | "
        f"day-start ${start:.2f} | "
        f"daily P&L ${daily_pnl:+.2f} | "
        f"(was ${prev:+.2f}, drift ${drift:+.2f})"
    )
    if abs(drift) >= warn_threshold:
        logger.warning(msg)
    else:
        logger.info(msg)

    state.live_daily_pnl = daily_pnl
    state.daily_pnl      = daily_pnl


def _reconcile_loop(client, interval_secs, warn_threshold):
    time.sleep(15)   # let the bot finish startup first
    logger.info(
        f"Reconciler started (balance-based) — "
        f"checking every {interval_secs/60:.1f} min"
    )
    while True:
        _reconcile_once(client, warn_threshold)
        time.sleep(interval_secs)


_reconciler_thread: Optional[threading.Thread] = None


def start_reconciler(client, config: dict) -> None:
    global _reconciler_thread

    rc_cfg = config.get("reconcile", {}) or {}
    if not bool(rc_cfg.get("enabled", False)):
        logger.info("Kalshi reconciler not enabled — skipping")
        return

    interval_min = float(rc_cfg.get("interval_minutes", 1))
    warn_thresh  = float(rc_cfg.get("warn_threshold", 1.0))

    _reconciler_thread = threading.Thread(
        target=_reconcile_loop,
        args=(client, interval_min * 60, warn_thresh),
        daemon=True,
        name="kalshi-reconciler",
    )
    _reconciler_thread.start()


# ── Legacy helpers kept so any code that imports them still compiles ──────────

def compute_daily_pnl(client, ticker_prefix=None) -> dict:
    """Deprecated — use get_balance() approach instead. Kept for compat."""
    bal = client.get_balance()
    start = float(getattr(state, "balance_at_day_start", 0.0) or 0.0)
    pnl = round(bal - start, 2) if start > 0 else 0.0
    return {"pnl": pnl, "balance": bal, "fills_count": 0, "settlements_count": 0}


def compute_lifetime_pnl(client, ticker_prefix=None) -> dict:
    """Deprecated — kept for compat."""
    return {"pnl": 0.0, "fills_count": 0, "settlements_count": 0}
