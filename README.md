
# Travel-Booking-RAG

Two small FastAPI services you can run locally:

1. **Booking App** (booking_app.py) — mock multi-supplier aggregator with timeouts, circuit breaker, token-bucket rate limiting, TTL cache, and exports of search → checkout → confirm as text files.

   * Async fan-out/fan-in to 3 suppliers
   * **Timeouts**, **circuit breaker**, **token-bucket** rate limiting
   * **TTL cache** for identical searches
   * Deterministic **scoring/ranking** (price + duration + reliability)
   * Prometheus **metrics** (per-supplier latency + totals)
   * Optional **exports** of `search → checkout → confirm` as text files

2. **RAG App** – retrieval over your policy docs and the exported booking logs, with TF-IDF + optional FAISS/Azure embeddings and optional LLM answers.

   * TF-IDF + optional FAISS (semantic) using Azure OpenAI embeddings (SBERT fallback supported in code)
   * Optional LLM answers (Azure OpenAI / OpenAI-compatible)
   * Returns **citations** + **timings**
   * Can index **multiple folders** (e.g., policies **and** exported booking flows)

---

## Repo layout

```
travel-aggregator-rag/
├─ __pycache__/             # Python cache (can ignore)
├─ docs/                    # Put .txt/.md policy docs here
├─ exports/                 # Booking app writes here (search/checkout/confirm logs)
├─ mermaid/                 # (your diagrams – optional)
├─ .env                     # environment for both apps (see below)
├─ booking_app.py           # Booking service (port 8001)
├─ rag_app.py               # RAG service (port 8002)
├─ README.md
└─ requirements.txt         # shared minimal deps (or install per-file if you split)

```

---

## Quick Start

```bash
# Create & activate an env
conda create -n travel python=3.11 -y
conda activate travel
```

---

## Install

```bash
pip install -r requirements.txt
```

---

## Configuration


Create `aggregator_app/.env` (optional):

```ini
# aggregator_app/.env
EXPORT_DIR=exports
REPRICE_PROB=0.18
REPRICE_MAX_PCT=0.12
SOFT_HOLD_TTL_SEC=120
# REDIS_URL=redis://localhost:6379/0
```

### Create .env file

```ini
# One or many root folders with .txt/.md (semicolon-separated on Windows)
DOC_ROOTS=path\path;

# Enable FAISS+higher quality retrieval (optional)
USE_FAISS=1

# Azure OpenAI Embeddings (optional but recommended)
AZURE_OPENAI_ENDPOINT=https://<your-resource>.openai.azure.com
AZURE_OPENAI_API_KEY=YOUR_KEY
AZURE_OPENAI_API_VERSION=2024-10-21
AZURE_OPENAI_EMBED_DEPLOYMENT=text-embedding-3-large

# Azure OpenAI Chat for synthesized answers (optional)
AZURE_OPENAI_DEPLOYMENT=gpt-4o
```
---

## Run the services

### 1) Booking App (port **8001**)

```bash
uvicorn booking_app:app --reload --port 8001
```
Health:

```powershell
Invoke-RestMethod -Method GET -Uri http://localhost:8001/health
```

### 2) RAG App (port **8002**)

```bash
uvicorn rag_app:app --reload --port 8002
```
Health:

```powershell
Invoke-RestMethod -Method GET -Uri http://localhost:8002/diag/docs
Invoke-RestMethod -Method GET -Uri http://localhost:8002/diag/azure
Invoke-RestMethod -Method GET -Uri http://localhost:8002/diag/deployments
Invoke-RestMethod -Method GET -Uri http://localhost:8002/diag/embed_try
```

---

## End-to-end demo (Aggregator → exports → RAG)

### A) Run a full booking flow (and write exports)

Use this PowerShell script (replace dates/origins as you like):

```powershell
  # ===== Config =====
  $origin      = "BER"
  $destination = "AMS"
  $date        = "2025-10-02"
  $passengers  = 1
  $timeout_ms  = 800
  $top_k       = 10
  $interactive = $false   # set $true if you want a GUI picker (requires Out-GridView)

  # One trace ID across the whole flow so files group together
  $trace = ([guid]::NewGuid().ToString("N")).Substring(0,16)

  # 1) Search
  $searchUri = "http://localhost:8001/search?origin=$origin&destination=$destination&date=$date&passengers=$passengers&timeout_ms=$timeout_ms&top_k=$top_k"
  $searchHeaders = @{ Accept = "application/json"; "X-Trace-Id" = $trace }
  $resp = Invoke-RestMethod -Method GET -Uri $searchUri -Headers $searchHeaders

  $script:i = 1
  $offers = $resp.offers | Sort-Object -Property score -Descending
  "--- Offers (sorted by score) ---"
  $offers | Select-Object `
    @{n="#";e={$script:i;$script:i++}}, provider, offer_id,
    @{n="price";e={$_.price}}, currency,
  @{n="duration_min";e={$_.duration_min}},
  @{n="score";e={$_.score}} | Format-Table -AutoSize -RepeatHeader

