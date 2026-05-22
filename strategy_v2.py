"""
strategy_v2.py — BOT 2.0 Late-Window Edge Strategy
====================================================
Core philosophy: do nothing for the first 12 minutes of each 15-min cycle.
In the final 3 minutes, if BTC is clearly far from the strike AND Kalshi
hasn't fully priced that in yet, enter once and hold to settlement.

No stop-losses. No momentum signals. One trade per cycle maximum.
The edge comes from Kalshi's late-cycle mispricing, not from predicting direction.
"""

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone

from bot_state_v2 import state_v2, V2TradeRecord
from telegram_kill import notify

logger = logging.getLogger("strategy_v2")


async def v2_trading_loop(price_store, kalshi_client):
    """
    BOT 2.0 main loop. Runs as a coroutine alongside the V1 loop.
    Checks for edge in the final minutes of each 15-min Kalshi market.
    """
    logger.info("BOT 2.0: Strategy loop started (late-window edge mode)")

    current_ticker     = None
    settlement_task    = None

    while True:
        try:
            # ── Sync price feeds ─────────────────────────────────────────
            state_v2.cf_estimate     = price_store.get_cf_estimate()
            state_v2.feeds_connected = price_store.is_ready()

            # ── Gate: only run when armed ─────────────────────────────────
            if not state_v2.trading_enabled:
                state_v2.status = "Disarmed — press ARM to start"
                await asyncio.sleep(5)
                continue

            if not price_store.is_ready():
                state_v2.status = "Waiting for price feeds..."
                await asyncio.sleep(5)
                continue

            # ── Get active market ─────────────────────────────────────────
            market = kalshi_client.get_active_15min_market()
            if market is None:
                state_v2.status          = "No active market — waiting..."
                state_v2.current_market  = None
                state_v2.floor_strike    = None
                state_v2.seconds_until_close = None
                await asyncio.sleep(30)
                continue

            ticker             = market.get("ticker", "")
            close_time_iso     = market.get("close_time", "")
            seconds_left       = market.get("_seconds_until_close", 0)
            floor_strike       = market.get("_floor_strike")

            # ── New cycle detection ───────────────────────────────────────
            if ticker != current_ticker:
                # Do NOT cancel settlement_task here — it must keep running
                # across cycle boundaries so it can record the result when
                # the previous market actually settles. Dropping the reference
                # is enough; asyncio tasks run until done or explicitly cancelled.
                logger.info(f"BOT 2.0: New market {ticker} | {seconds_left/60:.1f} min left | strike ${floor_strike:,.0f}")
                current_ticker             = ticker
                state_v2.traded_this_cycle = False
                state_v2.current_trade     = None
                settlement_task            = None

            # Update dashboard state
            state_v2.current_market      = ticker
            state_v2.seconds_until_close = seconds_left
            state_v2.floor_strike        = floor_strike

            # ── Compute distance ──────────────────────────────────────────
            cf = state_v2.cf_estimate
            if cf is None or floor_strike is None:
                state_v2.status = "Waiting for price data..."
                await asyncio.sleep(5)
                continue

            distance = cf - floor_strike   # positive = above strike, negative = below
            state_v2.distance_dollars = distance

            # ── Get market prices ─────────────────────────────────────────
            try:
                prices  = kalshi_client.get_market_prices(ticker)
                yes_ask = prices.get("yes_ask", 0)
                no_ask  = prices.get("no_ask", 0)
                state_v2.market_yes_price = yes_ask
                state_v2.market_no_price  = no_ask
            except Exception as e:
                logger.warning(f"BOT 2.0: Could not fetch prices: {e}")
                await asyncio.sleep(5)
                continue

            # ── Monitor open position (hold to settlement — no stop-loss) ─
            if state_v2.current_trade is not None:
                trade      = state_v2.current_trade
                secs_held  = time.time() - trade.opened_at
                sell_price = yes_ask if trade.side == "yes" else no_ask
                gain_cents = sell_price - trade.price_cents if sell_price > 0 else 0
                state_v2.status = (
                    f"Holding {trade.side.upper()} @ {trade.price_cents}c | "
                    f"now {sell_price}c ({gain_cents:+d}c) | "
                    f"{seconds_left/60:.1f}m left — holding to settlement"
                )
                await asyncio.sleep(5)
                continue

            # ── Already traded this cycle ─────────────────────────────────
            if state_v2.traded_this_cycle:
                mins_left = seconds_left / 60
                state_v2.status = f"Traded this cycle — waiting for next market ({mins_left:.1f}m left)"
                await asyncio.sleep(5)
                continue

            # ── Entry window gate: only act in final N minutes ────────────
            entry_window_secs = state_v2.settings.entry_window_minutes * 60
            if seconds_left > entry_window_secs:
                mins_to_window = (seconds_left - entry_window_secs) / 60
                dist_str = f"${abs(distance):,.0f} {'above' if distance > 0 else 'below'} strike"
                state_v2.status = (
                    f"Watching — BTC {dist_str} | "
                    f"entry window in {mins_to_window:.1f}m"
                )
                await asyncio.sleep(5)
                continue

            # ── Distance gate ─────────────────────────────────────────────
            min_dist = state_v2.settings.min_distance_dollars
            if abs(distance) < min_dist:
                state_v2.status = (
                    f"In window but BTC only ${abs(distance):,.0f} from strike "
                    f"(need ${min_dist:,.0f}) — skipping"
                )
                await asyncio.sleep(5)
                continue

            # ── Determine side and check edge ─────────────────────────────
            side       = "yes" if distance > 0 else "no"
            entry_price = yes_ask if side == "yes" else no_ask

            if entry_price <= 0:
                state_v2.status = "No market prices available"
                await asyncio.sleep(5)
                continue

            # Edge check: is Kalshi still offering a good price?
            if side == "yes":
                max_yes = state_v2.settings.max_entry_yes_cents
                if yes_ask > max_yes:
                    state_v2.status = (
                        f"YES signal but market already priced in "
                        f"({yes_ask}c > {max_yes}c threshold) — no edge"
                    )
                    await asyncio.sleep(5)
                    continue
            else:
                min_yes_for_no = state_v2.settings.min_entry_no_yes_cents
                if yes_ask < min_yes_for_no:
                    state_v2.status = (
                        f"NO signal but YES already cheap "
                        f"({yes_ask}c < {min_yes_for_no}c threshold) — no edge"
                    )
                    await asyncio.sleep(5)
                    continue

            # ── EDGE FOUND — enter trade ──────────────────────────────────
            max_bet       = state_v2.settings.max_bet_dollars
            num_contracts = max(1, int((max_bet * 100) / entry_price))
            cost_dollars  = round(num_contracts * entry_price / 100, 2)
            # Expected gain if we hold to settlement and win
            edge_cents    = 100 - entry_price

            logger.info(
                f"BOT 2.0: ENTRY {side.upper()} | {ticker} | "
                f"{num_contracts}x @ {entry_price}c | "
                f"BTC ${cf:,.0f} vs strike ${floor_strike:,.0f} "
                f"(${abs(distance):,.0f} {'above' if distance > 0 else 'below'}) | "
                f"{'PAPER' if state_v2.paper_mode else 'LIVE'}"
            )

            try:
                if not state_v2.paper_mode:
                    kalshi_client.place_order(
                        ticker=ticker, side=side,
                        price_cents=entry_price, num_contracts=num_contracts,
                        paper_mode=False,
                    )

                trade_id = str(uuid.uuid4())[:8]
                trade = V2TradeRecord(
                    trade_id      = trade_id,
                    ticker        = ticker,
                    side          = side,
                    price_cents   = entry_price,
                    num_contracts = num_contracts,
                    cost_dollars  = cost_dollars,
                    opened_at     = time.time(),
                    entry_btc     = cf,
                    entry_strike  = floor_strike,
                )

                state_v2.current_trade     = trade
                state_v2.traded_this_cycle = True
                state_v2.add_trade(trade)

                mode_tag = "PAPER" if state_v2.paper_mode else "LIVE"
                state_v2.status = (
                    f"[{mode_tag}] {side.upper()} {num_contracts}x @ {entry_price}c | "
                    f"BTC ${abs(distance):,.0f} from strike — holding to settlement"
                )
                notify(
                    f"BOT2 ENTRY [{mode_tag}]: {side.upper()} {num_contracts}x @ {entry_price}c "
                    f"(+{edge_cents}c if wins) | BTC ${cf:,.0f} vs strike ${floor_strike:,.0f} "
                    f"(${abs(distance):,.0f} {'above' if distance > 0 else 'below'}) on {ticker}"
                )

                # Watch for settlement
                settlement_task = asyncio.create_task(
                    _watch_settlement(
                        kalshi_client  = kalshi_client,
                        trade          = trade,
                        ticker         = ticker,
                        close_time_iso = close_time_iso,
                    )
                )

            except Exception as e:
                logger.error(f"BOT 2.0: Order failed: {e}")
                state_v2.status = f"Order error: {e}"

        except Exception as e:
            logger.error(f"BOT 2.0: Unexpected error: {e}", exc_info=True)

        await asyncio.sleep(5)


