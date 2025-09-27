# Travel Policy RAG App

RAG over small **travel policy/FAQ** snippets (EU261, refunds, baggage, seats, privacy guardrails).

## Run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# optional LLM env:
# export OPENAI_API_KEY=...
# export OPENAI_BASE_URL=https://<your-azure>.openai.azure.com/
# export OPENAI_MODEL=gpt-4o-mini
python app.py --rebuild
curl -s -X POST http://localhost:8000/answer -H 'Content-Type: application/json'   -d '{"query":"What are my refund rights if my EU train is delayed by 2 hours?"}' | jq
```
