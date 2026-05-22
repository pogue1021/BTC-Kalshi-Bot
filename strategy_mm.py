"""
strategy_mm.py — BOT 3.0 Market Maker Strategy
================================================
Posts limit orders on both sides of active KXBTC15M markets and earns the
bid-ask spread when takers cross the quotes. Direction-agnostic.

Key rules:
  - Only quote when YES price > 85c (near YES) or < 15c (near NO).
    These zones have low tick volatility so spread capture is reliable.
  - Post 2c inside the natural bid and ask (configurable).
  - Reprice every 5 seconds if the market has moved.
  - Pull all quotes when net inventory exceeds hard limit.
  - Hold to settlement — no panic selling of inventory.

Imports only: bot_state_mm, telegram_kill, asyncio, logging, time,
              datetime, uuid  (no V1 / V2 dependencies).
"""

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

from bot_state_mm import state_mm, MMFillRecord
from telegram_kill import notify

logger = logging.getLogger("strategy_mm")


# ─────────────────────────────────────────────────────────────
# UTILITIES
# ─────────────────────────────────────────────────────────────

def _secs_left(close_time_str: str) -> float:
    try:
        close_time = datetime.fromisoformat(close_time_str.replace("Z", "+00:00"))
        return (close_time - datetime.now(timezone.utc)).total_seconds()
    except Exception:
        return 0.0


def _position_size(max_dollars: float, price_cents: int) -> int:
    if price_cents <= 0 or price_cents >= 100:
        return 0
    return max(1, int((max_dollars * 100) / price_cents))


# ─────────────────────────────────────────────────────────────
# ORDER MANAGEMENT
# ─────────────────────────────────────────────────────────────

def _already_resting(ticker: str, side: str, price_cents: int) -> bool:
    """True if we already have a live resting order at this side+price."""
    for o in state_mm.resting_orders.get(ticker, []):
        if o["side"] == side and o["price_cents"] == price_cents:
            return True
    return False


def _cancel_all_quotes(ticker: str, kalshi_client, paper_mode: bool):
    """Cancel every resting order for this ticker."""
    orders = list(state_mm.resting_orders.get(ticker, []))
    if not orders:
        return
    for o in orders:
        if not paper_mode and not o.get("paper"):
            try:
                kalshi_client.cancel_order(o["order_id"])
            except Exception as e:
                logger.debug(f"MM | {ticker} | cancel {o['order_id']}: {e}")
    state_mm.resting_orders[ticker] = []
    logger.debug(f"MM | {ticker} | cancelled all quotes ({len(orders)} orders)")


def _cancel_stale_quotes(ticker: str, kalshi_client, prices: dict,
                         inside_cents: int, paper_mode: bool):
    """
    Cancel any quote that is more than (inside_cents + 1)c away from where
    it would be priced today.  Stale = market moved, quote is off-price.
    """
    orders = state_mm.resting_orders.get(ticker, [])
    if not orders:
        return

    yes_bid = prices.get("yes_bid", 0)
    yes_ask = prices.get("yes_ask", 0)
    stale_ids = set()

    for o in orders:
        if o["side"] == "yes":
            target = yes_bid + inside_cents
            if abs(o["price_cents"] - target) > inside_cents + 3:
                stale_ids.add(o["order_id"])
        elif o["side"] == "no":
            our_yes_equiv = 100 - o["price_cents"]
            target_yes_ask = yes_ask - inside_cents
            if abs(our_yes_equiv - target_yes_ask) > inside_cents + 3:
                stale_ids.add(o["order_id"])

    if not stale_ids:
        return

    for o in orders:
        if o["order_id"] in stale_ids and not paper_mode and not o.get("paper"):
            try:
                kalshi_client.cancel_order(o["order_id"])
            except Exception as e:
                logger.debug(f"MM | {ticker} | stale cancel {o['order_id']}: {e}")

    state_mm.resting_orders[ticker] = [
        o for o in orders if o["order_id"] not in stale_ids
    ]
    logger.debug(f"MM | {ticker} | cancelled {len(stale_ids)} stale quote(s)")


