import sys, yaml, os
from datetime import datetime
from pathlib import Path
sys.path.insert(0, '.')
from kalshi_client import load_client_from_config
from dotenv import load_dotenv

load_dotenv(Path('.') / '.env')

with open('config.yaml') as f:
    config = yaml.safe_load(f)

# Inject .env credentials the same way main.py does
kalshi = config.setdefault('kalshi', {})
kalshi['api_key_id']       = os.environ.get('KALSHI_API_KEY_ID',       kalshi.get('api_key_id', ''))
kalshi['private_key_path'] = os.environ.get('KALSHI_PRIVATE_KEY_PATH', kalshi.get('private_key_path', ''))

client = load_client_from_config(config)

now = datetime.now()
midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
min_ts = int(midnight.timestamp())

prefix = 'KXBTC15M'
fills       = [f for f in client.get_fills(min_ts=min_ts)       if (f.get('ticker') or '').startswith(prefix)]
settlements = [s for s in client.get_settlements(min_ts=min_ts) if (s.get('ticker') or '').startswith(prefix)]

print(f"=== TODAY: {now.strftime('%Y-%m-%d')} ===")
print(f"Fills: {len(fills)}  |  Settlements: {len(settlements)}")
print()

print("=== RECENT FILLS (last 15) ===")
for f in fills[-15:]:
    ts     = (f.get('created_time') or '')[:19]
    action = (f.get('action') or '?').upper()
    side   = (f.get('side')   or '?').upper()
    ticker = (f.get('ticker') or '?')[-14:]
    price  = f.get('yes_price_dollars', '?')
    qty    = f.get('count_fp', '?')
    print(f"  {ts}  {action:4s}  {side:3s}  {ticker}  price={price}  qty={qty}")

print()
print("=== OPEN POSITIONS ===")
settled_tickers = {s.get('ticker') for s in settlements}
sell_tickers    = {f.get('ticker') for f in fills if (f.get('action') or '').lower() == 'sell'}
settled_or_sold = settled_tickers | sell_tickers
open_buys = [f for f in fills if (f.get('action') or '').lower() == 'buy' and f.get('ticker') not in settled_or_sold]
if open_buys:
    for f in open_buys:
        print(f"  {f.get('ticker')}  {(f.get('side') or '?').upper()}  qty={f.get('count_fp')}  price={f.get('yes_price_dollars')}")
else:
    print("  None")

print()
balance = client.get_balance()
print(f"=== ACCOUNT BALANCE: ${balance:.2f} ===")

# P&L summary
cost     = sum((float(f.get('yes_price_dollars') or 0) * float(f.get('count_fp') or 0)) for f in fills if (f.get('action') or '').lower() == 'buy')
proceeds = sum((float(f.get('yes_price_dollars') or 0) * float(f.get('count_fp') or 0)) for f in fills if (f.get('action') or '').lower() == 'sell')
settled_rev = sum(float(s.get('revenue') or 0) / (100 if float(s.get('revenue') or 0) > 1000 else 1) for s in settlements)
fees     = sum(float(f.get('fee_cost') or 0) for f in fills)
pnl      = proceeds + settled_rev - cost - fees
print(f"=== TODAY P&L: ${pnl:+.2f} (buys -${cost:.2f} | sells +${proceeds:.2f} | settled +${settled_rev:.2f} | fees -${fees:.2f}) ===")
