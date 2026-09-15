"""Rank the audience against the ICP model in icp.yaml.

Every weight and threshold is read from config. This module knows how to apply
a scoring model, not what the model says. A signal is only credited when it is
definitely true: enrichment writes null when it could not tell, and an unknown
is never scored as an absence.

    python src/score.py
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
ICP_CONFIG = REPO_ROOT / "config" / "icp.yaml"
INPUT_PATH = REPO_ROOT / "data" / "verify.json"
OUTPUT_PATH = REPO_ROOT / "data" / "score.json"

log = logging.getLogger("score")


def load_icp_model() -> dict:
    if not ICP_CONFIG.exists():
        raise FileNotFoundError(f"missing {ICP_CONFIG.relative_to(REPO_ROOT)}")
    return yaml.safe_load(ICP_CONFIG.read_text()) or {}


def fired_signals(practice: dict, definitions: dict) -> list[dict]:
    """Signals that are definitely true on this practice.

    None means enrichment could not establish the fact, usually because the
    site was unreachable. That is not the same as false, and scoring it as an
    absence would quietly punish practices for our own coverage gaps.
    """
    fired = []
    for name, definition in (definitions or {}).items():
        if practice.get(name) is True:
            fired.append({"signal": name, "weight": definition.get("weight", 0), "why": definition.get("why")})
    return fired


def unknown_signals(practice: dict, definitions: dict) -> list[str]:
    return [name for name in (definitions or {}) if practice.get(name) is None]


def score_practice(practice: dict, icp: dict) -> dict:
    signals = fired_signals(practice, icp.get("signals"))
    exclusions = fired_signals(practice, icp.get("exclusions"))
    unknown = unknown_signals(practice, icp.get("signals")) + unknown_signals(practice, icp.get("exclusions"))

    total = sum(item["weight"] for item in signals) + sum(item["weight"] for item in exclusions)
    qualified_at = icp.get("qualified_at", 0)

    return {
        **practice,
        "score": total,
        "qualified": total >= qualified_at,
        "signals_fired": [item["signal"] for item in signals],
        "exclusions_fired": [item["signal"] for item in exclusions],
        "signals_unknown": unknown,
        "score_breakdown": signals + exclusions,
        # A practice judged on fewer facts deserves less confidence in its rank.
        "signal_coverage": round(
            1 - len(unknown) / max(len(icp.get("signals") or {}) + len(icp.get("exclusions") or {}), 1), 2
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score and rank the audience against icp.yaml.")
    parser.add_argument("--limit", type=int, help="Score only the first N practices")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

    try:
        if not INPUT_PATH.exists():
            log.error("%s not found. Run src/verify.py first.", INPUT_PATH.relative_to(REPO_ROOT))
            return 2

        icp = load_icp_model()
        practices = json.loads(INPUT_PATH.read_text())["practices"]
        if args.limit:
            practices = practices[: args.limit]

        scored = [score_practice(practice, icp) for practice in practices]
        # Rank on score, then on how much we actually know, then alphabetically
        # so the order never wobbles between runs on ties.
        scored.sort(key=lambda row: (-row["score"], -row["signal_coverage"], row["name"].lower()))
        for rank, row in enumerate(scored, start=1):
            row["rank"] = rank

        qualified = sum(1 for row in scored if row["qualified"])
        log.info(
            "Scored %d practices, %d qualified at %s or above",
            len(scored), qualified, icp.get("qualified_at"),
        )

        OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT_PATH.write_text(
            json.dumps(
                {
                    "step": "score",
                    "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                    "qualified_at": icp.get("qualified_at"),
                    "count": len(scored),
                    "qualified": qualified,
                    "practices": scored,
                },
                indent=2,
            )
        )
        log.info("Wrote %d scored practices to %s", len(scored), OUTPUT_PATH.relative_to(REPO_ROOT))
    except Exception as error:
        log.error("Scoring failed: %s", error)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