def _place_quote(ticker: str, side: str, price_cents: int, contracts: int,
                 kalshi_client, paper_mode: bool):
    """Place a limit order and record it in resting_orders."""
    if paper_mode:
        order_id = f"PAPER-{uuid.uuid4().hex[:8]}"
    else:
        try:
            resp     = kalshi_client.place_order(
                ticker, side, price_cents, contracts, paper_mode=False
            )
            order    = resp.get("order", {}) or {}
            order_id = order.get("order_id") or f"LIVE-{uuid.uuid4().hex[:8]}"
        except Exception as e:
            logger.warning(f"MM | {ticker} | place_order failed ({side}@{price_cents}c): {e}")
            return

    state_mm.resting_orders.setdefault(ticker, []).append({
        "order_id":    order_id,
        "side":        side,
        "price_cents": price_cents,
        "contracts":   contracts,
        "placed_at":   time.time(),
        "paper":       paper_mode,
    })
    side_label = "YES BID" if side == "yes" else f"NO BID (YES ASK@{100 - price_cents}c)"
    logger.info(
        f"MM | {ticker} | QUOTE | {side_label} {contracts}x @ {price_cents}c "
        f"({'PAPER' if paper_mode else 'LIVE'})"
    )


# ─────────────────────────────────────────────────────────────
# FILL DETECTION
# ─────────────────────────────────────────────────────────────

def _detect_fills_live(ticker: str, kalshi_client) -> List[dict]:
    """
    Compare our tracked resting orders against Kalshi's open-order list.
    Any order we think is resting that no longer appears = filled.
    """
    try:
        open_orders = kalshi_client.get_open_orders(ticker=ticker)
    except Exception as e:
        logger.warning(f"MM | {ticker} | get_open_orders failed: {e}")
        return []

    open_ids    = {o.get("order_id", "") for o in open_orders}
    our_resting = state_mm.resting_orders.get(ticker, [])
    filled      = [o for o in our_resting if o["order_id"] not in open_ids]

    # Remove filled orders from tracked list
    state_mm.resting_orders[ticker] = [
        o for o in our_resting if o["order_id"] in open_ids
    ]
    return filled


def _detect_fills_paper(ticker: str, prices: dict) -> List[dict]:
    """
    Paper-mode fill simulation. After 30 seconds, an order fills if the
    market price hasn't moved more than 3c AGAINST our quote.

    As a market maker we post inside the spread, meaning we ARE the best
    bid/ask. Takers cross us — the market price doesn't need to come to us.
    The 3c tolerance lets the simulation correctly capture fills in the YES
    zone (prices moving toward 100c) and NO zone (prices toward 0c) where
    the market moves WITH us, not against us.

    A quote is abandoned (stays resting) only if the market moved sharply
    the wrong way, making our price uncompetitive by more than 3c.
    """
    our_resting = state_mm.resting_orders.get(ticker, [])
    if not our_resting:
        return []

    yes_bid = prices.get("yes_bid", 0)
    yes_ask = prices.get("yes_ask", 0)
    now     = time.time()
    tolerance = 3   # cents — market can move this far against us and still fill
    filled, still_resting = [], []

    for o in our_resting:
        age = now - o.get("placed_at", now)
        if age < 10:
            still_resting.append(o)
            continue

        if o["side"] == "yes":
            # YES bid at X: fill if market hasn't dropped more than `tolerance`c
            # below our bid (i.e., we're still competitive / were crossed)
            if yes_bid >= o["price_cents"] - tolerance:
                filled.append(o)
            else:
                still_resting.append(o)
        else:  # "no"
            # NO bid (YES ask at Y): fill if market hasn't risen more than
            # `tolerance`c above our YES-equivalent ask
            our_yes_equiv = 100 - o["price_cents"]
            if yes_ask <= our_yes_equiv + tolerance:
                filled.append(o)
            else:
                still_resting.append(o)

    state_mm.resting_orders[ticker] = still_resting
    return filled


# ─────────────────────────────────────────────────────────────
# FILL RECORDING
# ─────────────────────────────────────────────────────────────

