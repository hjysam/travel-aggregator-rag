# Aggregator App (Checkout + Confirm + Redis)

Now includes:
- **/checkout**: soft-hold with idempotency and re-pricing simulation
- **/confirm**: idempotent booking confirmation generating a deterministic PNR
- **Redis-backed** soft-hold & bookings if `REDIS_URL` is set (fallback to in-memory TTL)

## Run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# optional:
# export REDIS_URL=redis://localhost:6379/0
# export REPRICE_PROB=0.25
# export REPRICE_MAX_PCT=0.20
# export SOFT_HOLD_TTL_SEC=180
python app.py
```

## Demo script
```bash
# 1) Search
curl -s "http://localhost:8001/search?origin=BER&destination=MUC&date=2025-10-02&passengers=1" | jq '.offers[0]'

# 2) Checkout (+ idempotency)
OFFER_ID=$(curl -s "http://localhost:8001/search?origin=BER&destination=MUC&date=2025-10-02&passengers=1" | jq -r '.offers[0].offer_id')
CHK=$(curl -s -X POST "http://localhost:8001/checkout" -H "Content-Type: application/json" -H "X-Idempotency-Key: demo-abc"   -d "{"offer_id":"$OFFER_ID","passengers":1}")
echo $CHK | jq

# 3) Confirm (idempotent booking)
PRICE=$(echo $CHK | jq -r '.price_after')
curl -s -X POST "http://localhost:8001/confirm" -H "Content-Type: application/json" -H "X-Idempotency-Key: demo-abc"   -d "{"offer_id":"$OFFER_ID","accept_price":$PRICE,"payment_token":"tok_123"}" | jq

# 4) Replay confirm -> same PNR
curl -s -X POST "http://localhost:8001/confirm" -H "Content-Type: application/json" -H "X-Idempotency-Key: demo-abc"   -d "{"offer_id":"$OFFER_ID","accept_price":$PRICE,"payment_token":"tok_123"}" | jq
```
