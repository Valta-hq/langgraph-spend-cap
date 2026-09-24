# LangGraph `recursion_limit` does not stop the bill

LangGraph's `recursion_limit` bounds how many times a graph can step. It
says nothing about dollars. A node that calls a paid model on every step
can retry 5 times, ask for more tokens each time, and still bill in full —
the step counter has no idea what any individual step cost.

This repo is a 2-minute, reproducible demo of the fix: the agent **never
holds your OpenAI key**. It talks to Valta's OpenAI-compatible proxy with a
Valta virtual key. Valta checks the agent's
[Cap](https://valta.co/docs/concepts/cap) before forwarding each call, and
once the next call would break the per-run cap, Valta refuses it and
**OpenAI is never called**. There's no key in the agent it could use to go
around Valta.

## Install

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

## Try it with zero keys

```bash
python demo.py --dry-run
```

No network calls, no keys. An offline stand-in makes the same per-run
decision the proxy makes, with the same estimate math.

## Set up the live path

1. Sign up at [valta.co](https://valta.co) and open an agent's page
   (`/dashboard/agents/<agentId>`).
2. In the **Cap** section:
   - Click **Enable Cap**.
   - Set **Per run (USD)** to `0.06`. Leave daily/monthly blank.
   - Click **Save limits**.
3. In **Enforced proxy (OpenAI)** on the same card:
   - **Save key** — paste your OpenAI key. Valta stores it encrypted; only
     the last 4 characters are ever shown again.
   - **Create virtual key** — copy the `vk_live_...` secret. It's shown once.
4. Fill in `.env` — note there is no OpenAI key here:
   ```
   OPENAI_BASE_URL=https://valta.co/v1
   OPENAI_API_KEY=vk_live_...
   ```
   Use `https://valta.co/v1` exactly (no `www`).
5. Check the proxy is up:
   ```bash
   curl https://valta.co/v1/health
   # {"status":"ok","failClosed":true,...}
   ```
   `"degraded"` (HTTP 503) means Valta can't decide right now, so it denies
   every call rather than forwarding any.
6. Run it:
   ```bash
   python demo.py
   ```

The live path works on Valta's Free plan. Hops 1–3 are real OpenAI calls,
so the OpenAI key you stored needs credit — a few tenths of a cent covers a
run. With an unfunded key, Valta approves hop 1 and OpenAI answers
`insufficient_quota`; the demo stops and says so.

### See a deny with no OpenAI credit at all

A denied call never reaches OpenAI, so it doesn't need credit. After step 3,
send one call that's over the $0.06 per-run cap by itself:

```bash
curl https://valta.co/v1/chat/completions \
  -H "Authorization: Bearer $OPENAI_API_KEY" -H "Content-Type: application/json" \
  -d '{"model":"gpt-4.1","messages":[{"role":"user","content":"hi"}],"max_tokens":8000}'
```

Windows `cmd.exe` (one line, escaped quotes):

```
curl https://valta.co/v1/chat/completions -H "Authorization: Bearer vk_live_..." -H "Content-Type: application/json" -d "{\"model\":\"gpt-4.1\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"max_tokens\":8000}"
```

Response (HTTP 402):

```json
{"approved":false,"reason":"per_run_limit","id":"allow_…","message":"Valta per-run limit reached for this run. No request was sent to OpenAI.","error":{"message":"…","type":"valta_denied","code":"per_run_limit"}}
```

The agent page in Valta shows it under **Last deny**.

## What the demo does

- One LangGraph node that loops up to 6 times. Each retry doubles
  `max_tokens` (1000 → 2000 → 4000 → 8000), a common "the answer got cut
  off, try again with more room" retry pattern.
- Every attempt is a normal `openai` SDK call — the only change from a
  plain OpenAI app is the base URL, the key, and an `X-Valta-Run-Id`
  header that groups the attempts into one run for the per-run limit.
- Before forwarding, Valta estimates the call's cost from the prompt and
  `max_tokens` (gpt-4.1 list price) and checks it against the run's
  remaining budget. After an approved call it records the **real** cost
  from OpenAI's usage numbers, so the run's ledger is true to what was
  spent.
- A deny is final. The graph stops; it never catch-and-retries a deny.

Why hop 4 is always the one denied: the estimates are $0.008, $0.016,
$0.032, $0.064. Hops 1–3 pass even if every reply used its full
`max_tokens` budget ($0.056 at hop 3's check, under $0.06). Hop 4's
estimate alone is $0.064, over the $0.06 cap, so it's refused no matter
what came before.

## Expected output

```
DRY RUN -- run_id=run_5350565317b5 per_run_limit=$0.06

hop | max_tokens | estimated_usd | actual_usd | approved | reason        | allow_id              | reached_openai
-----------------------------------------------------------------------------------------------------------------
1   | 1000       | 0.0080        | 0.00012    | True     | -             | allow_dryrun_a8e30042 | True
2   | 2000       | 0.0160        | 0.00012    | True     | -             | allow_dryrun_3f301b9d | True
3   | 4000       | 0.0320        | 0.00012    | True     | -             | allow_dryrun_c83a8c1d | True
4   | 8000       | 0.0640        | -          | False    | per_run_limit | allow_dryrun_2895acbd | False

Stopped at hop 4: per_run_limit (allow_id=allow_dryrun_2895acbd)
Valta refused that call before forwarding it -- it never reached OpenAI.
```

(Real `--dry-run` output, trailing spaces trimmed; ids are random per run. A live run shows real
`allow_...` ids and real `actual_usd` values from OpenAI's usage.)

## Check it on OpenAI's side

After a live run, open your
[OpenAI usage page](https://platform.openai.com/usage): **hops 1–3 appear
as three gpt-4.1 requests. Hop 4 does not appear at all** — Valta refused
it before it was sent, so there's nothing on OpenAI's side to bill.

On the Valta side, the agent page's **Last deny** shows hop 4's
`per_run_limit` with the same `allow_…` id the demo printed.

## What a deny looks like on the wire

The proxy answers a denied call with HTTP `402` (budget) or `403`
(frozen, Cap off, no key stored), and an OpenAI-style error body plus flat
fields you can match on:

```json
{
  "approved": false,
  "reason": "per_run_limit",
  "id": "allow_…",
  "message": "Valta per-run limit reached for this run. No request was sent to OpenAI.",
  "error": { "message": "…", "type": "valta_denied", "code": "per_run_limit" }
}
```

The OpenAI SDK raises `openai.APIStatusError`; `demo.py` reads `reason`
and `id` from `e.response.json()`. SDKs don't auto-retry 402/403.

## Limits of the proxy today

- OpenAI Chat Completions only, non-streaming (`stream: true` gets a 400).
- If you don't set `max_tokens`, Valta assumes 1,024 output tokens for the
  pre-check, then records the real cost afterwards — an unbounded reply
  can overshoot the cap on that one call, and the overage denies the next
  one. Set `max_tokens` for a strict per-call bound.
- Freezing the agent (dashboard, or `POST /api/v1/agents/<id>/freeze`)
  denies the very next call with `403 frozen`.
- Free plan: 1 active virtual key, 10 requests/min, and $50/month of tracked
  spend across the account. Past that, the proxy answers `402 plan_limit`
  with the upgrade link — it doesn't let calls through.

## This is not LangSmith

Visibility after the call is not a deny. A dashboard that shows you spent
$40 on a retry loop an hour ago doesn't stop the 41st dollar from going
out. Here the decision happens before the request leaves for OpenAI, and
the agent has no key to skip it with.

## What this demo is not

No web UI, no Docker, no CrewAI/AutoGen, no wallet deposits, no x402, no
marketplace. One graph, one proxy, one paid model, denied on schedule.

## License

MIT — see [LICENSE](LICENSE).