def _record_fill(ticker: str, order: dict):
    """
    Process a filled order: update inventory, FIFO P&L, and fill log.
    """
    side        = order["side"]         # "yes" or "no"
    price_c     = order["price_cents"]
    contracts   = order["contracts"]
    pnl_dollars = None

    if side == "yes":
        # Bought YES contracts → long inventory, push to FIFO cost queue
        state_mm.inventory[ticker] = state_mm.inventory.get(ticker, 0) + contracts
        state_mm._buy_queue.setdefault(ticker, []).append((price_c, contracts))
        yes_price_c = price_c
        fill_side   = "yes_buy"

    else:
        # Bought NO (= sold YES) → short inventory, FIFO-match against buys
        yes_equiv   = 100 - price_c          # YES-equivalent price of this NO trade
        state_mm.inventory[ticker] = state_mm.inventory.get(ticker, 0) - contracts

        realized_cents = 0.0
        remaining      = contracts
        buy_queue      = state_mm._buy_queue.get(ticker, [])

        while remaining > 0 and buy_queue:
            buy_price_c, buy_qty = buy_queue[0]
            matched        = min(buy_qty, remaining)
            realized_cents += (yes_equiv - buy_price_c) * matched
            remaining      -= matched
            if matched == buy_qty:
                buy_queue.pop(0)
            else:
                buy_queue[0] = (buy_price_c, buy_qty - matched)

        pnl_dollars = round(realized_cents / 100.0, 4)
        if pnl_dollars != 0:
            state_mm.record_realized_pnl(pnl_dollars)

        yes_price_c = yes_equiv
        fill_side   = "yes_sell"

    net_inv  = state_mm.inventory.get(ticker, 0)
    fill     = MMFillRecord(
        fill_id     = f"fill-{uuid.uuid4().hex[:8]}",
        ticker      = ticker,
        side        = fill_side,
        price_cents = yes_price_c,
        contracts   = contracts,
        filled_at   = time.time(),
        pnl_dollars = pnl_dollars,
    )
    state_mm.add_fill(fill)

    pnl_str = f" | round-trip ${pnl_dollars:+.4f}" if pnl_dollars is not None else ""
    logger.info(
        f"MM | {ticker} | FILL | {fill_side.replace('_',' ').upper()} "
        f"{contracts}x @ {yes_price_c}c | inventory={net_inv:+d}{pnl_str}"
    )

    mode_tag = "PAPER" if state_mm.paper_mode else "LIVE"
    notify(
        f"MM FILL [{mode_tag}]: {fill_side.replace('_',' ').upper()} "
        f"{contracts}x @ {yes_price_c}c on {ticker} | inv={net_inv:+d}"
        + (f" | P&L ${pnl_dollars:+.4f}" if pnl_dollars else "")
    )


# ─────────────────────────────────────────────────────────────
# SETTLEMENT WATCHER
# ─────────────────────────────────────────────────────────────

async def _watch_settlement_mm(kalshi_client, ticker: str, close_time_iso: str):
    """
    Wait for a market to settle, then calculate settlement P&L on any
    remaining inventory and clean up all state for that ticker.
    """
    close_time = datetime.fromisoformat(close_time_iso.replace("Z", "+00:00"))

    while True:
        now              = datetime.now(timezone.utc)
        secs_since_close = (now - close_time).total_seconds()

        if secs_since_close < 90:
            await asyncio.sleep(15)
            continue

        try:
            data    = kalshi_client._get(f"/markets/{ticker}")
            market  = data.get("market", {})
            status  = market.get("status", "")
            result  = market.get("result", "")

            is_settled = (
                status in ("settled", "finalized", "resolved") or
                (status == "closed" and result != "")
            )

            if is_settled and result != "":
                settled_yes = result.lower() == "yes"
                buy_queue   = state_mm._buy_queue.get(ticker, [])
                net_inv     = state_mm.inventory.get(ticker, 0)
                settle_cents = 0.0

                # Settle remaining YES long positions from FIFO queue
                for buy_price_c, qty in buy_queue:
                    if settled_yes:
                        settle_cents += (100 - buy_price_c) * qty
                    else:
                        settle_cents -= buy_price_c * qty

                # Settle any net short position not covered by buy queue
                # (more NO buys than YES buys → net short YES)
                residual_short = max(0, -net_inv - sum(q for _, q in buy_queue))
                if residual_short > 0 and not settled_yes:
                    # Short YES + YES lost → gain (we sold YES that's worth 0)
                    # The gain was already realized at sale price; cost is 0 at settlement
                    pass  # already captured in FIFO

                settle_dollars = round(settle_cents / 100.0, 4)

                if net_inv != 0 or buy_queue:
                    state_mm.record_settlement_pnl(settle_dollars)
                    result_str = "YES" if settled_yes else "NO"
                    logger.info(
                        f"MM | {ticker} | SETTLED {result_str} | "
                        f"net_inv={net_inv:+d} | settle P&L ${settle_dollars:+.4f}"
                    )
                    mode_tag = "PAPER" if state_mm.paper_mode else "LIVE"
                    notify(
                        f"MM SETTLED [{mode_tag}]: {ticker} → {result_str} | "
                        f"net_inv={net_inv:+d} | P&L ${settle_dollars:+.4f}"
                    )

                # Clean up all state for this ticker
                state_mm.inventory.pop(ticker, None)
                state_mm.resting_orders.pop(ticker, None)
                state_mm._buy_queue.pop(ticker, None)
                return

            if secs_since_close > 1800:
                logger.warning(f"MM | {ticker} | settlement timeout — cleaning up")
                state_mm.inventory.pop(ticker, None)
                state_mm.resting_orders.pop(ticker, None)
                state_mm._buy_queue.pop(ticker, None)
                return

        except Exception as e:
            logger.warning(f"MM | {ticker} | settlement check error: {e}")

        await asyncio.sleep(30)


