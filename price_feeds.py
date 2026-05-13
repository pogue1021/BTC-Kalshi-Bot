"""
price_feeds.py -- Real-Time Price Feed Module
=============================================
Connects to ALL four CF Benchmarks constituent exchanges via WebSocket
and streams live BTC prices. Calculates a real-time CF estimate price
that approximates the rate Kalshi's markets will settle on.

CF Benchmarks constituent exchanges (for the BRR/BRTI settlement rate):
  - Coinbase  -- highest USD volume, largest weight
  - Kraken    -- second largest, fast price discovery
  - Bitstamp  -- consistent liquidity, long-running exchange
  - Gemini    -- US-regulated, part of the BRR since 2019

Strategy: monitor all four, compute a weighted CF estimate, measure
its momentum, and bet when the aggregate is moving faster than
Kalshi's market is repricing.
"""

import asyncio
import json
import time
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Optional
import websockets

logger = logging.getLogger(__name__)

PRICE_HISTORY_SIZE = 120  # ~2 minutes of per-second samples


@dataclass
class PriceSnapshot:
    exchange: str
    price: float
    timestamp: float = field(default_factory=time.time)


# Approximate volume weights for CF Benchmarks BRR calculation.
# Coinbase typically contributes the most USD volume, Kraken second.
# Weights are renormalized at runtime based on which feeds are live.
CF_WEIGHTS = {
    "coinbase": 0.35,
    "kraken":   0.30,
    "bitstamp": 0.20,
    "gemini":   0.15,
}


class PriceStore:
    """
    Stores latest prices from all four CF Benchmarks constituent exchanges.
    Provides a real-time CF estimate and momentum calculation.
    """

    def __init__(self):
        self._prices: dict[str, Optional[float]] = {
            "coinbase": None,
            "kraken":   None,
            "bitstamp": None,
            "gemini":   None,
        }
        self._histories: dict[str, deque] = {
            k: deque(maxlen=PRICE_HISTORY_SIZE) for k in self._prices
        }
        self._last_updates: dict[str, float] = {k: 0.0 for k in self._prices}

    # ── Writers ──────────────────────────────────────────────────────────────

    def _update(self, exchange: str, price: float):
        now = time.time()
        self._prices[exchange] = price
        self._last_updates[exchange] = now
        self._histories[exchange].append(PriceSnapshot(exchange, price, now))

    def update_binance(self, price: float):   # Kraken feeds this (kept for compat)
        self._update("kraken", price)

    def update_coinbase(self, price: float):
        self._update("coinbase", price)

    def update_bitstamp(self, price: float):
        self._update("bitstamp", price)

    def update_gemini(self, price: float):
        self._update("gemini", price)

    # ── Readers ───────────────────────────────────────────────────────────────

    @property
    def binance_price(self) -> Optional[float]:   # Kraken price (kept for compat)
        return self._prices["kraken"]

    @property
    def coinbase_price(self) -> Optional[float]:
        return self._prices["coinbase"]

    @property
    def bitstamp_price(self) -> Optional[float]:
        return self._prices["bitstamp"]

    @property
    def gemini_price(self) -> Optional[float]:
        return self._prices["gemini"]

    def age_seconds(self, exchange: str) -> float:
        t = self._last_updates.get(exchange, 0.0)
        return float("inf") if t == 0 else time.time() - t

    def live_exchanges(self) -> list[str]:
        """Returns exchanges with a price updated within the last 15 seconds."""
        return [k for k in self._prices if self._prices[k] is not None
                and self.age_seconds(k) < 15]

    # ── CF Estimate ───────────────────────────────────────────────────────────

    def get_cf_estimate(self) -> Optional[float]:
        """
        Weighted average of all live constituent exchange prices.
        Approximates the CF Benchmarks real-time rate (BRTI).
        Weights are renormalized based on which exchanges are live.
        """
        live = self.live_exchanges()
        if not live:
            return None

        total_weight = sum(CF_WEIGHTS[k] for k in live)
        if total_weight == 0:
            return None

        return sum(
            self._prices[k] * CF_WEIGHTS[k] / total_weight
            for k in live
        )

    def get_cf_estimate_n_seconds_ago(self, seconds: int) -> Optional[float]:
        """
        CF estimate from N seconds ago, used to calculate momentum.
        Builds a weighted average from each exchange's historical snapshots.
        """
        target_time = time.time() - seconds
        prices_then: dict[str, float] = {}

        for exchange, history in self._histories.items():
            # Walk backwards to find the closest snapshot at or before target_time
            for snap in reversed(history):
                if snap.timestamp <= target_time:
                    prices_then[exchange] = snap.price
                    break
            # If history doesn't go back far enough, use oldest available
            if exchange not in prices_then and history:
                prices_then[exchange] = history[0].price

        if not prices_then:
            return None

        total_weight = sum(CF_WEIGHTS[k] for k in prices_then)
        if total_weight == 0:
            return None

        return sum(
            prices_then[k] * CF_WEIGHTS[k] / total_weight
            for k in prices_then
        )

    def get_binance_price_n_seconds_ago(self, seconds: int) -> Optional[float]:
        """Kept for signal_engine backward compatibility -- returns Kraken history."""
        target_time = time.time() - seconds
        history = self._histories["kraken"]
        for snap in reversed(history):
            if snap.timestamp <= target_time:
                return snap.price
        if history:
            return history[0].price
        return None

    def exchange_consensus(self) -> Optional[str]:
        """
        Returns 'up', 'down', or None based on whether live exchanges
        agree on the short-term direction (useful for confidence scoring).
        """
        live = self.live_exchanges()
        if len(live) < 2:
            return None

        # Compare each exchange's current price to 10 seconds ago
        directions = []
        for exchange in live:
            history = self._histories[exchange]
            target = time.time() - 10
            old_price = None
            for snap in reversed(history):
                if snap.timestamp <= target:
                    old_price = snap.price
                    break
            if old_price and self._prices[exchange]:
                directions.append(1 if self._prices[exchange] > old_price else -1)

        if not directions:
            return None

        up_count   = sum(1 for d in directions if d > 0)
        down_count = sum(1 for d in directions if d < 0)

        if up_count == len(directions):
            return "up"
        if down_count == len(directions):
            return "down"
        return None

    def is_ready(self) -> bool:
        """
        Ready when at least 2 feeds are live. Coinbase preferred but not
        required — if it's down the other three can still compute a CF estimate.
        """
        live = sum(
            1 for k in ("coinbase", "kraken", "bitstamp", "gemini")
            if self._prices[k] is not None and self.age_seconds(k) < 15
        )
        return live >= 2


