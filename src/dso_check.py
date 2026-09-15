"""Decide which practice chains are DSO owned, at national scale.

This step works at the chain level, not the practice level. It takes the
distinct chain names found in data/discover.json, asks the NPPES NPI Registry
how many locations each one runs nationally and across how many states, and
compares that footprint to the floors in icp.yaml.

DSOs register every practice as its own legal entity but share one "Doing
Business As" name, so the DBA is the chain fingerprint. NPPES matches
organization_name against DBA as well as legal name, which is what makes this
generalise to any market without a hardcoded list.

    python src/dso_check.py
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import re
import socket
import sys
import time
from pathlib import Path

import requests
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
ICP_CONFIG = REPO_ROOT / "config" / "icp.yaml"
DISCOVER_PATH = REPO_ROOT / "data" / "discover.json"
CACHE_DIR = REPO_ROOT / "data" / "cache"
OUTPUT_PATH = REPO_ROOT / "data" / "dso_check.json"

NPPES_HOST = "npiregistry.cms.hhs.gov"
NPPES_ENDPOINT = f"https://{NPPES_HOST}/api/"
NPPES_VERSION = "2.1"
# Overpass and NPPES etiquette ask for a reachable contact. The repo URL is it.
USER_AGENT = "signal-scanner/0.1 (+https://github.com/danishtauseef71/signal-scanner)"
HTTP_TIMEOUT_SECONDS = 45
DELAY_BETWEEN_CALLS_SECONDS = 1.0

# NPPES silently clamps limit to 200. Hitting the cap means "at least this many".
PAGE_LIMIT = 200

# NPPES rejects these outright, with a misleading "last_name" error.
API_BREAKING = re.compile(r"[^A-Z0-9 \-]")
# Trailing legal and credential noise, stripped so name variants converge.
LEGAL_SUFFIXES = {
    "LLC", "L L C", "PC", "PLLC", "PLC", "INC", "INCORPORATED", "PA", "LTD",
    "CORP", "CORPORATION", "CO", "COMPANY", "DDS", "DMD", "MD", "MS", "GROUP",
}
# Below this a wildcard matches half the registry, so we refuse to query it.
MIN_CORE_LENGTH = 4

# Chain name falls back to the practice name, since only a small minority of
# OSM records carry operator or brand. Order matters: an explicit operator is
# stronger evidence of a chain than the practice's own signage.
CHAIN_NAME_FIELDS = ("operator", "brand", "name")

REQUIRED_THRESHOLDS = ("npi_location_floor", "npi_state_floor")

log = logging.getLogger("dso_check")


class ConfigError(Exception):
    """icp.yaml is missing something only the ICP owner should add."""


# --- config ---------------------------------------------------------------

SAMPLE_BLOCK = """
dso_detection:
  npi_location_floor: 25       # locations nationally before a chain counts as a DSO
  npi_state_floor: 3           # distinct states before a chain counts as a DSO
  require_both: true           # true = both floors must trip, false = either
  known_dso_names: []          # backstop, forces dso_owned regardless of counts
  known_independent_names: []  # backstop, blocks a false positive
""".strip()


def load_dso_thresholds() -> dict:
    """Read the dso_detection block. The ICP model is the operator's to set."""
    if not ICP_CONFIG.exists():
        raise ConfigError(f"missing {ICP_CONFIG.relative_to(REPO_ROOT)}")

    icp = yaml.safe_load(ICP_CONFIG.read_text()) or {}
    thresholds = icp.get("dso_detection")
    if not thresholds:
        raise ConfigError(
            "config/icp.yaml has no dso_detection block. The ICP model is yours to "
            "set, so add this and pick the numbers:\n\n" + SAMPLE_BLOCK
        )

    missing = [key for key in REQUIRED_THRESHOLDS if thresholds.get(key) is None]
    if missing:
        raise ConfigError(
            f"dso_detection is missing {', '.join(missing)} in config/icp.yaml"
        )

    return thresholds


# --- name normalisation ---------------------------------------------------


