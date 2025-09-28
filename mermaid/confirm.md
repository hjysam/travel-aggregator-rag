```mermaid
flowchart TD
  %% /checkout
  K0[Checkout] --> K1[Idempotency key]
  K1 --> K2{Hold exists?}
  K2 -- Yes --> K3[Return existing hold]
  K2 -- No --> K4[Dummy offer + reprice]
  K4 --> K5[Save hold TTL=SOFT_HOLD_TTL_SEC]
  K5 --> K6[Return CheckoutOut]

  %% /confirm
  P0[Confirm] --> P1[Idempotency key]
  P1 --> P2{Booking exists?}
  P2 -- Yes --> P3[Return booking]
  P2 -- No --> P4[Load hold]
  P4 --> P5{Hold present?}
  P5 -- No --> E1[409 no soft hold]
  P5 -- Yes --> P6{Price matches?}
  P6 -- No --> E2[409 price mismatch]
  P6 -- Yes --> P7[Generate PNR; save booking]
  P7 --> P8[Return ConfirmOut]
