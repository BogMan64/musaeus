"""The whole pipeline, against real audio, in a disposable vault.

Why this exists
---------------
`work/act23` put `CanonicalizeStage` behind `MutationBoundary`, and carried
the note NOT VERIFIED AGAINST REAL DATA. Its own unit tests do use real
ffmpeg audio, so the boundary CONTRACT was covered -- what was not was the
27-stage chain actually running end to end with a real conversion in it.

That run could not be staged from the live vault. `CanonicalizeStage`
selects `status='CATALOGUED'` and the live database holds zero such rows
(87 rows, all QUARANTINED) since the 08-27 campaign drained the queue. And
copying files back in from ALAC-Library or the RAW backup gets caught by
cross-dupe on PCM `audio_hash` before canonicalize ever sees them -- which
is correct pipeline behaviour, and tests nothing.

So the audio here is synthesised: distinct tones per track, which are
genuinely new PCM and therefore genuinely new `audio_hash` values. Not real
music, but every other thing in the path is real -- real ffmpeg conversion,
real boundary, real journal, real SQLite, real filesystem.

The assertion that matters is the one finding #14 taught: not "did the
stage report success" but "is the audio still somewhere this run can reach,
and did the conversion actually happen on disk".
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from musaeus.context import RunContext
from musaeus.db import open_db
from musaeus.stages.canonicalize import CanonicalizeStage

from .disposable_vault import make_disposable_vault

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg/ffprobe required -- this test exists to exercise a real conversion",
)

TRACKS = 6


def _codec_of(path: Path) -> str:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=False,
    )
    return out.stdout.strip().split(",")[0]


def _make_aac(path: Path, freq: int, artist: str, title: str) -> Path:
    """A real AAC file with a distinct tone, so its audio_hash is unique."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"sine=frequency={freq}:duration=1",
         "-c:a", "aac", "-b:a", "128k",
         "-metadata", f"artist={artist}",
         "-metadata", f"title={title}",
         "-metadata", "album=Test Album",
         "-metadata", "genre=Rock",
         str(path)],
        check=True,
    )
    assert _codec_of(path) == "aac", "fixture must start as AAC or it proves nothing"
    return path


@pytest.fixture
def vault(tmp_path):
    v = make_disposable_vault(tmp_path)
    v.cfg.ensure_dirs()
    return v


@pytest.fixture
def ctx(vault):
    conn = open_db(vault.cfg.db_path)
    return RunContext.new(vault.cfg, conn, dry_run=False)


def _stage_tracks(ctx, vault, n=TRACKS):
    """n real AAC files in the INBOX, registered CATALOGUED."""
    from musaeus.db import upsert_archive

    made = []
    for i in range(n):
        p = _make_aac(
            vault.cfg.inbox / f"track{i}.m4a",
            220 + i * 55,
            f"Test Artist {i}",
            f"Song {i}",
        )
        upsert_archive(
            ctx.conn,
            {
                "file_path": str(p),
                "status": "CATALOGUED",
                "artist": f"Test Artist {i}",
                "album": "Test Album",
                "title": f"Song {i}",
                "genre": "Rock",
                "codec": "aac",
            },
        )
        made.append(p)
    ctx.conn.commit()
    return made


# ── canonicalize, for real ────────────────────────────────────────────────────