def normalise_org_name(value: str) -> str:
    """Uppercase, drop punctuation NPPES rejects, strip trailing legal suffixes.

    "Western Dental Services, Inc." and "Western Dental" both reduce towards
    "WESTERN DENTAL", so a wildcard on the shorter one still matches the longer.
    """
    cleaned = API_BREAKING.sub(" ", value.upper())
    tokens = cleaned.split()
    while tokens and tokens[-1] in LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def chain_candidates_in_market(practices: list[dict]) -> dict[str, dict]:
    """Distinct chain names to check, keyed by raw value.

    Falls back to the practice name when operator and brand are absent, which
    is the common case: most OSM dental records carry neither tag, and checking
    only tagged records would leave the large majority never checked at all.
    """
    candidates: dict[str, dict] = {}
    for practice in practices:
        for field in CHAIN_NAME_FIELDS:
            raw = (practice.get(field) or "").strip()
            if not raw:
                continue
            entry = candidates.setdefault(raw, {"source_fields": set(), "osm_ids": []})
            entry["source_fields"].add(field)
            entry["osm_ids"].append(practice["osm_id"])
            break  # first populated field wins, per CHAIN_NAME_FIELDS order
    return candidates


# --- NPPES ----------------------------------------------------------------


def assert_nppes_reachable() -> None:
    """Fail loudly on DNS, which is the failure mode people actually hit."""
    try:
        socket.getaddrinfo(NPPES_HOST, 443)
    except socket.gaierror as error:
        raise ConfigError(
            f"cannot resolve {NPPES_HOST} ({error.strerror}). DNS is failing, not "
            "the API. Some networks fail the whole cms.hhs.gov zone while "
            "resolving everything else. Check with:\n"
            f"    nslookup {NPPES_HOST}\n"
            f"    nslookup {NPPES_HOST} 8.8.8.8\n"
            "If only the second works, add 8.8.8.8 or 1.1.1.1 to your DNS settings."
        ) from error


def build_org_query(core: str) -> dict:
    return {
        "version": NPPES_VERSION,
        "organization_name": f"{core}*",
        "enumeration_type": "NPI-2",
        "limit": PAGE_LIMIT,
    }


def cache_path_for_query(params: dict) -> Path:
    """Key on the full parameter set, so a changed query never reads a stale file."""
    canonical = json.dumps(params, sort_keys=True)
    digest = hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:12]
    return CACHE_DIR / f"npi_org_{digest}.json"


def fetch_org_records(core: str, refresh: bool = False) -> dict:
    """Wildcard search on the normalised core, from cache when we have it."""
    params = build_org_query(core)
    cache_path = cache_path_for_query(params)

    if cache_path.exists() and not refresh:
        return json.loads(cache_path.read_text())

    response = requests.get(
        NPPES_ENDPOINT,
        params=params,
        headers={"User-Agent": USER_AGENT},
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    payload = response.json()

    if payload.get("Errors"):
        raise RuntimeError(f"NPPES rejected {core!r}: {payload['Errors']}")

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(payload, indent=2))
    time.sleep(DELAY_BETWEEN_CALLS_SECONDS)
    return payload


# --- verification ---------------------------------------------------------


def record_names(record: dict) -> list[str]:
    """Legal name plus every DBA. DSOs hide the chain in the DBA."""
    names = [record.get("basic", {}).get("organization_name")]
    names += [other.get("organization_name") for other in record.get("other_names") or []]
    return [name for name in names if name]


def record_matches_core(record: dict, core: str) -> bool:
    """A wildcard match is only real if some name actually starts with the core.

    WESTERN DENTAL* returns prefix collisions like EMILY LEE DDS PC, and
    counting raw result_count would inflate the chain's footprint.

    Prefix verification is a filter, not proof. It still admits longer names
    that merely share the prefix, so an unrelated "Western Dental Clinic" in
    another state counts towards Western Dental's footprint. That inflates
    counts for generic names ("FAMILY DENTAL", "SMILE CENTER") more than for
    distinctive ones, and is the main reason to keep known_independent_names
    as a backstop.
    """
    return any(normalise_org_name(name).startswith(core) for name in record_names(record))


def location_state(record: dict) -> str | None:
    for address in record.get("addresses") or []:
        if address.get("address_purpose") == "LOCATION":
            return address.get("state")
    return None


def measure_national_footprint(payload: dict, core: str) -> dict:
    """Distinct location NPIs and distinct states, from verified matches only."""
    returned = payload.get("results") or []
    npis, states = set(), set()

    for record in returned:
        if not record_matches_core(record, core):
            continue
        if record.get("number"):
            npis.add(record["number"])
        state = location_state(record)
        if state:
            states.add(state)

    return {
        "npi_locations": len(npis),
        "npi_states": len(states),
        "states": sorted(states),
        "records_returned": len(returned),
        # At the cap the true footprint is larger than what we counted.
        "truncated": len(returned) >= PAGE_LIMIT,
    }


# --- the decision ---------------------------------------------------------


