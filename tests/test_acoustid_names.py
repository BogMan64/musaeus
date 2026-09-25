"""Untagged files named by their sound, in Act 1 (Grey, 2026-09-25).

A file with no tags and no "Artist - Title" name used to be left unnamed. It
can still be identified by its audio: fpcalc makes a fingerprint and AcoustID
names the recording. But an AcoustID result is a CLUSTER of recordings in no
meaningful order, and often a polluted one -- acousticid.py records 14
unrelated tracks that recordings[0] would have named "Metro Station - Now
That We're Done". So a name is only taken when:

  - the file name still gives a title ("03 - Yesterday" -> "Yesterday"), and
    a recording in the result has that title -- no title, no name;
  - every recording with that title names the SAME artist (covers disagree,
    and a disagreement is refused, not settled by order);
  - the match scores at least 0.90, stricter than the 0.80 used to confirm a
    file that already has a name.

Every test stubs fpcalc and the AcoustID query and sets a fake key: the real
key is in the environment, and a test must never reach the live service.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import musaeus.stages.acousticid as acoustid_mod
from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.stages import ACT1_INTAKE_CORRECTION, ENRICHMENT
from musaeus.stages.acousticid import AcousticIDStage, LookupUnavailable
from musaeus.stages.acoustid_name import AcoustIDNameStage
from musaeus.stages.mb_enrich import MBEnrichStage
from musaeus.stages.scholar import ScholarStage

BEATLES = {"id": "r1", "title": "Yesterday", "artists": [{"name": "The Beatles"}]}
METRO = {"id": "r0", "title": "Now That We're Done", "artists": [{"name": "Metro Station"}]}
COVER = {"id": "r2", "title": "Yesterday", "artists": [{"name": "Matt Monro"}]}


def _ctx(tmp_path: Path, key: str | None = "test-key") -> RunContext:
    cfg = MusicConfig(
        vault_root=tmp_path,
        inbox=tmp_path / "INBOX",
        staging=tmp_path / "STAGING",
        quarantine=tmp_path / "QUARANTINE",
        runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData",
        alac_library=tmp_path / "Libraries" / "ALAC_Library",
        alac_archive=tmp_path / "Libraries" / "ALAC-Archival",
        db_path=tmp_path / "musaeus.db",
        acousticid_api_key=key,
    )
    cfg.ensure_dirs()
    return RunContext.new(cfg, open_db(cfg.db_path), dry_run=False)


@pytest.fixture
def lookup(monkeypatch):
    """Stub fpcalc and the AcoustID query; record what was asked."""
    calls: list[str] = []
    answer: dict = {"results": []}

    def fake_fpcalc(path):
        calls.append(Path(path).name)
        return 200.0, "FINGERPRINT"

    def fake_query(fingerprint, duration, api_key):
        assert api_key == "test-key"
        if isinstance(answer["results"], Exception):
            raise answer["results"]
        return answer["results"]

    monkeypatch.setattr(acoustid_mod, "_fpcalc", fake_fpcalc)
    monkeypatch.setattr(acoustid_mod, "_acousticid_query", fake_query)
    return answer, calls


def _untagged(ctx: RunContext, name: str, **fields) -> Path:
    p = ctx.inbox / name
    p.write_bytes(b"audio")
    upsert_archive(
        ctx.conn,
        {"file_path": str(p), "status": "CATALOGUED", "audio_hash": name, **fields},
    )
    ctx.conn.commit()
    return p


def _named(ctx: RunContext, p: Path) -> tuple:
    return tuple(
        ctx.conn.execute(
            "SELECT artist, title FROM archive WHERE file_path = ?", (str(p),)
        ).fetchone()
    )


def test_an_untagged_track_number_file_is_named_by_its_sound(tmp_path, lookup):
    answer, _ = lookup
    answer["results"] = [{"score": 0.95, "recordings": [BEATLES]}]
    ctx = _ctx(tmp_path)
    p = _untagged(ctx, "03 - Yesterday.m4a")
    result = AcoustIDNameStage().run(ctx)
    assert _named(ctx, p) == ("The Beatles", "Yesterday")
    assert result.files_changed == 1
    fp, checked = ctx.conn.execute(
        "SELECT chromaprint, acousticid_checked_at FROM archive"
    ).fetchone()
    assert fp == "FINGERPRINT", "the fingerprint is kept for enrichment"
    assert checked is None, "enrichment's duplicate pass must still run on it"


def test_a_polluted_cluster_is_filtered_by_the_title_in_the_file_name(tmp_path, lookup):
    answer, _ = lookup
    answer["results"] = [{"score": 0.97, "recordings": [METRO, BEATLES]}]
    ctx = _ctx(tmp_path)
    p = _untagged(ctx, "03 - Yesterday.m4a")
    AcoustIDNameStage().run(ctx)
    assert _named(ctx, p) == ("The Beatles", "Yesterday")


def test_covers_that_disagree_about_the_artist_are_refused(tmp_path, lookup):
    answer, _ = lookup
    answer["results"] = [{"score": 0.97, "recordings": [BEATLES, COVER]}]
    ctx = _ctx(tmp_path)
    p = _untagged(ctx, "03 - Yesterday.m4a")
    AcoustIDNameStage().run(ctx)
    assert _named(ctx, p) == (None, None)


def test_no_title_in_the_file_name_means_no_name(tmp_path, lookup):
    answer, calls = lookup
    answer["results"] = [{"score": 0.99, "recordings": [BEATLES]}]
    ctx = _ctx(tmp_path)
    p = _untagged(ctx, "07.m4a")
    AcoustIDNameStage().run(ctx)
    assert _named(ctx, p) == (None, None)
    assert calls == [], "nothing to confirm against, so nothing was asked"


def test_a_weak_match_is_refused(tmp_path, lookup):
    answer, _ = lookup
    answer["results"] = [{"score": 0.85, "recordings": [BEATLES]}]
    ctx = _ctx(tmp_path)
    p = _untagged(ctx, "03 - Yesterday.m4a")
    AcoustIDNameStage().run(ctx)
    assert _named(ctx, p) == (None, None)


def test_a_credit_is_built_with_its_join_phrases(tmp_path, lookup):
    answer, _ = lookup
    duet = {
        "id": "r3",
        "title": "Ebony and Ivory",
        "artists": [
            {"name": "Paul McCartney", "joinphrase": " & "},
            {"name": "Stevie Wonder"},
        ],
    }
    answer["results"] = [{"score": 0.95, "recordings": [duet]}]
    ctx = _ctx(tmp_path)
    p = _untagged(ctx, "05 - Ebony and Ivory.m4a")
    AcoustIDNameStage().run(ctx)
    assert _named(ctx, p) == ("Paul McCartney & Stevie Wonder", "Ebony and Ivory")


@pytest.mark.parametrize("trouble", ["unavailable", "no_key", "no_fpcalc"])
def test_act_one_never_fails_on_this(tmp_path, lookup, monkeypatch, trouble):
    answer, _ = lookup
    answer["results"] = LookupUnavailable("HTTP 503")
    key = None if trouble == "no_key" else "test-key"
    if trouble == "no_fpcalc":

        def missing(path):
            raise RuntimeError("fpcalc not found in PATH")

        monkeypatch.setattr(acoustid_mod, "_fpcalc", missing)
    ctx = _ctx(tmp_path, key=key)
    p = _untagged(ctx, "03 - Yesterday.m4a")
    result = AcoustIDNameStage().run(ctx)
    assert result.success
    assert _named(ctx, p) == (None, None)
    assert result.notes, "the reason it named nothing must be reported"


def test_a_tagged_row_is_never_touched(tmp_path, lookup):
    answer, calls = lookup
    answer["results"] = [{"score": 0.99, "recordings": [BEATLES]}]
    ctx = _ctx(tmp_path)
    p = _untagged(ctx, "03 - Yesterday.m4a", artist="Dion", title="Yesterday")
    AcoustIDNameStage().run(ctx)
    assert _named(ctx, p) == ("Dion", "Yesterday")
    assert calls == []


def test_the_pipeline_places_both_steps(tmp_path):
    act1 = list(ACT1_INTAKE_CORRECTION)
    assert act1.index(AcoustIDNameStage) == act1.index(ScholarStage) + 1
    enrich = list(ENRICHMENT)
    assert enrich.index(AcousticIDStage) == enrich.index(MBEnrichStage) + 1


def test_each_result_is_judged_on_its_own_strongest_first(tmp_path, lookup):
    # Measured against the live service 2026-09-25 ("Come And Get It"): the
    # best result also listed a member's solo re-recording, the next one
    # only Badfinger. Pooling every result refused a track that one result
    # names without any disagreement.
    answer, _ = lookup
    answer["results"] = [
        {"score": 0.97, "recordings": [BEATLES, COVER]},
        {"score": 0.93, "recordings": [BEATLES]},
    ]
    ctx = _ctx(tmp_path)
    p = _untagged(ctx, "03 - Yesterday.m4a")
    AcoustIDNameStage().run(ctx)
    assert _named(ctx, p) == ("The Beatles", "Yesterday")


def test_a_result_that_agrees_wins_even_when_a_weaker_one_holds_covers(tmp_path, lookup):
    # The live "T.N.T" shape: the strongest result is AC/DC alone; a second
    # result mixes AC/DC with two cover bands.
    answer, _ = lookup
    answer["results"] = [
        {"score": 0.971, "recordings": [BEATLES, COVER]},
        {"score": 0.973, "recordings": [BEATLES]},
    ]
    ctx = _ctx(tmp_path)
    p = _untagged(ctx, "03 - Yesterday.m4a")
    AcoustIDNameStage().run(ctx)
    assert _named(ctx, p) == ("The Beatles", "Yesterday")
