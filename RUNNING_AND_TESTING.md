# Running, testing and deploying the Vera bot

Everything here is free-tier. Commands are given for **Windows PowerShell** first, then macOS/Linux where they differ.
Run everything from the project root (the folder that contains `app/`).

---

## 1. Prerequisites

| Need | Why | Notes |
|---|---|---|
| Python 3.12 or 3.13 | runtime (Vercel uses 3.12; `.python-version` pins it) | `python --version` |
| Accounts (all free) | **Groq** (LLM), **Upstash** (Redis), **Vercel** (hosting), **UptimeRobot** (keep-alive) | |
| Node.js + Vercel CLI | deploy | `npm i -g vercel` |

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1          # macOS/Linux: source .venv/bin/activate
pip install -r requirements-dev.txt
```

## 2. The dataset

`dataset/` holds the seeds (5 categories, 10 merchants, 15 customers, 25 triggers). The generator expands them to the full
50 / 200 / 100 set:

```powershell
python dataset/generate_dataset.py --seed-dir dataset --out expanded
```

It writes `expanded/{categories,merchants,customers,triggers}/*.json` and `expanded/test_pairs.json` (30 canonical pairs).
Our tests and `scripts/full_run.py` do **not** need this step (they expand in memory with identical logic and explicit UTF-8), but
if `expanded/` exists the scripts use it.

> Windows note: `generate_dataset.py` reads the seeds with the default codec, so on Windows the rupee sign in `expanded/` files
> can come out garbled. Run it with `python -X utf8 dataset/generate_dataset.py ...` (or `$env:PYTHONUTF8=1`).

## 3. Run locally

**a) In-memory store + mock LLM. No keys needed.**

```powershell
$env:STORE_BACKEND="memory"; $env:LLM_MODE="mock"
uvicorn app.main:app --port 8080
```
macOS/Linux: `STORE_BACKEND=memory LLM_MODE=mock uvicorn app.main:app --port 8080`

**b) In-memory store + live Groq** (real LLM writing; state is lost on restart):

```powershell
$env:STORE_BACKEND="memory"; $env:LLM_MODE="live"; $env:GROQ_API_KEY="gsk_..."
uvicorn app.main:app --port 8080
```

**c) Upstash + live Groq (same as production):**

```powershell
$env:STORE_BACKEND="redis"; $env:LLM_MODE="live"
$env:UPSTASH_REDIS_REST_URL="https://<id>.upstash.io"; $env:UPSTASH_REDIS_REST_TOKEN="..."
$env:GROQ_API_KEY="gsk_..."; $env:REDIS_KEY_PREFIX="vera_dev:"     # separate namespace from production
uvicorn app.main:app --port 8080
```

Stop a background server on Windows: `Get-CimInstance Win32_Process | ? { $_.CommandLine -match "uvicorn" } | % { Stop-Process -Id $_.ProcessId -Force }`.

## 4. The test suite

```powershell
pytest -q                 # ~295 tests, mock LLM, in-memory store, ~15 s, zero API calls
pytest -m live -s         # opt-in: a handful of REAL Groq calls (needs GROQ_API_KEY, ~10k tokens)
$env:UPSTASH_REDIS_REST_URL="..."; $env:UPSTASH_REDIS_REST_TOKEN="..."; pytest tests/test_store.py -q   # also runs the Lua scripts on a real DB (prefix vera_test:)
```
Expected: `289 passed, 6 skipped` (the 5 skips are the Redis-only store tests when no Upstash URL is set).

| File | Covers |
|---|---|
| `test_store.py` | atomic version CAS (new / same / stale / replaced), counters, `SET NX` exclusivity, capped lists, wipe. Runs on memory and (optionally) real Redis |
| `test_normalize.py` | both trigger shapes, unknown kinds, urgency clamp, default suppression key |
| `test_resolver.py` | fact sheet, derived numbers, humanized text, salutation/language, latest-version joins |
| `test_policy_decision.py` | expiry, consent scopes, opt-out, restraint rules, scoring, one-per-merchant, cap 20, tie determinism |
| `test_dedup.py` | the three dedup layers, next-best hook, rationale check |
| `test_verifier.py` | each verifier rule (numbers, entities, taboo, jargon, one ask, salutation, URL, language, duplicates) |
| `test_fallback.py` | template composer passes the verifier for **every** trigger in the expanded dataset |
| `test_reply_classifier.py` | ~50 table-driven intent cases incl. every string from `judge_simulator.py` and Hinglish |
| `test_reply_flows.py`, `test_reply_engine.py` | replay scenarios, action-mode lint, merchant-level state, customer flows, turn cap, language switch |
| `test_llm_router.py` | quota reservation, failover, cooling, structured-output request shape, deadlines, broken key |
| `test_prompts_and_llm_flows.py` | prompt size/shape, originality of few-shots, draft-cache determinism, LLM reply path |
| `test_batch.py` | optional batch composition: one call for several decisions, per-item verification and fallback |
| `test_tick.py`, `test_api_contract.py`, `test_determinism.py` | end-to-end HTTP contract, adaptive injection, restraint, teardown, determinism |

## 5. Key test cases and the rule each protects

| Test | Proves | Rule |
|---|---|---|
| `test_context_versioning_200_noop_409` | new -> 200, same version -> 200 no-op (nothing changes), lower -> 409 with `current_version` | testing brief §2.1 |
| `test_context_400s`, `test_payload_too_large…` | bad scope / malformed / oversize give the documented 400 shapes | §2.1 |
| `test_top_level_shape`, `test_ids_inside_payload_shape` | triggers normalize from either payload shape | §3.4 vs challenge brief §6 |
| `test_category_version_bump_new_digest_item_is_used` | digest is resolved from the **latest** category version at tick time | adaptive injection |
| `test_expired_trigger_is_not_sent` | `expires_at < now` => no send | restraint |
| `test_customer_consent_is_enforced` | consent scope must cover the trigger kind | privacy |
| `test_one_action_per_merchant_per_tick`, `test_cap_20…` | ≤1 action/merchant, ≤20/tick, empty tick is fine | §2.2 |
| `test_layer1/2/3…` in `test_dedup.py` | same event, same content, near-identical wording never resent | anti-repetition (-2) |
| `test_hallucinated_number_rejected`, `test_unknown_entity_rejected`, `test_taboo_rejected`, `test_jargon…`, `test_two_questions…`, `test_wrong_salutation…` | verifier catches fabrication (-2), jargon (-1), multiple CTAs, wrong "Dr." form | rubric |
| `test_fallback_passes_verifier_for_every_expanded_trigger` | the no-LLM path is always valid and grounded | robustness |
| `test_auto_reply_hell_across_four_conversation_ids` | canned replies on different conversation ids: one owner-directed line, then `end` | replay test 1 |
| `test_intent_transition…`, `test_every_commit_template_passes_lint` | "ok let's do it" gets action mode, no qualifying phrases | replay test 2 |
| `test_hostile_then_gst_keeps_conversation_open`, `test_hostile_with_stop_ends…` | abuse then GST stays polite/on mission; explicit stop ends and suppresses | replay test 3 |
| `test_turn_cap…` | wrap-up at turn 5, end at 6 | conversation flow |
| `test_429_then_secondary_then_cooling`, `test_everything_failing_returns_none…` | 429 -> secondary -> template; failing model cooled for 60 s | LLM resilience |
| `test_slow_llm_cannot_break_the_tick_deadline` | a 30 s LLM still returns inside the tick deadline with valid messages | -1 per timeout |
| `test_deliberately_broken_key_never_errors` | invalid Groq key => grounded template messages, no errors | acceptance criteria |
| `test_same_inputs_give_identical_outputs`, `test_draft_cache…` | determinism (cache + temperature 0) | requirement 7 |
| `test_teardown_wipes_everything` | teardown clears every key and counters | §11 |

## 6. Manual endpoint tests

Scripted: `scripts/smoke.sh` (bash) and `scripts/smoke.ps1` (PowerShell). They pass on a **clean** instance (they push a category
at version 1); on a deployment that already holds data, teardown first.

```powershell
$env:BOT_URL="http://localhost:8080"; .\scripts\smoke.ps1          # or: BOT_URL=... bash scripts/smoke.sh
```

By hand (PowerShell):

```powershell
$u = "http://localhost:8080"
Invoke-RestMethod "$u/v1/healthz"
Invoke-RestMethod "$u/v1/metadata"
$ctx = @{ scope="category"; context_id="dentists"; version=1; delivered_at="2026-04-26T09:45:00Z"
          payload=(Get-Content dataset/categories/dentists.json -Raw -Encoding utf8 | ConvertFrom-Json) } | ConvertTo-Json -Depth 20
Invoke-RestMethod -Method Post "$u/v1/context" -ContentType "application/json; charset=utf-8" -Body ([Text.Encoding]::UTF8.GetBytes($ctx))
Invoke-RestMethod -Method Post "$u/v1/tick" -ContentType "application/json" -Body '{"now":"2026-04-26T10:35:00Z","available_triggers":["trg_001_research_digest_dentists"]}'
Invoke-RestMethod -Method Post "$u/v1/reply" -ContentType "application/json" -Body '{"conversation_id":"conv_001","merchant_id":"m_001_drmeera_dentist_delhi","customer_id":null,"from_role":"merchant","message":"Yes please send the abstract","received_at":"2026-04-26T10:42:00Z","turn_number":2}'
Invoke-RestMethod -Method Post "$u/v1/teardown"
```

By hand (curl, macOS/Linux/Git-Bash):

```bash
curl $BOT_URL/v1/healthz
curl -X POST -H "Content-Type: application/json" -d '{"scope":"category","context_id":"dentists","version":1,"payload":'"$(cat dataset/categories/dentists.json)"'}' $BOT_URL/v1/context
curl -X POST -H "Content-Type: application/json" -d '{"now":"2026-04-26T10:35:00Z","available_triggers":["trg_001_research_digest_dentists"]}' $BOT_URL/v1/tick
```
(The judge pushes the contexts itself; a trigger only produces a message once its merchant, category and trigger have all been pushed.)

## 7. Our full-lifecycle harness: `scripts/full_run.py`

```powershell
python -X utf8 scripts/full_run.py --bot-url http://localhost:8080 --quiet          # add --teardown-first on a used instance
python -X utf8 scripts/full_run.py --bot-url https://<project>.vercel.app --teardown-first
```

It plays the judge: warmup (5 + 50 + 200 contexts, checks `healthz` says 255), the stale/re-push behaviour, then **12 ticks of 5
simulated minutes** with triggers pushed incrementally and these injections: tick 3 category **version bump with 5 new digest items
per category**, tick 4 **performance bumps** on 10 merchants (+ renewal triggers that must show the new numbers), tick 6-7 **5 new
customers followed by `recall_due` two minutes later**, tick 8 an **unseen trigger kind**. After each tick it plays merchants
(engaged, auto-reply x2 on different conversation ids, hostile then GST, commitment, off-topic, "busy", question).

Hard failures (exit 1): any non-200 or malformed JSON, response > 25 s, >1 action per merchant per tick, >20 per tick, a verifier
violation, a repeated body, a reused `conversation_id`, a wrong `send_as`, ignored language preference, adaptive data not used in the
message, or a reply that breaks the reply rules. Warnings: numbers not literally in the pushed contexts (derived numbers like
"232 days to go" are expected there).

Reference result (local, mock LLM): 74 actions, 74 unique bodies, p95 latency 0.00 s, zero hard failures.

## 8. magicpin's `judge_simulator.py`

The simulator is configured by constants at the top of the file (`BOT_URL`, `LLM_PROVIDER`, `LLM_API_KEY`, `LLM_MODEL`,
`TEST_SCENARIO`). To avoid editing it use the wrapper, which sets those from environment variables:

```powershell
$env:BOT_URL="https://<project>.vercel.app"
$env:SIM_PROVIDER="groq"; $env:SIM_API_KEY="<a key for the JUDGING model>"; $env:SIM_MODEL="<a small Groq model id, not gpt-oss>"
$env:SIM_SCENARIO="auto_reply_hell"      # warmup | phase2_short | auto_reply_hell | intent_transition | hostile | all | full_evaluation
python -X utf8 scripts/run_simulator.py
```

Reading it: each message gets five 0-10 scores (specificity, category fit, merchant fit, decision quality, engagement) minus penalties,
total /50; `FINAL SUMMARY` averages them. Replay scenarios print PASS/WARN/FAIL lines: `auto_reply_hell` should end by turn 2,
`intent_transition` must say "correctly switched to ACTION mode", `hostile` should say "correctly ENDED".

**Warnings**
* The simulator's *own* judging LLM makes calls too. If it uses the same Groq gpt-oss models it eats the bot's per-minute/per-day
  quota. Give it a different model (any small Groq model listed in your console) or a different provider key.
* `full_evaluation` scores ~100 triggers and burns the daily token budget. Run it at most once, never in a loop.
* The simulator ticks with the **real clock** (`datetime.utcnow()`), while the seed triggers expire between Apr and Dec 2026.
  Once real time passes an `expires_at` the bot correctly refuses to send, so `phase2_short` can show "0 actions". That is the
  restraint logic working, not a bug; use `scripts/full_run.py` (simulated clock) to see messages.
* The simulator's replay scenarios reuse the first merchant; a hostile "stop" run opts that merchant out for 7 days. Run
  `POST /v1/teardown` between scenario runs if you want a clean slate.

## 9. Deploy to Vercel

1. **Upstash**: console.upstash.com -> Create Database (Redis, Regional, free) -> copy the **REST URL** and **REST token** (not the `redis://` URL).
2. **Groq**: console.groq.com -> API Keys -> create one. Note the limits shown for `openai/gpt-oss-120b` / `-20b` (defaults in `.env.example` match the free plan: 30 RPM, 8,000 TPM, 1,000 RPD, 200,000 TPD).
3. **Vercel CLI** (from the project root):

```powershell
vercel login
vercel link                        # create a new project when asked; framework preset: Other/FastAPI is auto-detected
```
4. **Environment variables** (Production). One by one:

```powershell
vercel env add STORE_BACKEND production          # redis
vercel env add LLM_MODE production               # live
vercel env add UPSTASH_REDIS_REST_URL production
vercel env add UPSTASH_REDIS_REST_TOKEN production
vercel env add GROQ_API_KEY production
vercel env add TEAM_NAME production              # Sobhan
vercel env add TEAM_MEMBERS production           # Sobhan
vercel env add CONTACT_EMAIL production
vercel env add BOT_VERSION production            # 1.0.0
vercel env add SUBMITTED_AT production           # e.g. 2026-09-25T09:00:00Z
vercel env add DEBUG_TOKEN production            # any long random string
```
Everything else has a sensible default (see `.env.example`). Do not commit real values; `.env` is git-ignored.

5. **Deploy**: `vercel --prod`. The URL is printed. Verify: `Invoke-RestMethod https://<project>.vercel.app/v1/healthz` -> `{"status":"ok",...}`.
   If `/v1/*` returns 404, see Troubleshooting.
6. **Logs**: `vercel logs https://<project>.vercel.app` (or Dashboard -> project -> Logs). Every no-op decision, dropped draft and LLM failover is logged.
7. **Redeploy**: change code, `vercel --prod` again. **Do not redeploy while the judge is running** (a cold start mid-test is avoidable risk).

Layout used (verified against current Vercel docs): a FastAPI `app` in `app/main.py`, pinned with `[tool.vercel] entrypoint = "app.main:app"`
in `pyproject.toml`; `vercel.json` sets `maxDuration: 60` for that function (Hobby allows up to 300 s with fluid compute); internal
deadlines are tick 20 s and reply 15 s (`min(20|15, maxDuration - 5)`), well inside the judge's 30 s.

## 10. Keep-alive and monitoring (UptimeRobot)

uptimerobot.com -> Add New Monitor -> **HTTP(s)**, URL `https://<project>.vercel.app/v1/healthz`, interval **5 minutes**, alert contact = your
e-mail. `healthz` never calls the LLM and does one Redis command, so this costs ~288 Redis commands/day, keeps an instance warm and
e-mails you if the bot is ever down. `healthz` returns `"status":"degraded"` (still HTTP 200) if Redis is unreachable.

## 11. Pre-submission checklist

Mirrors `challenge-testing-brief.md` §12, plus ours:

- [ ] Public HTTPS URL reachable; all 5 endpoints (+ teardown) return the right schemas (`scripts/smoke.ps1`)
- [ ] `/v1/context` idempotent on `(scope, context_id, version)`; 409 on stale; 400 shapes
- [ ] `/v1/tick` returns within 30 s even with nothing to send (`{"actions": []}`); `/v1/reply` within 30 s for any conversation
- [ ] `LLM_MODE=live` in **production** (`vercel env ls` shows it) and the Groq key is valid (`pytest -m live` once)
- [ ] `DEBUG_TOKEN` set; `GET /v1/_debug?token=...` shows both models' quota state
- [ ] Metadata filled (`/v1/metadata`: team, members, model, approach, contact_email, version, submitted_at)
- [ ] `python scripts/full_run.py --bot-url <prod> --teardown-first` -> PASS
- [ ] `judge_simulator.py` scenarios `warmup`, `auto_reply_hell`, `intent_transition`, `hostile` behave as in section 8
- [ ] **`POST /v1/teardown` run once to clear test data before submitting**, then `GET /v1/healthz` shows all four counts `0`
- [ ] Groq console: remaining daily tokens for both models are healthy (the harness and simulator both spend quota)
- [ ] UptimeRobot monitor green; no secrets in the repo (`git grep -n "gsk_"` and `git grep -n "upstash.io"` are empty)

## 12. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Groq 429 in logs | Expected under load. The router skips a model whose per-minute/day budget is spent **without calling it**; a real 429 cools that model for 60 s and the secondary/template takes over. Check `/v1/_debug` |
| Every message looks templated | `LLM_MODE` is `mock`, `GROQ_API_KEY` missing/invalid (see `vercel logs`: `llm failure ... auth`), quota spent, or the model is cooling. Templates are the intended safe fallback |
| `healthz` says `degraded` | Upstash unreachable or wrong REST URL/token (the `redis://` URL will not work). Re-check the two env vars |
| Redis auth errors | Token belongs to a different database, or has trailing whitespace/newline. Re-add with `vercel env rm` + `vercel env add`, then redeploy |
| First request slow (2-4 s) | Cold start. UptimeRobot every 5 minutes keeps instances warm; the judge's warmup also primes it |
| 404 on `/v1/*` | Entrypoint not detected. Confirm `pyproject.toml` has `[tool.vercel] entrypoint = "app.main:app"` and `requirements.txt` lists `fastapi`; redeploy; check the build log for "Python" |
| 500 / 504 | Should not happen: every route catches errors. Look at `vercel logs`. A 504 means the function exceeded `maxDuration`; keep `TICK_DEADLINE_S <= 55` |
| Hindi shows as `â‚¹` / `Ã` | A client sending or reading non-UTF-8 (PowerShell `Invoke-RestMethod` with a string body, Windows Python without `-X utf8`). The bot itself is UTF-8 end to end |
| `No actions` from a tick | Read the log: `no_op trigger=... reason=expired / awaiting_reply / merchant_opted_out / already_sent / consent_scope_mismatch / missing_join` |
| Local test hangs on port 8080 | An old uvicorn is still running (see the stop command in section 3) |

## 13. Quota budgeting and command usage

**Groq (per model, free plan):** 30 RPM, 8,000 TPM, 1,000 RPD, 200,000 TPD; the router keeps 10 % headroom on each.

| Call | Tokens (typical) |
|---|---|
| Outbound compose | prompt ~0.9-1.2k + completion ~0.15-0.3k (low reasoning) = **~1.3-1.5k** |
| Repair (if the first draft fails verification) | +~1.5k |
| LLM reply to a question | ~0.9-1.1k |

So each model can serve about **5 composes per minute** and **~140 per day**; two models ≈ 10/min, ≈ 280/day. A 20-action tick
therefore uses the LLM for the highest-scoring ~8-10 decisions and the deterministic template for the rest (same verifier, so still
grounded). A full judge run (~100 first messages + ~20 % repairs + ~100 replies of which only questions use the LLM) needs roughly
**150-200k tokens**, i.e. about one day's budget for both models together. Do not repeat `full_run.py` against the live LLM, or run
`full_evaluation`, more than once per day. For iteration use `LLM_MODE=mock`.

**Batch mode (optional, off by default).** `LLM_BATCH_SIZE=2..4` composes that many decisions in one LLM call: one shared system
prompt and one style example instead of one per message, so the prompt is ~40-50 % smaller per message (asserted in
`tests/test_batch.py`) and each batch costs one request instead of N. Trade-off: no per-item repair (an item that fails
verification simply gets the deterministic template) and slightly less individual attention per message. Turn it on only when the
per-minute token limit is what is holding back LLM-written messages: set `LLM_BATCH_SIZE=3` in Vercel and redeploy.

Check remaining quota: Groq console -> Limits/Usage, or `GET /v1/_debug?token=$DEBUG_TOKEN` (`rate_limits.models.<id>.minute/day`).

**Upstash (free plan: 500,000 commands/month, ~16.6k/day):** every command inside a pipeline counts.

| Operation | Commands |
|---|---|
| `/v1/context` | 1 (Lua script) |
| `/v1/healthz` | 1 |
| `/v1/tick` | ~ (triggers + merchants + categories + customers in the request) + ~5 lookups + ~14 per action sent + ~3 per LLM call |
| `/v1/reply` | ~6-8 |

Estimated **one complete judge run ≈ 6-9k commands** (255 warmup contexts, ~130 incremental contexts, 12 ticks, ~100 actions, ~150
replies), plus ~300/day for UptimeRobot. That is ~60-80 full runs per month on the free plan; the smoke/harness runs count too.


---

## 14. Live verification notes (real Upstash + Groq + Cerebras, run on 2026-09-26)

Local convenience: `app/main.py` loads `.env` automatically for local runs (real environment variables win; it is skipped on Vercel and
under pytest). Scripts that use the real services: `scripts/probe.py` (Redis latency + one real call per LLM target),
`scripts/score_sample.py` (scores the bot's real messages with the official judge prompt, using a judge model that is not one of the
bot's), `scripts/run_simulator.py` (official simulator, with the user-agent fix below).

What the live run showed, and what changed because of it:

* **Redis latency is ~270 ms per command from a laptop.** The tick therefore batches its reads (`asyncio.gather`), reserves all LLM
  leases with one atomic Lua call (`quota_reserve_n`) and the reply handler fetches its four independent values together. **On Vercel,
  create the function in the same region as the Upstash database** (Vercel project -> Settings -> Functions -> Region; Upstash
  console shows the DB region). A cross-continent pair costs seconds per request.
* **Cerebras (`ALT_*`)** works, but without `reasoning_effort: "low"` it spends the whole token budget thinking and returns empty
  content. gpt-oss models now always get `low` on every provider. `ALT_JSON_MODE=schema` enables strict structured output for it.
  Alt-provider free-tier limits are separate: `ALT_MODEL_RPM/TPM/RPD/TPD` (default = `MODEL_*`).
* **Real 429s happen** even with local accounting (shared account limits). The router cools that model for 60 s and fails over; a full
  run had 6 such 429s and zero failed requests.
* **Generated triggers have placeholder payloads** (75 of the 100). They now lead with a real fact derived from the merchant/category
  data, or an honest kind-level statement, never "quick profile check-in". Digest kinds with no id use the category's latest item of
  that kind; an explicit unknown id is still a missing join.
* **Customer-facing messages never see merchant-internal facts** (subscription, peer benchmarks, performance). This was a real bug
  found by scoring: the LLM had written "Your Pro plan has 16 days left" to a customer.
* **LLM drafts are additionally rejected** for invented causes/advice (speculation words, and any sentence before the ask that shares
  no content word with a fact) and for units that do not match the facts ("95 customers" is not "95%").
* **`CONSENT_MODE`**: the generated dataset gives every generated customer only `["promotional_offers"]`, so `strict` (default; scope
  must cover the trigger kind) sends nothing for ~25% of generated customer triggers (recall_due, customer_lapsed_soft,
  appointment_tomorrow, chronic_refill_due, trial_followup). `lenient` sends to any customer with an active consent.
* **`judge_simulator.py` bug on Groq**: its `urllib` user agent is rejected by Groq with HTTP 403. `scripts/run_simulator.py` and
  `scripts/score_sample.py` work around it without editing the file.
* **Scores with the official judge prompt** (a non-gpt-oss judge model, noisy +-9 per message): messages built from real trigger payloads
  ~35-43/50; messages for placeholder-payload triggers ~20-30/50. That judge only sees a subset of the merchant/customer fields, so
  it also flags true facts from other fields as "invented".
