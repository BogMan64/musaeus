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


def test_a_same_recording_pair_goes_to_the_keep_rule(tmp_path, monkeypatch):
    # Grey, 2026-10-23: fingerprint pairs use the same keep rule as the
    # duplicates step (reversing choice (b) of 2026-09-25, "his to decide").
    # One copy of each song: the lossless one stays (the stage pairs only
    # copies of about the same length, so length rarely decides).
    from musaeus.stages.dupe_resolver import DupeResolverStage

    monkeypatch.setattr(acoustid_mod, "_fpcalc", lambda p: (200.0, "FP-" + Path(p).name))
    monkeypatch.setattr(acoustid_mod, "_acousticid_lookup", lambda *a, **k: ("rec-1", 0.97))
    ctx = _ctx(tmp_path)
    album = _untagged(
        ctx,
        "Jr. Walker - Shotgun.m4a",
        artist="Jr. Walker",
        title="Shotgun",
        duration=200.0,
        codec="alac",
    )
    mix = _untagged(
        ctx,
        "Jr. Walker - Shotgun (Original Mix).m4a",
        artist="Jr. Walker",
        title="Shotgun (Original Mix)",
        duration=200.0,
        codec="aac",
    )
    result = AcousticIDStage().run(ctx)
    assert any("the keep rule decides" in n for n in result.notes), result.notes
    assert any("Shotgun (Original Mix)" in n for n in result.notes), "the pair is named"
    DupeResolverStage().run(ctx)
    assert album.is_file(), "the lossless copy stays"
    assert not mix.is_file(), "the AAC copy was not moved to review"
    pending = ctx.conn.execute(
        "SELECT COUNT(*) FROM duplicates WHERE duplicate_type = 'ACOUSTIC' AND status = 'pending'"
    ).fetchone()[0]
    assert pending == 0


def test_a_pair_whose_titles_differ_stays_for_grey(tmp_path, monkeypatch):
    """A fingerprint can name "Blue" and "Blue Sky" one recording: the keep rule
    must not split two songs by name (Codex review of #228)."""
    from musaeus.stages.dupe_resolver import DupeResolverStage

    monkeypatch.setattr(acoustid_mod, "_fpcalc", lambda p: (200.0, "FP-" + Path(p).name))
    monkeypatch.setattr(acoustid_mod, "_acousticid_lookup", lambda *a, **k: ("rec-1", 0.97))
    ctx = _ctx(tmp_path)
    blue = _untagged(ctx, "Joni - Blue.m4a", artist="Joni", title="Blue", duration=200.0)
    sky = _untagged(
        ctx, "Joni - Blue Sky.m4a", artist="Joni", title="Blue Sky", duration=200.0, codec="aac"
    )
    result = AcousticIDStage().run(ctx)
    assert any("for you to decide" in n for n in result.notes), result.notes
    DupeResolverStage().run(ctx)
    assert blue.is_file() and sky.is_file(), "two songs by name were split by the keep rule"


@pytest.mark.parametrize(
    ("a", "b", "same"),
    [
        ("Hold On I'm Comin' (Original Mix)", "Hold On, I'm Comin'", True),
        ("Lady (Remastered 2006)", "Lady", True),
        ("Blue", "Blue Sky", False),
        ("Medley (A/B)", "Medley (C/D)", False),
        ("Song - Part 1", "Song - Part 2", False),
        ("Interlude (1)", "Interlude (2)", False),
        ("Interlude (2020)", "Interlude (2021)", False),
        ("Live and Let Die", "And Let Die", False),
        ("Song - 2011 Remaster", "Song", True),
    ],
)
def test_only_a_version_label_is_set_aside_before_titles_are_compared(a, b, same):
    """Codex review of #228, round 2: every bracket was dropped, so two medleys
    named apart only in brackets were one song."""
    assert acoustid_mod._same_song(a, b) is same


def test_a_pair_with_an_unmeasured_length_stays_for_grey(tmp_path, monkeypatch):
    """The 3 s check needs both lengths: unsure stays a person's (Codex review of #228, round 3)."""
    from musaeus.stages.dupe_resolver import DupeResolverStage

    monkeypatch.setattr(acoustid_mod, "_fpcalc", lambda p: (200.0, "FP-" + Path(p).name))
    monkeypatch.setattr(acoustid_mod, "_acousticid_lookup", lambda *a, **k: ("rec-1", 0.97))
    ctx = _ctx(tmp_path)
    a = _untagged(ctx, "Toto - Africa.m4a", artist="Toto", title="Africa", codec="alac")
    b = _untagged(ctx, "Toto - Africa (2).m4a", artist="Toto", title="Africa", codec="aac")
    result = AcousticIDStage().run(ctx)
    assert any("for you to decide" in n for n in result.notes), result.notes
    DupeResolverStage().run(ctx)
    assert a.is_file() and b.is_file()


def test_a_pair_with_a_blank_artist_stays_for_grey(tmp_path, monkeypatch):
    """A blank artist skips the cover check: unsure stays a person's (Codex review of #228, round 4)."""
    from musaeus.stages.dupe_resolver import DupeResolverStage

    monkeypatch.setattr(acoustid_mod, "_fpcalc", lambda p: (200.0, "FP-" + Path(p).name))
    monkeypatch.setattr(acoustid_mod, "_acousticid_lookup", lambda *a, **k: ("rec-1", 0.97))
    ctx = _ctx(tmp_path)
    a = _untagged(
        ctx, "Toto - Africa.m4a", artist="Toto", title="Africa", duration=200.0, codec="alac"
    )
    b = _untagged(ctx, "Africa.m4a", artist="", title="Africa", duration=200.0, codec="aac")
    AcousticIDStage().run(ctx)
    DupeResolverStage().run(ctx)
    assert a.is_file() and b.is_file()


