"""Match each practice to the NPPES NPI Registry and count its providers.

Overpass and NPPES describe the same businesses with no shared identifier, so
matching is fuzzy on name and address together against a confidence floor from
icp.yaml. Below the floor the NPI fields stay blank. We never guess a match.

Queries are keyed by postcode rather than city: Phoenix has over two thousand
dentists and city-wide paging runs past skip=1800, while a postcode returns a
small set that covers every practice in it.

    python src/enrich_npi.py
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import re
import sys
import time
from pathlib import Path

import requests
import yaml
from rapidfuzz import fuzz

REPO_ROOT = Path(__file__).resolve().parent.parent
ICP_CONFIG = REPO_ROOT / "config" / "icp.yaml"
INPUT_PATH = REPO_ROOT / "data" / "enrich_web.json"
CACHE_DIR = REPO_ROOT / "data" / "cache"
OUTPUT_PATH = REPO_ROOT / "data" / "enrich_npi.json"

NPPES_ENDPOINT = "https://npiregistry.cms.hhs.gov/api/"
NPPES_VERSION = "2.1"
USER_AGENT = "signal-scanner/0.1 (+https://github.com/danishtauseef71/signal-scanner)"
HTTP_TIMEOUT_SECONDS = 45
DELAY_BETWEEN_CALLS_SECONDS = 1.0
PAGE_LIMIT = 200
MAX_PAGES_PER_POSTCODE = 5

# Must follow a state code. A bare 5-digit match grabs the street house
# number ("11851 N 28th Dr") and sends the query to an unrelated postcode.
POSTCODE_PATTERN = re.compile(r"\b[A-Z]{2}\s+(\d{5})(?:-\d{4})?\s*$")
STREET_NOISE = re.compile(r"\b(ste|suite|unit|apt|#|bldg|floor|fl)\b.*$", re.I)
NON_ALNUM = re.compile(r"[^a-z0-9]+")

# Name alone is not enough: "Smile Dental" appears everywhere. Address alone is
# not enough either: several practices share a medical building. Weighted so a
# strong address match cannot carry a completely different practice name.
NAME_WEIGHT = 0.6
ADDRESS_WEIGHT = 0.4

log = logging.getLogger("enrich_npi")


# --- normalisation --------------------------------------------------------


def normalise_for_match(value: str) -> str:
    return NON_ALNUM.sub(" ", (value or "").lower()).strip()


def street_line(address: str) -> str:
    """First line of an address, with suite noise removed."""
    first = (address or "").split(",")[0]
    return normalise_for_match(STREET_NOISE.sub("", first))


def postcode_of(address: str) -> str | None:
    """Postcode only when the address actually carries one."""
    match = POSTCODE_PATTERN.search((address or "").strip())
    return match.group(1) if match else None


# --- NPPES ----------------------------------------------------------------


def cache_path_for_postcode(postcode: str, enumeration_type: str) -> Path:
    return CACHE_DIR / f"npi_zip_{postcode}_{enumeration_type.lower().replace('-', '')}.json"


def fetch_providers_in_postcode(postcode: str, enumeration_type: str, refresh: bool = False) -> list[dict]:
    """Every dentist record in one postcode, paged and cached as a single file."""
    cache_path = cache_path_for_postcode(postcode, enumeration_type)
    if cache_path.exists() and not refresh:
        return json.loads(cache_path.read_text())["results"]

    collected: list[dict] = []
    for page in range(MAX_PAGES_PER_POSTCODE):
        response = requests.get(
            NPPES_ENDPOINT,
            params={
                "version": NPPES_VERSION,
                "postal_code": postcode,
                "country_code": "US",
                "taxonomy_description": "Dent*",
                "enumeration_type": enumeration_type,
                "limit": PAGE_LIMIT,
                "skip": page * PAGE_LIMIT,
            },
            headers={"User-Agent": USER_AGENT},
            timeout=HTTP_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("Errors"):
            raise RuntimeError(f"NPPES rejected postcode {postcode}: {payload['Errors']}")

        batch = payload.get("results") or []
        collected.extend(batch)
        time.sleep(DELAY_BETWEEN_CALLS_SECONDS)
        if len(batch) < PAGE_LIMIT:
            break

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps({"postcode": postcode, "results": collected}, indent=2))
    return collected


def provider_location(record: dict) -> dict:
    for address in record.get("addresses") or []:
        if address.get("address_purpose") == "LOCATION":
            return address
    return {}


def provider_display_name(record: dict) -> str:
    basic = record.get("basic") or {}
    if basic.get("organization_name"):
        return basic["organization_name"]
    return " ".join(part for part in (basic.get("first_name"), basic.get("last_name")) if part)


# --- matching -------------------------------------------------------------


def match_confidence(practice: dict, record: dict) -> float:
    """Blend name and address similarity into one score out of 100."""
    location = provider_location(record)
    npi_street = normalise_for_match(STREET_NOISE.sub("", location.get("address_1", "")))
    practice_street = street_line(practice.get("address") or "")

    name_score = fuzz.token_set_ratio(
        normalise_for_match(practice.get("name", "")), normalise_for_match(provider_display_name(record))
    )
    # With no address on either side, address similarity is unknown, not zero.
    if not npi_street or not practice_street:
        return name_score * NAME_WEIGHT

    address_score = fuzz.token_set_ratio(practice_street, npi_street)
    return name_score * NAME_WEIGHT + address_score * ADDRESS_WEIGHT


def find_matching_providers(practice: dict, candidates: list[dict], floor: float) -> list[dict]:
    """Every provider at this practice scoring at or above the floor."""
    matched = []
    for record in candidates:
        confidence = match_confidence(practice, record)
        if confidence >= floor:
            matched.append({"record": record, "confidence": round(confidence, 1)})
    return sorted(matched, key=lambda m: -m["confidence"])


def count_providers_at_practice(practice: dict, individuals: list[dict], floor: float) -> list[dict]:
    """Individual dentists whose listed address matches this practice.

    Falls back to address-only agreement, because an individual dentist is
    enumerated under their own name, not the practice's, so name similarity
    would wrongly reject every provider at a practice named after nobody.
    """
    practice_street = street_line(practice.get("address") or "")
    if not practice_street:
        return []

    at_address = []
    for record in individuals:
        npi_street = normalise_for_match(STREET_NOISE.sub("", provider_location(record).get("address_1", "")))
        if not npi_street:
            continue
        if fuzz.token_set_ratio(practice_street, npi_street) >= floor:
            at_address.append(record)
    return at_address


# --- enrichment -----------------------------------------------------------


def blank_npi_fields(reason: str) -> dict:
    """Below the floor or unmatchable. Leave it blank, never guess."""
    return {
        "npi": None,
        "npi_match_confidence": None,
        "npi_organization_name": None,
        "npi_enumeration_date": None,
        "npi_taxonomy": None,
        "provider_count": None,
        "three_plus_providers": None,
        "solo_provider": None,
        "npi_note": reason,
    }


def enrich_practice_with_npi(practice: dict, floor: float, refresh: bool) -> dict:
    enriched = dict(practice)
    postcode = postcode_of(practice.get("address") or "")

    if not postcode:
        return {**enriched, **blank_npi_fields("no postcode on the practice address")}

    organisations = fetch_providers_in_postcode(postcode, "NPI-2", refresh=refresh)
    individuals = fetch_providers_in_postcode(postcode, "NPI-1", refresh=refresh)

    org_matches = find_matching_providers(practice, organisations, floor)
    providers = count_providers_at_practice(practice, individuals, floor)
    provider_count = len(providers) or None

    if not org_matches:
        blank = blank_npi_fields(f"no organisation above the confidence floor of {floor:g}")
        # Provider count comes from address agreement, so it can stand alone.
        if provider_count:
            blank.update(
                {
                    "provider_count": provider_count,
                    "three_plus_providers": provider_count >= 3,
                    "solo_provider": provider_count == 1,
                    "npi_note": blank["npi_note"] + ", provider count from address match only",
                }
            )
        return {**enriched, **blank}

    best = org_matches[0]
    basic = best["record"].get("basic") or {}
    taxonomies = best["record"].get("taxonomies") or []
    primary = next((t for t in taxonomies if t.get("primary")), taxonomies[0] if taxonomies else {})

    return {
        **enriched,
        "npi": best["record"].get("number"),
        "npi_match_confidence": best["confidence"],
        "npi_organization_name": basic.get("organization_name"),
        "npi_enumeration_date": basic.get("enumeration_date"),
        "npi_taxonomy": primary.get("desc"),
        "provider_count": provider_count,
        "three_plus_providers": (provider_count >= 3) if provider_count else None,
        "solo_provider": (provider_count == 1) if provider_count else None,
        "npi_note": None,
    }


# --- entry point ----------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Match practices to the NPPES NPI Registry.")
    parser.add_argument("--limit", type=int, help="Process only the first N practices")
    parser.add_argument("--refresh", action="store_true", help="Ignore the cache and re-query")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

    try:
        if not INPUT_PATH.exists():
            log.error("%s not found. Run src/enrich_web.py first.", INPUT_PATH.relative_to(REPO_ROOT))
            return 2

        icp = yaml.safe_load(ICP_CONFIG.read_text()) or {}
        floor = float((icp.get("matching") or {}).get("npi_confidence_floor", 85))

        practices = json.loads(INPUT_PATH.read_text())["practices"]
        if args.limit:
            practices = practices[: args.limit]

        postcodes = {postcode_of(p.get("address") or "") for p in practices} - {None}
        log.info("Matching %d practices across %d postcodes, floor %g", len(practices), len(postcodes), floor)

        enriched, matched, blanked = [], 0, 0
        for index, practice in enumerate(practices, start=1):
            try:
                row = enrich_practice_with_npi(practice, floor, args.refresh)
            except Exception as error:  # one bad postcode never kills the run
                log.warning("  [%d/%d] %-38s lookup failed: %s", index, len(practices), practice["name"][:38], str(error)[:60])
                row = {**practice, **blank_npi_fields(f"lookup failed: {str(error)[:120]}")}

            if row.get("npi"):
                matched += 1
                log.info("  [%d/%d] %-38s NPI %s  conf %.0f  providers %s", index, len(practices),
                         practice["name"][:38], row["npi"], row["npi_match_confidence"], row.get("provider_count") or "?")
            else:
                blanked += 1
                log.info("  [%d/%d] %-38s blank: %s", index, len(practices), practice["name"][:38], row["npi_note"][:52])
            enriched.append(row)

        OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT_PATH.write_text(
            json.dumps(
                {
                    "step": "enrich_npi",
                    "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                    "confidence_floor": floor,
                    "count": len(enriched),
                    "matched": matched,
                    "blank": blanked,
                    "practices": enriched,
                },
                indent=2,
            )
        )
        log.info("Wrote %d practices to %s, %d matched, %d blank", len(enriched), OUTPUT_PATH.relative_to(REPO_ROOT), matched, blanked)
    except Exception as error:
        log.error("NPI enrichment failed: %s", error)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
