"""Run the whole pipeline, or resume it partway through.

Each step is a standalone module that reads the previous step's file, so this
only decides what to run and in what order. A step that fails stops the run:
later steps read the file an earlier one writes, so continuing past a failure
would silently score a stale or partial list.

After every step the funnel is redrawn from whatever data files exist, so the
shape of the list builds up on screen as the run proceeds.

    python run.py                          # everything
    python run.py --city phoenix --limit 25
    python run.py --from-step score        # resume, reusing what is on disk
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
SRC_DIR = REPO_ROOT / "src"
DATA_DIR = REPO_ROOT / "data"

# Pipeline order. Each entry lists the arguments that step actually accepts,
# so an unsupported flag is never passed down and argparse never rejects a run.
PIPELINE = [
    ("discover", {"city", "limit", "refresh"}),
    ("dso_check", {"refresh"}),
    ("enrich_web", {"limit", "refresh"}),
    ("enrich_npi", {"limit", "refresh"}),
    ("verify", {"limit", "refresh"}),
    ("score", {"limit"}),
    ("brief", set()),
    ("export", set()),
]
STEP_NAMES = [name for name, _ in PIPELINE]

BAR_WIDTH = 40
BAR_CHAR = "#"

log = logging.getLogger("run")


# --- reading what is actually on disk --------------------------------------


def read_step_output(step: str) -> dict | None:
    """Return a step's data file, or None when it has not been written yet."""
    path = DATA_DIR / f"{step}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def practice_ids(document: dict | None, predicate=None) -> set[str] | None:
    """osm_ids in a step's output, optionally filtered."""
    if document is None:
        return None
    return {
        row["osm_id"]
        for row in document.get("practices", [])
        if predicate is None or predicate(row)
    }


def dso_owned_ids(document: dict | None) -> set[str]:
    if document is None:
        return set()
    return {osm_id for chain in document.get("chains", []) if chain["dso_owned"] for osm_id in chain["osm_ids"]}


def funnel_cuts() -> list[tuple[str, set[str] | None]]:
    """The funnel as strict subset cuts, each derived from the one above it.

    Every stage is intersected with its predecessor rather than counted
    independently, so a stage can never report more accounts than the stage it
    is cut from. That matters here: score.py ranks the whole audience, so a
    practice can qualify while having no MX record and never reach the export.
    Intersecting makes the last cut mean "qualified and reachable", which is
    what the campaign actually gets.
    """
    discover = read_step_output("discover")
    dso = read_step_output("dso_check")
    verify = read_step_output("verify")
    score = read_step_output("score")

    discovered = practice_ids(discover)
    has_website = practice_ids(discover, lambda row: bool(row.get("website")))

    not_dso = None
    if has_website is not None and dso is not None:
        not_dso = has_website - dso_owned_ids(dso)

    deliverable = None
    if not_dso is not None:
        reachable = practice_ids(verify, lambda row: bool(row.get("deliverable")))
        if reachable is not None:
            deliverable = not_dso & reachable

    qualified = None
    if deliverable is not None:
        passing = practice_ids(score, lambda row: bool(row.get("qualified")))
        if passing is not None:
            qualified = deliverable & passing

    return [
        ("discovered", discovered),
        ("has website", has_website),
        ("not DSO owned", not_dso),
        ("deliverable domain", deliverable),
        ("qualified", qualified),
    ]


def coverage_lines() -> list[tuple[str, int, int]]:
    """Facts we could establish, as a proportion of the audience we tried.

    These are not cuts. Nothing is dropped for being unreachable or unmatched;
    the practice stays in the list with null signals, so these belong below the
    funnel rather than inside it.
    """
    lines = []

    web = read_step_output("enrich_web")
    if web is not None:
        rows = web.get("practices", [])
        lines.append(("site reachable", sum(1 for row in rows if row.get("site_reachable")), len(rows)))

    npi = read_step_output("enrich_npi")
    if npi is not None:
        rows = npi.get("practices", [])
        lines.append(("NPI org matched", sum(1 for row in rows if row.get("npi")), len(rows)))
        lines.append(("provider count known", sum(1 for row in rows if row.get("provider_count") is not None), len(rows)))

    return lines


# --- drawing ---------------------------------------------------------------


def bar_for(count: int, total: int) -> str:
    if not total or not count:
        return ""
    return BAR_CHAR * max(1, round(count / total * BAR_WIDTH))


def render_funnel(after_step: str) -> None:
    cuts = funnel_cuts()
    top = next((ids for _, ids in cuts if ids is not None), None)
    total = len(top) if top else 0

    log.info("")
    log.info("  FUNNEL  (after %s)", after_step)

    previous = None
    for label, ids in cuts:
        if ids is None:
            log.info("    %-20s %5s", label, "-")
            continue

        removed = "" if previous is None else f"-{previous - len(ids)}"
        log.info("    %-20s %5d  %7s  %s", label, len(ids), removed, bar_for(len(ids), total))
        previous = len(ids)

    coverage = coverage_lines()
    if coverage:
        log.info("  COVERAGE  (not cuts, nothing is dropped for these)")
        for label, found, attempted in coverage:
            share = f"{found / attempted * 100:.0f}%" if attempted else "-"
            log.info("    %-20s %5d / %-4d %5s", label, found, attempted, share)
    log.info("")


# --- running ---------------------------------------------------------------


def steps_to_run(from_step: str | None) -> list[tuple[str, set]]:
    if not from_step:
        return PIPELINE
    if from_step not in STEP_NAMES:
        raise SystemExit(f"unknown step {from_step!r}. Steps: {', '.join(STEP_NAMES)}")
    return PIPELINE[STEP_NAMES.index(from_step):]


def build_step_command(step: str, accepts: set, args: argparse.Namespace) -> list[str]:
    command = [sys.executable, str(SRC_DIR / f"{step}.py")]
    if "city" in accepts and args.city:
        command += ["--city", args.city]
    if "limit" in accepts and args.limit:
        command += ["--limit", str(args.limit)]
    if "refresh" in accepts and args.refresh:
        command += ["--refresh"]
    return command


def run_step(step: str, command: list[str]) -> bool:
    log.info("")
    log.info("=== %s ===", step)
    started = time.monotonic()
    result = subprocess.run(command)
    elapsed = time.monotonic() - started

    if result.returncode != 0:
        log.error("%s failed with exit code %d after %.1fs", step, result.returncode, elapsed)
        return False
    log.info("%s finished in %.1fs", step, elapsed)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the signal scanner pipeline end to end.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Steps: " + " -> ".join(STEP_NAMES),
    )
    parser.add_argument("--city", help="City key from config/cities.yaml")
    parser.add_argument("--limit", type=int, help="Cap the number of practices processed")
    parser.add_argument("--from-step", dest="from_step", help="Resume from this step onwards")
    parser.add_argument("--refresh", action="store_true", help="Ignore caches and refetch")
    parser.add_argument("--no-funnel", action="store_true", help="Skip the funnel display")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    selected = steps_to_run(args.from_step)
    log.info("Running %d steps: %s", len(selected), " -> ".join(name for name, _ in selected))

    started = time.monotonic()
    for step, accepts in selected:
        if not run_step(step, build_step_command(step, accepts, args)):
            log.error("Pipeline stopped at %s. Later steps read this step's output.", step)
            return 1
        if not args.no_funnel:
            render_funnel(step)

    log.info("Pipeline finished in %.1fs. Campaign file: data/export.csv", time.monotonic() - started)
    return 0


if __name__ == "__main__":
    sys.exit(main())
