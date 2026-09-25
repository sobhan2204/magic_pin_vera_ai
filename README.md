# Vera merchant bot (magicpin AI Challenge)

Status: **Phase 1 of 6** (skeleton that is always valid and deployable). Full README lands in Phase 5.

**Approach.** Deterministic code decides; the LLM only writes. Pipeline per message:
`normalize -> resolve (closed fact sheet) -> policy gate -> decide -> write -> verify -> repair -> template fallback`.
The template fallback is built only from fact-sheet facts and passes the same verifier, so the bot always has a valid, grounded answer.

**Run locally (no keys needed):**

```powershell
pip install -r requirements-dev.txt
$env:STORE_BACKEND="memory"; $env:LLM_MODE="mock"
uvicorn app.main:app --port 8080
pytest -q
```

**Deploy:** Vercel Python runtime, entrypoint `app.main:app` (see `pyproject.toml`, `vercel.json`), state in Upstash Redis.
See `.env.example` for configuration.