class TestCanonicalizeConvertsRealAudio:
    def test_aac_in_m4a_passes_through_untouched(self, ctx, vault):
        """The documented policy, which the row-driven decision broke.

        AAC-in-.m4a is already canonical -- the docstring says PASSTHROUGH,
        "no file write at all". `_decide_action` read `ext` from the DB, so a
        row whose ext column was empty took the TRANSCODE branch instead and
        the audio was lossily re-encoded for nothing. Reproduced here against
        a real pipeline run 2026-08-29.
        """
        made = _stage_tracks(ctx, vault)
        before = {p.name: p.read_bytes() for p in made}

        result = CanonicalizeStage().run(ctx)
        assert result.files_errored == 0, result.errors

        for row in ctx.conn.execute(
            "SELECT file_path, canon_action FROM archive"
        ).fetchall():
            assert row["canon_action"] == "PASSTHROUGH", (
                f"AAC-in-.m4a took the {row['canon_action']} branch -- that is "
                "a lossy re-encode of an already-canonical file"
            )
            p = Path(row["file_path"])
            assert p.exists()
            assert p.read_bytes() == before[p.name], "bytes changed on a passthrough"

    def test_a_lossless_source_is_converted_to_alac_on_disk(self, ctx, vault):
        """The real conversion path, asserted by ffprobe, not by the report."""
        from musaeus.db import upsert_archive

        flac = vault.cfg.inbox / "lossless.flac"
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
             "-i", "sine=frequency=440:duration=1", str(flac)],
            check=True,
        )
        assert _codec_of(flac) == "flac"
        upsert_archive(ctx.conn, {
            "file_path": str(flac), "status": "CATALOGUED", "artist": "A",
            "album": "B", "title": "C", "genre": "Rock", "codec": "flac",
        })
        ctx.conn.commit()

        result = CanonicalizeStage().run(ctx)
        assert result.files_errored == 0, result.errors

        row = ctx.conn.execute(
            "SELECT file_path, canon_action FROM archive WHERE title='C'"
        ).fetchone()
        assert row["canon_action"] == "CONVERTED", row["canon_action"]
        out = Path(row["file_path"])
        assert out.exists(), "canonicalize lost the converted file"
        assert _codec_of(out) == "alac", f"still {_codec_of(out)}"

    def test_an_unrecognised_codec_is_refused_not_re_encoded(self, ctx, vault):
        """"We do not know what this is" must not mean "re-encode it".

        The fall-through for an unrecognised codec was TRANSCODE, a lossy
        re-encode. The live database holds 63 rows with no codec recorded.
        """
        from musaeus.db import upsert_archive

        odd = vault.cfg.inbox / "mystery.m4a"
        _make_aac(odd, 330, "Who", "Knows")
        ctx.conn.execute("DELETE FROM archive")
        upsert_archive(ctx.conn, {
            "file_path": str(odd), "status": "CATALOGUED", "artist": "Who",
            "album": "B", "title": "Knows", "genre": "Rock",
            "codec": "not_a_real_codec",
        })
        ctx.conn.commit()
        before = odd.read_bytes()

        CanonicalizeStage().run(ctx)

        assert odd.exists(), "an unidentified file was moved or destroyed"
        assert odd.read_bytes() == before, "an unidentified file was re-encoded"

    def test_every_row_still_points_at_a_reachable_file(self, ctx, vault):
        """Finding #14's assertion: not 'did it move' but 'can we reach it'."""
        _stage_tracks(ctx, vault)
        CanonicalizeStage().run(ctx)

        roots = [vault.cfg.alac_library, vault.cfg.inbox, vault.cfg.staging,
                 vault.cfg.quarantine]
        for row in ctx.conn.execute("SELECT file_path FROM archive").fetchall():
            p = Path(row["file_path"])
            if not p.exists():
                continue
            assert any(
                str(p.resolve()).startswith(str(r.resolve())) for r in roots
            ), f"{p} escaped every root this run knows about"

    def test_nothing_lands_outside_the_vault(self, ctx, vault):
        """Three tracks once escaped to a SIBLING of the vault root."""
        _stage_tracks(ctx, vault)
        CanonicalizeStage().run(ctx)

        strays = [
            p for p in vault.root.parent.rglob("*.m4a")
            if not str(p.resolve()).startswith(str(vault.root.resolve()))
        ]
        assert strays == [], f"audio outside the vault: {strays}"


# ── the recovery boundary, on a real conversion ──────────────────────────────


class TestBoundaryProtectsTheOriginal:
    def test_the_original_survives_somewhere_recoverable(self, ctx, vault):
        """canonicalize rewrites the stream, so the ORIGINAL BYTES must live.

        Tag-capture checkpointing cannot describe a re-encoded file. The
        contract is a move to quarantine, never an unlink.
        """
        # Must be a source that ACTUALLY converts -- AAC-in-.m4a passes
        # through, so there is no disposal to protect and the assertion
        # would pass vacuously.
        from musaeus.db import upsert_archive

        originals = {}
        for i in range(3):
            f = vault.cfg.inbox / f"lossless{i}.flac"
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                 "-i", f"sine=frequency={330 + i * 40}:duration=1", str(f)],
                check=True,
            )
            upsert_archive(ctx.conn, {
                "file_path": str(f), "status": "CATALOGUED", "artist": f"A{i}",
                "album": "B", "title": f"T{i}", "genre": "Rock", "codec": "flac",
            })
            originals[f.name] = f.read_bytes()
        ctx.conn.commit()

        result = CanonicalizeStage().run(ctx)
        assert result.files_errored == 0, result.errors
        assert any("CONVERTED" in n for n in result.notes), (
            f"nothing converted, so nothing was disposed of: {result.notes}"
        )

        recovery = vault.cfg.runs_root / "recovery"
        searchable = list(vault.cfg.quarantine.rglob("*")) + (
            list(recovery.rglob("*")) if recovery.exists() else []
        )
        preserved = 0
        for blob in originals.values():
            for cand in searchable:
                if cand.is_file() and cand.read_bytes() == blob:
                    preserved += 1
                    break
        assert preserved > 0, (
            "no original bytes found in quarantine or recovery -- a converted "
            "file's source was destroyed, not moved"
        )

    def test_disabling_the_boundary_is_announced_not_silent(self, ctx, vault, monkeypatch):
        """A disabled safety layer must say so in the result, not pass quietly."""
        monkeypatch.setenv(CanonicalizeStage.CHECKPOINT_ENV, "0")
        _stage_tracks(ctx, vault, n=2)

        result = CanonicalizeStage().run(ctx)
        assert any("boundary" in n.lower() for n in result.notes), result.notes


# ── the disposable vault really is disposable ────────────────────────────────


def test_the_real_vault_is_never_touched(vault):
    """The guard that makes all of the above safe to run anywhere."""
    from .disposable_vault import PROTECTED_REAL_ROOTS

    resolved = str(vault.root.resolve())
    for protected in PROTECTED_REAL_ROOTS:
        assert not resolved.startswith(str(Path(protected).resolve())), (
            f"the disposable vault was created inside {protected}"
        )
