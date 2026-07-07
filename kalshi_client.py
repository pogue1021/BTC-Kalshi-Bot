"""
kalshi_client.py — Kalshi API Client
======================================
Handles all communication with the Kalshi trading API:
  - RSA-PSS request signing (required for all authenticated endpoints)
  - Finding the active BTC 15-minute market
  - Fetching live market prices
  - Placing YES/NO orders (or logging them in paper mode)

Authentication:
  Kalshi uses RSA-PSS signatures. Every request to a protected
  endpoint must be signed with your private key. The setup guide
  explains how to generate and download your key from Kalshi's
  settings page.
"""

import base64
import hashlib
import json
import logging
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Optional

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

logger = logging.getLogger(__name__)


class OrderNotFilledError(Exception):
    """
    Raised when an order is accepted by Kalshi but does not fully fill —
    typically because the price moved between when the bot read it and
    when the order arrived (so our limit price is now below the new ask
    and the order rests on the book instead of crossing the spread).

    Callers should treat this the same as "no trade happened" — do NOT
    record the trade, do NOT track stop-loss/take-profit on it. The bot
    will get another chance on the next signal evaluation.

    The corresponding resting order is best-effort cancelled before this
    is raised, so it shouldn't fill behind our backs later.
    """
    pass

# ─────────────────────────────────────────────────────────────
# API URLS
# ─────────────────────────────────────────────────────────────
LIVE_BASE_URL  = "https://api.elections.kalshi.com/trade-api/v2"
DEMO_BASE_URL  = "https://demo-api.kalshi.co/trade-api/v2"

# Kalshi's 15-min Bitcoin series ticker (from URL: demo.kalshi.co/markets/kxbtc15m/...)
BTC_SERIES_TICKER = "KXBTC15M"


