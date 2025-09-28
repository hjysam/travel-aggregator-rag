```mermaid
flowchart LR
  A[Client] --> B[Middleware add_trace_and_metrics]
  B --> C[FastAPI Router]
  C -->|GET /health| H[Health]
  C -->|GET /metrics| M[Metrics]
  C -->|GET /search| S[Search]
  C -->|POST /checkout| K[Checkout]
  C -->|POST /confirm| P[Confirm]

  subgraph Stores
    Cc[Search cache]
    Hh[Holds]
    Bk[Bookings]
  end
  subgraph Controls
    T[TokenBucket]
    R[CircuitBreaker]
    Pr[Prometheus counters]
  end

  S --> Cc
  K --> Hh
  P --> Bk
  S --- T
  S --- R
  B --- Pr