# ─────────────────────────────────────────────────────────────
# PER-MARKET QUOTING LOGIC
# ─────────────────────────────────────────────────────────────

async def _process_market(ticker: str, close_time: str, kalshi_client) -> dict:
    """
    Run one full quoting iteration for a single market.
    Returns a dict for the dashboard's active_markets_info list.
    """
    cfg        = state_mm.settings
    paper_mode = state_mm.paper_mode
    secs       = _secs_left(close_time)

    info = {
        "ticker":       ticker,
        "seconds_left": round(secs),
        "yes_bid":      0,
        "yes_ask":      0,
        "spread":       0,
        "quoting":      False,
        "skip_reason":  "",
        "inventory":    state_mm.inventory.get(ticker, 0),
        "resting_bid":  None,
        "resting_ask":  None,
    }

    try:
        prices  = kalshi_client.get_market_prices(ticker)
        yes_bid = prices.get("yes_bid", 0)
        yes_ask = prices.get("yes_ask", 0)
        spread  = (yes_ask - yes_bid) if yes_ask > yes_bid else 0

        info["yes_bid"] = yes_bid
        info["yes_ask"] = yes_ask
        info["spread"]  = spread

        # ── Guard: no book yet (market just opened or between cycles) ─────
        if yes_bid == 0 or yes_ask == 0:
            info["skip_reason"] = "No prices yet — market just opened"
            return info

        # ── Gate: daily loss limit ────────────────────────────────────────
        if state_mm.daily_pnl_dollars <= -cfg.max_daily_loss:
            _cancel_all_quotes(ticker, kalshi_client, paper_mode)
            info["skip_reason"] = f"Daily loss limit (${state_mm.daily_pnl_dollars:.2f})"
            return info

        # ── Gate: time remaining ──────────────────────────────────────────
        if secs < cfg.min_seconds_remaining:
            _cancel_all_quotes(ticker, kalshi_client, paper_mode)
            info["skip_reason"] = f"Too close to close ({secs:.0f}s)"
            return info

        # ── Gate: price zone ──────────────────────────────────────────────
        in_yes_zone = yes_bid >= cfg.quote_yes_threshold
        in_no_zone  = yes_bid <= cfg.quote_no_threshold
        if not in_yes_zone and not in_no_zone:
            _cancel_all_quotes(ticker, kalshi_client, paper_mode)
            info["skip_reason"] = (
                f"YES={yes_bid}c not in zone (>{cfg.quote_yes_threshold}c "
                f"or <{cfg.quote_no_threshold}c)"
            )
            return info

        # ── Gate: spread wide enough ──────────────────────────────────────
        if spread < cfg.min_market_spread_cents:
            info["skip_reason"] = f"Spread {spread}c < {cfg.min_market_spread_cents}c minimum"
            return info

        # ── Gate: hard inventory limit ────────────────────────────────────
        net_inv = state_mm.inventory.get(ticker, 0)
        if abs(net_inv) > cfg.hard_inventory_limit:
            _cancel_all_quotes(ticker, kalshi_client, paper_mode)
            info["skip_reason"] = f"Hard inventory limit ({net_inv:+d} contracts)"
            return info

        # ── Detect and record fills from previous loop ────────────────────
        filled = (
            _detect_fills_paper(ticker, prices) if paper_mode
            else _detect_fills_live(ticker, kalshi_client)
        )
        for order in filled:
            _record_fill(ticker, order)

        # Re-read inventory after fills
        net_inv         = state_mm.inventory.get(ticker, 0)
        info["inventory"] = net_inv

        # ── Cancel stale quotes ───────────────────────────────────────────
        _cancel_stale_quotes(ticker, kalshi_client, prices, cfg.quote_inside_cents, paper_mode)

        # ── Determine what to post ────────────────────────────────────────
        want_bid = True
        want_ask = True
        if net_inv >= cfg.max_inventory_contracts:
            want_bid = False   # already long, don't add more YES
        if net_inv <= -cfg.max_inventory_contracts:
            want_ask = False   # already short, don't sell more YES

        # ── Post YES bid ──────────────────────────────────────────────────
        if want_bid:
            bid_price = min(yes_bid + cfg.quote_inside_cents, yes_ask - 1)
            bid_price = max(1, bid_price)
            if not _already_resting(ticker, "yes", bid_price):
                qty = _position_size(cfg.max_bet_dollars, bid_price)
                if qty > 0:
                    _place_quote(ticker, "yes", bid_price, qty, kalshi_client, paper_mode)

        # ── Post YES ask via NO buy ───────────────────────────────────────
        if want_ask:
            ask_price = max(yes_ask - cfg.quote_inside_cents, yes_bid + 1)
            ask_price = min(99, ask_price)
            no_price  = max(1, min(99, 100 - ask_price))
            if not _already_resting(ticker, "no", no_price):
                qty = _position_size(cfg.max_bet_dollars, no_price)
                if qty > 0:
                    _place_quote(ticker, "no", no_price, qty, kalshi_client, paper_mode)

        # ── Update dashboard info ─────────────────────────────────────────
        info["quoting"] = True
        resting = state_mm.resting_orders.get(ticker, [])
        bid_o   = next((o for o in resting if o["side"] == "yes"), None)
        ask_o   = next((o for o in resting if o["side"] == "no"),  None)
        info["resting_bid"] = bid_o["price_cents"]          if bid_o else None
        info["resting_ask"] = 100 - ask_o["price_cents"]    if ask_o else None

        bid_str = f"{info['resting_bid']}c" if info["resting_bid"] else "—"
        ask_str = f"{info['resting_ask']}c" if info["resting_ask"] else "—"
        logger.info(
            f"MM | {ticker} | QUOTING | YES={yes_bid}c spread={spread}c "
            f"| bid@{bid_str} ask@{ask_str} | inventory={net_inv:+d}"
        )

    except Exception as e:
        logger.error(f"MM | {ticker} | process error: {e}", exc_info=True)
        info["skip_reason"] = f"Error: {e}"

    return info