async def _watch_settlement(kalshi_client, trade, ticker, close_time_iso):
    """
    Wait for the market to settle, then record the P&L.
    No stop-loss — we always hold to the end. This is the whole point.
    """
    logger.info(f"BOT 2.0: Watching {ticker} for settlement...")
    close_time = datetime.fromisoformat(close_time_iso.replace("Z", "+00:00"))

    while True:
        now             = datetime.now(timezone.utc)
        secs_since_close = (now - close_time).total_seconds()

        if secs_since_close < 90:
            await asyncio.sleep(15)
            continue

        try:
            market_data = kalshi_client._get(f"/markets/{ticker}")
            market      = market_data.get("market", {})
            status      = market.get("status", "")
            result      = market.get("result", "")

            is_settled = (
                status in ("settled", "finalized", "resolved") or
                (status == "closed" and result != "")
            )

            if is_settled and result != "":
                settled_yes = result.lower() == "yes"
                won         = (trade.side == "yes" and settled_yes) or \
                              (trade.side == "no"  and not settled_yes)

                if won:
                    pnl = trade.num_contracts * (100 - trade.price_cents) / 100
                else:
                    pnl = -trade.cost_dollars

                pnl     = round(pnl, 2)
                outcome = "win" if won else "loss"

                state_v2.close_trade(
                    trade_id  = trade.trade_id,
                    pnl       = pnl,
                    outcome   = outcome,
                    closed_at = time.time(),
                )
                state_v2.current_trade = None

                result_emoji = "✅" if won else "❌"
                logger.info(
                    f"BOT 2.0: {result_emoji} {ticker} settled {result.upper()} | "
                    f"{trade.side.upper()} → {outcome.upper()} | P&L ${pnl:+.2f}"
                )
                state_v2.status = (
                    f"Last trade: {outcome.upper()} ${pnl:+.2f} "
                    f"({trade.side.upper()} @ {trade.price_cents}c on {ticker})"
                )
                mode_tag = "PAPER" if state_v2.paper_mode else "LIVE"
                notify(
                    f"BOT2 SETTLED [{mode_tag}]: {result_emoji} {outcome.upper()} ${pnl:+.2f} "
                    f"({trade.side.upper()} {trade.num_contracts}x @ {trade.price_cents}c on {ticker})"
                )
                return

            if secs_since_close > 1800:
                logger.warning(f"BOT 2.0: Settlement timeout for {ticker}")
                state_v2.current_trade = None
                return

        except Exception as e:
            logger.warning(f"BOT 2.0: Settlement check error: {e}")

        await asyncio.sleep(30)
