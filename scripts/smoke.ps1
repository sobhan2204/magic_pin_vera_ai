# PowerShell equivalent of smoke.sh.   $env:BOT_URL="https://<project>.vercel.app"; .\scripts\smoke.ps1
$ErrorActionPreference = "Continue"
$base = if ($env:BOT_URL) { $env:BOT_URL.TrimEnd("/") } else { "http://localhost:8080" }
$script:pass = 0; $script:fail = 0

function Check($name, $want, $method, $path, $body) {
    try {
        $args = @{ Uri = "$base$path"; Method = $method; UseBasicParsing = $true; TimeoutSec = 30 }
        if ($body) { $args.Body = [System.Text.Encoding]::UTF8.GetBytes($body); $args.ContentType = "application/json; charset=utf-8" }
        $r = Invoke-WebRequest @args
        $code = [int]$r.StatusCode; $text = $r.Content
    } catch {
        $code = if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode } else { 0 }
        $text = $_.ErrorDetails.Message
    }
    if ($code -eq $want) { $script:pass++; Write-Host "[PASS] $name ($code)" -ForegroundColor Green }
    else { $script:fail++; Write-Host "[FAIL] $name : wanted $want got $code :: $text" -ForegroundColor Red }
    if ($text -and $code -eq 200) { Write-Host "       $text" }
}

Check "healthz" 200 GET "/v1/healthz" $null
Check "metadata" 200 GET "/v1/metadata" $null
Check "context: category" 200 POST "/v1/context" '{"scope":"category","context_id":"dentists","version":1,"payload":{"slug":"dentists","voice":{"tone":"peer_clinical","vocab_taboo":["guaranteed"]},"peer_stats":{"avg_ctr":0.03},"digest":[{"id":"d_2026W17_jida_fluoride","kind":"research","title":"3-month fluoride recall cuts caries 38% better","source":"JIDA Oct 2026, p.14","trial_n":2100}]}}'
Check "context: merchant" 200 POST "/v1/context" '{"scope":"merchant","context_id":"m_001_drmeera_dentist_delhi","version":1,"payload":{"merchant_id":"m_001_drmeera_dentist_delhi","category_slug":"dentists","identity":{"name":"Dr. Meera''s Dental Clinic","owner_first_name":"Meera","languages":["en","hi"]},"performance":{"views":2410,"calls":18,"ctr":0.021}}}'
Check "context: stale -> 409" 409 POST "/v1/context" '{"scope":"merchant","context_id":"m_001_drmeera_dentist_delhi","version":0,"payload":{}}'
Check "context: bad scope -> 400" 400 POST "/v1/context" '{"scope":"nope","context_id":"x","version":1,"payload":{}}'
Check "context: trigger" 200 POST "/v1/context" '{"scope":"trigger","context_id":"trg_smoke_1","version":1,"payload":{"kind":"research_digest","merchant_id":"m_001_drmeera_dentist_delhi","payload":{"top_item_id":"d_2026W17_jida_fluoride"},"urgency":2,"suppression_key":"smoke:research","expires_at":"2099-01-01T00:00:00Z"}}'
Check "tick: empty" 200 POST "/v1/tick" '{"now":"2026-04-26T10:35:00Z","available_triggers":[]}'
Check "tick: one trigger" 200 POST "/v1/tick" '{"now":"2026-04-26T10:35:00Z","available_triggers":["trg_smoke_1"]}'
Check "reply: intent" 200 POST "/v1/reply" '{"conversation_id":"conv_smoke_x","merchant_id":"m_001_drmeera_dentist_delhi","from_role":"merchant","message":"Ok lets do it. Whats next?","turn_number":2}'
Check "reply: auto-reply" 200 POST "/v1/reply" '{"conversation_id":"conv_smoke_auto","merchant_id":"m_001_drmeera_dentist_delhi","from_role":"merchant","message":"Thank you for contacting us! Our team will respond shortly.","turn_number":2}'

Write-Host "`npassed=$script:pass failed=$script:fail"
Write-Host "reminder: Invoke-RestMethod -Method Post $base/v1/teardown   (clears the smoke data)"
if ($script:fail -gt 0) { exit 1 }