# 2) Select offer (top1 or GUI)
$selected = if ($interactive -and (Get-Command Out-GridView -ErrorAction SilentlyContinue)) {
  $offers | Select-Object provider,offer_id,price,currency,duration_min,score |
    Out-GridView -Title "Pick an offer" -PassThru
} else { $offers | Select-Object -First 1 }

if (-not $selected) { Write-Host "No offer selected." ; return }

$offerId = $selected.offer_id
$headers = @{
  "Content-Type"      = "application/json"
  "X-Idempotency-Key" = $offerId
  "X-Trace-Id"        = $trace
}

# 3) Checkout (soft-hold + repricing)
$coBody = @{ offer_id = $offerId; passengers = $passengers } | ConvertTo-Json -Compress
$checkout = Invoke-RestMethod -Uri "http://localhost:8001/checkout" -Method POST -Headers $headers -Body $coBody
"--- Checkout ---"
$checkout | Select-Object offer_id, repriced, price_before, price_after, currency, hold_expires_in_sec, message | Format-List

# 4) Confirm (idempotent)
$acceptPrice = $checkout.price_after
$cfBody = @{ offer_id = $offerId; accept_price = $acceptPrice; payment_token = "tok_demo_123" } | ConvertTo-Json -Compress
$confirm = Invoke-RestMethod -Uri "http://localhost:8001/confirm" -Method POST -Headers $headers -Body $cfBody
"--- Confirm ---"
$confirm | Select-Object booking_ref, final_price, currency, idempotency_key, message | Format-List

# Idempotent replay (same PNR)
$confirm2 = Invoke-RestMethod -Uri "http://localhost:8001/confirm" -Method POST -Headers $headers -Body $cfBody
"Idempotent replay booking_ref: $($confirm2.booking_ref)"

# Locations of exported files
Write-Host "`nTrace ID used: $trace" -ForegroundColor Green
Write-Host "Exports under: $(Join-Path (Get-Location) 'exports')" -ForegroundColor Green
Write-Host "Expected files:" -ForegroundColor Green
Write-Host " - $trace`_search.txt" -ForegroundColor Green
Write-Host " - $trace`_checkout.txt" -ForegroundColor Green
Write-Host " - $trace`_confirm.txt" -ForegroundColor Green
Write-Host " - $trace.log.txt (combined)" -ForegroundColor Green
```

> The Booking App will emit four files in `./exports`. These are just pretty text logs you can index with the RAG app.

### B) Point RAG to both policies **and** the exported booking logs

1. Ensure `rag_app/.env` contains **both** folders in `DOC_ROOTS` (Windows uses `;` to separate):

   ```
   DOC_ROOTS=C:\Users\samuel_hon\OneDrive\Desktop\travel-aggregator-rag\rag_app\docs;C:\Users\samuel_hon\OneDrive\Desktop\travel-aggregator-rag\exports
   ```

2. Rebuild the index and run the RAG app:

   ```powershell
   cd .\rag_app
   python app.py --rebuild
   uvicorn app:app --reload --port 8002
   ```

3. Ask questions:

   ```powershell
   # health
   Invoke-RestMethod -Method GET -Uri http://localhost:8002/health

   # policy question
   $body = @{ query = "Can I get a refund if my train is late?"; k = 6 } | ConvertTo-Json
   Invoke-RestMethod -Method POST -Uri http://localhost:8002/answer -ContentType "application/json" -Body $body

   # booking question (pulled from exports you just created)
   $body = @{ query = "What was the confirmed price on my last booking?"; k = 6 } | ConvertTo-Json
   Invoke-RestMethod -Method POST -Uri http://localhost:8002/answer -ContentType "application/json" -Body $body
   ```
---

## Useful endpoints

**Booking App (8001)**

* `GET /health` – service health
* `GET /search` – run a search (query params)
* `POST /checkout` – soft hold + repricing
* `POST /confirm` – confirm booking (idempotent)
* `GET /metrics` – Prometheus metrics

**RAG App (8002)**

* `GET /health` – RAG health + chunk count
* `POST /answer` – `{ "query": "...", "k": 6 }`
* `POST /reindex` – forces reindex
* `GET /diag/azure` – show Azure env seen by server
* `GET /diag/deployments` – list Azure deployments (if configured)
* `GET /diag/embed_try` – test one embedding call
* `GET /diag/docs` – show resolved DOC_ROOTS and discovered files

---
