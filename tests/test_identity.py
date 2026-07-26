from lib.identity import (
    hard_match,
    identity_key,
    normalize_domain,
    normalize_linkedin,
    normalize_name,
    normalize_x_handle,
)


def test_normalize_domain_strips_scheme_www_and_path():
    assert normalize_domain("https://www.Acme.com/careers?x=1") == "acme.com"
    assert normalize_domain("acme.com") == "acme.com"
    assert normalize_domain("http://www2.acme.co.uk") == "acme.co.uk"


def test_normalize_domain_rejects_junk():
    assert normalize_domain("") == ""
    assert normalize_domain("not a url") == ""
    assert normalize_domain("localhost") == ""


def test_normalize_name_drops_corporate_noise():
    assert normalize_name("Acme Technologies, Inc.") == "acme"
    assert normalize_name("The Acme Company") == "acme"
    assert normalize_name("Acme AI") == "acme"


def test_normalize_name_never_empties_an_all_noise_name():
    # "The Company" is all filler, but it is still an identity we must key on.
    assert normalize_name("The Company") == "thecompany"


def test_normalize_linkedin_extracts_slug():
    assert normalize_linkedin("https://www.linkedin.com/company/acme-hq/") == "acme-hq"
    assert normalize_linkedin("https://example.com/acme") == ""


def test_normalize_x_handle_forms():
    assert normalize_x_handle("@AcmeHQ") == "acmehq"
    assert normalize_x_handle("https://x.com/AcmeHQ") == "acmehq"
    assert normalize_x_handle("https://twitter.com/AcmeHQ?s=20") == "acmehq"
    assert normalize_x_handle("") == ""


def test_identity_key_prefers_domain_then_linkedin_then_name():
    assert identity_key("Acme", "https://acme.com", "linkedin.com/company/acme") == "domain:acme.com"
    assert identity_key("Acme", "", "linkedin.com/company/acme-hq") == "li:acme-hq"
    assert identity_key("Acme Inc.", "", "") == "name:acme"
    assert identity_key("", "", "") == ""


def test_hard_match_mirrors_crm_core_keys():
    a = {"company": "Acme Inc", "website": "https://acme.com"}
    b = {"company": "Acme Technologies", "website": "http://www.acme.com/about"}
    assert hard_match(a, b) == "website_domain"

    c = {"company": "Acme", "linkedin": "linkedin.com/company/acme-hq"}
    d = {"company": "Totally Different", "linkedin": "https://www.linkedin.com/company/acme-hq/"}
    assert hard_match(c, d) == "linkedin"

    e = {"company": "Acme Labs, Inc."}
    f = {"company": "acme"}
    assert hard_match(e, f) == "normalized_name"

    assert hard_match({"company": "Acme"}, {"company": "Zenith"}) == ""


def test_hard_match_does_not_collide_on_empty_fields():
    """Two candidates with no website must not match on a shared empty domain."""
    a = {"company": "Acme", "website": ""}
    b = {"company": "Zenith", "website": ""}
    assert hard_match(a, b) == ""