class KalshiClient:
    """
    Client for the Kalshi Trading API v2.
    Set use_demo=True (or config use_demo: true) to trade on the
    practice account without risking real money.
    """

    def __init__(
        self,
        api_key_id: str,
        private_key_pem: str,
        use_demo: bool = True,
        cross_spread_buffer_cents: int = 1,
    ):
        self.api_key_id = api_key_id
        self.base_url   = DEMO_BASE_URL if use_demo else LIVE_BASE_URL
        self.mode_label = "DEMO" if use_demo else "LIVE"
        self.session    = requests.Session()
        # How aggressively to cross the spread when placing orders, in cents.
        # 0 = price exactly at ask/bid (fills only if price doesn't move).
        # 1 = pay up to 1c more on buys / accept up to 1c less on sells —
        #     greatly improves fill rate, costs ~$0.13 per 13-contract trade.
        # 2+ = even more aggressive; rarely needed.
        # Buys still typically fill AT the original ask (matching engine fills
        # against resting orders at their price, not ours), so the buffer is
        # an upper bound on slippage, not a guaranteed extra cost.
        self.cross_spread_buffer_cents = max(0, int(cross_spread_buffer_cents))

        # Load the private key once at startup
        try:
            self._private_key = serialization.load_pem_private_key(
                private_key_pem.encode("utf-8"),
                password=None
            )
            logger.info(f"Kalshi client initialized ({self.mode_label} mode)")
        except Exception as e:
            raise ValueError(
                f"Could not load private key: {e}\n"
                "Make sure your .pem file path in config.yaml is correct."
            ) from e

    # ─────────────────────────────────────────────────────────
    # REQUEST SIGNING
    # ─────────────────────────────────────────────────────────

    def _sign_request(self, method: str, path: str, body: str = "") -> dict:
        """
        Generates the required auth headers for a Kalshi API request.
        Kalshi requires RSA-PSS signing of: timestamp + method + full_path
        The full path must include the /trade-api/v2 prefix.
        Example message: 1703123456789GET/trade-api/v2/portfolio/balance
        """
        timestamp_ms = str(int(time.time() * 1000))

        # Strip query parameters from path for signing
        path_no_query = path.split("?")[0]

        # Kalshi requires the FULL path including /trade-api/v2 prefix in the signature
        full_path_for_signing = "/trade-api/v2" + path_no_query
        message = timestamp_ms + method.upper() + full_path_for_signing

        signature = self._private_key.sign(
            message.encode("utf-8"),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH
            ),
            hashes.SHA256()
        )

        return {
            "KALSHI-ACCESS-KEY":       self.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("utf-8"),
            "Content-Type":            "application/json",
        }

    # Retry policy: how many attempts and how long to wait between them.
    # Triggered for transient network failures only — connection resets,
    # timeouts, and Kalshi 5xx server errors. Auth/4xx errors raise immediately
    # so we don't waste retries on misconfigured requests.
    _RETRY_ATTEMPTS = 3
    _RETRY_BACKOFF_SECS = (0.5, 1.5, 3.0)   # one entry per retry attempt

    def _request_with_retry(self, method: str, full_url: str, headers: dict,
                            body_str: str = None) -> dict:
        """
        Execute a signed request with retry on transient failures.
        Re-signs each attempt because Kalshi's signature includes a timestamp
        that becomes stale after a few seconds.
        """
        # Strip the base_url to get the path for re-signing
        path_with_query = full_url[len(self.base_url):]
        path_no_query   = path_with_query.split("?")[0]

        last_err = None
        for attempt in range(self._RETRY_ATTEMPTS):
            # Re-sign on every attempt — timestamp must be fresh
            if attempt > 0:
                if method == "POST":
                    headers = self._sign_request("POST", path_no_query, body_str or "")
                elif method == "DELETE":
                    headers = self._sign_request("DELETE", path_with_query)
                else:
                    headers = self._sign_request("GET", path_with_query)

            try:
                if method == "POST":
                    response = self.session.post(
                        full_url, headers=headers, data=body_str, timeout=10,
                    )
                elif method == "DELETE":
                    response = self.session.delete(
                        full_url, headers=headers, timeout=10,
                    )
                else:
                    response = self.session.get(
                        full_url, headers=headers, timeout=10,
                    )

                # Retry only on 5xx (server-side transient). 4xx is our fault — raise immediately.
                if 500 <= response.status_code < 600:
                    last_err = requests.HTTPError(
                        f"Kalshi {response.status_code}: {response.text[:200]}"
                    )
                    raise last_err

                response.raise_for_status()
                return response.json()

            except (requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout,
                    requests.exceptions.HTTPError) as e:
                # Don't retry on 4xx — those won't fix themselves
                if isinstance(e, requests.exceptions.HTTPError):
                    status = getattr(e.response, "status_code", 0) if e.response is not None else 0
                    if 400 <= status < 500:
                        raise
                last_err = e
                if attempt < self._RETRY_ATTEMPTS - 1:
                    delay = self._RETRY_BACKOFF_SECS[attempt]
                    logger.warning(
                        f"Kalshi {method} {path_no_query} failed "
                        f"(attempt {attempt+1}/{self._RETRY_ATTEMPTS}): "
                        f"{type(e).__name__} — retrying in {delay}s"
                    )
                    # On connection errors, evict any stale pooled sockets — common
                    # after laptop sleep/wake or long idle periods. Fresh attempt
                    # will dial a new TCP connection rather than reuse a dead one.
                    if isinstance(e, requests.exceptions.ConnectionError):
                        try:
                            self.session.close()
                        except Exception:
                            pass
                        self.session = requests.Session()
                    time.sleep(delay)
                    continue
                # Out of retries — re-raise so caller can handle
                raise

    def _get(self, path: str, params: dict = None) -> dict:
        """Make an authenticated GET request (auto-retries transient network errors)."""
        query = ("?" + urllib.parse.urlencode(params)) if params else ""
        full_path = path + query
        headers = self._sign_request("GET", full_path)
        return self._request_with_retry("GET", self.base_url + full_path, headers)

    def _post(self, path: str, body: dict) -> dict:
        """Make an authenticated POST request (auto-retries transient network errors)."""
        body_str = json.dumps(body)
        headers  = self._sign_request("POST", path, body_str)
        return self._request_with_retry("POST", self.base_url + path, headers, body_str=body_str)

    def _delete(self, path: str) -> dict:
        """Make an authenticated DELETE request (auto-retries transient network errors)."""
        headers = self._sign_request("DELETE", path)
        return self._request_with_retry("DELETE", self.base_url + path, headers)

    # ─────────────────────────────────────────────────────────
    # ACCOUNT
    # ─────────────────────────────────────────────────────────

    def get_balance(self) -> float:
        """Returns account balance in dollars."""
        data = self._get("/portfolio/balance")
        # Balance is returned in cents
        return data.get("balance", 0) / 100

    # ─────────────────────────────────────────────────────────
    # MARKET DISCOVERY
    # ─────────────────────────────────────────────────────────

    def get_open_btc_markets(self) -> list:
        """
        Fetches all currently open BTC markets.
        Returns a list of market dicts sorted by close time (soonest first).
        """
        data = self._get("/markets", params={
            "series_ticker": BTC_SERIES_TICKER,
            "status": "open",
            "limit": 50,
        })
        markets = data.get("markets", [])

        # Sort by close_time so index 0 = soonest to close
        markets.sort(key=lambda m: m.get("close_time", ""))
        return markets

    def get_active_15min_market(self) -> Optional[dict]:
        """
        Returns the currently active 15-minute BTC market, or None if not found.
        The 'active' market is the one that closes soonest and closes within 15 minutes.
        """
        markets = self.get_open_btc_markets()

        now = datetime.now(timezone.utc)

        for market in markets:
            close_time_str = market.get("close_time", "")
            if not close_time_str:
                continue

            try:
                close_time = datetime.fromisoformat(
                    close_time_str.replace("Z", "+00:00")
                )
            except ValueError:
                continue

            seconds_until_close = (close_time - now).total_seconds()

            # We want a market that closes within 15 minutes (and is still open)
            if 0 < seconds_until_close <= 15 * 60:
                market["_seconds_until_close"] = seconds_until_close

                # Extract the target/strike price — this is what YES/NO settles against.
                # Kalshi uses 'floor_strike' for "BTC above X" markets.
                # Fallback: parse from subtitle field (e.g. "Bitcoin ↑ $73,110.24")
                floor_strike = market.get("floor_strike")
                if floor_strike is None:
                    # Try cap_strike as secondary
                    floor_strike = market.get("cap_strike")
                if floor_strike is None:
                    # Parse from subtitle: find first $-prefixed number
                    import re
                    subtitle = market.get("subtitle", "") or market.get("title", "")
                    match = re.search(r'\$([0-9,]+\.?\d*)', subtitle)
                    if match:
                        floor_strike = float(match.group(1).replace(",", ""))

                # Log all fields once per new market to help diagnose field names
                logger.debug(f"Market fields: { {k:v for k,v in market.items() if not k.startswith('_')} }")

                if floor_strike is not None:
                    try:
                        market["_floor_strike"] = float(floor_strike)
                        logger.debug(f"Target price for {market.get('ticker')}: ${market['_floor_strike']:,.2f}")
                    except (ValueError, TypeError):
                        market["_floor_strike"] = None
                        logger.warning(f"Could not parse floor_strike: {floor_strike!r}")
                else:
                    market["_floor_strike"] = None
                    logger.warning(f"No floor_strike found in market data for {market.get('ticker')}")

                return market

        logger.warning("No active 15-minute BTC market found. Markets may not be open yet.")
        return None

    def get_market_prices(self, ticker: str) -> dict:
        """
        Gets the current orderbook/prices for a market.
        Returns a dict with yes_bid, yes_ask, no_bid, no_ask (all in cents).

        The Kalshi API can return prices in two formats depending on the endpoint:
          - Integer cents (0-100): e.g. 89 means 89 cents
          - Decimal (0.0-1.0):     e.g. 0.89 means 89 cents
        This method normalises both to integer cents.
        """
        data = self._get(f"/markets/{ticker}")
        market = data.get("market", {})

        def dollars_to_cents(val) -> int:
            """Convert a dollar string/float (e.g. '0.89') to integer cents (89)."""
            if val is None:
                return 0
            return round(float(val) * 100)

        def to_cents(val) -> int:
            """Convert a cents value (int or float) to integer cents."""
            if val is None:
                return 0
            val = float(val)
            if 0 < val < 1:          # decimal fraction — convert to cents
                return round(val * 100)
            return int(val)

        # Live Kalshi API uses *_dollars fields (e.g. yes_ask_dollars = 0.89)
        # Older/demo API used plain cents fields (yes_ask = 89)
        # Try dollars fields first, fall back to cents fields
        yes_ask = dollars_to_cents(market.get("yes_ask_dollars")) or to_cents(market.get("yes_ask"))
        yes_bid = dollars_to_cents(market.get("yes_bid_dollars")) or to_cents(market.get("yes_bid"))
        no_ask  = dollars_to_cents(market.get("no_ask_dollars"))  or to_cents(market.get("no_ask"))
        no_bid  = dollars_to_cents(market.get("no_bid_dollars"))  or to_cents(market.get("no_bid"))

        logger.debug(f"Market prices {ticker}: yes_ask={yes_ask}c  no_ask={no_ask}c")

        return {
            "yes_bid":       yes_bid,
            "yes_ask":       yes_ask,
            "no_bid":        no_bid,
            "no_ask":        no_ask,
            "volume":        market.get("volume_fp", market.get("volume", 0)),
            "open_interest": market.get("open_interest_fp", market.get("open_interest", 0)),
        }

    # ─────────────────────────────────────────────────────────
    # ORDER PLACEMENT
    # ─────────────────────────────────────────────────────────

    def place_order(
        self,
        ticker: str,
        side: str,           # "yes" or "no"
        price_cents: int,    # price you're willing to pay (0-100)
        num_contracts: int,  # number of contracts (each contract = $1 max payout)
        paper_mode: bool = True,
        maker: bool = True,  # True = post at bid (maker, lower fees); False = cross spread (taker)
    ) -> dict:
        """
        Places a limit order on Kalshi.

        In paper_mode=True, the order is only logged — nothing is sent to Kalshi.
        Set paper_mode=False in config.yaml only when you're ready to trade live.

        Args:
            ticker:        Market ticker (e.g. 'KXBTC-25APR061430')
            side:          'yes' or 'no'
            price_cents:   Price per contract in cents (e.g. 55 = $0.55)
            num_contracts: How many $1 contracts to buy
            paper_mode:    If True, just log and return a fake order

        Returns:
            Order response dict (or simulated response in paper mode)
        """

        cost_dollars = (price_cents * num_contracts) / 100
        label = "[PAPER]" if paper_mode else f"[{self.mode_label}]"

        logger.info(
            f"{label} Order → {side.upper()} {num_contracts}x on {ticker} "
            f"@ {price_cents}¢ (cost: ${cost_dollars:.2f})"
        )

        if paper_mode:
            # Simulate an order response without hitting the API
            return {
                "paper_mode": True,
                "order": {
                    "ticker":         ticker,
                    "side":           side,
                    "yes_price":      price_cents if side == "yes" else (100 - price_cents),
                    "no_price":       price_cents if side == "no"  else (100 - price_cents),
                    "count":          num_contracts,
                    "status":         "resting",
                    "order_id":       f"PAPER-{int(time.time())}",
                    "created_time":   datetime.now(timezone.utc).isoformat(),
                }
            }

        if maker:
            # Maker order: post at exactly price_cents — no spread crossing.
            # Order rests on the book at the best bid. Kalshi charges 4x lower
            # maker fees vs taker fees. Grace period is longer since the order
            # waits for a taker to cross rather than filling immediately.
            final_price = max(1, min(99, price_cents))
            grace_secs  = 20.0
            logger.info(
                f"Maker bid: {final_price}c on {side.upper()} — resting at best bid"
            )
        else:
            # Taker order: cross the spread to guarantee immediate fill.
            final_price = max(1, min(99, price_cents + self.cross_spread_buffer_cents))
            grace_secs  = 8.0
            if final_price != price_cents:
                logger.info(
                    f"Crossing spread: bidding {final_price}c on {side.upper()} "
                    f"(was {price_cents}c) — buffer={self.cross_spread_buffer_cents}c"
                )

        yes_price = final_price if side == "yes" else (100 - final_price)

        # Kalshi deprecated /portfolio/orders (v1) in favor of /portfolio/events/orders
        # (v2). The v2 book only speaks in YES-leg bid/ask terms: "bid" = buy YES,
        # "ask" = sell YES (economically equivalent to buying NO at 1-price). Since
        # `yes_price` above already converts our yes/no price into YES-leg terms,
        # the same conversion tells us which book side to submit:
        #   buy YES -> bid (gain YES exposure)   |   buy NO -> ask (gain NO exposure)
        book_side = "bid" if side == "yes" else "ask"

        body = {
            "ticker":                     ticker,
            "side":                       book_side,
            "count":                      f"{num_contracts:.2f}",
            "price":                      f"{yes_price / 100:.2f}",
            "time_in_force":              "good_till_canceled" if maker else "immediate_or_cancel",
            "self_trade_prevention_type": "taker_at_cross",
            "client_order_id":            f"btcbot-{int(time.time())}",
        }

        try:
            response = self._post("/portfolio/events/orders", body)
        except requests.HTTPError as e:
            logger.error(f"Order failed: {e.response.status_code} — {e.response.text}")
            raise

        # v2's create-order response is flat (order_id/fill_count/remaining_count,
        # no nested "order" or "status"). Kalshi can take a couple seconds to
        # make a just-created order visible via GET /portfolio/orders/{id}, so
        # don't fetch it here — hand off an unknown-status stub and let
        # _verify_fill_or_cancel's own retry loop (which already tolerates
        # transient errors while polling) do the first real status check.
        # An eager fetch here previously raised on that propagation delay and
        # got the whole order misreported as failed even when it had filled.
        order_id = response.get("order_id", "unknown")
        return self._verify_fill_or_cancel(
            {"order": {"order_id": order_id, "status": ""}},
            context=f"buy {side.upper()} {num_contracts}x @ {price_cents}c on {ticker}",
            grace_secs=grace_secs,
        )

    def sell_position(
        self,
        ticker: str,
        side: str,           # "yes" or "no" — the side you originally bought
        price_cents: int,    # current sell price in cents (use yes_bid / no_bid)
        num_contracts: int,
        paper_mode: bool = True,
    ) -> dict:
        """
        Exit (sell) an open position before market settlement.

        On Kalshi you sell by placing a sell-action order on the same side
        you originally bought. E.g. if you bought YES, you sell YES back.

        In paper_mode=True, the exit is only logged — nothing is sent to Kalshi.
        """
        label = "[PAPER]" if paper_mode else f"[{self.mode_label}]"
        logger.info(
            f"{label} EXIT → sell {side.upper()} {num_contracts}x on {ticker} "
            f"@ {price_cents}¢ (stop-loss)"
        )

        if paper_mode:
            return {
                "paper_mode": True,
                "order": {
                    "ticker":       ticker,
                    "side":         side,
                    "action":       "sell",
                    "count":        num_contracts,
                    "status":       "filled",
                    "order_id":     f"PAPER-EXIT-{int(time.time())}",
                    "created_time": datetime.now(timezone.utc).isoformat(),
                }
            }

        # Cross the spread on the sell side too: accept up to `buffer` cents
        # less than the displayed bid to ensure the exit actually fills.
        # Critical for stop-loss exits — if a SL exit rests, you keep the
        # losing position and bleed more. Better to accept 1c worse than miss.
        buffered_sell_price = max(1, min(99, price_cents - self.cross_spread_buffer_cents))
        # Kalshi uses yes_price for both sides in the order body
        yes_price = buffered_sell_price if side == "yes" else (100 - buffered_sell_price)
        if buffered_sell_price != price_cents:
            logger.info(
                f"Crossing spread on exit: accepting {buffered_sell_price}c on "
                f"{side.upper()} (was {price_cents}c) — buffer={self.cross_spread_buffer_cents}c"
            )

        # Closing a position flips the bid/ask mapping from an entry order:
        # closing YES (selling YES) increases NO exposure -> ask; closing NO
        # (buying YES back) increases YES exposure -> bid. reduce_only caps
        # the fill to our existing position so this can never flip into a
        # new opening position on the opposite side.
        book_side = "ask" if side == "yes" else "bid"

        body = {
            "ticker":                     ticker,
            "side":                       book_side,
            "count":                      f"{num_contracts:.2f}",
            "price":                      f"{yes_price / 100:.2f}",
            "time_in_force":              "immediate_or_cancel",
            "self_trade_prevention_type": "taker_at_cross",
            "reduce_only":                True,
            "client_order_id":            f"btcbot-exit-{int(time.time())}",
        }

        try:
            response = self._post("/portfolio/events/orders", body)
        except requests.HTTPError as e:
            logger.error(f"Exit order failed: {e.response.status_code} — {e.response.text}")
            raise

        # See place_order: don't eagerly fetch — a just-created order can 404
        # for a couple seconds before Kalshi indexes it for GET. Hand off an
        # unknown-status stub and let the retry loop's first poll handle it.
        order_id = response.get("order_id", "unknown")

        # Exits need a short grace window — in a falling market, waiting 8s
        # means the fill price gets much worse. 3s is enough to catch a
        # momentary matching-engine lag without bleeding on the way down.
        return self._verify_fill_or_cancel(
            {"order": {"order_id": order_id, "status": ""}},
            context=f"sell {side.upper()} {num_contracts}x @ {price_cents}c on {ticker}",
            grace_secs=3.0,
        )

    def cancel_order(self, order_id: str) -> dict:
        """
        Cancel a resting order by its order ID.

        Kalshi deprecated the v1 POST /portfolio/orders/{id}/cancel endpoint
        alongside order creation (see place_order). It now 404s unconditionally,
        which previously made every cancel attempt look like "order not found"
        even for orders genuinely still resting — the caller would give up,
        assume no trade happened, and walk away from a real live order that
        later filled on its own, completely untracked. Now uses the v2
        DELETE /portfolio/events/orders/{id} endpoint instead.
        """
        return self._delete(f"/portfolio/events/orders/{order_id}")

    def get_order(self, order_id: str) -> dict:
        """Fetch the current state of a single order. Used to poll for fill."""
        data = self._get(f"/portfolio/orders/{order_id}")
        return data.get("order", data)

    def _verify_fill_or_cancel(
        self,
        response: dict,
        *,
        context: str,
        grace_secs: float = 8.0,
        poll_interval: float = 0.5,
    ) -> dict:
        """
        Confirm a freshly-placed order actually filled. If Kalshi initially
        reports it as `resting` (limit price below the new ask, e.g. price
        ticked between read and place), poll the order for up to `grace_secs`
        before giving up — sometimes a marketable limit fills a beat after
        acceptance and we don't want to cancel those prematurely.

        Returns the (possibly updated) response on success.
        Raises OrderNotFilledError if still unfilled after the grace period;
        the resting order is best-effort canceled before the raise so it
        can't sneak-fill later behind the bot's back.

        `context` is a short label like "buy YES 14x @ 70c" used for clearer
        log lines — does NOT change behavior.
        """
        order    = response.get("order", {}) or {}
        order_id = order.get("order_id", "unknown")
        status   = (order.get("status") or "").lower()

        # Kalshi marks a fully matched order as "executed". "filled" tolerated
        # in case the API ever uses that synonym.
        FILLED = ("executed", "filled")

        if status in FILLED:
            logger.info(f"Order {order_id} filled immediately ({context}, status={status})")
            return response

        # Initial response says not filled yet — give the matching engine a
        # short grace window in case it just hasn't caught up.
        logger.info(
            f"Order {order_id} not filled yet (status={status!r}, {context}). "
            f"Waiting up to {grace_secs:.1f}s for matching engine..."
        )

        waited = 0.0
        while waited < grace_secs:
            time.sleep(poll_interval)
            waited += poll_interval
            try:
                updated = self.get_order(order_id)
            except Exception as poll_err:
                # Don't bail on a transient API hiccup — keep waiting.
                logger.warning(f"Could not poll order {order_id}: {poll_err}")
                continue

            new_status = (updated.get("status") or "").lower()
            if new_status in FILLED:
                logger.info(
                    f"Order {order_id} filled after {waited:.1f}s grace ({context}, "
                    f"status={new_status})"
                )
                response["order"] = updated
                return response
            status = new_status  # for the final log line

        # Still not filled — cancel and signal "no trade" upstream.
        logger.warning(
            f"Order {order_id} did NOT fill within {grace_secs:.1f}s "
            f"(final status={status!r}, {context}). Canceling and treating as no-trade."
        )
        try:
            self.cancel_order(order_id)
        except Exception as cancel_err:
            # A 404 on cancel means Kalshi already removed this order from the
            # open-orders list — which is exactly what happens when an order
            # executes. The bot was treating this as "no trade" and re-trying,
            # creating ghost positions on Kalshi that it didn't track.
            #
            # Fix: if cancel returns 404, confirm by fetching the order.
            # If it shows "executed", return it as a real fill so the caller
            # records the trade, applies stop-loss, and tracks the position.
            cancel_http_status = getattr(
                getattr(cancel_err, "response", None), "status_code", 0
            )
            if cancel_http_status == 404:
                try:
                    confirmed = self.get_order(order_id)
                    confirmed_status = (confirmed.get("status") or "").lower()
                    if confirmed_status in FILLED:
                        logger.info(
                            f"Order {order_id} confirmed executed after 404 cancel "
                            f"({context}) — recording as filled."
                        )
                        response["order"] = confirmed
                        return response
                    # Order still 'resting' after cancel-404 — the cancel may have
                    # arrived while Kalshi was matching it. Wait 2 more seconds
                    # and check one final time before giving up.
                    if confirmed_status == "resting":
                        logger.warning(
                            f"Order {order_id} cancel got 404 but still resting — "
                            f"waiting 2s for match to confirm ({context})"
                        )
                        time.sleep(2)
                        try:
                            final = self.get_order(order_id)
                            final_status = (final.get("status") or "").lower()
                            if final_status in FILLED:
                                logger.info(
                                    f"Order {order_id} confirmed executed on final check "
                                    f"({context}) — recording as filled."
                                )
                                response["order"] = final
                                return response
                        except Exception:
                            pass  # fall through to OrderNotFilledError
                    logger.warning(
                        f"Order {order_id} cancel got 404 but order status is "
                        f"{confirmed_status!r} — not treating as filled ({context})"
                    )
                except Exception as confirm_err:
                    # get_order also returned 404 — order was purged after execution.
                    # Both cancel and fetch returning 404 strongly indicates the
                    # order filled and was cleaned up by Kalshi. Treat as filled.
                    confirm_http_status = getattr(
                        getattr(confirm_err, "response", None), "status_code", 0
                    )
                    if confirm_http_status == 404:
                        logger.info(
                            f"Order {order_id} not found on cancel or confirm "
                            f"(both 404) — assuming executed ({context}). "
                            f"Recording as filled."
                        )
                        response.setdefault("order", {})["status"] = "executed"
                        return response
                    logger.warning(
                        f"Could not confirm order {order_id} status after "
                        f"404 cancel: {confirm_err}"
                    )
            # Non-404 cancel failure — the order may have filled in the gap
            # between our grace window and the cancel attempt. Do one final
            # status check before raising so we don't create a ghost position.
            logger.error(
                f"Could not cancel order {order_id}: {cancel_err} — "
                f"checking order status before giving up."
            )
            try:
                final_check  = self.get_order(order_id)
                final_status = (final_check.get("status") or "").lower()
                if final_status in FILLED:
                    logger.info(
                        f"Order {order_id} confirmed filled after cancel error "
                        f"({context}) — recording as filled."
                    )
                    response["order"] = final_check
                    return response
            except Exception:
                pass  # can't confirm — fall through to OrderNotFilledError

        raise OrderNotFilledError(
            f"Order {order_id} status={status!r} (not filled within {grace_secs:.1f}s) — canceled"
        )

    def get_open_orders(self, ticker: str = None) -> list:
        """Get all open (resting) orders, optionally filtered by market ticker."""
        params = {"status": "resting"}
        if ticker:
            params["ticker"] = ticker
        data = self._get("/portfolio/orders", params=params)
        return data.get("orders", [])

    def get_fills(
        self,
        min_ts: int = None,
        max_ts: int = None,
        ticker: str = None,
        limit: int = 1000,
    ) -> list:
        """
        Fetch all fills (executed trades) from Kalshi for the given time range.
        Used by the background reconciler to compute Kalshi-truth P&L.

        Handles pagination via the cursor field — keeps requesting until
        Kalshi returns no more pages. Safe to call from a background thread;
        does not touch any bot state.

        Args:
            min_ts: unix timestamp lower bound (inclusive). None = no lower bound.
            max_ts: unix timestamp upper bound (inclusive). None = no upper bound.
            ticker: optional market ticker filter.
            limit:  page size (Kalshi caps at 1000).

        Returns:
            list of fill dicts. Each fill typically has:
              trade_id, order_id, ticker, side ("yes"/"no"),
              action ("buy"/"sell"), yes_price, no_price, count,
              is_taker, created_time, fees (optional)
        """
        all_fills = []
        cursor = None
        while True:
            params = {"limit": min(limit, 1000)}
            if min_ts is not None: params["min_ts"] = int(min_ts)
            if max_ts is not None: params["max_ts"] = int(max_ts)
            if ticker:             params["ticker"] = ticker
            if cursor:             params["cursor"] = cursor
            data = self._get("/portfolio/fills", params=params)
            page = data.get("fills", []) or []
            all_fills.extend(page)
            cursor = data.get("cursor") or ""
            if not cursor or not page:
                break
        return all_fills

    def get_settlements(
        self,
        min_ts: int = None,
        max_ts: int = None,
        limit: int = 1000,
    ) -> list:
        """
        Fetch settled positions (markets that closed and paid out) from
        Kalshi for the given time range. Same pagination/threading rules
        as get_fills().

        Returns:
            list of settlement dicts. Each settlement typically has:
              ticker, market_result ("yes"/"no"),
              yes_count, no_count, revenue (settlement payout in cents),
              settled_time
        """
        all_settlements = []
        cursor = None
        while True:
            params = {"limit": min(limit, 1000)}
            if min_ts is not None: params["min_ts"] = int(min_ts)
            if max_ts is not None: params["max_ts"] = int(max_ts)
            if cursor:             params["cursor"] = cursor
            data = self._get("/portfolio/settlements", params=params)
            page = data.get("settlements", []) or []
            all_settlements.extend(page)
            cursor = data.get("cursor") or ""
            if not cursor or not page:
                break
        return all_settlements


