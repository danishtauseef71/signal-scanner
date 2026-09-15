"""Check that each practice domain can actually receive email.

The campaign channel is email, so a domain with no MX is a dead account no
matter how well it scores. This step resolves MX per domain, flags free mail
providers, and sifts the addresses enrich_web harvested into role and personal
buckets.

Catch-all detection is deliberately not attempted. Establishing it requires
opening SMTP conversations with third party mail servers and probing addresses
we know do not exist, which is intrusive, gets sending IPs blocklisted, and is
not something to do unasked. catch_all is recorded as null with a reason.

    python src/verify.py
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
from pathlib import Path
from urllib.parse import urlparse

import dns.exception
import dns.resolver

REPO_ROOT = Path(__file__).resolve().parent.parent
INPUT_PATH = REPO_ROOT / "data" / "enrich_npi.json"
CACHE_PATH = REPO_ROOT / "data" / "cache" / "mx_lookups.json"
OUTPUT_PATH = REPO_ROOT / "data" / "verify.json"

DNS_TIMEOUT_SECONDS = 10

# Mail on one of these means the practice has no domain mailbox of its own.
FREE_MAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "hotmail.com",
    "outlook.com", "live.com", "msn.com", "aol.com", "icloud.com", "me.com",
    "mac.com", "comcast.net", "cox.net", "sbcglobal.net", "att.net", "verizon.net",
    "protonmail.com", "proton.me", "gmx.com", "mail.com", "qwestoffice.net",
}

# Shared mailboxes. Deliverable, but not a person, so sequencers treat them
# differently and they should never be personalised as if they were a contact.
ROLE_LOCAL_PARTS = {
    "info", "office", "admin", "contact", "hello", "help", "support", "sales",
    "frontdesk", "front desk", "reception", "appointments", "appointment",
    "scheduling", "schedule", "billing", "accounts", "accounting", "team",
    "smile", "smiles", "dental", "care", "newpatients", "new patients",
    "patientcare", "mail", "email", "inquiries", "enquiries", "webmaster",
    "noreply", "no-reply", "donotreply", "marketing", "hr", "careers", "jobs",
}

# MX hostname fragments that identify who actually runs the mailbox.
MX_PROVIDERS = (
    ("Google Workspace", ("google.com", "googlemail.com", "aspmx.l.google")),
    ("Microsoft 365", ("outlook.com", "protection.outlook", "microsoft.com")),
    ("Proofpoint", ("pphosted.com", "ppe-hosted.com")),
    ("Mimecast", ("mimecast.com",)),
    ("Barracuda", ("barracudanetworks.com", "ess.barracuda")),
    ("Zoho", ("zoho.com", "zohomail.com")),
    ("GoDaddy", ("secureserver.net", "godaddy.com")),
    ("Rackspace", ("emailsrvr.com", "rackspace.com")),
    ("Cloudflare", ("mx.cloudflare.net",)),
    ("Intermedia", ("intermedia.net",)),
)

log = logging.getLogger("verify")


# --- domains --------------------------------------------------------------


def domain_from_website(website: str | None) -> str | None:
    """Bare registrable host, lowercased, www stripped."""
    if not website:
        return None
    candidate = website.strip()
    if not candidate.startswith(("http://", "https://")):
        candidate = f"https://{candidate}"
    host = (urlparse(candidate).netloc or "").lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    return host or None


def identify_mx_provider(mx_hosts: list[str]) -> str | None:
    blob = " ".join(mx_hosts).lower()
    for provider, fragments in MX_PROVIDERS:
        if any(fragment in blob for fragment in fragments):
            return provider
    return None


# --- DNS ------------------------------------------------------------------


def build_resolver() -> dns.resolver.Resolver:
    resolver = dns.resolver.Resolver()
    resolver.timeout = DNS_TIMEOUT_SECONDS
    resolver.lifetime = DNS_TIMEOUT_SECONDS
    return resolver


def lookup_mx(domain: str, resolver: dns.resolver.Resolver) -> dict:
    """Resolve MX, falling back to A, which some small hosts still rely on."""
    try:
        answers = resolver.resolve(domain, "MX")
        hosts = sorted(str(record.exchange).rstrip(".").lower() for record in answers)
        return {"has_mx": True, "mx_hosts": hosts, "mx_provider": identify_mx_provider(hosts), "dns_error": None}
    except dns.resolver.NoAnswer:
        # No MX but an A record means mail may still be accepted at the host.
        try:
            resolver.resolve(domain, "A")
            return {"has_mx": False, "mx_hosts": [], "mx_provider": None, "dns_error": "no MX, A record only"}
        except dns.exception.DNSException as error:
            return {"has_mx": False, "mx_hosts": [], "mx_provider": None, "dns_error": f"no MX or A: {type(error).__name__}"}
    except dns.resolver.NXDOMAIN:
        return {"has_mx": False, "mx_hosts": [], "mx_provider": None, "dns_error": "NXDOMAIN, domain does not exist"}
    except dns.exception.DNSException as error:
        return {"has_mx": False, "mx_hosts": [], "mx_provider": None, "dns_error": type(error).__name__}


def load_mx_cache() -> dict:
    if CACHE_PATH.exists():
        return json.loads(CACHE_PATH.read_text())
    return {}


def save_mx_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, indent=2, sort_keys=True))


# --- addresses ------------------------------------------------------------


def is_role_address(address: str) -> bool:
    return address.split("@")[0].strip().lower() in ROLE_LOCAL_PARTS


def sift_addresses(emails: list[str], domain: str | None) -> dict:
    """Split harvested addresses by whether we can actually sequence them."""
    on_domain, off_domain, free_mail = [], [], []
    for address in emails:
        address_domain = address.split("@")[-1].lower()
        if domain and address_domain == domain:
            on_domain.append(address)
        elif address_domain in FREE_MAIL_DOMAINS:
            free_mail.append(address)
        else:
            off_domain.append(address)

    role = [address for address in on_domain if is_role_address(address)]
    personal = [address for address in on_domain if not is_role_address(address)]
    return {
        "emails_on_domain": on_domain,
        "emails_role": role,
        "emails_personal": personal,
        "emails_free_mail": free_mail,
        "emails_other_domain": off_domain,
    }


# --- verification ---------------------------------------------------------


def verify_practice(practice: dict, cache: dict, resolver: dns.resolver.Resolver, refresh: bool) -> dict:
    domain = domain_from_website(practice.get("site_url") or practice.get("website"))

    if not domain:
        return {
            **practice, "domain": None, "has_mx": False, "mx_hosts": [], "mx_provider": None,
            "dns_error": "no domain on the practice record", "free_mail_domain": None,
            "catch_all": None, "catch_all_note": "not tested, requires SMTP probing",
            "deliverable": False, **sift_addresses(practice.get("emails_found") or [], None),
        }

    if domain in cache and not refresh:
        mx = cache[domain]
    else:
        mx = lookup_mx(domain, resolver)
        cache[domain] = mx

    addresses = sift_addresses(practice.get("emails_found") or [], domain)
    return {
        **practice,
        "domain": domain,
        **mx,
        "free_mail_domain": domain in FREE_MAIL_DOMAINS,
        "catch_all": None,
        "catch_all_note": "not tested, requires SMTP probing of third party servers",
        # Deliverable means the domain can receive mail at all. Whether a given
        # mailbox exists is a separate question this step does not answer.
        "deliverable": bool(mx["has_mx"]) and domain not in FREE_MAIL_DOMAINS,
        **addresses,
    }


# --- entry point ----------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MX verify each practice domain.")
    parser.add_argument("--limit", type=int, help="Process only the first N practices")
    parser.add_argument("--refresh", action="store_true", help="Ignore the cache and re-resolve")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

    try:
        if not INPUT_PATH.exists():
            log.error("%s not found. Run src/enrich_npi.py first.", INPUT_PATH.relative_to(REPO_ROOT))
            return 2

        practices = json.loads(INPUT_PATH.read_text())["practices"]
        if args.limit:
            practices = practices[: args.limit]

        cache = load_mx_cache()
        resolver = build_resolver()
        log.info("Verifying %d practices, %d domains already cached", len(practices), len(cache))

        verified, deliverable = [], 0
        for index, practice in enumerate(practices, start=1):
            try:
                row = verify_practice(practice, cache, resolver, args.refresh)
            except Exception as error:  # one bad domain never kills the run
                log.warning("  [%d/%d] %-34s failed: %s", index, len(practices), practice["name"][:34], str(error)[:60])
                row = {**practice, "domain": None, "has_mx": False, "deliverable": False, "dns_error": str(error)[:120]}

            if row.get("deliverable"):
                deliverable += 1
            log.info(
                "  [%d/%d] %-34s %-26s %s%s",
                index, len(practices), practice["name"][:34], (row.get("domain") or "-")[:26],
                (row.get("mx_provider") or ("MX ok" if row.get("has_mx") else row.get("dns_error") or "no MX")),
                f"  role:{len(row.get('emails_role') or [])} personal:{len(row.get('emails_personal') or [])}",
            )
            verified.append(row)

        save_mx_cache(cache)
        OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT_PATH.write_text(
            json.dumps(
                {
                    "step": "verify",
                    "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                    "count": len(verified),
                    "deliverable": deliverable,
                    "catch_all_note": "catch-all detection not attempted, requires SMTP probing",
                    "practices": verified,
                },
                indent=2,
            )
        )
        log.info("Wrote %d practices to %s, %d deliverable", len(verified), OUTPUT_PATH.relative_to(REPO_ROOT), deliverable)
    except Exception as error:
        log.error("Verification failed: %s", error)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
