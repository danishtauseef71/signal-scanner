"""Write a short opening angle for each qualified account.

Templates are the default and need no API key. If ANTHROPIC_API_KEY is set,
each brief is written by Claude instead, grounded only in the signals that
actually fired so it cannot invent facts about a practice.

The Anthropic SDK is imported lazily, inside the Claude path, so the pipeline
runs on the declared stack without it. To enable Claude briefs:

    pip install anthropic
    export ANTHROPIC_API_KEY=...

    python src/brief.py
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
ICP_CONFIG = REPO_ROOT / "config" / "icp.yaml"
INPUT_PATH = REPO_ROOT / "data" / "score.json"
OUTPUT_PATH = REPO_ROOT / "data" / "brief.json"

CLAUDE_MODEL = "claude-opus-5"
CLAUDE_MAX_TOKENS = 300

# One clause per signal, written from the buyer's side rather than ours.
SIGNAL_CLAUSES = {
    "hiring_reception": "you're hiring for the front desk",
    "no_online_booking": "every appointment still has to come through the phone",
    "three_plus_providers": "you're running {provider_count} providers",
    "accepting_new_patients": "you're taking new patients",
    "multiple_locations": "front desk load is split across locations",
}

log = logging.getLogger("brief")


def load_icp_model() -> dict:
    return yaml.safe_load(ICP_CONFIG.read_text()) or {}


def signal_clause(signal: str, practice: dict) -> str | None:
    clause = SIGNAL_CLAUSES.get(signal)
    if not clause:
        return None
    if "{provider_count}" in clause:
        count = practice.get("provider_count")
        if not count:
            return None
        return clause.format(provider_count=count)
    return clause


def template_brief(practice: dict) -> str:
    """Deterministic fallback. Only ever states signals that actually fired."""
    clauses = [c for c in (signal_clause(s, practice) for s in practice.get("signals_fired", [])) if c]

    if not clauses:
        return (
            f"{practice['name']} looks like a fit on profile, but no specific "
            "signal fired. Worth a generic opener or more research first."
        )

    if len(clauses) == 1:
        evidence = clauses[0]
    else:
        evidence = ", ".join(clauses[:-1]) + f", and {clauses[-1]}"

    return (
        f"Noticed {evidence}. That usually means missed calls outside chair time "
        "turn into lost bookings, which is exactly the gap an AI receptionist closes."
    )


def claude_brief(practice: dict, icp: dict, client) -> str:
    """Ask Claude for the opener, grounded strictly in the fired signals."""
    evidence = [
        {"signal": item["signal"], "why_it_matters": item["why"]}
        for item in practice.get("score_breakdown", [])
        if item["weight"] > 0
    ]

    prompt = (
        f"You write one-sentence cold email openers for a company selling: {icp.get('client')}.\n\n"
        f"Practice: {practice['name']}\n"
        f"Signals that fired, with why each matters to the seller:\n"
        f"{json.dumps(evidence, indent=2)}\n"
        f"Provider count: {practice.get('provider_count') or 'unknown'}\n\n"
        "Write one sentence, under 30 words, opening with a specific observation "
        "about this practice drawn ONLY from the signals above. State nothing that "
        "is not in those signals. No greeting, no sign-off, no exclamation marks."
    )

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=CLAUDE_MAX_TOKENS,
        output_config={"effort": "low"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        messages=[{"role": "user", "content": prompt}],
    )

    if response.stop_reason == "refusal":
        raise RuntimeError("Claude declined to write this brief")
    return "".join(block.text for block in response.content if block.type == "text").strip()


def build_claude_client():
    """Lazy import so the SDK is only needed when a key is actually set."""
    try:
        from anthropic import Anthropic
    except ImportError as error:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is set but the anthropic package is not installed. "
            "Run: pip install anthropic"
        ) from error
    return Anthropic()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write an opening angle per qualified account.")
    parser.add_argument("--all", action="store_true", help="Brief every practice, not just qualified ones")
    parser.add_argument("--template-only", action="store_true", help="Never call Claude, even with a key set")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

    try:
        if not INPUT_PATH.exists():
            log.error("%s not found. Run src/score.py first.", INPUT_PATH.relative_to(REPO_ROOT))
            return 2

        icp = load_icp_model()
        practices = json.loads(INPUT_PATH.read_text())["practices"]
        targets = practices if args.all else [p for p in practices if p.get("qualified")]

        client = None
        source = "template"
        if os.environ.get("ANTHROPIC_API_KEY") and not args.template_only:
            try:
                client = build_claude_client()
                source = "claude"
            except Exception as error:  # a missing SDK must not kill the run
                log.warning("Falling back to templates: %s", error)

        log.info("Briefing %d of %d practices using %s", len(targets), len(practices), source)

        # Every practice is carried forward, briefed or not. Writing only the
        # briefed subset would drop the rest of the audience from export.py,
        # which reads this step's file.
        target_ids = {practice["osm_id"] for practice in targets}
        briefed = []
        for practice in practices:
            if practice["osm_id"] not in target_ids:
                briefed.append({**practice, "brief": "", "brief_source": "not briefed"})
                continue

            written_by = source
            try:
                text = claude_brief(practice, icp, client) if client else template_brief(practice)
            except Exception as error:  # one bad brief never kills the run
                log.warning("  %s: falling back to template (%s)", practice["name"][:34], str(error)[:60])
                text = template_brief(practice)
                written_by = "template"
            briefed.append({**practice, "brief": text, "brief_source": written_by})

        OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT_PATH.write_text(
            json.dumps(
                {
                    "step": "brief",
                    "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                    "brief_source": source,
                    "count": len(briefed),
                    "practices": briefed,
                },
                indent=2,
            )
        )
        written = sum(1 for row in briefed if row["brief"])
        log.info("Wrote %d practices to %s, %d with a brief", len(briefed), OUTPUT_PATH.relative_to(REPO_ROOT), written)
    except Exception as error:
        log.error("Briefing failed: %s", error)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