def load_client_from_config(config: dict) -> KalshiClient:
    """
    Convenience function: builds a KalshiClient from the config dict.
    Reads the private key from the file path specified in config.yaml.
    """
    kalshi_cfg   = config.get("kalshi", {})
    api_key_id   = kalshi_cfg.get("api_key_id", "")
    key_path     = kalshi_cfg.get("private_key_path", "kalshi_private_key.pem")
    use_demo     = kalshi_cfg.get("use_demo", True)
    # Spread-crossing buffer in cents (default 1) — see KalshiClient.__init__
    cross_buffer = int(kalshi_cfg.get("cross_spread_buffer_cents", 1))

    if api_key_id == "YOUR_KALSHI_API_KEY_ID" or not api_key_id:
        raise ValueError(
            "You haven't set your Kalshi API key yet!\n"
            "Edit config.yaml and fill in your api_key_id and private_key_path.\n"
            "See SETUP_GUIDE.md for step-by-step instructions."
        )

    try:
        with open(key_path, "r") as f:
            private_key_pem = f.read()
    except FileNotFoundError:
        raise FileNotFoundError(
            f"Private key file not found: '{key_path}'\n"
            "Make sure the .pem file is in the same folder as this bot,\n"
            "and that private_key_path in config.yaml matches the filename."
        )

    return KalshiClient(
        api_key_id, private_key_pem,
        use_demo=use_demo,
        cross_spread_buffer_cents=cross_buffer,
    )
