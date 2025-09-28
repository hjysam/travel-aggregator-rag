```mermaid
flowchart TD
  S0[Search handler] --> S1[Build key]
  S1 --> S2{Cache hit?}
  S2 -- Yes --> S3[Return cached]
  S2 -- No --> S4[Pick active suppliers]
  S4 --> F((Fan out))

  F --> SA[Supplier A]
  F --> SB[Supplier B]
  F --> SC[Supplier C]

  SA --> A1{Open?}
  SB --> B1{Open?}
  SC --> C1{Open?}

  A1 -- Yes --> Aopen[circuit_open]
  A1 -- No --> Aok[Simulate, maybe error, offers]
  B1 -- Yes --> Bopen[circuit_open]
  B1 -- No --> Bok[Simulate, maybe error, offers]
  C1 -- Yes --> Copen[circuit_open]
  C1 -- No --> Cok[Simulate, maybe error, offers]

  Aok --> AGG[Aggregate]
  Bok --> AGG
  Cok --> AGG
  Aopen --> AGG
  Bopen --> AGG
  Copen --> AGG

  AGG --> S5[Score, sort, top_k]
  S5 --> S6[Cache 30s]
  S6 --> S7[Return SearchOut]
