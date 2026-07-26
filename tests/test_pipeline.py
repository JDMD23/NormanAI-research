import csv

from lib.candidates import Candidate
from lib.sinks import write_intake_csv


def test_dedupe_within_merges_evidence_and_keeps_strongest():
    from lib.pipeline import dedupe_within

    # The tighter mode is the more specific claim and should win the merge.
    broad = Candidate(
        company="Acme Inc",
        website="https://acme.com",
        mode="funding",
        nyc_evidence="",
        nyc_angle="none",
        keyword_hits=["Series B"],
        source_urls=["https://a.example"],
        lane="web_funding",
    )
    tight = Candidate(
        company="Acme Technologies",
        website="http://www.acme.com/about",
        mode="office_expansion",
        nyc_evidence="Opening a Manhattan office",
        nyc_angle="strong",
        keyword_hits=["opening a New York office"],
        source_urls=["https://b.example"],
        lane="web_office_expansion",
    )
    weak, strong = broad, tight

    merged = dedupe_within([weak, strong])
    assert len(merged) == 1
    kept = merged[0]
    assert kept.mode == "office_expansion"
    assert kept.nyc_angle == "strong"
    assert set(kept.source_urls) == {"https://a.example", "https://b.example"}
    assert set(kept.keyword_hits) == {"opening a New York office", "Series B"}
    assert "+" in kept.lane


def test_dedupe_keeps_genuinely_different_companies():
    from lib.pipeline import dedupe_within

    a = Candidate(company="Acme", website="https://acme.com")
    b = Candidate(company="Zenith", website="https://zenith.com")
    assert len(dedupe_within([a, b])) == 2


def test_written_csv_round_trips(tmp_path):
    cands = [
        Candidate(
            company="Acme",
            website="https://acme.com",
            x_handle="@acmehq",
            hq="NYC",
            industries="Fintech",
            last_funding_usd=25_000_000,
            last_funding_type="Series B",
        ),
        Candidate(company="Zenith"),
    ]
    path = write_intake_csv(cands, tmp_path / "out.csv")

    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))

    assert len(rows) == 2
    assert rows[0]["Company"] == "Acme"
    assert rows[0]["X Handle"] == "https://x.com/acmehq"
    assert rows[0]["Last Funding Amount (in USD)"] == "25000000"
    # Everything unknown on the second row stays blank.
    assert rows[1]["Website"] == ""
    assert rows[1]["Total Funding Amount (in USD)"] == ""


def test_seen_store_blocks_a_second_emission(tmp_path):
    from lib import state

    cand = Candidate(company="Acme", website="https://acme.com")
    db = tmp_path / "research.db"

    with state.connect(db) as conn:
        state.record_seen(conn, cand)
        assert state.is_emitted(conn, cand.key) is False
        state.mark_emitted(conn, [cand.key])

    with state.connect(db) as conn:
        assert state.is_emitted(conn, cand.key) is True
        assert state.stats(conn) == {"seen": 1, "emitted": 1, "held": 0}


def test_seen_store_counts_repeat_sightings_of_one_company(tmp_path):
    from lib import state

    db = tmp_path / "research.db"
    first = Candidate(company="Acme", website="https://acme.com")
    again = Candidate(company="Acme Inc.", website="https://www.acme.com/about")

    with state.connect(db) as conn:
        state.record_seen(conn, first)
        state.record_seen(conn, again)
        row = conn.execute("SELECT times_seen, company FROM seen").fetchone()

    assert row["times_seen"] == 2, "same company by domain must not create a second row"
