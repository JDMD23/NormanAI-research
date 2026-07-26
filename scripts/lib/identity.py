"""Identity keys for candidate dedup.

Deliberately mirrors NormanAI-crm-core's `crm_identity_keys.py` normalisation so
a candidate this repo considers "new" is the same one intake considers new.
If crm-core's rules change, change these to match — divergence here shows up as
duplicate rows on JD's board.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

# Suffixes that carry no identity. Kept in sync with crm-core.
_NAME_NOISE = {
    "inc", "inc.", "llc", "l.l.c", "ltd", "ltd.", "limited", "corp", "corp.",
    "corporation", "co", "co.", "company", "the", "labs", "lab", "technologies",
    "technology", "tech", "holdings", "group", "sa", "ag", "gmbh", "bv", "plc",
    "ai",
}

_WWW = re.compile(r"^www\d*\.")
_NON_ALNUM = re.compile(r"[^a-z0-9\s]+")
_WS = re.compile(r"\s+")


def normalize_domain(url: str) -> str:
    """https://www.Acme.com/careers?x=1 -> acme.com. Empty string when unusable."""
    raw = (url or "").strip()
    if not raw:
        return ""
    if "//" not in raw:
        raw = "https://" + raw
    try:
        host = (urlparse(raw).hostname or "").lower()
    except ValueError:
        return ""
    if not host:
        return ""
    host = _WWW.sub("", host)
    return host if "." in host else ""


def normalize_name(name: str) -> str:
    """Acme Technologies, Inc. -> acme. Empty string when nothing survives."""
    low = (name or "").strip().lower()
    if not low:
        return ""
    low = _NON_ALNUM.sub(" ", low)
    tokens = [t for t in _WS.split(low) if t and t not in _NAME_NOISE]
    if not tokens:
        # Name was entirely noise words ("The Company") — fall back to the raw
        # collapsed form rather than claiming no identity.
        tokens = [t for t in _WS.split(low) if t]
    return "".join(tokens)


def normalize_linkedin(url: str) -> str:
    """Reduce a company LinkedIn URL to its slug."""
    raw = (url or "").strip().lower()
    if not raw:
        return ""
    match = re.search(r"linkedin\.com/company/([^/?#]+)", raw)
    return match.group(1).strip("/") if match else ""


def normalize_x_handle(value: str) -> str:
    """@Acme, x.com/Acme, https://twitter.com/Acme -> acme."""
    raw = (value or "").strip()
    if not raw:
        return ""
    match = re.search(r"(?:x|twitter)\.com/([^/?#]+)", raw, re.I)
    if match:
        raw = match.group(1)
    raw = raw.lstrip("@").strip("/")
    if not raw or "/" in raw:
        return ""
    return raw.lower()


def x_handle_url(value: str) -> str:
    """Canonical https://x.com/<handle>, matching crm_intake's expectation."""
    handle = normalize_x_handle(value)
    return f"https://x.com/{handle}" if handle else ""


def identity_key(company: str, website: str = "", linkedin: str = "") -> str:
    """Single stable key for the local seen-store.

    Domain wins because it is the strongest anchor crm-core recognises; LinkedIn
    slug is next; name is the last resort.
    """
    domain = normalize_domain(website)
    if domain:
        return f"domain:{domain}"
    slug = normalize_linkedin(linkedin)
    if slug:
        return f"li:{slug}"
    name = normalize_name(company)
    return f"name:{name}" if name else ""


def hard_match(a: dict, b: dict) -> str:
    """Return the reason two candidates are the same company, or ''.

    Mirrors crm-core's hard-dedup keys: domain, LinkedIn, Crunchbase, name.
    """
    ad, bd = normalize_domain(a.get("website", "")), normalize_domain(b.get("website", ""))
    if ad and ad == bd:
        return "website_domain"
    al, bl = normalize_linkedin(a.get("linkedin", "")), normalize_linkedin(b.get("linkedin", ""))
    if al and al == bl:
        return "linkedin"
    ac, bc = (a.get("crunchbase") or "").strip().lower(), (b.get("crunchbase") or "").strip().lower()
    if ac and ac == bc:
        return "crunchbase"
    an, bn = normalize_name(a.get("company", "")), normalize_name(b.get("company", ""))
    if an and an == bn:
        return "normalized_name"
    return ""
