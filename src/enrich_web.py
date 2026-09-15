"""Fetch each practice site and extract the buying signals in icp.yaml.

Builds the addressable audience once, here, at the top of the enrichment chain:
practices that have a website and are not DSO owned. Later steps inherit that
audience by reading this step's output rather than re-deriving it.

    python src/enrich_web.py
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
from urllib.parse import urljoin, urlparse

import requests
import yaml
from bs4 import BeautifulSoup

REPO_ROOT = Path(__file__).resolve().parent.parent
ICP_CONFIG = REPO_ROOT / "config" / "icp.yaml"
DISCOVER_PATH = REPO_ROOT / "data" / "discover.json"
DSO_PATH = REPO_ROOT / "data" / "dso_check.json"
CACHE_DIR = REPO_ROOT / "data" / "cache" / "web"
OUTPUT_PATH = REPO_ROOT / "data" / "enrich_web.json"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36 signal-scanner/0.1"
)
HTTP_TIMEOUT_SECONDS = 20
DELAY_BETWEEN_CALLS_SECONDS = 1.0
MAX_SUBPAGES_PER_SITE = 3
MAX_PAGE_BYTES = 2_000_000

# Internal pages worth following, most valuable first.
SUBPAGE_HINTS = (
    ("careers", re.compile(r"career|job|employment|hiring|join.?our.?team", re.I)),
    ("contact", re.compile(r"contact|reach.?us|get.?in.?touch", re.I)),
    ("locations", re.compile(r"location|our.?office|find.?us", re.I)),
    ("about", re.compile(r"about|our.?team|meet.?the.?(doctor|dentist|team)", re.I)),
)

# Real online booking: a scheduling vendor is embedded or linked.
BOOKING_VENDORS = re.compile(
    r"nexhealth|localmed|zocdoc|flexbooker|yapi|solutionreach|doctible|podium|"
    r"denticon|dentrixascend|revenuewell|lighthouse360|patientpop|dentalintel|"
    r"smilesnap|simplifeye|birdeye|curvedental|sesamecommunications|tebra|"
    r"getweave|swellcx|appointmentplus|setmore|calendly|acuityscheduling",
    re.I,
)
# Explicit self-serve scheduling language.
BOOKING_STRONG = re.compile(
    r"book\s+(online|now|an?\s+appointment)|schedule\s+(online|now|an?\s+appointment)|"
    r"online\s+(booking|scheduling)|self.?schedule",
    re.I,
)
# A form that emails the office is not online booking, but is worth recording.
BOOKING_WEAK = re.compile(r"request\s+an?\s+appointment|appointment\s+request", re.I)

HIRING_ROLES = re.compile(
    r"front\s+desk|receptionist|patient\s+coordinator|scheduling\s+coordinator|"
    r"front\s+office|treatment\s+coordinator|patient\s+care\s+coordinator",
    re.I,
)
HIRING_CONTEXT = re.compile(r"hiring|now\s+hiring|join\s+our\s+team|apply|open\s+position|career", re.I)

ACCEPTING_PATIENTS = re.compile(
    r"accepting\s+new\s+patients|new\s+patients\s+(are\s+)?welcome|"
    r"now\s+accepting\s+patients|welcoming\s+new\s+patients",
    re.I,
)
MULTI_LOCATION = re.compile(
    r"our\s+locations|all\s+locations|choose\s+a\s+location|multiple\s+locations|"
    r"other\s+locations|view\s+locations",
    re.I,
)

EMAIL_PATTERN = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
# Images and tracking pixels masquerading as emails.
EMAIL_NOISE = re.compile(r"\.(png|jpe?g|gif|svg|webp|css|js)$", re.I)

log = logging.getLogger("enrich_web")


# --- audience -------------------------------------------------------------


def build_addressable_audience(practices: list[dict], dso_verdicts: list[dict], require_website: bool) -> list[dict]:
    """Practices we can actually run a campaign against.

    require_website is a hard filter, not a weight: the channel is email, so no
    domain means there is nothing to send to.
    """
    dso_osm_ids = {osm_id for verdict in dso_verdicts if verdict["dso_owned"] for osm_id in verdict["osm_ids"]}

    audience, no_site, dso_owned = [], 0, 0
    for practice in practices:
        if require_website and not practice.get("website"):
            no_site += 1
            continue
        if practice["osm_id"] in dso_osm_ids:
            dso_owned += 1
            continue
        audience.append(practice)

    log.info(
        "Audience %d of %d practices, dropped %d with no website and %d DSO owned",
        len(audience), len(practices), no_site, dso_owned,
    )
    return audience


# --- fetching -------------------------------------------------------------


def cache_path_for_url(url: str) -> Path:
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    return CACHE_DIR / f"page_{digest}.json"


def fetch_page(url: str, refresh: bool = False) -> dict | None:
    """Return {url, status, html} from cache or the network. None on failure."""
    cache_path = cache_path_for_url(url)
    if cache_path.exists() and not refresh:
        return json.loads(cache_path.read_text())

    response = requests.get(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
        timeout=HTTP_TIMEOUT_SECONDS,
        allow_redirects=True,
    )
    content_type = response.headers.get("content-type", "")
    page = {
        "url": response.url,
        "status": response.status_code,
        "html": response.text[:MAX_PAGE_BYTES] if "html" in content_type else "",
    }
    response.raise_for_status()

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(page))
    time.sleep(DELAY_BETWEEN_CALLS_SECONDS)
    return page


def normalise_site_url(website: str) -> str:
    website = website.strip()
    if not website.startswith(("http://", "https://")):
        website = f"https://{website}"
    return website


def choose_subpages(homepage_url: str, html: str) -> list[str]:
    """Pick the few internal links most likely to carry signals."""
    soup = BeautifulSoup(html, "html.parser")
    home_host = urlparse(homepage_url).netloc.lower()
    # Nav links often point back at the homepage, which we already have.
    seen = {homepage_url.rstrip("/")}
    chosen: dict[str, str] = {}

    for anchor in soup.find_all("a", href=True):
        target = urljoin(homepage_url, anchor["href"]).split("#")[0]
        if urlparse(target).netloc.lower() != home_host:
            continue
        if target.rstrip("/") in seen:
            continue
        haystack = f"{anchor['href']} {anchor.get_text(' ', strip=True)}"
        for kind, pattern in SUBPAGE_HINTS:
            if kind not in chosen and pattern.search(haystack):
                chosen[kind] = target
                seen.add(target.rstrip("/"))
                break
        if len(chosen) >= MAX_SUBPAGES_PER_SITE:
            break

    return list(chosen.values())[:MAX_SUBPAGES_PER_SITE]


# --- signal extraction ----------------------------------------------------


def page_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return soup.get_text(" ", strip=True)


def harvest_emails(html: str) -> list[str]:
    """mailto: links and inline addresses, for verify.py to sift later."""
    found = set()
    soup = BeautifulSoup(html, "html.parser")
    for anchor in soup.select('a[href^="mailto:"]'):
        address = anchor["href"][7:].split("?")[0].strip().lower()
        if address:
            found.add(address)
    for match in EMAIL_PATTERN.findall(html):
        address = match.lower()
        if not EMAIL_NOISE.search(address):
            found.add(address)
    return sorted(found)


def read_booking_signal(pages: list[dict]) -> dict:
    """Distinguish a real scheduling widget from a contact form.

    no_online_booking is worth 3 points because booking runs through the phone,
    which is the thing being replaced. A form that emails the office still means
    the phone does the work, so it does not count as online booking.
    """
    vendor = strong = weak = None

    def first_hit(pattern, haystack):
        match = pattern.search(haystack)
        return match.group(0) if match else None

    for page in pages:
        # Vendor widgets hide in script src and iframe URLs, so search raw html.
        vendor = vendor or first_hit(BOOKING_VENDORS, f"{page['url']} {page['html']}")
        strong = strong or first_hit(BOOKING_STRONG, page["text"])
        weak = weak or first_hit(BOOKING_WEAK, page["text"])

    has_booking = bool(vendor or strong)
    return {
        "online_booking": has_booking,
        "no_online_booking": not has_booking,
        "booking_evidence": {"vendor": vendor, "phrase": strong, "request_form_only": weak if not has_booking else None},
    }


def read_hiring_signal(pages: list[dict]) -> dict:
    """A front desk role being advertised means they already pay to solve this."""
    for page in pages:
        text = page["text"]
        role = HIRING_ROLES.search(text)
        if role and HIRING_CONTEXT.search(text):
            return {"hiring_reception": True, "hiring_evidence": role.group(0), "hiring_page": page["url"]}
    return {"hiring_reception": False, "hiring_evidence": None, "hiring_page": None}


def read_signal(pages: list[dict], pattern: re.Pattern, key: str) -> dict:
    for page in pages:
        hit = pattern.search(page["text"])
        if hit:
            return {key: True, f"{key}_evidence": hit.group(0)}
    return {key: False, f"{key}_evidence": None}


def enrich_practice(practice: dict, refresh: bool) -> dict:
    """Fetch the site, read every signal off it. Never raises."""
    enriched = dict(practice)
    homepage_url = normalise_site_url(practice["website"])

    home = fetch_page(homepage_url, refresh=refresh)
    pages = [{**home, "text": page_text(home["html"])}]

    for subpage_url in choose_subpages(home["url"], home["html"]):
        try:
            subpage = fetch_page(subpage_url, refresh=refresh)
            pages.append({**subpage, "text": page_text(subpage["html"])})
        except Exception as error:  # a dead subpage never kills the practice
            log.debug("  subpage failed %s: %s", subpage_url, error)

    emails = sorted({address for page in pages for address in harvest_emails(page["html"])})

    enriched.update(read_booking_signal(pages))
    enriched.update(read_hiring_signal(pages))
    enriched.update(read_signal(pages, ACCEPTING_PATIENTS, "accepting_new_patients"))
    enriched.update(read_signal(pages, MULTI_LOCATION, "multiple_locations"))
    enriched.update(
        {
            "site_url": home["url"],
            "site_reachable": True,
            "pages_fetched": [page["url"] for page in pages],
            "emails_found": emails,
            "fetch_error": None,
        }
    )
    return enriched


def unreachable_practice(practice: dict, error: Exception) -> dict:
    """Keep the row, mark it, let score.py decide. Never drop silently."""
    return {
        **practice,
        "site_url": practice.get("website"),
        "site_reachable": False,
        "pages_fetched": [],
        "emails_found": [],
        "online_booking": None,
        "no_online_booking": None,
        "hiring_reception": None,
        "accepting_new_patients": None,
        "multiple_locations": None,
        "fetch_error": str(error)[:200],
    }


# --- entry point ----------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch practice sites and extract ICP signals.")
    parser.add_argument("--limit", type=int, help="Process only the first N practices")
    parser.add_argument("--refresh", action="store_true", help="Ignore the cache and refetch")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

    try:
        for required in (DISCOVER_PATH, DSO_PATH):
            if not required.exists():
                log.error("%s not found. Run the earlier steps first.", required.relative_to(REPO_ROOT))
                return 2

        icp = yaml.safe_load(ICP_CONFIG.read_text()) or {}
        require_website = (icp.get("filters") or {}).get("require_website", True)

        practices = json.loads(DISCOVER_PATH.read_text())["practices"]
        dso_verdicts = json.loads(DSO_PATH.read_text())["chains"]
        audience = build_addressable_audience(practices, dso_verdicts, require_website)

        if args.limit:
            audience = audience[: args.limit]

        enriched, unreachable = [], 0
        for index, practice in enumerate(audience, start=1):
            try:
                row = enrich_practice(practice, args.refresh)
                fired = [k for k in ("no_online_booking", "hiring_reception", "accepting_new_patients", "multiple_locations") if row.get(k)]
                log.info("  [%d/%d] %-42s %s", index, len(audience), practice["name"][:42], ", ".join(fired) or "-")
            except Exception as error:  # one dead site never kills the run
                unreachable += 1
                row = unreachable_practice(practice, error)
                log.warning("  [%d/%d] %-42s unreachable: %s", index, len(audience), practice["name"][:42], str(error)[:60])
            enriched.append(row)

        OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT_PATH.write_text(
            json.dumps(
                {
                    "step": "enrich_web",
                    "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                    "count": len(enriched),
                    "unreachable": unreachable,
                    "practices": enriched,
                },
                indent=2,
            )
        )
        log.info("Wrote %d practices to %s, %d unreachable", len(enriched), OUTPUT_PATH.relative_to(REPO_ROOT), unreachable)
    except Exception as error:
        log.error("Web enrichment failed: %s", error)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
