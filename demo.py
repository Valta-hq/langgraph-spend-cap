#!/usr/bin/env python3
"""
LangGraph retries a "paid model" call up to 6 times (simulating a
tool-fail retry loop). Valta Cap denies the call BEFORE the provider is
ever reached, once a $3 per-run cap is hit -- printing the deny reason
and the allow_id, not just a warning after the fact.

Usage:
    python demo.py                 # live: real Valta Cap API + real OpenAI call
    python demo.py --dry-run       # no keys needed: recorded deny fixture
"""
from __future__ import annotations

import argparse
import os
import sys
import uuid
from typing import Any, Callable, TypedDict

from dotenv import load_dotenv
from langgraph.graph import StateGraph, END

from valta_cap import CapClient, CapError

MAX_HOPS = 6
PER_RUN_LIMIT_USD = 3.00
ESTIMATED_USD_PER_HOP = 1.00  # high enough that hop 4 exceeds the $3 per-run cap


class GraphState(TypedDict):
    hop: int
    run_id: str
    rows: list[dict[str, Any]]
    stopped: bool


def make_dry_run_allow() -> Callable[..., dict[str, Any]]:
    """A recorded fixture, not a fake SDK -- mirrors the real Cap ledger
    math (cumulative spend against a $3 per-run cap) so `--dry-run` proves
    the same deny-before-hop-4 behavior with zero network calls and zero
    keys. Every field name matches the real /api/v1/cap/allow response."""
    spent: dict[str, float] = {}

    def allow(agent: str, run_id: str, estimated_usd: float, **kwargs: Any) -> dict[str, Any]:
        already_spent = spent.get(run_id, 0.0)
        would_be = round(already_spent + estimated_usd, 2)
        allow_id = f"allow_dryrun_{uuid.uuid4().hex[:8]}"

        if would_be > PER_RUN_LIMIT_USD:
            return {
                "approved": False,
                "reason": "per_run_limit",
                "id": allow_id,
                "remaining": {"run": max(0.0, round(PER_RUN_LIMIT_USD - already_spent, 2)), "day": None, "month": None},
            }

        spent[run_id] = would_be
        return {
            "approved": True,
            "id": allow_id,
            "remaining": {"run": round(PER_RUN_LIMIT_USD - would_be, 2), "day": None, "month": None},
            "remainingPlanUsd": None,
        }

    return allow


def make_dry_run_report() -> Callable[..., dict[str, Any]]:
    def report(allow_id: str, actual_usd: float) -> dict[str, Any]:
        return {"ok": True, "allowId": allow_id, "actualUsd": actual_usd}

    return report


def call_provider(dry_run: bool, model: str) -> str:
    if dry_run:
        return "(dry-run) stub provider response -- no real API call made"
    from openai import OpenAI

    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    completion = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": "Say hello in five words or fewer."}],
    )
    return completion.choices[0].message.content or ""


