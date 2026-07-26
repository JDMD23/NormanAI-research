"""Qualification: does this candidate belong in the CRM at all?

Research qualifies, it does not score. There is no numeric threshold here and
no ranking — crm-core's score agent does that after intake, with real enriched
inputs. What this module answers is narrower and binary: did we find enough to
justify creating a row.

Two postures, per JD's operating rules:

  funding          BROAD — high coverage. NYC evidence NOT required.
  office_expansion TIGHT — concrete NYC space demand only.
  hiring_growth    TIGHT — real NYC headcount growth only.
  founder_language TIGHT — genuine founder statements of space pressure only.

The tight modes are tight because a false positive there is expensive: it puts
a company on the board claiming an office need that isn't real, and JD calls it.
A false positive in funding mode just means an extra row the lanes will sort out.
"""

from __future__ import annotations

import re

from lib.config import load_config

VERDICT_QUALIFY = "qualify"
VERDICT_REJECT = "reject"


def modes_config() -> dict:
    return load_config("modes")


def watched_accounts() -> list[str]:
    return [h.lower().lstrip("@") for h in modes_config()["watchedAccounts"]]


def has_identity(cand) -> bool:
    """Identifiable = a website or a verifiable handle, per the operating rules."""
    return bool(cand.website.strip() or cand.x_handle.strip() or cand.linkedin.strip())


def normalise_nyc_angle(cand) -> str:
    """Clamp the model's claimed NYC angle to what its evidence supports."""
    claimed = (cand.nyc_angle or "").strip().lower()
    valid = modes_config()["nycAngle"]["values"]
    if claimed not in valid:
        claimed = "none"
    if not cand.nyc_evidence.strip():
        # No evidence means no angle, whatever the model asserted.
        return "none"
    return claimed


def _sector_matches(cand, sectors: list[str]) -> bool:
    """Word-boundary match, not substring.

    Plain `in` is a trap here: the sector "AI" is a substring of "chain",
    "certain", and "mountain", so a burrito franchise qualified as an AI
    company. Short sector tokens make this a routine false positive, not an
    edge case.
    """
    haystack = f"{cand.industries} {cand.one_liner} {cand.signal_notes}".lower()
    return any(
        re.search(rf"\b{re.escape(s.lower())}\b", haystack) for s in sectors
    )


def _excluded_industry(cand) -> bool:
    excluded = modes_config()["exclusions"]["industries"]
    haystack = f"{cand.industries} {cand.one_liner}".lower()
    return any(bad.lower() in haystack for bad in excluded)


def qualify(cand) -> dict:
    """Return {verdict, reason, nyc_angle}. Never raises."""
    cfg = modes_config()
    mode_key = cand.mode or "funding"
    mode = cfg["modes"].get(mode_key)
    if mode is None:
        return {"verdict": VERDICT_REJECT, "reason": f"unknown mode '{mode_key}'",
                "nyc_angle": "none"}

    angle = normalise_nyc_angle(cand)

    if not cand.company.strip():
        return {"verdict": VERDICT_REJECT, "reason": "no company name", "nyc_angle": angle}

    if _excluded_industry(cand):
        return {"verdict": VERDICT_REJECT, "reason": "excluded industry (biotech/therapeutics)",
                "nyc_angle": angle}

    if mode.get("requireIdentity", True) and not has_identity(cand):
        return {"verdict": VERDICT_REJECT,
                "reason": "not identifiable — no website, X handle, or LinkedIn",
                "nyc_angle": angle}

    if not cand.source_urls:
        return {"verdict": VERDICT_REJECT, "reason": "no source url", "nyc_angle": angle}

    if mode["posture"] == "broad":
        # Funding: coverage is the goal. Qualify on provenance OR keyword, plus
        # a relevant sector. NYC is reported, never required.
        from_watched = (cand.source_handle or "").lower().lstrip("@") in watched_accounts()
        if not (from_watched or cand.keyword_hits):
            return {"verdict": VERDICT_REJECT,
                    "reason": "no funding keyword and not from a watched account",
                    "nyc_angle": angle}
        if not _sector_matches(cand, mode["sectors"]):
            return {"verdict": VERDICT_REJECT, "reason": "sector not relevant",
                    "nyc_angle": angle}
        why = "watched account" if from_watched else f"keyword: {cand.keyword_hits[0]}"
        return {"verdict": VERDICT_QUALIFY, "reason": f"funding/broad — {why}",
                "nyc_angle": angle}

    # Tight modes: the concrete signal is the whole point.
    if mode.get("requireNycEvidence", True) and angle in {"none", "weak"}:
        return {"verdict": VERDICT_REJECT,
                "reason": f"{mode_key}/tight — NYC angle '{angle}' is not concrete enough",
                "nyc_angle": angle}
    if not cand.keyword_hits:
        return {"verdict": VERDICT_REJECT,
                "reason": f"{mode_key}/tight — no concrete signal phrase matched",
                "nyc_angle": angle}
    if mode_key == "hiring_growth":
        minimum = mode.get("minNycRoles")
        roles = cand.nyc_open_roles_estimate
        senior = any(
            k.lower() in " ".join(cand.keyword_hits).lower()
            for k in ("vp of", "head of", "cro", "coo", "chief of staff",
                      "office manager", "facilities manager")
        )
        milestone = any(
            k in " ".join(cand.keyword_hits).lower()
            for k in ("doubled", "tripled", "building our nyc team",
                      "building out our new york team")
        )
        if not senior and not milestone and (roles is None or roles < minimum):
            return {"verdict": VERDICT_REJECT,
                    "reason": f"hiring_growth/tight — needs {minimum}+ NYC roles, "
                              "a senior NYC hire, or a stated team build-out",
                    "nyc_angle": angle}

    return {"verdict": VERDICT_QUALIFY,
            "reason": f"{mode_key}/tight — {cand.keyword_hits[0]}",
            "nyc_angle": angle}


def apply(candidates: list) -> tuple[list, list]:
    """Split candidates into (qualified, rejected), annotating each in place."""
    qualified, rejected = [], []
    for cand in candidates:
        result = qualify(cand)
        cand.nyc_angle = result["nyc_angle"]
        cand.fit_hint = result["nyc_angle"]
        cand.qualify_reason = result["reason"]
        (qualified if result["verdict"] == VERDICT_QUALIFY else rejected).append(cand)
    return qualified, rejected
