"""
Tests for the doctor integrity report.

Two of these pin traps that were rediscovered the hard way on
2026-08-21/22, and both nearly caused real loss:

  song_key must treat "Al Green - Call Me (Come Back Home)" and "Al Green
  - Call Me" as the same recording. Comparing raw titles produced a
  phantom "500 songs lost" report.

  Hash is the wrong test for NEAR duplicates. Different encodings of one
  recording have different PCM hashes by definition, so a hash-based
  survival check calls every near-dupe unique -- which nearly deleted
  1,002 songs whose only copy was in quarantine.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from musaeus.doctor import diagnose, song_key


class TestSongKey:
    def test_edition_suffix_in_brackets_is_ignored(self):
        assert song_key("Al Green", "Call Me (Come Back Home)") == song_key("Al Green", "Call Me")

    def test_trailing_edition_marker_is_ignored(self):
        assert song_key("Foghat", "Slow Ride - 2016 Remaster") == song_key("Foghat", "Slow Ride")

    def test_punctuation_and_case_are_ignored(self):
        assert song_key("AC/DC", "T.N.T.") == song_key("ac dc", "TNT")

    def test_different_songs_stay_different(self):
        assert song_key("Queen", "Bicycle Race") != song_key("Queen", "Somebody to Love")

    def test_same_title_different_artist_stays_different(self):
        assert song_key("Beatles, The", "Yesterday") != song_key("Boyz II Men", "Yesterday")

    def test_missing_values_do_not_raise(self):
        assert song_key(None, None) == ("", "")


@pytest.fixture
def vault(tmp_path: Path):
    lib = tmp_path / "ALAC-Library"
    lib.mkdir()
    db = tmp_path / "musaeus.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE archive (file_path TEXT, artist TEXT, title TEXT, status TEXT, "
        "audio_hash TEXT, genre TEXT, finalized_at TEXT, duration REAL)"
    )
    conn.execute(
        "CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, ts TEXT, "
        "event_type TEXT, file_path TEXT, old_value TEXT, new_value TEXT, stage TEXT, note TEXT)"
    )
    conn.commit()
    conn.close()
    # An empty-but-present ledger: a MISSING ledger is a legitimate warning,
    # and this fixture is meant to exercise the clean case.
    idx = tmp_path / "hash_index.db"
    ic = sqlite3.connect(idx)
    ic.execute("CREATE TABLE finalized_hashes (audio_hash TEXT, file_path TEXT)")
    ic.commit()
    ic.close()
    return SimpleNamespace(
        db_path=db,
        alac_library=lib,
        hash_index_path=tmp_path / "hash_index.db",
        vault_root=tmp_path,
    )


def _add(
    vault, name, *, artist, title, status="CATALOGUED", on_disk=True, genre="Rock", indexed=True,
    duration=200.0,
):
    """Add a row, and by default index it.

    A finalized row missing from the ledger is a real failure -- it is what
    stranded 6,537 rows on 2026-08-21 -- so the fixture has to index by
    default or every test trips that check instead of the one it means to.
    """
    p = vault.alac_library / name
    if on_disk:
        p.write_bytes(b"x")
    h = f"h_{name}"
    conn = sqlite3.connect(vault.db_path)
    conn.execute(
        "INSERT INTO archive VALUES (?,?,?,?,?,?,?,?)",
        (str(p), artist, title, status, h, genre, "2026-01-01", duration),
    )
    conn.commit()
    conn.close()
    if indexed:
        ic = sqlite3.connect(vault.hash_index_path)
        ic.execute("INSERT INTO finalized_hashes VALUES (?,?)", (h, str(p)))
        ic.commit()
        ic.close()
    return p


class TestDiagnose:
    def test_a_clean_library_reports_ok(self, vault):
        _add(vault, "a.m4a", artist="Queen", title="Bicycle Race")
        rep = diagnose(vault)
        assert not rep.failed
        assert "OK" in rep.render()

    def test_a_missing_file_with_no_other_copy_is_a_failure(self, vault):
        _add(vault, "gone.m4a", artist="Aretha Franklin", title="Respect", on_disk=False)
        rep = diagnose(vault)
        assert rep.failed
        assert any("genuinely gone" in f.detail for f in rep.findings)

    def test_a_missing_file_whose_recording_survives_is_only_a_warning(self, vault):
        """The distinction that matters: moved is not lost."""
        _add(vault, "here.m4a", artist="Al Green", title="Call Me")
        _add(vault, "gone.m4a", artist="Al Green", title="Call Me (Come Back Home)", on_disk=False)
        rep = diagnose(vault)
        assert not rep.failed

    def test_a_finalized_row_missing_from_the_ledger_is_a_failure(self, vault):
        """Cross-batch dedup silently stops working when this drifts.

        On 2026-08-21 an over-eager ledger prune stranded 6,537 finalized
        rows with no index entry; the audit caught it, but only after the
        fact.
        """
        _add(vault, "unindexed.m4a", artist="Queen", title="Bicycle Race", indexed=False)
        rep = diagnose(vault)
        assert rep.failed
        assert any("hash ledger" in f.check and f.count == 1 for f in rep.findings)

    def test_a_file_with_no_row_is_reported(self, vault):
        (vault.alac_library / "orphan.m4a").write_bytes(b"x")
        rep = diagnose(vault)
        assert any(f.check == "library files with no row" and f.count == 1 for f in rep.findings)

    def test_quarantine_holding_a_sole_copy_is_a_failure(self, vault):
        """Purging quarantine wholesale nearly lost 1,002 songs this way."""
        _add(vault, "q.m4a", artist="Herb Alpert", title="Tijuana Taxi", status="DUPE_REVIEW")
        rep = diagnose(vault)
        assert rep.failed
        assert any("sole copy" in f.check for f in rep.findings)

    def test_quarantine_is_fine_when_the_library_has_the_song(self, vault):
        _add(vault, "keep.m4a", artist="Herb Alpert", title="Tijuana Taxi")
        _add(
            vault,
            "q.m4a",
            artist="Herb Alpert",
            title="Tijuana Taxi (Remastered)",
            status="DUPE_REVIEW",
        )
        rep = diagnose(vault)
        assert not rep.failed

    def test_it_never_writes_to_the_database(self, vault):
        """Read-only: safe to run while a pipeline holds the write lock."""
        _add(vault, "a.m4a", artist="Queen", title="Bicycle Race")
        before = vault.db_path.stat().st_mtime_ns
        diagnose(vault)
        assert vault.db_path.stat().st_mtime_ns == before


class TestLedgerStalenessIsReportedNotWarned:
    """`finalized_hashes.file_path` is documented in db.py as the path at
    time of finalize -- an immutable snapshot -- and audit.py states a row
    moved afterwards is *expected* not to match it.

    So a non-zero count is the normal state of any library where something
    has been renamed, moved or deliberately removed. Warning on it forever
    trains the reader to ignore the whole report, and on 2026-08-24 it did
    worse than that: it tempted a "repair" that rewrote 9 historical
    snapshots to make the number smaller.

    The 4.17 cascade came from CrossDupeStage acting on an unverified hit,
    and is fixed there -- cross_dupe.py confirms the twin exists before
    believing it. Nothing downstream is harmed by these entries.
    """

    def test_a_ledger_entry_for_a_removed_file_does_not_fail_the_report(self, vault):
        import sqlite3

        _add(vault, "kept.m4a", artist="Al Green", title="Call Me")
        # An entry naming content deliberately purged: no archive row, no file.
        ic = sqlite3.connect(vault.hash_index_path)
        ic.execute(
            "INSERT INTO finalized_hashes VALUES (?,?)",
            ("deadbeef" * 8, str(vault.alac_library / "Knock Off - Hey Jude.m4a")),
        )
        ic.commit()
        ic.close()

        rep = diagnose(vault)

        assert not rep.failed
        assert "OK" in rep.render()

    def test_the_count_is_still_reported_so_drift_stays_visible(self, vault):
        import sqlite3

        _add(vault, "kept.m4a", artist="Al Green", title="Call Me")
        ic = sqlite3.connect(vault.hash_index_path)
        ic.execute(
            "INSERT INTO finalized_hashes VALUES (?,?)",
            ("deadbeef" * 8, str(vault.alac_library / "Purged - Something.m4a")),
        )
        ic.commit()
        ic.close()

        rep = diagnose(vault)
        line = next(f for f in rep.findings if "gone file" in f.check)

        # Visible as a drift indicator -- a sudden jump is what 4.17 looked
        # like from outside -- but it must not count against the report.
        assert "1" in line.detail
        assert line.count == 0


class TestSoleCopyFallsBackToAudioHash:
    """song_key alone cannot see a copy held under a different artist name.

    That is now the normal case, not an edge case: ClassicalComposerStage
    renames the KEPT copy to the composer while the quarantined duplicate
    keeps its performer credit, so the pair can never match by name.
    Measured 2026-08-25 — Carmignola's Four Seasons and Sarah Nemtanu's
    BWV 1043 both reported as sole copies while their audio sat safely
    catalogued under Vivaldi and Bach.
    """

    def test_a_duplicate_kept_under_another_name_is_not_a_sole_copy(self, vault):
        import sqlite3

        kept = _add(vault, "kept.m4a", artist="Antonio Vivaldi", title="The Four Seasons")
        gone = _add(vault, "dupe.m4a", artist="Giuliano Carmignola",
                    title="The Four Seasons", on_disk=True)
        # same recording, same audio, different credit
        conn = sqlite3.connect(vault.db_path)
        conn.execute("UPDATE archive SET audio_hash='deadbeef' WHERE file_path IN (?,?)",
                     (str(kept), str(gone)))
        conn.execute("UPDATE archive SET status='DUPE_REVIEW' WHERE file_path=?", (str(gone),))
        conn.commit()
        conn.close()

        rep = diagnose(vault)

        # Assert on the finding under test, not rep.failed: rewriting
        # audio_hash after _add has indexed it deliberately desynchronises
        # the hash ledger, which is a different check.
        line = next(f for f in rep.findings if "sole copy" in f.check)
        assert line.count == 0
        assert line.level == "ok"

    def test_a_quarantined_row_whose_audio_is_held_nowhere_still_fails(self, vault):
        """The check must keep working — this is the 4.17 signal."""
        import sqlite3

        _add(vault, "other.m4a", artist="Someone Else", title="Different Song")
        gone = _add(vault, "only.m4a", artist="Lost Artist", title="Only Copy")
        conn = sqlite3.connect(vault.db_path)
        conn.execute("UPDATE archive SET status='DUPE_REVIEW', audio_hash='uniquehash' "
                     "WHERE file_path=?", (str(gone),))
        conn.commit()
        conn.close()

        rep = diagnose(vault)

        assert rep.failed
        assert any("sole copy" in f.check and f.count == 1 for f in rep.findings)


class TestRejectedAudioPresentAnyway:
    """The real escape, 2026-09-04. Santana's "Bella" was in INBOX twice: a
    FLAC that Canonicalize converted, duration-checked and correctly
    REFUSED, and an already-ALAC copy of the same truncated audio that was
    classified PASSTHROUGH -- no output, so the check never ran. The
    rejection held; its twin walked past.

    Comparing durations across copies was tried first and abandoned: against
    the real library it produced 802 findings, mostly live versions
    legitimately longer than their studio originals. The exact signal is the
    audio_hash of the file that was rejected."""

    def _reject(self, vault, path):
        conn = sqlite3.connect(vault.db_path)
        conn.execute(
            "INSERT INTO events (event_type, file_path, old_value) VALUES (?,?,?)",
            ("CANONICALIZE_VERIFY_FAILED", str(path) + ".FAILED_VERIFY", str(path)),
        )
        conn.commit()
        conn.close()

    def _same_hash(self, vault, a, b):
        conn = sqlite3.connect(vault.db_path)
        h = conn.execute("SELECT audio_hash FROM archive WHERE file_path=?", (str(a),)).fetchone()[0]
        conn.execute("UPDATE archive SET audio_hash=? WHERE file_path=?", (h, str(b)))
        conn.commit()
        conn.close()

    def test_the_santana_escape_is_caught(self, vault):
        # CATALOGUED, as the real rejected sources are -- the check must not
        # count the refused file itself as an escape. It did, on the first
        # run against the live library: 4 findings, all of them the sources.
        src = _add(vault, "bella.flac", artist="Santana", title="Bella")
        twin = _add(vault, "bella.m4a", artist="Santana", title="Bella")
        self._same_hash(vault, src, twin)     # the same truncated audio
        self._reject(vault, src)              # the FLAC was refused
        f = _finding(diagnose(vault), "rejected audio present anyway")
        assert f.level == "fail"
        assert f.count == 1
        assert "bella.m4a" in f.detail

    def test_a_rejection_with_no_surviving_twin_is_ok(self, vault):
        src = _add(vault, "solo.flac", artist="Rage", title="Testify")
        self._reject(vault, src)
        f = _finding(diagnose(vault), "rejected audio present anyway")
        assert f.level == "ok"
        assert "1 rejection(s) on record" in f.detail

    def test_no_rejections_says_so(self, vault):
        _add(vault, "a.m4a", artist="Queen", title="Bicycle Race")
        f = _finding(diagnose(vault), "rejected audio present anyway")
        assert f.level == "ok"
        assert "no rejections" in f.detail

    def test_a_live_version_longer_than_the_studio_cut_is_never_flagged(self, vault):
        """The abandoned duration check called this a truncation. It is a
        different recording and shares no audio_hash, so it cannot trip
        this one."""
        _add(vault, "studio.m4a", artist="Alice Cooper", title="Is It My Body", duration=160.0)
        _add(vault, "live.m4a", artist="Alice Cooper",
             title="Is It My Body (Live in Miami)", duration=486.0)
        assert _finding(diagnose(vault), "rejected audio present anyway").level == "ok"


def _finding(rep, check):
    return next(f for f in rep.findings if f.check == check)