# ─────────────────────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────────────────────

async def mm_trading_loop(price_store, kalshi_client):
    """
    BOT 3.0 market-maker main loop. Runs alongside V1 and V2
    via asyncio.gather() in main.py.
    """
    logger.info("BOT 3.0: Market maker loop started")

    # Tracks which tickers already have a settlement watcher spawned
    settlement_watchers: Dict[str, bool] = {}

    while True:
        try:
            # ── Sync price feeds ──────────────────────────────────────────
            state_mm.cf_estimate     = price_store.get_cf_estimate()
            state_mm.feeds_connected = price_store.is_ready()

            if not state_mm.trading_enabled:
                state_mm.status = "Disarmed — press ARM to start"
                await asyncio.sleep(5)
                continue

            if not price_store.is_ready():
                state_mm.status = "Waiting for price feeds..."
                await asyncio.sleep(5)
                continue

            # ── Get all active KXBTC15M markets ──────────────────────────
            try:
                raw_markets = kalshi_client.get_open_btc_markets()
            except Exception as e:
                logger.warning(f"BOT 3.0: Could not fetch markets: {e}")
                await asyncio.sleep(10)
                continue

            active_tickers = set()
            markets_info   = []

            for market in raw_markets:
                ticker     = market.get("ticker", "")
                close_time = market.get("close_time", "")
                if not ticker or not close_time:
                    continue

                secs = _secs_left(close_time)
                if secs <= 0:
                    continue

                active_tickers.add(ticker)

                # Spawn settlement watcher once per ticker
                if ticker not in settlement_watchers:
                    settlement_watchers[ticker] = True
                    asyncio.create_task(
                        _watch_settlement_mm(kalshi_client, ticker, close_time)
                    )
                    logger.debug(f"MM | {ticker} | settlement watcher started")

                # Run quoting logic for this market
                info = await _process_market(ticker, close_time, kalshi_client)
                markets_info.append(info)

            # ── Clean up watchers for expired tickers ─────────────────────
            for old in list(settlement_watchers):
                if old not in active_tickers:
                    del settlement_watchers[old]

            # ── Update dashboard ──────────────────────────────────────────
            state_mm.active_markets_info = markets_info

            if not markets_info:
                state_mm.status = "No active markets — waiting..."
            else:
                n_quoting = sum(1 for m in markets_info if m["quoting"])
                if n_quoting:
                    state_mm.status = f"Quoting on {n_quoting}/{len(markets_info)} market(s)"
                else:
                    reasons = [m["skip_reason"] for m in markets_info if m.get("skip_reason")]
                    state_mm.status = reasons[0] if reasons else "Watching — conditions not met"

        except Exception as e:
            logger.error(f"BOT 3.0: Unexpected error: {e}", exc_info=True)

        await asyncio.sleep(5)
