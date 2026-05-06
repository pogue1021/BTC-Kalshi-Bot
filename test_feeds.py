"""
test_feeds.py -- Quick diagnostic for price feed connections.
Run this to see exactly what each exchange is sending (or why it's failing).

    python test_feeds.py
"""

import asyncio
import json
import sys
import websockets


async def test_binance_us_aggtrade():
    url = "wss://stream.binance.us:9443/ws/btcusdt@aggTrade"
    print(f"\n[Binance US aggTrade] Connecting to {url} ...")
    try:
        async with websockets.connect(url, open_timeout=5) as ws:
            print("[Binance US aggTrade] Connected! Waiting for first message...")
            msg = await asyncio.wait_for(ws.recv(), timeout=8)
            data = json.loads(msg)
            print(f"[Binance US aggTrade] RAW: {json.dumps(data, indent=2)}")
            price = data.get("p")
            print(f"[Binance US aggTrade] Price field 'p' = {price}")
    except Exception as e:
        print(f"[Binance US aggTrade] FAILED: {type(e).__name__}: {e}")


async def test_binance_global_aggtrade():
    url = "wss://stream.binance.com:9443/ws/btcusdt@aggTrade"
    print(f"\n[Binance Global aggTrade] Connecting to {url} ...")
    try:
        async with websockets.connect(url, open_timeout=5) as ws:
            print("[Binance Global aggTrade] Connected! Waiting for first message...")
            msg = await asyncio.wait_for(ws.recv(), timeout=8)
            data = json.loads(msg)
            print(f"[Binance Global aggTrade] RAW: {json.dumps(data, indent=2)}")
            price = data.get("p")
            print(f"[Binance Global aggTrade] Price field 'p' = {price}")
    except Exception as e:
        print(f"[Binance Global aggTrade] FAILED: {type(e).__name__}: {e}")


async def test_coinbase():
    url = "wss://advanced-trade-ws.coinbase.com"
    print(f"\n[Coinbase] Connecting to {url} ...")
    try:
        async with websockets.connect(url, open_timeout=5) as ws:
            sub = {"type": "subscribe", "product_ids": ["BTC-USD"], "channel": "ticker"}
            await ws.send(json.dumps(sub))
            print("[Coinbase] Connected and subscribed! Waiting for messages...")
            for _ in range(4):
                msg = await asyncio.wait_for(ws.recv(), timeout=8)
                data = json.loads(msg)
                print(f"[Coinbase] RAW: {json.dumps(data, indent=2)[:400]}")
    except Exception as e:
        print(f"[Coinbase] FAILED: {type(e).__name__}: {e}")


async def test_kraken():
    url = "wss://ws.kraken.com/v2"
    print(f"\n[Kraken] Connecting to {url} ...")
    try:
        async with websockets.connect(url, open_timeout=5) as ws:
            sub = {"method": "subscribe", "params": {"channel": "trade", "symbol": ["BTC/USD"]}}
            await ws.send(json.dumps(sub))
            print("[Kraken] Connected and subscribed! Waiting for a trade message...")
            # Wait up to 20 messages to find an actual trade (first few are handshake/heartbeat)
            got_trade = False
            for _ in range(20):
                msg = await asyncio.wait_for(ws.recv(), timeout=8)
                data = json.loads(msg)
                channel = data.get("channel", "")
                if channel == "trade":
                    trades = data.get("data", [])
                    if trades:
                        price = trades[-1].get("price")
                        print(f"[Kraken] Trade price: {price}  -- WORKING!")
                        got_trade = True
                        break
                else:
                    print(f"[Kraken] preamble msg: channel={channel!r} (skipping)")
            if not got_trade:
                print("[Kraken] No trade received in 20 messages -- market may be quiet, but connection is good")
    except Exception as e:
        print(f"[Kraken] FAILED: {type(e).__name__}: {e}")


async def main():
    print("=" * 60)
    print("  Price Feed Diagnostic")
    print("=" * 60)
    await test_kraken()
    await test_coinbase()
    print("\n" + "=" * 60)
    print("Done.")
    print("=" * 60)


asyncio.run(main())
