import csv

from lib.candidates import Candidate, score
from lib.sinks import write_intake_csv


def test_dedupe_within_merges_evidence_and_keeps_strongest():
    from research_run import dedupe_within

    # One signal vs two, so "strongest" holds regardless of how weights are tuned.
    weak = score(Candidate(
        company="Acme Inc",
        website="https://acme.com",
        nyc_proof="NYC office",
        signals=["hiring_surge"],
        source_urls=["https://a.example"],
        lane="nyc_press",
    ))
    strong = score(Candidate(
        company="Acme Technologies",
        website="http://www.acme.com/about",
        nyc_proof="Opening a Manhattan office",
        signals=["hiring_surge", "nyc_office_opening"],
        source_urls=["https://b.example"],
        lane="funding_nyc",
    ))
    assert strong.signal_strength > weak.signal_strength

    merged = dedupe_within([weak, strong])
    assert len(merged) == 1
    kept = merged[0]
    assert kept.signal_strength == strong.signal_strength
    assert set(kept.source_urls) == {"https://a.example", "https://b.example"}
    assert set(kept.signals) == {"nyc_office_opening", "hiring_surge"}
    assert "+" in kept.lane


def test_dedupe_keeps_genuinely_different_companies():
    from research_run import dedupe_within

    a = score(Candidate(company="Acme", website="https://acme.com", nyc_proof="x", signals=["hiring_surge"]))
    b = score(Candidate(company="Zenith", website="https://zenith.com", nyc_proof="x", signals=["hiring_surge"]))
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

    cand = score(Candidate(company="Acme", website="https://acme.com", nyc_proof="x"))
    db = tmp_path / "research.db"

    with state.connect(db) as conn:
        state.record_seen(conn, cand)
        assert state.is_emitted(conn, cand.key) is False
        state.mark_emitted(conn, [cand.key])

    with state.connect(db) as conn:
        assert state.is_emitted(conn, cand.key) is True
        assert state.stats(conn) == {"seen": 1, "emitted": 1, "held": 0}


def test_seen_store_keeps_best_strength(tmp_path):
    from lib import state

    db = tmp_path / "research.db"
    weak = score(Candidate(company="Acme", website="https://acme.com", nyc_proof="x", signals=["headcount_growth"]))
    strong = score(Candidate(company="Acme", website="https://acme.com", nyc_proof="x", signals=["nyc_office_opening"]))

    with state.connect(db) as conn:
        state.record_seen(conn, strong)
        state.record_seen(conn, weak)
        row = conn.execute("SELECT best_strength, times_seen FROM seen").fetchone()

    assert row["best_strength"] == strong.signal_strength
    assert row["times_seen"] == 2
