# LangGraph `recursion_limit` does not stop the bill

LangGraph's `recursion_limit` bounds how many times a graph can step. It
says nothing about dollars. A node that calls a paid model on every step
can retry 5 times, fail differently each time, and still bill in full —
the step counter has no idea what any individual step cost, and by the
time it fires, every one of those calls has already gone out and already
been billed.

This repo is a 2-minute, reproducible demo of the actual fix: a real
LangGraph retry loop tries to call a paid model 6 times, and
[Valta Cap](https://valta.co/docs/concepts/cap) denies the call **before**
the provider is ever reached, once a $3 per-run cap is hit — not a
warning after the fact, a hard stop before the next paid call.

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

This uses a recorded fixture that mirrors the real Cap ledger's exact
math and response shape — no network calls, no `VALTA_API_KEY`, no
`OPENAI_API_KEY` needed.

## Set up the live path

1. Sign up at [valta.co](https://valta.co) and create an agent (or reuse
   an existing one).
2. Open that agent's dashboard page (`/dashboard/agents/<agentId>`),
   scroll to the **Cap** section, and:
   - Toggle **Enable Cap** on.
   - Set **per-run limit** to `3.00`. Leave daily/monthly blank for this demo.
   - Click **Save limits**.

   Cap is enabled from the dashboard, not the SDK/API in this demo — the
   docs are explicit that this is the same thing `POST /api/v1/cap/settings`
   does, so use whichever is easier for you; the dashboard is what these
   instructions describe.
3. Create a Valta API key from **API Keys** in the dashboard.
4. Fill in `.env`:
   ```
   VALTA_API_KEY=vlt_live_...
   VALTA_AGENT_ID=ag_...
   OPENAI_API_KEY=sk-proj-...
   ```
5. Run it for real:
   ```bash
   python demo.py
   ```

## What the demo actually does

- One LangGraph node, allowed to loop back on itself up to 6 times
  (simulating a tool-fail retry loop that keeps re-attempting the same
  paid call).
- Every single attempt calls `valta_cap.CapClient.allow()` — the real,
  documented `POST /api/v1/cap/allow` — **before** the provider. If
  `approved` is `false`, the loop stops immediately. It never
  catch-and-retries a deny; that would defeat the entire point.
- Each hop estimates `$1.00` against a `$3.00` per-run cap, so the math
  plays out over exactly 4 attempts: 3 approved, the 4th denied, well
  before the loop's own 6-attempt ceiling.
- On approval, the demo calls the model (or a stub, in `--dry-run`), then
  calls `report()`.

  **Demo ledger uses the same $1.00 estimate as the "actual" cost, on
  purpose** — this keeps hop 4's deny deterministic and reproducible for
  the expected output below. A real integration should compute
  `actual_usd` from the provider's own usage response
  (`completion.usage`) and a real per-token price for the model, then
  pass that real number to `report()` so the ledger genuinely trues up.
  This demo isn't doing that math, and isn't pretending to.

## Expected output

```
DRY RUN -- agent=ag_dryrun_demo run_id=run_aaf4036559da per_run_limit=$3.00

hop | estimated_usd | approved | reason        | allow_id              | provider_called
----------------------------------------------------------------------------------------
1   | 1.0           | True     | -             | allow_dryrun_89973a43 | True
2   | 1.0           | True     | -             | allow_dryrun_14086e22 | True
3   | 1.0           | True     | -             | allow_dryrun_92c5e17b | True
4   | 1.0           | False    | per_run_limit | allow_dryrun_250e6598 | False

Stopped at hop 4: per_run_limit (allow_id=allow_dryrun_250e6598)
No provider call was made for the denied hop or any hop after it.
```

(This is a real, unedited paste from `python demo.py --dry-run` — allow
ids are randomly generated per run, so yours will differ.)

## A note on the Python SDK

There is no official Valta Python SDK usage here, on purpose.
`valta_cap.py` in this repo calls
[the documented REST endpoints](https://valta.co/docs/concepts/cap#api-reference)
directly with `requests`. The published `valta-python-sdk` package on PyPI
does not expose a `cap` resource at all as of this writing — Valta's own
docs say Cap support in the Python SDK lives on a separate, unmerged
branch. Rather than pretend an SDK method exists that doesn't, this demo
is honest about hitting the REST API instead.

## This is not LangSmith

Visibility after the call is not a deny. A dashboard that shows you spent
$40 on a retry loop an hour ago doesn't stop the 41st dollar from going
out. Cap's `allow()` call happens before the provider request, in your
own code path — the decision exists before the money moves, not after.

## What this demo is not

No web UI, no Docker, no CrewAI/AutoGen, no wallet deposits, no x402, no
marketplace. One graph, one Cap gate, one paid-model call, denied on
schedule.

## License

MIT — see [LICENSE](LICENSE).
