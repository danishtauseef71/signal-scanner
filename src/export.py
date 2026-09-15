"""Write the campaign ready CSV for the sequencer.

Column names are the ones sequencers expect, and every personalisation
variable is a field a template can drop in directly. Only deliverable
accounts are exported by default: a row the sequencer cannot send to is
worse than no row, because it burns domain reputation on a bounce.

    python src/export.py
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import logging
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INPUT_PATH = REPO_ROOT / "data" / "brief.json"
OUTPUT_PATH = REPO_ROOT / "data" / "export.csv"

# Sequencer-facing names, not internal ones.
COLUMNS = [
    "company", "website", "domain", "email", "email_type", "phone",
    "address", "city", "state", "postcode",
    "score", "rank", "qualified", "signals_fired", "signal_coverage",
    "provider_count", "npi", "npi_taxonomy",
    "opening_angle", "signal_headline", "provider_phrase",
    "mx_provider", "deliverable", "osm_id",
]

ADDRESS_TAIL = re.compile(r",\s*([^,]+),\s*([A-Z]{2})\s+(\d{5})(?:-\d{4})?\s*$")

# Human readable forms for the personalisation variables.
SIGNAL_HEADLINES = {
    "hiring_reception": "hiring for the front desk",
    "no_online_booking": "booking only by phone",
    "three_plus_providers": "multi provider practice",
    "multiple_locations": "more than one location",
    "accepting_new_patients": "accepting new patients",
}
# Highest intent first: this picks the one angle a subject line should use.
HEADLINE_PRIORITY = [
    "hiring_reception", "no_online_booking", "three_plus_providers",
    "multiple_locations", "accepting_new_patients",
]

log = logging.getLogger("export")


def split_address(address: str | None) -> tuple[str, str, str]:
    """Pull city, state and postcode out of the single address line."""
    match = ADDRESS_TAIL.search((address or "").strip())
    return match.groups() if match else ("", "", "")


def best_email(practice: dict) -> tuple[str, str]:
    """Prefer a named person, fall back to the shared mailbox.

    Returns the address and its type, so the sequencer can pick a different
    template for a role mailbox that no single person owns.
    """
    for address in practice.get("emails_personal") or []:
        return address, "personal"
    for address in practice.get("emails_role") or []:
        return address, "role"
    return "", "none"


def signal_headline(practice: dict) -> str:
    fired = set(practice.get("signals_fired") or [])
    for signal in HEADLINE_PRIORITY:
        if signal in fired:
            return SIGNAL_HEADLINES[signal]
    return ""


def provider_phrase(practice: dict) -> str:
    """A drop-in phrase, blank when we do not know, never a guess."""
    count = practice.get("provider_count")
    if not count:
        return ""
    return "a solo practice" if count == 1 else f"a {count} provider practice"


def export_row(practice: dict) -> dict:
    city, state, postcode = split_address(practice.get("address"))
    email, email_type = best_email(practice)
    return {
        "company": practice.get("name", ""),
        "website": practice.get("site_url") or practice.get("website") or "",
        "domain": practice.get("domain") or "",
        "email": email,
        "email_type": email_type,
        "phone": practice.get("phone") or "",
        "address": practice.get("address") or "",
        "city": city,
        "state": state,
        "postcode": postcode,
        "score": practice.get("score", 0),
        "rank": practice.get("rank", ""),
        "qualified": "yes" if practice.get("qualified") else "no",
        "signals_fired": "|".join(practice.get("signals_fired") or []),
        "signal_coverage": practice.get("signal_coverage", ""),
        "provider_count": practice.get("provider_count") or "",
        "npi": practice.get("npi") or "",
        "npi_taxonomy": practice.get("npi_taxonomy") or "",
        "opening_angle": practice.get("brief", ""),
        "signal_headline": signal_headline(practice),
        "provider_phrase": provider_phrase(practice),
        "mx_provider": practice.get("mx_provider") or "",
        "deliverable": "yes" if practice.get("deliverable") else "no",
        "osm_id": practice.get("osm_id", ""),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write the sequencer ready CSV.")
    parser.add_argument("--include-undeliverable", action="store_true",
                        help="Export accounts with no MX as well")
    parser.add_argument("--require-email", action="store_true",
                        help="Export only accounts with an address to send to")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

    try:
        if not INPUT_PATH.exists():
            log.error("%s not found. Run src/brief.py first.", INPUT_PATH.relative_to(REPO_ROOT))
            return 2

        practices = json.loads(INPUT_PATH.read_text())["practices"]
        rows, dropped_mx, dropped_email = [], 0, 0

        for practice in practices:
            if not practice.get("deliverable") and not args.include_undeliverable:
                dropped_mx += 1
                continue
            row = export_row(practice)
            if args.require_email and not row["email"]:
                dropped_email += 1
                continue
            rows.append(row)

        OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with OUTPUT_PATH.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=COLUMNS)
            writer.writeheader()
            writer.writerows(rows)

        with_email = sum(1 for row in rows if row["email"])
        log.info(
            "Wrote %d rows to %s, %d with an email address", len(rows), OUTPUT_PATH.relative_to(REPO_ROOT), with_email
        )
        if dropped_mx:
            log.info("Held back %d accounts with no MX record", dropped_mx)
        if dropped_email:
            log.info("Held back %d accounts with no email address", dropped_email)
    except Exception as error:
        log.error("Export failed: %s", error)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
