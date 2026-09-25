# Vera merchant bot: magicpin AI Challenge

HTTP bot for magicpin's "Vera" (WhatsApp assistant for dentists, salons, restaurants, gyms, pharmacies). Vercel (Python/FastAPI) +
Upstash Redis + Groq. Run/test/deploy guide: [`RUNNING_AND_TESTING.md`](RUNNING_AND_TESTING.md).

## Approach: deterministic code decides, the LLM only writes

```
normalize -> resolve (closed fact sheet) -> policy gate -> decide -> write (LLM) -> verify -> repair (1x) -> template fallback
```

* **Decide in code.** Whether to send, to whom, and why: expiry, consent scopes, opt-outs, restraint (no second nudge while a
  conversation is open, none after two unanswered sends unless urgent), one action per merchant per tick, cap 20, scoring by
  urgency + expiry proximity + freshness. Three dedup layers: event reservation (atomic `SET NX`), action identity (next-best hook
  fact if already sent), wording similarity.
* **Resolve facts in code.** Every number the message may use (months since a visit, days to a deadline, CTR as a %, peer gaps) is
  computed from the latest pushed contexts and handed to the writer as a numbered, plain-English fact list. Adaptive injections
  (new digest items, new performance numbers, new customers, unseen trigger kinds) are picked up because facts are re-resolved
  from the newest context versions at decision time.
* **Write with an LLM inside a closed world.** `openai/gpt-oss-120b` (primary) then `openai/gpt-oss-20b`, temperature 0, fixed seed,
  low reasoning effort, Groq strict JSON-schema output. The prompt lists only the facts and one original style example.
* **Verify everything.** Numbers, names and entities must trace to a fact; taboo words, jargon/field names, URLs, multiple asks,
  wrong salutation ("Dr. Meera"), missing Hindi-English code-mix and near-duplicates are rejected; the rationale is checked against
  the message. One repair call, then a **deterministic template composer** that passes the same verifier for every trigger kind in the
  dataset. The bot therefore always has a valid, grounded answer (no key, no quota, 429s, timeouts, slow model).
* **Replies are rules-first** with merchant-level (not conversation-level) memory: opt-out, hostile, canned auto-reply (detected
  across different conversation ids: one owner-directed line, then exit), commitment -> action mode (lint forbids qualifying
  questions), later/busy, off-topic (polite redirect), and an LLM-answered question path that is verified like an outbound message.

## Model choice and why

`gpt-oss-120b` for quality and reliable structured JSON, `gpt-oss-20b` as the fast/cheap second model on the same key, an optional
independent OpenAI-compatible provider wired but off by default, and the template fallback for a guarantee of validity. Groq's free
tier is small (8k tokens/min per model), so quota is reserved atomically **before** each call, in score order: the best decisions
get the LLM, the rest get verified templates, and we never trigger a 429 on purpose.

## Trade-offs

* **Serverless + Redis:** nothing lives in process memory; all state, atomic version checks and quota counters are in Upstash. Cost:
  more round-trips (a full judge run is ~6-9k commands).
* **Closed-world grounding over creativity:** the writer may not add colour that is not in the data (no invented studies, prices,
  competitors). This costs some flair and protects against the fabrication penalty.
* **Restraint over spam:** we skip sends that would be a second nudge to an unanswered merchant, or that are near-duplicates.
* **Optional batch composition** (`LLM_BATCH_SIZE`, default off): several decisions per LLM call when the token limit is the
  bottleneck; failed items fall back to the verified template.
* **Determinism:** temperature 0 + seed + a per-(trigger, context versions, day, prompt version) draft cache.

## What extra context would help most

Real slot availability and price catalogues per merchant (so customer messages can quote more than the offers list), per-merchant
conversation outcomes (to learn which levers work), a merchant/customer timezone and quiet hours, and the judge's simulated clock in
`/v1/tick` for every call (the simulator uses the real clock, which makes seed triggers look expired).
