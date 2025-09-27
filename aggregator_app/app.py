#!/usr/bin/env python3
import asyncio, time, random, json, os, hashlib
from typing import List, Dict, Any, Tuple, Optional
from fastapi import FastAPI, Query, Header, HTTPException, Request, Response
from pydantic import BaseModel
from prometheus_client import Counter, Histogram, Summary, generate_latest, CONTENT_TYPE_LATEST

app = FastAPI(title="Omio Aggregator Demo (Obs + Trace)")

# ------------ Config / toggles ------------
REPRICE_PROB = float(os.getenv("REPRICE_PROB", "0.18"))
REPRICE_MAX_PCT = float(os.getenv("REPRICE_MAX_PCT", "0.12"))
SOFT_HOLD_TTL_SEC = int(os.getenv("SOFT_HOLD_TTL_SEC", "120"))
USE_REDIS = bool(os.getenv("REDIS_URL"))

# ------------ Metrics ------------
REQ_COUNT = Counter("http_requests_total", "HTTP requests", ["route", "method", "code"])
REQ_LATENCY = Histogram("http_request_duration_seconds", "HTTP latency seconds", ["route", "method"])
SUPPLIER_LAT_MS = Summary("supplier_latency_ms", "Supplier latency ms", ["supplier", "status"])
BREAKER_OPEN = Counter("circuit_breaker_opens_total", "Breaker opens", ["supplier"])

# Trace-id middleware
@app.middleware("http")
async def add_trace_and_metrics(request: Request, call_next):
    start = time.time()
    trace_id = request.headers.get("X-Trace-Id") or hashlib.sha1(f"{start}:{id(request)}".encode()).hexdigest()[:16]
    route = request.url.path
    method = request.method
    try:
        response = await call_next(request)
        code = response.status_code
    except Exception as e:
        code = 500
        raise
    finally:
        REQ_COUNT.labels(route=route, method=method, code=str(code)).inc()
        REQ_LATENCY.labels(route=route, method=method).observe(time.time() - start)
    # attach trace id header
    if 'response' in locals():
        response.headers["X-Trace-Id"] = trace_id
        return response

# ------------ Utilities: currency & scoring ------------
CURRENCY_RATES = {"EUR": 1.0, "USD": 0.92, "GBP": 1.16, "SGD": 0.68}
def to_eur(amount: float, currency: str) -> float:
    rate = CURRENCY_RATES.get(currency.upper(), 1.0)
    return round(amount * rate, 2)

def score_offer(eur: float, duration_min: int, reliability: float) -> float:
    price_term = 1.0 / (1.0 + eur/50.0)
    dur_term   = 1.0 / (1.0 + duration_min/120.0)
    return round((0.6*price_term + 0.3*dur_term + 0.1*reliability), 4)

# ------------ Token bucket rate limiter (per supplier) ------------
class TokenBucket:
    def __init__(self, rate_per_sec: float, burst: int):
        self.rate = rate_per_sec
        self.capacity = burst
        self.tokens = burst
        self.updated = time.time()
        self.lock = asyncio.Lock()
    async def acquire(self):
        async with self.lock:
            now = time.time()
            elapsed = now - self.updated
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
            self.updated = now
            if self.tokens < 1.0:
                wait = (1.0 - self.tokens) / self.rate
                await asyncio.sleep(wait)
                self.tokens = 0.0
                self.updated = time.time()
            else:
                self.tokens -= 1.0

# ------------ Circuit breaker ------------
class CircuitBreaker:
    def __init__(self, fail_threshold=3, open_secs=5, supplier_name=""):
        self.fail_threshold = fail_threshold
        self.open_secs = open_secs
        self.fail_count = 0
        self.open_until = 0.0
        self.supplier_name = supplier_name
    def can_call(self) -> bool:
        return time.time() >= self.open_until
    def on_success(self):
        self.fail_count = 0
    def on_failure(self):
        self.fail_count += 1
        if self.fail_count >= self.fail_threshold:
            self.open_until = time.time() + self.open_secs
            self.fail_count = 0
            BREAKER_OPEN.labels(self.supplier_name).inc()