def decide_dso_owned(footprint: dict, raw_value: str, thresholds: dict) -> tuple[bool, str]:
    """Thresholds first, curated lists only as an override. Returns (verdict, why)."""
    core = normalise_org_name(raw_value)

    def listed(key: str) -> bool:
        return any(
            normalise_org_name(name) == core for name in (thresholds.get(key) or [])
        )

    over_locations = footprint["npi_locations"] >= thresholds["npi_location_floor"]
    over_states = footprint["npi_states"] >= thresholds["npi_state_floor"]
    require_both = thresholds.get("require_both", True)
    computed = (over_locations and over_states) if require_both else (over_locations or over_states)

    if listed("known_independent_names"):
        return False, "known_independent_names override"
    if listed("known_dso_names"):
        return True, "known_dso_names override"

    joiner = "and" if require_both else "or"
    return computed, (
        f"{footprint['npi_locations']} locations {joiner} {footprint['npi_states']} states "
        f"vs floors {thresholds['npi_location_floor']}/{thresholds['npi_state_floor']}"
    )


def check_chain_name(raw_value: str, entry: dict, thresholds: dict, refresh: bool) -> dict:
    core = normalise_org_name(raw_value)
    result = {
        "chain_name": raw_value,
        "normalised_core": core,
        "source_fields": sorted(entry["source_fields"]),
        "practice_count_in_market": len(entry["osm_ids"]),
        "osm_ids": entry["osm_ids"],
    }

    if len(core) < MIN_CORE_LENGTH:
        log.warning("  core %r too short to query safely, skipping", core)
        return {**result, "dso_owned": False, "decided_by": "core too short", "npi_locations": None}

    payload = fetch_org_records(core, refresh=refresh)
    footprint = measure_national_footprint(payload, core)
    dso_owned, decided_by = decide_dso_owned(footprint, raw_value, thresholds)

    if dso_owned or footprint["npi_locations"]:
        log.info(
            "  %-38s %3d locations, %2d states%s -> dso_owned=%s",
            core[:38],
            footprint["npi_locations"],
            footprint["npi_states"],
            " (capped)" if footprint["truncated"] else "        ",
            dso_owned,
        )
    return {**result, **footprint, "dso_owned": dso_owned, "decided_by": decided_by}


# --- entry point ----------------------------------------------------------


def write_dso_verdicts(verdicts: list[dict], thresholds: dict) -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "step": "dso_check",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "thresholds": {key: thresholds.get(key) for key in (*REQUIRED_THRESHOLDS, "require_both")},
        "count": len(verdicts),
        "dso_count": sum(1 for verdict in verdicts if verdict["dso_owned"]),
        "chains": verdicts,
    }
    OUTPUT_PATH.write_text(json.dumps(document, indent=2))
    log.info("Wrote %d chain verdicts to %s", len(verdicts), OUTPUT_PATH.relative_to(REPO_ROOT))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Flag DSO owned chains via NPPES.")
    parser.add_argument("--refresh", action="store_true", help="Ignore the cache and re-query")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

    try:
        thresholds = load_dso_thresholds()
        if not DISCOVER_PATH.exists():
            raise ConfigError("data/discover.json not found. Run src/discover.py first.")

        practices = json.loads(DISCOVER_PATH.read_text())["practices"]
        candidates = chain_candidates_in_market(practices)
        log.info("Checking %d distinct chain names across %d practices", len(candidates), len(practices))

        if thresholds["npi_location_floor"] > PAGE_LIMIT:
            log.warning(
                "npi_location_floor %d exceeds the %d result cap, so it can never trip",
                thresholds["npi_location_floor"],
                PAGE_LIMIT,
            )

        assert_nppes_reachable()

        verdicts = []
        for raw_value, entry in sorted(candidates.items()):
            try:
                verdicts.append(check_chain_name(raw_value, entry, thresholds, args.refresh))
            except Exception as error:  # one bad lookup never kills the run
                log.warning("  skipping %r: %s", raw_value, error)
                verdicts.append(
                    {
                        "chain_name": raw_value,
                        "normalised_core": normalise_org_name(raw_value),
                        "source_fields": sorted(entry["source_fields"]),
                        "practice_count_in_market": len(entry["osm_ids"]),
                        "osm_ids": entry["osm_ids"],
                        "dso_owned": False,
                        "decided_by": f"lookup failed: {error}",
                        "npi_locations": None,
                    }
                )

        write_dso_verdicts(verdicts, thresholds)
    except ConfigError as error:
        log.error("%s", error)
        return 2
    except Exception as error:
        log.error("DSO check failed: %s", error)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
