"""Quick probe: does data-api /activity paginate via offset?"""
from __future__ import annotations

import httpx

ADDR = "0x56687bf447db6ffa42ffe2204a05edaa20f55839"  # Theo4
URL = "https://data-api.polymarket.com/activity"

with httpx.Client(timeout=20.0) as c:
    p0 = c.get(URL, params={"user": ADDR, "limit": 5, "offset": 0}).json()
    p1 = c.get(URL, params={"user": ADDR, "limit": 5, "offset": 5}).json()
    p_big = c.get(URL, params={"user": ADDR, "limit": 5, "offset": 100}).json()

print(f"page0 (offset=0):  n={len(p0)} ts={[r['timestamp'] for r in p0]}")
print(f"page1 (offset=5):  n={len(p1)} ts={[r['timestamp'] for r in p1]}")
print(f"big   (offset=100):n={len(p_big)} ts={[r['timestamp'] for r in p_big]}")

if p0 and p1:
    print(f"\npage0[0].tx = {p0[0]['transactionHash'][:14]}")
    print(f"page1[0].tx = {p1[0]['transactionHash'][:14]}")
    print(f"identical first row? {p0[0]['transactionHash'] == p1[0]['transactionHash']}")
    print(f"page0 tx set & page1 tx set overlap: "
          f"{len(set(r['transactionHash'] for r in p0) & set(r['transactionHash'] for r in p1))}")