# ------------ Mock suppliers ------------
class Supplier:
    def __init__(self, name: str, reliability: float, mean_latency_ms: int, currency: str):
        self.name = name
        self.reliability = reliability
        self.mean_latency_ms = mean_latency_ms
        self.currency = currency
        self.bucket = TokenBucket(rate_per_sec=8, burst=4)
        self.breaker = CircuitBreaker(fail_threshold=3, open_secs=5, supplier_name=name)

    async def search(self, origin: str, destination: str, date: str, passengers: int, timeout_ms=800) -> Tuple[List[Dict[str,Any]], float, str]:
        if not self.breaker.can_call():
            return [], 0.0, "circuit_open"

        t0 = time.time()
        await self.bucket.acquire()
        latency = max(10, int(random.gauss(self.mean_latency_ms, self.mean_latency_ms*0.25)))
        fail_prob = max(0.0, (1.0 - self.reliability) * 0.6)
        status = "ok"
        try:
            await asyncio.wait_for(asyncio.sleep(latency/1000.0), timeout=timeout_ms/1000.0)
            if random.random() < fail_prob:
                raise RuntimeError("supplier_error")
            rng = random.Random(f"{self.name}:{origin}:{destination}:{date}:{passengers}")
            offers = []
            for _ in range(rng.randint(3,6)):
                base_price = rng.uniform(19, 149)
                duration = rng.randint(45, 420)
                depart_hour = rng.randint(5, 21)
                depart_min = rng.choice([0, 10, 20, 30, 40, 50])
                arr_min_total = depart_hour*60 + depart_min + duration
                arrive_hour = (arr_min_total // 60) % 24
                arrive_min  = arr_min_total % 60
                offer = {
                    "provider": self.name,
                    "offer_id": "",
                    "price": round(base_price, 2),
                    "currency": self.currency,
                    "price_eur": to_eur(base_price, self.currency),
                    "duration_min": duration,
                    "depart_local": f"{date}T{depart_hour:02d}:{depart_min:02d}",
                    "arrive_local": f"{date}T{arrive_hour:02d}:{arrive_min:02d}",
                    "transfers": rng.choice([0,0,1]),
                    "refundable": rng.choice([True, False]),
                    "fare_class": rng.choice(["Saver","Standard","Flex"]),
                    "supplier_reliability": round(self.reliability, 2)
                }
                offer["score"] = score_offer(offer["price_eur"], offer["duration_min"], self.reliability)
                offers.append(offer)
            self.breaker.on_success()
            return offers, (time.time()-t0)*1000.0, status
        except Exception:
            self.breaker.on_failure()
            status = "timeout_or_error"
            return [], (time.time()-t0)*1000.0, status
        finally:
            SUPPLIER_LAT_MS.labels(self.name, status).observe((time.time()-t0)*1000.0)

SUPPLIERS = [
    Supplier("RailEuro", reliability=0.97, mean_latency_ms=180, currency="EUR"),
    Supplier("BusExpress", reliability=0.92, mean_latency_ms=260, currency="USD"),
    Supplier("AirLite", reliability=0.90, mean_latency_ms=320, currency="GBP"),
]

# ------------ Cache (Redis or TTL) ------------
class TTLCache:
    def __init__(self, ttl_sec=30, max_items=256):
        self.ttl = ttl_sec
        self.max = max_items
        self._store: Dict[str, Tuple[float, Any]] = {}

    def get(self, key: str):
        v = self._store.get(key)
        if not v: return None
        ts, data = v
        if time.time() - ts > self.ttl:
            self._store.pop(key, None)
            return None
        return data

    def set(self, key: str, value: Any, ttl: Optional[int]=None):
        if len(self._store) >= self.max:
            oldest = sorted(self._store.items(), key=lambda kv: kv[1][0])[0][0]
            self._store.pop(oldest, None)
        self._store[key] = (time.time(), value)

REDIS = None
if USE_REDIS:
    try:
        import redis
        REDIS = redis.from_url(os.getenv("REDIS_URL"))
    except Exception:
        REDIS = None

SEARCH_CACHE = TTLCache(ttl_sec=30, max_items=512) if REDIS is None else None

def cache_get(key: str):
    if REDIS:
        v = REDIS.get(key)
        return json.loads(v) if v else None
    return SEARCH_CACHE.get(key)

def cache_set(key: str, value: Any, ttl: int = 30):
    if REDIS:
        REDIS.setex(key, ttl, json.dumps(value))
    else:
        SEARCH_CACHE.set(key, value, ttl=ttl)

# ------------ Soft holds & bookings (Redis-backed if available) ------------
def offer_id_hash(offer: Dict[str, Any]) -> str:
    basis = f"{offer['provider']}|{offer['depart_local']}|{offer['arrive_local']}|{offer['price']}|{offer['currency']}|{offer['fare_class']}|{offer['transfers']}"
    return hashlib.sha256(basis.encode()).hexdigest()[:20]

def reprice_offer(offer: Dict[str, Any]) -> Dict[str, Any]:
    import random as _rnd
    if _rnd.random() > REPRICE_PROB:
        return {**offer, "repriced": False, "new_price": offer["price"], "new_price_eur": offer["price_eur"]}
    sign = 1 if _rnd.random() < 0.6 else -1
    pct = _rnd.random() * REPRICE_MAX_PCT
    new_price = round(max(1.0, offer["price"] * (1 + sign * pct)), 2)
    new_eur = to_eur(new_price, offer["currency"])
    return {**offer, "repriced": True, "old_price": offer["price"], "old_price_eur": offer["price_eur"], "new_price": new_price, "new_price_eur": new_eur}

def holds_get(key: str):
    if REDIS:
        v = REDIS.get(key)
        return json.loads(v) if v else None
    return HOLDS.get(key)

def holds_set(key: str, value: Any, ttl: int):
    if REDIS:
        REDIS.setex(key, ttl, json.dumps(value))
    else:
        HOLDS.set(key, value)

def bookings_get(key: str):
    if REDIS:
        v = REDIS.get(key)
        return json.loads(v) if v else None
    return BOOKINGS.get(key)

def bookings_set(key: str, value: Any):
    if REDIS:
        REDIS.set(key, json.dumps(value))
    else:
        BOOKINGS.set(key, value)

HOLDS = TTLCache(ttl_sec=SOFT_HOLD_TTL_SEC, max_items=2048)
BOOKINGS = TTLCache(ttl_sec=24*3600, max_items=4096)

# ------------ API Models ------------
class SearchOut(BaseModel):
    origin: str
    destination: str
    date: str
    passengers: int
    offers: List[Dict[str, Any]]
    timings_ms: Dict[str, float]
    supplier_status: Dict[str, str]
    cached: bool = False

class CheckoutIn(BaseModel):
    offer_id: str
    passengers: int = 1

class CheckoutOut(BaseModel):
    offer_id: str
    repriced: bool
    price_before: float
    price_after: float
    currency: str
    idempotency_key: str
    hold_expires_in_sec: int
    provider: str
    message: str

class ConfirmIn(BaseModel):
    offer_id: str
    accept_price: float
    payment_token: str

class ConfirmOut(BaseModel):
    booking_ref: str
    offer_id: str
    final_price: float
    currency: str
    idempotency_key: str
    message: str

# ------------ Endpoints ------------
@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

@app.get("/health")
def health():
    return {"status":"ok", "suppliers": [s.name for s in SUPPLIERS], "redis": bool(REDIS)}

@app.get("/search", response_model=SearchOut)
async def search(
    origin: str = Query(..., min_length=3, max_length=5),
    destination: str = Query(..., min_length=3, max_length=5),
    date: str = Query(..., regex=r"^\d{4}-\d{2}-\d{2}$"),
    passengers: int = Query(1, ge=1, le=8),
    timeout_ms: int = Query(800, ge=200, le=3000),
    top_k: int = Query(20, ge=1, le=50),
):
    key = f"{origin}:{destination}:{date}:{passengers}:{timeout_ms}:{top_k}"
    cached = cache_get(key)
    if cached:
        out = cached.copy()
        out["cached"] = True
        return out

    t0 = time.time()
    tasks = [s.search(origin, destination, date, passengers, timeout_ms) for s in SUPPLIERS if s.breaker.can_call()]
    results = await asyncio.gather(*tasks, return_exceptions=False)

    offers: List[Dict[str, Any]] = []
    timings: Dict[str, float] = {}
    status: Dict[str, str] = {}
    for s, (data, ms, stat) in zip([s for s in SUPPLIERS if s.breaker.can_call()], results):
        timings[s.name] = round(ms, 1)
        status[s.name]  = stat
        for off in data:
            off["offer_id"] = offer_id_hash(off)
            offers.append(off)

    offers.sort(key=lambda o: o["score"], reverse=True)
    offers = offers[:top_k]

    out = {
        "origin": origin,
        "destination": destination,
        "date": date,
        "passengers": passengers,
        "offers": offers,
        "timings_ms": {"total": round((time.time()-t0)*1000.0, 1), **timings},
        "supplier_status": status,
        "cached": False,
    }
    cache_set(key, out, ttl=30)
    return out

@app.post("/checkout", response_model=CheckoutOut)
async def checkout(payload: CheckoutIn, x_idempotency_key: Optional[str] = Header(None)):
    if not x_idempotency_key:
        x_idempotency_key = hashlib.sha1(payload.offer_id.encode()).hexdigest()[:16]

    hold_key = f"hold:{x_idempotency_key}"
    exist = holds_get(hold_key)
    if exist:
        ttl = max(0, SOFT_HOLD_TTL_SEC - int(time.time() - exist["ts"]))
        return {
            "offer_id": exist["offer"]["offer_id"],
            "repriced": exist["repriced"],
            "price_before": exist["price_before"],
            "price_after": exist["price_after"],
            "currency": exist["offer"]["currency"],
            "idempotency_key": x_idempotency_key,
            "hold_expires_in_sec": ttl,
            "provider": exist["offer"]["provider"],
            "message": "idempotent replay"
        }

    dummy_offer = {
        "offer_id": payload.offer_id,
        "provider": "Unknown",
        "price": 49.0,
        "currency": "EUR",
        "price_eur": 49.0,
        "duration_min": 120,
        "depart_local": "2025-10-02T08:00",
        "arrive_local": "2025-10-02T10:00",
        "fare_class": "Standard",
        "transfers": 0
    }
    new = reprice_offer(dummy_offer)
    record = {
        "offer": dummy_offer if not new.get("repriced") else {**dummy_offer, "price": new["new_price"], "price_eur": new["new_price_eur"]},
        "repriced": new.get("repriced", False),
        "price_before": new.get("old_price", dummy_offer["price"]),
        "price_after": new.get("new_price", dummy_offer["price"]),
        "ts": time.time()
    }
    holds_set(hold_key, record, ttl=SOFT_HOLD_TTL_SEC)

    return {
        "offer_id": payload.offer_id,
        "repriced": record["repriced"],
        "price_before": record["price_before"],
        "price_after": record["price_after"],
        "currency": dummy_offer["currency"],
        "idempotency_key": x_idempotency_key,
        "hold_expires_in_sec": SOFT_HOLD_TTL_SEC,
        "provider": dummy_offer["provider"],
        "message": "soft-hold created"
    }

def _pnr_from(offer_id: str, idempotency_key: str) -> str:
    rng = hashlib.sha256(f"{offer_id}|{idempotency_key}".encode()).hexdigest().upper()
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "-".join("".join(alphabet[int(rng[i:i+2],16)%len(alphabet)] for i in range(j, j+8, 2)) for j in range(0, 16, 4))

class ConfirmIn(BaseModel):
    offer_id: str
    accept_price: float
    payment_token: str

class ConfirmOut(BaseModel):
    booking_ref: str
    offer_id: str
    final_price: float
    currency: str
    idempotency_key: str
    message: str

@app.post("/confirm", response_model=ConfirmOut)
async def confirm(payload: ConfirmIn, x_idempotency_key: Optional[str] = Header(None)):
    if not x_idempotency_key:
        x_idempotency_key = hashlib.sha1((payload.offer_id + payload.payment_token).encode()).hexdigest()[:16]

    booking_key = f"booking:{x_idempotency_key}"
    existing = bookings_get(booking_key)
    if existing:
        return existing

    hold_key = f"hold:{x_idempotency_key}"
    hold = holds_get(hold_key)
    if not hold:
        raise HTTPException(status_code=409, detail="No active soft-hold (or hold expired). Please /checkout again.")

    final_price = hold["price_after"]
    currency = hold["offer"]["currency"]
    if abs(final_price - float(payload.accept_price)) > 0.01:
        raise HTTPException(status_code=409, detail="Price changed or mismatch. Confirm with updated price.")

    pnr = _pnr_from(payload.offer_id, x_idempotency_key)
    booking = {
        "booking_ref": pnr,
        "offer_id": payload.offer_id,
        "final_price": final_price,
        "currency": currency,
        "idempotency_key": x_idempotency_key,
        "message": "booking confirmed"
    }
    bookings_set(booking_key, booking)
    return booking

# ------------- main -------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8001, reload=False)