def build_graph(
    agent_id: str,
    cap_allow: Callable[..., dict[str, Any]],
    cap_report: Callable[..., dict[str, Any]] | None,
    dry_run: bool,
    model: str,
) -> Any:
    def attempt_call(state: GraphState) -> GraphState:
        hop = state["hop"] + 1

        # The one hard rule this whole demo exists to prove: call Cap
        # BEFORE the provider, on every single attempt, no exceptions.
        gate = cap_allow(
            agent=agent_id,
            run_id=state["run_id"],
            estimated_usd=ESTIMATED_USD_PER_HOP,
            merchant="openai",
            model=model,
            purpose="langgraph-retry-demo",
        )

        row: dict[str, Any] = {
            "hop": hop,
            "estimated_usd": ESTIMATED_USD_PER_HOP,
            "approved": gate["approved"],
            "reason": gate.get("reason") or "-",
            "allow_id": gate["id"],
            "provider_called": False,
        }

        if not gate["approved"]:
            # Denied -- stop. Do not catch-and-retry a deny; that would
            # defeat the entire point of a hard stop.
            state["rows"].append(row)
            state["hop"] = hop
            state["stopped"] = True
            return state

        call_provider(dry_run, model)
        row["provider_called"] = True
        state["rows"].append(row)

        if cap_report is not None:
            # Demo simplification, not a bug: this reports the same $1.00
            # used as the estimate, so hop 4's math stays exactly
            # deterministic for the README's expected output. A real
            # integration should compute actual_usd from the provider's
            # own usage response (e.g. completion.usage) and a real price
            # table for the model -- Valta doesn't compute that for you
            # (see cap.mdx), and this demo isn't inventing one either.
            cap_report(allow_id=gate["id"], actual_usd=ESTIMATED_USD_PER_HOP)

        state["hop"] = hop
        return state

    def should_continue(state: GraphState) -> str:
        if state["stopped"]:
            return END
        if state["hop"] >= MAX_HOPS:
            return END
        return "attempt_call"

    graph = StateGraph(GraphState)
    graph.add_node("attempt_call", attempt_call)
    graph.set_entry_point("attempt_call")
    graph.add_conditional_edges("attempt_call", should_continue, {"attempt_call": "attempt_call", END: END})
    return graph.compile()


def print_table(rows: list[dict[str, Any]]) -> None:
    headers = ["hop", "estimated_usd", "approved", "reason", "allow_id", "provider_called"]
    widths = {h: max(len(h), *(len(str(r[h])) for r in rows)) for h in headers}
    line = " | ".join(h.ljust(widths[h]) for h in headers)
    print(line)
    print("-" * len(line))
    for r in rows:
        print(" | ".join(str(r[h]).ljust(widths[h]) for h in headers))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Use a recorded deny fixture -- no keys, no network calls.")
    parser.add_argument("--model", default="gpt-4.1", help="Model name to pass through to Cap and (if live) OpenAI.")
    args = parser.parse_args()

    load_dotenv()

    agent_id = os.environ.get("VALTA_AGENT_ID", "ag_dryrun_demo" if args.dry_run else "")
    if not args.dry_run and not agent_id:
        print("VALTA_AGENT_ID is required for a live run. Set it in .env, or use --dry-run.", file=sys.stderr)
        return 1

    if args.dry_run:
        cap_allow = make_dry_run_allow()
        cap_report = make_dry_run_report()
    else:
        api_key = os.environ.get("VALTA_API_KEY", "")
        if not api_key:
            print("VALTA_API_KEY is required for a live run. Set it in .env, or use --dry-run.", file=sys.stderr)
            return 1
        if not os.environ.get("OPENAI_API_KEY"):
            print("OPENAI_API_KEY is required for a live run (Cap only gates the call -- it never talks to OpenAI for you).", file=sys.stderr)
            return 1
        client = CapClient(api_key=api_key)
        cap_allow = client.allow
        cap_report = client.report

    run_id = f"run_{uuid.uuid4().hex[:12]}"
    initial_state: GraphState = {"hop": 0, "run_id": run_id, "rows": [], "stopped": False}

    graph = build_graph(agent_id, cap_allow, cap_report, args.dry_run, args.model)

    print(f"{'DRY RUN' if args.dry_run else 'LIVE'} -- agent={agent_id} run_id={run_id} per_run_limit=${PER_RUN_LIMIT_USD:.2f}\n")

    try:
        final_state = graph.invoke(initial_state)
    except CapError as e:
        print(f"Valta Cap API error: {e}", file=sys.stderr)
        return 1

    print_table(final_state["rows"])

    denied = [r for r in final_state["rows"] if not r["approved"]]
    if denied:
        print(f"\nStopped at hop {denied[0]['hop']}: {denied[0]['reason']} (allow_id={denied[0]['allow_id']})")
        print("No provider call was made for the denied hop or any hop after it.")
    else:
        print(f"\nCompleted all {len(final_state['rows'])} hops without hitting the per-run cap.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
