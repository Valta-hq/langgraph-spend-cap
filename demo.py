#!/usr/bin/env python3
"""
A LangGraph retry loop calls a paid model up to 6 times, doubling
max_tokens on every retry (a common "the answer got cut off, try again with
more room" pattern). The agent never holds an OpenAI key: it talks to
Valta's OpenAI-compatible proxy with a Valta virtual key. Valta checks the
agent's Cap before forwarding each call -- and once the next call would
break the $0.06 per-run cap, Valta refuses it and OpenAI is never called.

Usage:
    python demo.py              # live: real Valta proxy -> real OpenAI
    python demo.py --dry-run    # no keys, no network: same math, offline
"""
from __future__ import annotations

import argparse
import math
import os
import sys
import uuid
from typing import Any, Callable, TypedDict

from dotenv import load_dotenv
from langgraph.graph import StateGraph, END

MAX_HOPS = 6
BASE_MAX_TOKENS = 1000
PER_RUN_LIMIT_USD = 0.06
PROMPT = "Say hello in five words or fewer."

# gpt-4.1 list price per 1M tokens, as used by the proxy's own pre-check.
PRICE_IN, PRICE_OUT = 2.00, 8.00


class GraphState(TypedDict):
    hop: int
    run_id: str
    rows: list[dict[str, Any]]
    stopped: bool


def max_tokens_for(hop: int) -> int:
    return BASE_MAX_TOKENS * 2 ** (hop - 1)


def dry_run_estimate(max_tokens: int) -> float:
    """Same pre-call estimate the proxy makes: ~4 chars/token input +
    framing overhead, plus max_tokens as the output bound."""
    input_tokens = math.ceil(len(PROMPT) / 4) + 4 + 3
    return (input_tokens * PRICE_IN + max_tokens * PRICE_OUT) / 1_000_000


def make_dry_run_call() -> Callable[[str, int], dict[str, Any]]:
    """Offline stand-in for the proxy -- mirrors its decision (per-run
    ledger, estimate checked BEFORE forwarding) and its response fields."""
    spent: dict[str, float] = {}

    def call(run_id: str, max_tokens: int) -> dict[str, Any]:
        est = dry_run_estimate(max_tokens)
        already = spent.get(run_id, 0.0)
        allow_id = f"allow_dryrun_{uuid.uuid4().hex[:8]}"
        if est > PER_RUN_LIMIT_USD or already + est > PER_RUN_LIMIT_USD:
            return {"approved": False, "reason": "per_run_limit", "id": allow_id, "estimated_usd": est}
        actual = 0.00012  # a five-word reply is ~20 tokens, nowhere near max_tokens
        spent[run_id] = already + actual
        return {"approved": True, "reason": "-", "id": allow_id, "estimated_usd": est, "actual_usd": actual}

    return call


def make_live_call(model: str) -> Callable[[str, int], dict[str, Any]]:
    """Real OpenAI client, pointed at Valta. OPENAI_BASE_URL and
    OPENAI_API_KEY (a Valta virtual key) come from the environment -- the
    agent has no OpenAI key to leak or to bypass Valta with."""
    import openai

    client = openai.OpenAI(max_retries=0)  # the graph is the retry loop; no SDK retries on top

    def call(run_id: str, max_tokens: int) -> dict[str, Any]:
        try:
            raw = client.chat.completions.with_raw_response.create(
                model=model,
                messages=[{"role": "user", "content": PROMPT}],
                max_tokens=max_tokens,
                extra_headers={"X-Valta-Run-Id": run_id},
            )
        except openai.APIStatusError as e:
            try:
                body = e.response.json()
            except Exception:
                body = {}
            if body.get("approved") is False:
                return {
                    "approved": False,
                    "reason": body.get("reason", "denied"),
                    "id": body.get("id", "-"),
                    "estimated_usd": None,
                }
            raise
        h = raw.headers
        return {
            "approved": True,
            "reason": "-",
            "id": h.get("x-valta-allow-id", "-"),
            "estimated_usd": float(h.get("x-valta-estimated-usd", "nan")),
            "actual_usd": float(h["x-valta-actual-usd"]) if "x-valta-actual-usd" in h else None,
        }

    return call


def build_graph(call: Callable[[str, int], dict[str, Any]]) -> Any:
    def attempt(state: GraphState) -> GraphState:
        hop = state["hop"] + 1
        max_tokens = max_tokens_for(hop)
        result = call(state["run_id"], max_tokens)

        est = result.get("estimated_usd")
        actual = result.get("actual_usd")
        state["rows"].append(
            {
                "hop": hop,
                "max_tokens": max_tokens,
                "estimated_usd": f"{est:.4f}" if isinstance(est, float) else "-",
                "actual_usd": f"{actual:.5f}" if isinstance(actual, float) else "-",
                "approved": result["approved"],
                "reason": result["reason"],
                "allow_id": result["id"],
                "reached_openai": result["approved"],
            }
        )
        state["hop"] = hop
        # A deny is final: never catch-and-retry it, that's the whole point.
        state["stopped"] = not result["approved"]
        return state

    def next_step(state: GraphState) -> str:
        return END if state["stopped"] or state["hop"] >= MAX_HOPS else "attempt"

    graph = StateGraph(GraphState)
    graph.add_node("attempt", attempt)
    graph.set_entry_point("attempt")
    graph.add_conditional_edges("attempt", next_step, {"attempt": "attempt", END: END})
    return graph.compile()


def print_table(rows: list[dict[str, Any]]) -> None:
    headers = ["hop", "max_tokens", "estimated_usd", "actual_usd", "approved", "reason", "allow_id", "reached_openai"]
    widths = {h: max(len(h), *(len(str(r[h])) for r in rows)) for h in headers}
    line = " | ".join(h.ljust(widths[h]) for h in headers)
    print(line)
    print("-" * len(line))
    for r in rows:
        print(" | ".join(str(r[h]).ljust(widths[h]) for h in headers))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="No keys, no network calls.")
    parser.add_argument("--model", default="gpt-4.1", help="The math in the README assumes gpt-4.1.")
    args = parser.parse_args()

    load_dotenv()

    if args.dry_run:
        call = make_dry_run_call()
    else:
        base_url = os.environ.get("OPENAI_BASE_URL", "")
        key = os.environ.get("OPENAI_API_KEY", "")
        if not base_url or not key:
            print("Set OPENAI_BASE_URL (Valta proxy) and OPENAI_API_KEY (Valta virtual key), or use --dry-run.", file=sys.stderr)
            return 1
        if not key.startswith("vk_live_"):
            print("OPENAI_API_KEY should be a Valta virtual key (vk_live_...), not a raw OpenAI key.", file=sys.stderr)
            return 1
        call = make_live_call(args.model)

    run_id = f"run_{uuid.uuid4().hex[:12]}"
    mode = "DRY RUN" if args.dry_run else f"LIVE via {os.environ.get('OPENAI_BASE_URL')}"
    print(f"{mode} -- run_id={run_id} per_run_limit=${PER_RUN_LIMIT_USD:.2f}\n")

    final = build_graph(call).invoke({"hop": 0, "run_id": run_id, "rows": [], "stopped": False})
    print_table(final["rows"])

    denied = [r for r in final["rows"] if not r["approved"]]
    if denied:
        d = denied[0]
        print(f"\nStopped at hop {d['hop']}: {d['reason']} (allow_id={d['allow_id']})")
        print("Valta refused that call before forwarding it -- it never reached OpenAI.")
    else:
        print(f"\nCompleted all {len(final['rows'])} hops without hitting the per-run cap.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
