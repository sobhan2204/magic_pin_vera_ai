#!/usr/bin/env bash
# Curl checks for every endpoint.   BOT_URL=https://<project>.vercel.app bash scripts/smoke.sh
# Note: it pushes a few contexts and sends one tick/reply, so run `POST /v1/teardown` afterwards on a real deployment.
set -u
BOT_URL="${BOT_URL:-http://localhost:8080}"
pass=0; fail=0
hdr='Content-Type: application/json'
[ "${SMOKE_TEARDOWN:-0}" = "1" ] && curl -s -m 30 -X POST "$BOT_URL/v1/teardown" >/dev/null && echo "(teardown done)"

check() {  # check "<name>" "<expected http code>" <curl args...>
  local name="$1" want="$2"; shift 2
  local out code body
  out=$(curl -s -m 30 -w $'\n%{http_code}' "$@")
  code="${out##*$'\n'}"; body="${out%$'\n'*}"
  if [ "$code" = "$want" ]; then pass=$((pass+1)); echo "[PASS] $name ($code)"; else fail=$((fail+1)); echo "[FAIL] $name: wanted $want got $code :: $body"; fi
  LAST="$body"
}

check "healthz"  200 "$BOT_URL/v1/healthz";  echo "       $LAST"
check "metadata" 200 "$BOT_URL/v1/metadata"; echo "       $LAST"

check "context: category (new)" 200 -X POST -H "$hdr" "$BOT_URL/v1/context" -d '{"scope":"category","context_id":"dentists","version":1,"delivered_at":"2026-04-26T09:45:00Z","payload":{"slug":"dentists","voice":{"tone":"peer_clinical","vocab_taboo":["guaranteed"]},"peer_stats":{"avg_ctr":0.03},"digest":[{"id":"d_2026W17_jida_fluoride","kind":"research","title":"3-month fluoride recall cuts caries 38% better","source":"JIDA Oct 2026, p.14","trial_n":2100}]}}'
check "context: merchant (new)" 200 -X POST -H "$hdr" "$BOT_URL/v1/context" -d '{"scope":"merchant","context_id":"m_001_drmeera_dentist_delhi","version":1,"delivered_at":"2026-04-26T09:45:30Z","payload":{"merchant_id":"m_001_drmeera_dentist_delhi","category_slug":"dentists","identity":{"name":"Dr. Meera'"'"'s Dental Clinic","owner_first_name":"Meera","locality":"Lajpat Nagar","languages":["en","hi"]},"performance":{"views":2410,"calls":18,"ctr":0.021},"offers":[{"id":"o1","title":"Dental Cleaning @ ₹299","status":"active"}]}}'
check "context: same version again (idempotent)" 200 -X POST -H "$hdr" "$BOT_URL/v1/context" -d '{"scope":"merchant","context_id":"m_001_drmeera_dentist_delhi","version":1,"payload":{"merchant_id":"m_001_drmeera_dentist_delhi","category_slug":"dentists"}}'
check "context: stale version -> 409" 409 -X POST -H "$hdr" "$BOT_URL/v1/context" -d '{"scope":"merchant","context_id":"m_001_drmeera_dentist_delhi","version":0,"payload":{}}'
check "context: bad scope -> 400" 400 -X POST -H "$hdr" "$BOT_URL/v1/context" -d '{"scope":"nope","context_id":"x","version":1,"payload":{}}'
check "context: malformed -> 400" 400 -X POST -H "$hdr" "$BOT_URL/v1/context" -d '{"scope":"merchant","version":1}'
check "context: trigger" 200 -X POST -H "$hdr" "$BOT_URL/v1/context" -d '{"scope":"trigger","context_id":"trg_smoke_1","version":1,"payload":{"id":"trg_smoke_1","scope":"merchant","kind":"research_digest","source":"external","merchant_id":"m_001_drmeera_dentist_delhi","customer_id":null,"payload":{"category":"dentists","top_item_id":"d_2026W17_jida_fluoride"},"urgency":2,"suppression_key":"smoke:research","expires_at":"2099-01-01T00:00:00Z"}}'

check "tick: empty"  200 -X POST -H "$hdr" "$BOT_URL/v1/tick" -d '{"now":"2026-04-26T10:35:00Z","available_triggers":[]}'; echo "       $LAST"
check "tick: one trigger" 200 -X POST -H "$hdr" "$BOT_URL/v1/tick" -d '{"now":"2026-04-26T10:35:00Z","available_triggers":["trg_smoke_1"]}'; echo "       $LAST"
check "reply: unknown conversation" 200 -X POST -H "$hdr" "$BOT_URL/v1/reply" -d '{"conversation_id":"conv_smoke_x","merchant_id":"m_001_drmeera_dentist_delhi","customer_id":null,"from_role":"merchant","message":"Ok lets do it. Whats next?","received_at":"2026-04-26T10:42:00Z","turn_number":2}'; echo "       $LAST"
check "reply: auto-reply" 200 -X POST -H "$hdr" "$BOT_URL/v1/reply" -d '{"conversation_id":"conv_smoke_auto","merchant_id":"m_001_drmeera_dentist_delhi","customer_id":null,"from_role":"merchant","message":"Thank you for contacting us! Our team will respond shortly.","received_at":"2026-04-26T10:42:00Z","turn_number":2}'; echo "       $LAST"

echo; echo "passed=$pass failed=$fail"
echo "reminder: POST $BOT_URL/v1/teardown to clear this smoke data on a real deployment"
[ "$fail" -eq 0 ]