# ─────────────────────────────────────────────────────────────
# KRAKEN FEED  (fast BTC/USD, CF Benchmarks constituent)
# ─────────────────────────────────────────────────────────────

KRAKEN_WS_URL = "wss://ws.kraken.com/v2"
KRAKEN_RECONNECT_DELAY = 5


async def run_binance_feed(price_store: PriceStore, print_prices: bool = True):
    """
    Kraken BTC/USD trade feed (function kept as run_binance_feed for compatibility).
    Fires on every executed trade via Kraken WebSocket v2.
    """
    while True:
        try:
            async with websockets.connect(
                KRAKEN_WS_URL, ping_interval=20, ping_timeout=10
            ) as ws:
                await ws.send(json.dumps({
                    "method": "subscribe",
                    "params": {"channel": "trade", "symbol": ["BTC/USD"]},
                }))
                logger.info("Connected to Kraken BTC/USD feed")

                async for raw_message in ws:
                    data = json.loads(raw_message)
                    if data.get("channel") == "trade":
                        trades = data.get("data", [])
                        if trades:
                            price = float(trades[-1]["price"])
                            price_store.update_binance(price)
                            _print_prices(price_store, print_prices)

        except websockets.exceptions.ConnectionClosed as e:
            logger.warning(f"Kraken closed: {e}. Reconnecting in {KRAKEN_RECONNECT_DELAY}s...")
            await asyncio.sleep(KRAKEN_RECONNECT_DELAY)
        except Exception as e:
            logger.error(f"Kraken feed error: {e}. Reconnecting in {KRAKEN_RECONNECT_DELAY}s...")
            await asyncio.sleep(KRAKEN_RECONNECT_DELAY)


# ─────────────────────────────────────────────────────────────
# COINBASE FEED  (settlement exchange, CF Benchmarks constituent)
# ─────────────────────────────────────────────────────────────

COINBASE_WS_URL = "wss://advanced-trade-ws.coinbase.com"
COINBASE_RECONNECT_DELAY = 5


