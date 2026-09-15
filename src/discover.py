"""Step 1 of the pipeline: discover dental practices from OpenStreetMap.

Queries the Overpass API for a city bounding box and writes the practice
universe to data/discover.json. The raw Overpass response is cached under
data/cache/ on first success, so re-runs read disk instead of the network.

    python src/discover.py --city phoenix --limit 50
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import re
import sys
import time
from pathlib import Path

import requests
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CITIES_CONFIG = REPO_ROOT / "config" / "cities.yaml"
CACHE_DIR = REPO_ROOT / "data" / "cache"
OUTPUT_PATH = REPO_ROOT / "data" / "discover.json"

OVERPASS_ENDPOINT = "https://overpass-api.de/api/interpreter"
# Overpass and NPPES etiquette ask for a reachable contact. The repo URL is it.
USER_AGENT = "signal-scanner/0.1 (+https://github.com/danishtauseef71/signal-scanner)"
QUERY_TIMEOUT_SECONDS = 60  # server side, declared inside the query
HTTP_TIMEOUT_SECONDS = 90  # client side, must outlast the server timeout
RETRY_STATUSES = {429, 502, 503, 504}
RETRY_BACKOFF_SECONDS = 5

# OSM tags the same fact several ways. Read in order, first non-empty wins.
NAME_TAGS = ("name", "official_name")
WEBSITE_TAGS = ("website", "contact:website", "url")
PHONE_TAGS = ("phone", "contact:phone", "telephone")

# Fields that count towards "more complete" when deduplicating.
POPULATED_FIELDS = ("address", "website", "phone", "operator", "brand", "lat", "lon")

# A suite number is often on the POI node but not the building way. Ignore it
# when matching, or the same practice keys two different ways.
UNIT_SUFFIX = re.compile(r"\b(ste|suite|unit|apt|#)\b.*$", re.IGNORECASE)

log = logging.getLogger("discover")


# --- geography ------------------------------------------------------------


def load_cities_config() -> dict:
    if not CITIES_CONFIG.exists():
        raise FileNotFoundError(f"missing {CITIES_CONFIG.relative_to(REPO_ROOT)}")
    return yaml.safe_load(CITIES_CONFIG.read_text()) or {}


def bbox_for_city(city: str | None) -> tuple[str, tuple[float, float, float, float]]:
    """Resolve a city name to its bounding box, defaulting per the config."""
    config = load_cities_config()
    cities = config.get("cities") or {}
    name = city or config.get("default_city")

    if name not in cities:
        available = ", ".join(sorted(cities)) or "none configured"
        raise KeyError(f"unknown city {name!r}. Available: {available}")

    bbox = cities[name].get("bbox")
    if not bbox or len(bbox) != 4:
        raise ValueError(f"city {name!r} needs a bbox of [south, west, north, east]")

    return name, tuple(float(value) for value in bbox)


# --- the query ------------------------------------------------------------


def build_dental_practice_query(bbox: tuple[float, float, float, float]) -> str:
    """Union of both dentist tagging schemes. Overpass dedupes co-tagged elements.

    nwr covers nodes, ways and relations, since plenty of practices are tagged
    on the building rather than a POI node. `out center` collapses each way and
    relation to a single coordinate instead of returning full geometry.
    """
    south, west, north, east = bbox
    area = f"{south},{west},{north},{east}"
    return "\n".join(
        [
            f"[out:json][timeout:{QUERY_TIMEOUT_SECONDS}];",
            "(",
            f'  nwr["amenity"="dentist"]({area});',
            f'  nwr["healthcare"="dentist"]({area});',
            ");",
            "out center;",
        ]
    )


# --- fetching and caching -------------------------------------------------


def cache_path_for_query(query: str) -> Path:
    """Key the cache on the query itself, so a changed bbox never reads a stale file."""
    digest = hashlib.sha1(query.encode("utf-8")).hexdigest()[:12]
    return CACHE_DIR / f"overpass_{digest}.json"


def request_overpass(query: str) -> dict:
    last_status = None
    for attempt in (1, 2):
        response = requests.post(
            OVERPASS_ENDPOINT,
            data={"data": query},
            headers={"User-Agent": USER_AGENT},
            timeout=HTTP_TIMEOUT_SECONDS,
        )
        last_status = response.status_code
        if response.status_code in RETRY_STATUSES and attempt == 1:
            log.warning(
                "Overpass returned %s, retrying in %ss", last_status, RETRY_BACKOFF_SECONDS
            )
            time.sleep(RETRY_BACKOFF_SECONDS)
            continue
        response.raise_for_status()
        return response.json()

    raise RuntimeError(f"Overpass kept returning {last_status}")


def fetch_dental_practices(query: str, refresh: bool = False) -> dict:
    """Return the raw Overpass payload, from cache when we already have it."""
    cache_path = cache_path_for_query(query)

    if cache_path.exists() and not refresh:
        log.info("Cache hit, reading %s", cache_path.relative_to(REPO_ROOT))
        return json.loads(cache_path.read_text())

    log.info("Querying Overpass at %s", OVERPASS_ENDPOINT)
    payload = request_overpass(query)

    # Cache on success only, so a failed run never poisons the next one.
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(payload, indent=2))
    log.info("Cached raw response to %s", cache_path.relative_to(REPO_ROOT))
    return payload


# --- parsing --------------------------------------------------------------


def first_tag(tags: dict, keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = (tags.get(key) or "").strip()
        if value:
            return value
    return None


def format_practice_address(tags: dict) -> str | None:
    """Assemble addr:* into one line: 123 N Main St Ste 4, Phoenix, AZ 85004."""
    street = " ".join(
        part for part in (tags.get("addr:housenumber"), tags.get("addr:street")) if part
    )
    unit = tags.get("addr:unit")
    if street and unit:
        street = f"{street} Ste {unit}"

    region = " ".join(part for part in (tags.get("addr:state"), tags.get("addr:postcode")) if part)
    parts = [part for part in (street, tags.get("addr:city"), region) if part]
    return ", ".join(parts) or None


def practice_from_osm_element(element: dict) -> dict | None:
    """One OSM element to one practice record. None when it has no usable name."""
    tags = element.get("tags") or {}
    name = first_tag(tags, NAME_TAGS)
    if not name:
        return None

    center = element.get("center") or {}
    return {
        "osm_id": f"{element['type']}/{element['id']}",
        "name": name,
        "address": format_practice_address(tags),
        "website": first_tag(tags, WEBSITE_TAGS),
        # Verbatim from OSM. export.py normalises for the sequencer.
        "phone": first_tag(tags, PHONE_TAGS),
        "operator": first_tag(tags, ("operator",)),
        "brand": first_tag(tags, ("brand",)),
        "lat": element.get("lat", center.get("lat")),
        "lon": element.get("lon", center.get("lon")),
    }


def practices_from_overpass_payload(payload: dict) -> list[dict]:
    practices, unnamed, unparseable = [], 0, 0

    for element in payload.get("elements") or []:
        try:
            practice = practice_from_osm_element(element)
        except Exception as error:  # one bad element never kills the run
            unparseable += 1
            log.warning("Skipping element %s: %s", element.get("id", "?"), error)
            continue

        if practice is None:
            unnamed += 1
            continue
        practices.append(practice)

    log.info(
        "Parsed %d practices, skipped %d unnamed and %d unparseable",
        len(practices),
        unnamed,
        unparseable,
    )
    return practices


# --- deduplication --------------------------------------------------------


def practice_dedupe_key(practice: dict):
    """Normalised name plus street line.

    Practices with no street address key on their osm_id instead, so two
    same-named chain locations with no address are never merged into one.
    """
    address = practice.get("address")
    if not address:
        return ("osm", practice["osm_id"])

    name = re.sub(r"[^a-z0-9]+", " ", practice["name"].lower()).strip()
    street = UNIT_SUFFIX.sub("", address.split(",")[0])
    street = re.sub(r"[^a-z0-9]+", " ", street.lower()).strip()
    return (name, street)


def populated_field_count(practice: dict) -> int:
    return sum(1 for field in POPULATED_FIELDS if practice.get(field))


def outranks(candidate: dict, incumbent: dict) -> bool:
    """More populated wins. On a tie, the one with a website wins."""
    candidate_fields = populated_field_count(candidate)
    incumbent_fields = populated_field_count(incumbent)
    if candidate_fields != incumbent_fields:
        return candidate_fields > incumbent_fields
    return bool(candidate.get("website")) and not incumbent.get("website")


def drop_duplicate_practices(practices: list[dict]) -> list[dict]:
    """Collapse the same practice tagged as both a POI node and a building way."""
    best: dict = {}
    for practice in practices:
        key = practice_dedupe_key(practice)
        incumbent = best.get(key)
        if incumbent is None or outranks(practice, incumbent):
            best[key] = practice

    dropped = len(practices) - len(best)
    if dropped:
        log.info("Dropped %d duplicate practices", dropped)
    return list(best.values())


# --- output ---------------------------------------------------------------


def write_discovered_practices(practices: list[dict], city: str, bbox: tuple) -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "step": "discover",
        "city": city,
        "bbox": list(bbox),
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "count": len(practices),
        "practices": practices,
    }
    OUTPUT_PATH.write_text(json.dumps(document, indent=2))
    log.info("Wrote %d practices to %s", len(practices), OUTPUT_PATH.relative_to(REPO_ROOT))


# --- entry point ----------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Discover dental practices via Overpass.")
    parser.add_argument("--city", help="City key from config/cities.yaml")
    parser.add_argument("--limit", type=int, help="Keep only the first N practices")
    parser.add_argument("--refresh", action="store_true", help="Ignore the cache and re-query")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

    try:
        city, bbox = bbox_for_city(args.city)
        query = build_dental_practice_query(bbox)
        log.info("Discovering dental practices in %s %s", city, bbox)

        payload = fetch_dental_practices(query, refresh=args.refresh)
        practices = drop_duplicate_practices(practices_from_overpass_payload(payload))
        practices.sort(key=lambda practice: practice["name"].lower())

        if args.limit:
            practices = practices[: args.limit]
            log.info("Limited to %d practices", len(practices))

        write_discovered_practices(practices, city, bbox)
    except Exception as error:
        log.error("Discovery failed: %s", error)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