def test_a_copy_already_moved_to_review_is_not_a_pair_to_decide(tmp_path, monkeypatch):
    # Enrichment of 2026-09-27 listed 756 "same recording" pairs for Grey;
    # 718 paired a library file with the copy Act 2 had already moved to the
    # review folder. That copy kept the AcoustID recording it was given when
    # it was still in the library, and the twin lookup did not ask where it
    # is now.
    monkeypatch.setattr(acoustid_mod, "_fpcalc", lambda p: (200.0, "FP-" + Path(p).name))
    monkeypatch.setattr(acoustid_mod, "_acousticid_lookup", lambda *a, **k: ("rec-1", 0.97))
    ctx = _ctx(tmp_path)
    moved = ctx.config.dupes_review_dir / "Tom Jones - It's Not Unusual.m4a"
    moved.parent.mkdir(parents=True, exist_ok=True)
    moved.write_bytes(b"audio")
    upsert_archive(
        ctx.conn,
        {
            "file_path": str(moved),
            "status": "DUPE_REVIEW",
            "audio_hash": "baked",
            "artist": "Tom Jones",
            "title": "It's Not Unusual",
            "duration": 200.0,
        },
    )
    # upsert_archive does not write the AcoustID columns; set it as the
    # earlier enrichment did.
    ctx.conn.execute(
        "UPDATE archive SET acousticid_recording = 'rec-1' WHERE file_path = ?", (str(moved),)
    )
    ctx.conn.commit()
    _untagged(
        ctx,
        "Tom Jones - It's Not Unusual.m4a",
        artist="Tom Jones",
        title="It's Not Unusual",
        duration=200.0,
    )
    AcousticIDStage().run(ctx)
    pairs = ctx.conn.execute(
        "SELECT COUNT(*) FROM duplicates WHERE duplicate_type = 'ACOUSTIC'"
    ).fetchone()[0]
    assert pairs == 0, "a copy already set aside was listed as a pair to decide"


@pytest.mark.parametrize(
    ("want", "credit", "agrees"),
    [
        ("Jimi Hendrix", "The Jimi Hendrix Experience", True),
        ("The Jimi Hendrix Experience", "Jimi Hendrix", True),
        ("Jimi Hendrix", "The Band", False),
        ("Dion", "Celine Dion", False),
    ],
)
def test_a_band_named_after_its_leader_agrees_with_the_leader(want, credit, agrees):
    """Codex review of #233: Hendrix's live "Tax Free" credited to The Experience
    was refused, and the studio-only check could then clear its live album."""
    assert acoustid_mod._artist_agrees(want, [credit]) is agrees


def _answered_in_library(ctx: RunContext, name: str, recording: str, **fields) -> Path:
    p = _untagged(ctx, name, **fields)
    # upsert_archive does not write the AcoustID columns.
    ctx.conn.execute(
        "UPDATE archive SET acousticid_recording = ?, acousticid_checked_at = '2026-10-01' "
        "WHERE file_path = ?",
        (recording, str(p)),
    )
    ctx.conn.commit()
    return p


def _in_ledger(ctx: RunContext, audio_hash: str, recording: str) -> None:
    from musaeus.db import ensure_fingerprints, remember_fingerprint

    led = acoustid_mod.sqlite3.connect(acoustid_mod._ledger_path(ctx))
    ensure_fingerprints(led)
    remember_fingerprint(led, audio_hash, "FP", 200.0, recording, 0.97, "2026-10-21")
    led.commit()
    led.close()


def _no_lookup(*a, **k):
    raise AssertionError("an answer the ledger holds was asked for again")


def test_an_answer_from_the_ledger_is_still_paired(tmp_path, monkeypatch):
    """A rerun batch: the arrival's answer comes back from the ledger, so it is
    not asked again -- but it was not compared with the library either, and
    Roberta Flack "Where Is The Love" sat twice in the library (75410 and
    12149, one recording; found 2026-10-27)."""
    monkeypatch.setattr(acoustid_mod, "_acousticid_lookup", _no_lookup)
    ctx = _ctx(tmp_path)
    kw = {"artist": "Roberta Flack", "title": "Where Is The Love", "duration": 200.0}
    _answered_in_library(ctx, "Roberta Flack - Where Is The Love.m4a", "rec-1", **kw)
    _untagged(ctx, "Roberta Flack - Where Is The Love (2).m4a", **kw)
    _in_ledger(ctx, "Roberta Flack - Where Is The Love (2).m4a", "rec-1")
    AcousticIDStage().run(ctx)
    pending = ctx.conn.execute(
        "SELECT COUNT(DISTINCT group_id) FROM duplicates "
        "WHERE duplicate_type = 'ACOUSTIC' AND status = 'pending'"
    ).fetchone()[0]
    assert pending == 1


def test_two_answers_from_the_ledger_pair_each_copy_once(tmp_path, monkeypatch):
    """Both are in the catalogue before their turn: never a copy with itself,
    never one pair twice."""
    monkeypatch.setattr(acoustid_mod, "_acousticid_lookup", _no_lookup)
    ctx = _ctx(tmp_path)
    kw = {"artist": "Toto", "title": "Africa", "duration": 200.0}
    _answered_in_library(ctx, "Toto - Africa.m4a", "rec-1", **kw)
    for name in ("Toto - Africa (2).m4a", "Toto - Africa (3).m4a"):
        _untagged(ctx, name, **kw)
        _in_ledger(ctx, name, "rec-1")
    AcousticIDStage().run(ctx)
    groups = ctx.conn.execute(
        "SELECT group_id, COUNT(DISTINCT file_path) FROM duplicates GROUP BY group_id"
    ).fetchall()
    assert sorted(n for _, n in groups) == [2, 2, 2]
