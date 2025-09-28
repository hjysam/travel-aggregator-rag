```mermaid
sequenceDiagram
  participant Client
  participant API as FastAPI_router_and_middleware
  participant Cache as Search_cache
  participant Holds as Holds_TTL
  participant Book as Bookings
  participant Sup as Suppliers_fanout

  Client->>API: GET /search
  API->>Cache: lookup key
  alt cache miss
    API->>Sup: async search fan out
    Sup-->>API: offers + timings + status
    API->>Cache: write 30s TTL
  end
  API-->>Client: SearchOut (offers)

  Client->>API: POST /checkout (offer_id)
  API->>Holds: get hold by idempotency key
  alt no hold
    API->>API: reprice dummy_offer
    API->>Holds: set TTL = SOFT_HOLD_TTL_SEC
  else hold exists
    API-->>Client: idempotent replay
  end
  API-->>Client: CheckoutOut (hold + price)

  Client->>API: POST /confirm (offer_id, accept_price)
  API->>Book: get booking by idempotency key
  alt booking exists
    API-->>Client: existing ConfirmOut
  else booking missing
    API->>Holds: get hold
    alt hold present and price matches
      API->>Book: save booking
      API-->>Client: ConfirmOut (booking_ref)
    else mismatch or absent
      API-->>Client: 409 error
    end
  end
