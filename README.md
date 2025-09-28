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

## Demo
- **Aggregator**: “Search” fans out to carriers → timeouts → partial results → score & rank → return top options with reasons and timings. Explain how you'd swap in real HTTP adapters, retries/backoff, and Redis.
- **RAG**: Answer “Can I get a refund if my train is late?” with citations. Show hybrid retrieval, guardrails (“abstain if unknown”), and how you’d add policy versions per carrier & locale.

```bash
# create & activate 
conda create -n travel python=3.11 -y
conda activate travel 
```
```bash
# install deps for both apps
pip install -r aggregator_app/requirements.txt
pip install -r rag_app/requirements.txt
```

## Run quickly
### 1) Aggregator
```bash
cd aggregator_app
python app.py  # serves on :8001
# Test:
curl "http://localhost:8001/search?origin=BER&destination=MUC&date=2025-10-02&passengers=1" | jq
```

#### Smoke Test
```Powershell
# search
$resp = Invoke-RestMethod "http://localhost:8001/search?origin=SIN&destination=LON&date=2025-10-01&passengers=1&top_k=3"
$sel  = $resp.offers | Sort-Object score -Descending | Select-Object -First 1
$sel | Format-List provider,offer_id,price,currency

# checkout (uses selected EUR price, may reprice)
$headers = @{ "Content-Type"="application/json"; "X-Idempotency-Key"=$sel.offer_id }
$coBody  = @{ offer_id=$sel.offer_id; passengers=1 } | ConvertTo-Json -Compress
$co      = Invoke-RestMethod -Uri "http://localhost:8001/checkout" -Method POST -Headers $headers -Body $coBody
$co | Format-List price_before,price_after,currency,repriced

# confirm (idempotent)
$cfBody = @{ offer_id=$sel.offer_id; accept_price=$co.price_after; payment_token="tok_demo_123" } | ConvertTo-Json -Compress
$cf     = Invoke-RestMethod -Uri "http://localhost:8001/confirm" -Method POST -Headers $headers -Body $cfBody
$cf | Format-List booking_ref,final_price,currency,idempotency_key
```
#### Full Test
```Powershell
# ===== Config =====
$origin      = "SIN"
$destination = "LON"
$date        = "2025-10-01"
$passengers  = 1
$timeout_ms  = 800
$top_k       = 10
$interactive = $true   # set $false to auto-pick the top offer

# ===== 1) Search =====
$searchUri = "http://localhost:8001/search?origin=$origin&destination=$destination&date=$date&passengers=$passengers&timeout_ms=$timeout_ms&top_k=$top_k"
$resp = Invoke-RestMethod -Method GET -Uri $searchUri -Headers @{ Accept = "application/json" }

# ===== List offers (sorted by score desc) =====
$offers = $resp.offers | Sort-Object -Property score -Descending
"--- Offers (sorted by score) ---"
$offers | Select-Object `
  @{n="#";e={$script:i;$script:i++}}, provider, offer_id,
  @{n="price";e={$_.price}}, currency,
  @{n="duration_min";e={$_.duration_min}},
  @{n="score";e={$_.score}} |
  Format-Table -AutoSize -RepeatHeader

# ===== 2) Select offer =====
$selected =
  if ($interactive -and (Get-Command Out-GridView -ErrorAction SilentlyContinue)) {
    $offers | Select-Object provider,offer_id,price,currency,duration_min,score |
      Out-GridView -Title "Pick an offer" -PassThru
  } else {
    $offers | Select-Object -First 1
  }

if (-not $selected) {
  Write-Host "No offer selected. Exiting." -ForegroundColor Yellow
  return
}

$offerId = $selected.offer_id
Write-Host "Selected offer_id: $offerId" -ForegroundColor Cyan

# ===== 3) Checkout (soft-hold + repricing) =====
$headers = @{
  "Content-Type"      = "application/json"
  "X-Idempotency-Key" = $offerId   # reuse across checkout & confirm
}
$coBody = @{ offer_id = $offerId; passengers = $passengers } | ConvertTo-Json -Compress

$checkout = Invoke-RestMethod -Uri "http://localhost:8001/checkout" -Method POST -Headers $headers -Body $coBody
"--- Checkout ---"
$checkout | Select-Object offer_id, repriced, price_before, price_after, currency, hold_expires_in_sec, message | Format-List

# ===== 4) Confirm (idempotent) =====
$acceptPrice = $checkout.price_after
$cfBody = @{ offer_id = $offerId; accept_price = $acceptPrice; payment_token = "tok_demo_123" } | ConvertTo-Json -Compress

$confirm = Invoke-RestMethod -Uri "http://localhost:8001/confirm" -Method POST -Headers $headers -Body $cfBody
"--- Confirm ---"
$confirm | Select-Object booking_ref, final_price, currency, idempotency_key, message | Format-List

# Prove idempotency (replay confirm -> same PNR)
$confirm2 = Invoke-RestMethod -Uri "http://localhost:8001/confirm" -Method POST -Headers $headers -Body $cfBody
"Idempotent replay booking_ref: $($confirm2.booking_ref)"

# ===== 5) Quick metrics peek =====
"`n--- Metrics (snippets) ---"
$metrics = (Invoke-WebRequest "http://localhost:8001/metrics").Content -split "`r?`n"
$groups = @{}

foreach ($line in $metrics) {
  if ($line -match '^travel_supplier_latency_ms_(count|sum)\{([^}]*)\}\s+([0-9.]+)') {
    $kind    = $matches[1]
    $labelStr= $matches[2]
    $val     = [double]$matches[3]

    # Normalize label order (note the extra parentheses before -join)
    $norm = ( ($labelStr -split ',') | Sort-Object ) -join ','

    if (-not $groups.ContainsKey($norm)) { $groups[$norm] = @{sum=0;count=0} }
    if ($kind -eq 'sum')   { $groups[$norm].sum   = $val }
    if ($kind -eq 'count') { $groups[$norm].count = $val }
  }
}

$groups.GetEnumerator() | ForEach-Object {
  $avg = if ($_.Value.count -gt 0) { [math]::Round($_.Value.sum / $_.Value.count, 2) } else { 'n/a' }
  "[$($_.Key)] avg_ms=$avg  (sum=$($_.Value.sum), count=$($_.Value.count))"
}

```


### 2) RAG
```bash
cd ../rag_app
# optional for Azure/OpenAI:
# export OPENAI_API_KEY=...
# export OPENAI_BASE_URL=https://<your-azure-endpoint>.openai.azure.com/
# export OPENAI_MODEL=gpt-4o-mini
python app.py --rebuild  # then:
curl -s -X POST http://localhost:8000/answer -H 'Content-Type: application/json'   -d '{"query":"What are my refund rights if my EU train is delayed by 2 hours?"}' | jq
```

## Prompts you can drive
- “How would you hit **p95 < 250 ms**?” → supplier concurrency, co-location, caching, prefetching, SLM for easy Q&A, circuit breakers.
- “What about **currencies & locales**?” → deterministic currency conversion layer, locale-aware formatting, pricing precision (decimal not float), tax/VAT.
- “How to handle **re-pricing at checkout**?” → soft hold → re-query exact fare with supplier → idempotent booking refs.
- “**GDPR/PII**?” → minimization, encryption at rest/flight, redaction in logs, TTLs, ‘right to be forgotten’ flows.
