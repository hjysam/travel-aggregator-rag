# travel-aggregator-rag

1) **Aggregator App** — A FastAPI microservice that simulates **multi-supplier search aggregation** with:
   - Async fan-out/fan-in to 3 mock suppliers
   - **Timeouts**, **circuit breaker**, simple **token-bucket rate limiting**
   - **TTL cache** for identical searches
   - Deterministic **scoring/ranking** (price + duration + reliability)
   - Per-supplier **latency metrics** and overall timings

2) **RAG App** — A FastAPI RAG microservice over **travel policies/FAQs** (EU261, refunds, baggage, seat reservations).
   - TF‑IDF + optional FAISS embeddings (MiniLM) for hybrid retrieval
   - Optional LLM generation through OpenAI‑compatible endpoints (works with Azure OpenAI)
   - Returns **citations** and **latency breakdown**

> Use these to *show* design, trade‑offs, and production‑readiness while you talk through travel‑relevant constraints (latency, reliability, multi‑modal routing, currencies, localization, SLAs/GDPR).

---

## What to demo (talk track)
- **Aggregator**: “Search” fans out to carriers → timeouts → partial results → score & rank → return top options with reasons and timings. Explain how you'd swap in real HTTP adapters, retries/backoff, and Redis.
- **RAG**: Answer “Can I get a refund if my train is late?” with citations. Show hybrid retrieval, guardrails (“abstain if unknown”), and how you’d add policy versions per carrier & locale.

## Run quickly
### 1) Aggregator
```bash
cd aggregator_app
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python app.py  # serves on :8001
# Test:
curl "http://localhost:8001/search?origin=BER&destination=MUC&date=2025-10-02&passengers=1" | jq
```

### 2) RAG
```bash
cd ../rag_app
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# optional for Azure/OpenAI:
# export OPENAI_API_KEY=...
# export OPENAI_BASE_URL=https://<your-azure-endpoint>.openai.azure.com/
# export OPENAI_MODEL=gpt-4o-mini
python app.py --rebuild  # then:
curl -s -X POST http://localhost:8000/answer -H 'Content-Type: application/json'   -d '{"query":"What are my refund rights if my EU train is delayed by 2 hours?"}' | jq
```

## Interview prompts you can drive
- “How would you hit **p95 < 250 ms**?” → supplier concurrency, co-location, caching, prefetching, SLM for easy Q&A, circuit breakers.
- “What about **currencies & locales**?” → deterministic currency conversion layer, locale-aware formatting, pricing precision (decimal not float), tax/VAT.
- “How to handle **re-pricing at checkout**?” → soft hold → re-query exact fare with supplier → idempotent booking refs.
- “**GDPR/PII**?” → minimization, encryption at rest/flight, redaction in logs, TTLs, ‘right to be forgotten’ flows.
