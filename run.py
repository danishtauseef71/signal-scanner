"""Run the whole pipeline, or resume it partway through.

Each step is a standalone module that reads the previous step's file, so this
only decides what to run and in what order. A step that fails stops the run:
later steps read the file an earlier one writes, so continuing past a failure
would silently score a stale or partial list.

    python run.py                          # everything
    python run.py --city phoenix --limit 25
    python run.py --from-step score        # resume, reusing what is on disk
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
SRC_DIR = REPO_ROOT / "src"

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

log = logging.getLogger("run")


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
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    selected = steps_to_run(args.from_step)
    log.info("Running %d steps: %s", len(selected), " -> ".join(name for name, _ in selected))

    started = time.monotonic()
    for step, accepts in selected:
        if not run_step(step, build_step_command(step, accepts, args)):
            log.error("Pipeline stopped at %s. Later steps read this step's output.", step)
            return 1

    log.info("")
    log.info("Pipeline finished in %.1fs. Campaign file: data/export.csv", time.monotonic() - started)
    return 0


if __name__ == "__main__":
    sys.exit(main())