async def run_coinbase_feed(price_store: PriceStore, print_prices: bool = True):
    """
    Coinbase Advanced Trade WebSocket -- BTC-USD ticker.
    No API key required for public market data.
    """
    while True:
        try:
            async with websockets.connect(COINBASE_WS_URL) as ws:
                await ws.send(json.dumps({
                    "type": "subscribe",
                    "product_ids": ["BTC-USD"],
                    "channel": "ticker",
                }))
                logger.info("Connected to Coinbase BTC-USD feed")

                async for raw_message in ws:
                    data = json.loads(raw_message)
                    channel  = data.get("channel", "")
                    msg_type = data.get("type", "")

                    if channel == "ticker":
                        for event in data.get("events", []):
                            for tick in event.get("tickers", []):
                                price_str = tick.get("price")
                                if price_str:
                                    price_store.update_coinbase(float(price_str))
                                    _print_prices(price_store, print_prices)
                    elif msg_type == "ticker":
                        price_str = data.get("price")
                        if price_str:
                            price_store.update_coinbase(float(price_str))
                            _print_prices(price_store, print_prices)

        except websockets.exceptions.ConnectionClosed as e:
            logger.warning(f"Coinbase closed: {e}. Reconnecting in {COINBASE_RECONNECT_DELAY}s...")
            await asyncio.sleep(COINBASE_RECONNECT_DELAY)
        except Exception as e:
            logger.error(f"Coinbase feed error: {e}. Reconnecting in {COINBASE_RECONNECT_DELAY}s...")
            await asyncio.sleep(COINBASE_RECONNECT_DELAY)


# ─────────────────────────────────────────────────────────────
# BITSTAMP FEED  (CF Benchmarks constituent)
# ─────────────────────────────────────────────────────────────

BITSTAMP_WS_URL = "wss://ws.bitstamp.net"
BITSTAMP_RECONNECT_DELAY = 5


async def run_bitstamp_feed(price_store: PriceStore, print_prices: bool = True):
    """
    Bitstamp BTC/USD live trade feed.
    Fires on every executed trade -- no auth required.
    """
    while True:
        try:
            async with websockets.connect(BITSTAMP_WS_URL) as ws:
                await ws.send(json.dumps({
                    "event": "bts:subscribe",
                    "data": {"channel": "live_trades_btcusd"},
                }))
                logger.info("Connected to Bitstamp BTC/USD feed")

                async for raw_message in ws:
                    data = json.loads(raw_message)
                    event = data.get("event", "")

                    if event == "trade":
                        # Bitstamp sends price as a float directly
                        raw_price = data.get("data", {}).get("price")
                        if raw_price is not None:
                            price_store.update_bitstamp(float(raw_price))
                    elif event == "bts:subscription_succeeded":
                        logger.info("Bitstamp subscription confirmed")

        except websockets.exceptions.ConnectionClosed as e:
            logger.warning(f"Bitstamp closed: {e}. Reconnecting in {BITSTAMP_RECONNECT_DELAY}s...")
            await asyncio.sleep(BITSTAMP_RECONNECT_DELAY)
        except Exception as e:
            logger.error(f"Bitstamp feed error: {e}. Reconnecting in {BITSTAMP_RECONNECT_DELAY}s...")
            await asyncio.sleep(BITSTAMP_RECONNECT_DELAY)


# ─────────────────────────────────────────────────────────────
# GEMINI FEED  (CF Benchmarks constituent)
# ─────────────────────────────────────────────────────────────

GEMINI_WS_URL = "wss://api.gemini.com/v1/marketdata/BTCUSD"
GEMINI_RECONNECT_DELAY = 5


async def run_gemini_feed(price_store: PriceStore, print_prices: bool = True):
    """
    Gemini BTC/USD market data feed (v1).
    Streams trade events -- no auth required, US-accessible.
    """
    while True:
        try:
            async with websockets.connect(GEMINI_WS_URL) as ws:
                logger.info("Connected to Gemini BTC/USD feed")

                async for raw_message in ws:
                    data = json.loads(raw_message)

                    if data.get("type") == "update":
                        for event in data.get("events", []):
                            if event.get("type") == "trade":
                                price_str = event.get("price")
                                if price_str:
                                    price_store.update_gemini(float(price_str))

        except websockets.exceptions.ConnectionClosed as e:
            logger.warning(f"Gemini closed: {e}. Reconnecting in {GEMINI_RECONNECT_DELAY}s...")
            await asyncio.sleep(GEMINI_RECONNECT_DELAY)
        except Exception as e:
            logger.error(f"Gemini feed error: {e}. Reconnecting in {GEMINI_RECONNECT_DELAY}s...")
            await asyncio.sleep(GEMINI_RECONNECT_DELAY)


# ─────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────

def _print_prices(price_store: PriceStore, enabled: bool):
    if not enabled:
        return
    cf = price_store.get_cf_estimate()
    live = price_store.live_exchanges()
    if cf and len(live) >= 2:
        parts = []
        for ex in ("kraken", "coinbase", "bitstamp", "gemini"):
            p = price_store._prices.get(ex)
            if p:
                parts.append(f"[{ex[:4].title()}] ${p:,.2f}")
        parts.append(f"[CF est] ${cf:,.2f}")
        print(f"\r{'  '.join(parts)}   ", end="", flush=True)
