"""scripts/musaeus_purge_multichannel.py --downmix (Grey, 2026-10-24).

Surround is downmixed to stereo and the song is kept. Run as it was, --downmix
dropped the cover art, emptied the recording's identity (audio_hash), turned
AAC into ALAC, and DELETED the song when a downmix failed.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from musaeus.config import MusicConfig
from musaeus.db import open_db, upsert_archive

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg/ffprobe needed"
)

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "musaeus_purge_multichannel.py"


def _load():
    spec = importlib.util.spec_from_file_location("musaeus_purge_multichannel", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _probe(path: Path, entries: str) -> str:
    return subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", entries, "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip


@pytest.fixture
def vault(tmp_path, monkeypatch):
    cfg = MusicConfig(
        vault_root=tmp_path, inbox=tmp_path / "INBOX", staging=tmp_path / "STAGING",
        quarantine=tmp_path / "QUARANTINE", runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData", alac_library=tmp_path / "ALAC-Library",
        db_path=tmp_path / "musaeus.db",
    )  # fmt: skip
    cfg.ensure_dirs()
    song = tmp_path / "Libraries" / "Beck - The Paisley Experience.m4a"
    song.parent.mkdir(parents=True)
    # Audio first, the cover after with mutagen: ffmpeg 6.1 (CI) and 5.1 build
    # an attached-picture mux differently, and 6.1's had no audio stream.
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-ac", "6",
         "-c:a", "alac", "-metadata", "title=The Paisley Experience", str(song)],
        capture_output=True, check=True,
    )  # fmt: skip
    cover = tmp_path / "cover.jpg"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=red:s=32x32:d=1", "-frames:v", "1",
                    str(cover)], capture_output=True, check=True)  # fmt: skip
    from mutagen.mp4 import MP4, MP4Cover

    m = MP4(song)
    m.tags["covr"] = [MP4Cover(cover.read_bytes(), imageformat=MP4Cover.FORMAT_JPEG)]
    m.save()
    conn = open_db(cfg.db_path)
    from musaeus.hasher import audio_hash

    upsert_archive(conn, {"file_path": str(song), "status": "CATALOGUED", "artist": "Beck",
                          "title": "The Paisley Experience", "codec": "alac", "channels": 6,
                          "duration": 2.0, "audio_hash": audio_hash(song)})  # fmt: skip
    conn.execute("INSERT INTO archive_tier_hashes (path, sha256) VALUES (?, 'old')", (str(song),))
    conn.commit()
    conn.close()
    mod = _load()
    monkeypatch.setattr(mod, "get_config", lambda: cfg)
    monkeypatch.setattr(sys, "argv", ["prog", "--apply", "--downmix"])
    return mod, cfg, song


def _arrived(cfg):
    """The song's audio hash as it arrived (set from the real audio in the fixture)."""
    conn = open_db(cfg.db_path)
    try:
        return conn.execute("SELECT audio_hash FROM archive ORDER BY id LIMIT 1").fetchone()[0]
    finally:
        conn.close()


def _row(cfg):
    conn = open_db(cfg.db_path)
    try:
        return conn.execute("SELECT * FROM archive").fetchone()
    finally:
        conn.close()


def test_downmix_keeps_the_song_its_art_tags_and_identity(vault):
    mod, cfg, song = vault
    before = _arrived(cfg)  # captured first: the row after the run proves nothing (Codex, r14)
    assert mod.main() == 0
    assert song.exists()
    assert _probe(song, "stream=channels").split()[0].strip(",") == "2"
    assert "mjpeg" in _probe(song, "stream=codec_name"), "the cover art is kept"
    assert "The Paisley Experience" in _probe(song, "format_tags=title")
    row = _row(cfg)
    assert row["channels"] == 2 and row["audio_hash"] == before and row["full_hash"]
    conn = open_db(cfg.db_path)
    assert conn.execute("SELECT COUNT(*) FROM archive_tier_hashes").fetchone()[0] == 0
    conn.close()
    assert not cfg.tunemymusic_csv_path.exists()


def test_a_failed_downmix_keeps_the_song_as_it_is(vault, monkeypatch):
    mod, cfg, song = vault
    from musaeus.stages import canonicalize as canon

    def _fail(src, dest):
        raise canon.CanonicalizeError("ffmpeg failed")

    monkeypatch.setattr(canon, "_convert_to_alac", _fail)
    mod.main()
    assert song.exists() and _row(cfg) is not None and _row(cfg)["channels"] == 6


def _gen(path: Path, channels: int, codec: str, seconds: int) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-ac", str(channels),
         "-c:a", codec, "-f", "mp4", str(path)],
        capture_output=True, check=True,
    )  # fmt: skip


def test_the_file_decides_the_codec_so_lossless_stays_lossless(vault):
    """Codex review of #237 (reproduced): a row saying "aac" for 6-channel ALAC made
    the song lossy AAC."""
    mod, cfg, song = vault
    conn = open_db(cfg.db_path)
    conn.execute("UPDATE archive SET codec = 'aac'")
    conn.commit()
    conn.close()
    mod.main()
    assert "alac" in _probe(song, "stream=codec_name") and _row(cfg)["channels"] == 2


def test_a_shorter_copy_never_replaces_the_song(vault, monkeypatch):
    """Codex review of #237 (reproduced): Canonicalize's 2-second tolerance let a
    1-second copy replace a 2-second song in place."""
    mod, cfg, song = vault
    from musaeus.stages import canonicalize as canon

    monkeypatch.setattr(canon, "_convert_to_alac", lambda src, dest: _gen(dest, 2, "alac", 1))
    mod.main()
    assert _probe(song, "stream=channels").split()[0].strip(",") == "6", "the song is kept as it is"


def test_the_preview_of_a_downmix_says_downmix(vault, monkeypatch, capsys):
    """Codex review of #237 (reproduced): `--check --downmix` printed "would remove"."""
    mod, cfg, song = vault
    monkeypatch.setattr(sys, "argv", ["prog", "--check", "--downmix"])
    mod.main()
    out = capsys.readouterr().out
    assert "would downmix" in out and "would remove" not in out


def test_only_an_m4a_is_swapped_in_place_and_its_row_takes_the_new_codec(vault, tmp_path):
    """Codex review of #237, round 2 (reproduced): a 6-channel .flac would be written
    back as MP4 under its .flac name, the row still saying "flac"."""
    mod, cfg, song = vault
    flac = song.with_suffix(".flac")
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-ac", "6",
                    "-c:a", "flac", str(flac)], capture_output=True, check=True)  # fmt: skip
    conn = open_db(cfg.db_path)
    upsert_archive(conn, {"file_path": str(flac), "status": "CATALOGUED", "artist": "Beck",
                          "title": "Other", "codec": "flac", "channels": 6, "duration": 2.0,
                          "audio_hash": "flac-one"})  # fmt: skip
    conn.execute("UPDATE archive SET codec = 'aac' WHERE file_path = ?", (str(song),))
    conn.commit()
    conn.close()
    mod.main()
    assert _probe(flac, "stream=channels").split()[0].strip(",") == "6", (
        "a .flac is left for a person"
    )
    conn = open_db(cfg.db_path)
    codec = conn.execute("SELECT codec FROM archive WHERE file_path = ?", (str(song),)).fetchone()[
        0
    ]
    conn.close()
    assert codec == "alac", "the swapped .m4a's row says what was written"


def test_downmix_never_removes_the_row_of_a_missing_file(vault):
    """Codex review of #237, round 4 (reproduced): a song temporarily missing on
    disk lost its row and ledger entry under --downmix."""
    mod, cfg, song = vault
    moved = song.with_name("elsewhere.m4a")
    song.rename(moved)
    mod.main()
    assert _row(cfg) is not None


def test_downmix_never_overwrites_a_file_at_its_temporary_name(vault):
    """Codex review of #237, round 4 (reproduced): the fixed ".stereo.tmp" name was
    overwritten and then moved away."""
    mod, cfg, song = vault
    other = song.with_name(song.name + ".stereo.tmp")
    other.write_bytes(b"someone else's file")
    mod.main()
    assert other.read_bytes() == b"someone else's file"
    assert _probe(song, "stream=channels").split()[0].strip(",") == "2"


def test_the_preview_only_promises_what_apply_does(vault, monkeypatch, capsys):
    """Codex review of #237, rounds 3-4 (reproduced): a surround .flac was previewed as
    "would downmix", and apply leaves it for a person."""
    from musaeus.hasher import audio_hash

    mod, cfg, song = vault
    flac = song.with_suffix(".flac")
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-ac", "6",
                    "-c:a", "flac", str(flac)], capture_output=True, check=True)  # fmt: skip
    conn = open_db(cfg.db_path)
    upsert_archive(conn, {"file_path": str(flac), "status": "CATALOGUED", "artist": "Beck",
                          "title": "Other", "codec": "flac", "channels": 6,
                          "audio_hash": audio_hash(flac)})  # fmt: skip
    conn.commit()
    conn.close()
    monkeypatch.setattr(sys, "argv", ["prog", "--check", "--downmix"])
    mod.main()
    out = capsys.readouterr().out
    assert "would downmix to stereo: 6ch  Beck - Other" not in out and "Beck - Other" in out


def test_a_refused_downmix_leaves_no_temporary_file(vault):
    mod, cfg, song = vault
    flac = song.with_suffix(".flac")
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-ac", "6",
                    "-c:a", "flac", str(flac)], capture_output=True, check=True)  # fmt: skip
    conn = open_db(cfg.db_path)
    upsert_archive(conn, {"file_path": str(flac), "status": "CATALOGUED", "artist": "Beck",
                          "title": "Other", "codec": "flac", "channels": 6, "audio_hash": "f"})  # fmt: skip
    conn.commit()
    conn.close()
    mod.main()
    assert not list(song.parent.glob(".stereo-*"))


def test_a_short_audio_stream_hidden_by_a_long_container_never_replaces_the_song(
    vault, monkeypatch
):
    """Codex review of #237, round 5 (reproduced): a copy whose container said 2 s but
    whose audio stream was 1 s replaced the 2-second song."""
    mod, cfg, song = vault
    from musaeus.stages import canonicalize as canon

    def _short(src, dest):
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                        "-f", "lavfi", "-i", "color=c=red:s=32x32:d=2", "-map", "0:a", "-map", "1:v",
                        "-ac", "2", "-c:a", "alac", "-c:v", "mjpeg", "-f", "mp4", str(dest)],
                       capture_output=True, check=True)  # fmt: skip

    monkeypatch.setattr(canon, "_convert_to_alac", _short)
    monkeypatch.setattr(canon, "_verify_conversion", lambda src, dest: 2)
    mod.main()
    assert _probe(song, "stream=channels").split()[0].strip(",") == "6"


def test_a_failure_after_the_encode_leaves_the_song_and_its_row_as_they_were(vault, monkeypatch):
    """Codex review of #237, round 5 (reproduced): hashing failed after the swap; the
    file was stereo and the row still said 6 channels."""
    mod, cfg, song = vault
    import musaeus.hasher as hasher

    def _boom(path):
        raise OSError("disk hiccup")

    monkeypatch.setattr(hasher, "file_hash", _boom)
    with pytest.raises(OSError):
        mod.main()
    assert (
        _probe(song, "stream=channels").split()[0].strip(",") == "6" and _row(cfg)["channels"] == 6
    )
    assert not list(song.parent.glob(".stereo-*")) and not list(song.parent.glob(".orig-*"))


def test_a_failed_install_leaves_the_song_at_its_path(vault, monkeypatch):
    """Codex review of #237, round 6 (reproduced): the original was moved aside, the
    install failed, and the song was left under a temporary name."""
    mod, cfg, song = vault
    real = os.replace

    def _fail_install(src, dst):
        if str(dst) == str(song) and ".stereo-" in str(src):
            raise OSError("install failed")
        return real(src, dst)

    monkeypatch.setattr(mod.os, "replace", _fail_install)
    with pytest.raises(OSError):
        mod.main()
    assert (
        _probe(song, "stream=channels").split()[0].strip(",") == "6" and _row(cfg)["channels"] == 6
    )
    assert not list(song.parent.glob(".stereo-*")) and not list(song.parent.glob(".orig-*"))


def test_identity_tags_and_permissions_survive_the_downmix(vault):
    """Codex review of #237, round 8 (both reproduced): the MusicBrainz recording ID
    (a freeform MP4 tag) was dropped by the encode, and the song became 0600."""
    from mutagen.mp4 import MP4, MP4FreeForm

    mod, cfg, song = vault
    m = MP4(song)
    m.tags["----:com.apple.iTunes:MusicBrainz Track Id"] = [MP4FreeForm(b"rec-123")]
    m.tags["----:com.apple.iTunes:replaygain_track_gain"] = [MP4FreeForm(b"-3.00 dB")]
    m.save()
    os.chmod(song, 0o644)
    mod.main()
    tags = MP4(song).tags
    assert bytes(tags["----:com.apple.iTunes:MusicBrainz Track Id"][0]) == b"rec-123"
    assert "----:com.apple.iTunes:replaygain_track_gain" not in tags, "loudness is measured again"
    assert oct(os.stat(song).st_mode & 0o777) == "0o644"


def test_the_checks_run_on_the_final_file_after_its_tags(vault, monkeypatch):
    """Codex review of #237, round 9 (reproduced): a tag rewrite that damaged the
    copy ran after the checks, and an unreadable file replaced the song."""
    mod, cfg, song = vault
    import musaeus.edition_bake as eb

    def _damage(src, dest, marker):
        dest.write_bytes(dest.read_bytes()[:200])

    monkeypatch.setattr(eb, "copy_tags", _damage)
    mod.main()
    assert _probe(song, "stream=channels").split()[0].strip(",") == "6"


def test_the_old_loudness_and_bitrate_do_not_describe_the_new_audio(vault):
    """Codex review of #237, round 10 (reproduced): lufs, lufs_tp, rg_gain, rg_peak and
    the bitrate still described the 6-channel audio after the downmix."""
    mod, cfg, song = vault
    conn = open_db(cfg.db_path)
    conn.execute(
        "UPDATE archive SET lufs = -9.0, lufs_tp = -0.1, rg_gain = -5.0, rg_peak = 0.99, bitrate = 9999999"
    )
    conn.commit()
    conn.close()
    mod.main()
    row = _row(cfg)
    assert (row["lufs"], row["lufs_tp"], row["rg_gain"], row["rg_peak"]) == (None, None, None, None)
    assert row["bitrate"] and row["bitrate"] != 9999999


def test_the_lossless_edition_copy_is_made_again_from_the_stereo_master(vault):
    """Codex review of #237, round 11 (reproduced): the edition planner trusts the
    kept audio_hash and only retags, so the Lossless copy stayed 6-channel. The AAC
    editions fold surround to stereo already and are left alone."""
    from musaeus import edition_ledger as el

    mod, cfg, song = vault
    from musaeus import edition_bake, edition_build

    lossless = Path(cfg.alac_library) / "song.m4a"
    lossless.parent.mkdir(parents=True, exist_ok=True)
    _gen(lossless, 6, "alac", 2)
    edition_bake.copy_tags(lossless, lossless, edition_build.marker_for(_arrived(cfg)))
    conn = el.open_ledger(el.ledger_path(cfg))
    for edition, out in (("lossless", lossless), ("car", cfg.vault_root / "CAR" / "song.m4a")):
        conn.execute(
            "INSERT INTO edition_copies (edition, master_hash, master_path, output_path, built_at) "
            "VALUES (?, ?, ?, ?, '2026-10-01')",
            (edition, _arrived(cfg), str(song), str(out)),
        )
    conn.execute(
        "INSERT INTO measurements VALUES (?, 'lossless/x', '{}', '2026-10-01')", (_arrived(cfg),)
    )
    conn.commit()
    conn.close()
    mod.main()
    conn = el.open_ledger(el.ledger_path(cfg))
    left = {r[0] for r in conn.execute("SELECT edition FROM edition_copies")}
    measured = conn.execute("SELECT COUNT(*) FROM measurements").fetchone()[0]
    conn.close()
    assert not lossless.exists() and left == {"car"} and measured == 0


def test_a_stale_lossless_record_never_deletes_another_song(vault):
    """Codex review of #237, round 12 (reproduced): the recorded Lossless path held a
    different song now, and it was deleted."""
    from musaeus import edition_bake, edition_build
    from musaeus import edition_ledger as el

    mod, cfg, song = vault
    other = Path(cfg.alac_library) / "other.m4a"
    other.parent.mkdir(parents=True, exist_ok=True)
    _gen(other, 2, "alac", 1)
    edition_bake.copy_tags(other, other, edition_build.marker_for("someone-else"))
    conn = el.open_ledger(el.ledger_path(cfg))
    conn.execute(
        "INSERT INTO edition_copies (edition, master_hash, master_path, output_path, built_at) "
        "VALUES ('lossless', ?, ?, ?, '2026-10-01')",
        (_arrived(cfg), str(song), str(other)),
    )
    conn.commit()
    conn.close()
    mod.main()
    assert other.exists()


def test_another_surround_song_in_its_place_is_never_changed(vault):
    """Codex review of #237, round 13 (reproduced): a different 6-channel song now at
    the catalogued path was downmixed under the old song's identity."""
    mod, cfg, song = vault
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=880:duration=2", "-ac", "6",
                    "-c:a", "alac", "-f", "mp4", str(song)], capture_output=True, check=True)  # fmt: skip
    mod.main()
    assert (
        _probe(song, "stream=channels").split()[0].strip(",") == "6" and _row(cfg)["channels"] == 6
    )


def test_a_failed_lossless_cleanup_rolls_the_swap_back_for_a_retry(vault, monkeypatch):
    """Codex review of #237, round 13 (reproduced): the stereo row committed before the
    old Lossless copy was removed; when that failed, a rerun found no surround row."""
    mod, cfg, song = vault

    def _fail(cfg_, h, master):
        raise OSError("cleanup failed")

    monkeypatch.setattr(mod, "_remake_lossless_copy", _fail)
    with pytest.raises(OSError):
        mod.main()
    assert (
        _probe(song, "stream=channels").split()[0].strip(",") == "6" and _row(cfg)["channels"] == 6
    )


def test_extended_attributes_survive_the_downmix(vault):
    """Codex review of #237, round 14 (reproduced): a user.* attribute on the master
    was lost when the stereo copy replaced it."""
    mod, cfg, song = vault
    try:
        os.setxattr(song, "user.musaeus_note", b"kept")
    except OSError:
        pytest.skip("this filesystem has no user extended attributes")
    mod.main()
    assert os.getxattr(song, "user.musaeus_note") == b"kept"
    assert _probe(song, "stream=channels").split()[0].strip(",") == "2"


def test_an_unreadable_attribute_list_stops_the_swap(vault, monkeypatch):
    """Codex review of #237, round 15 (reproduced): any error listing the attributes
    was read as "none", and the master was replaced without them."""
    import errno

    mod, cfg, song = vault

    def _io(path):
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(mod.os, "listxattr", _io)
    mod.main()
    assert _probe(song, "stream=channels").split()[0].strip(",") == "6"


@pytest.mark.parametrize("where", ["the master", "a car copy"])
def test_a_stale_lossless_record_never_deletes_outside_the_lossless_edition(vault, where):
    """Codex review of #237, round 16 (reproduced): a stale record naming the master,
    or the song's Car copy, carried this song's marker -- and was deleted."""
    from musaeus import edition_bake, edition_build
    from musaeus import edition_ledger as el

    mod, cfg, song = vault
    if where == "the master":
        target = song
    else:
        target = cfg.vault_root / "CAR_Library" / "song.m4a"
        target.parent.mkdir(parents=True, exist_ok=True)
        _gen(target, 2, "aac", 2)
    edition_bake.copy_tags(target, target, edition_build.marker_for(_arrived(cfg)))
    conn = el.open_ledger(el.ledger_path(cfg))
    conn.execute(
        "INSERT INTO edition_copies (edition, master_hash, master_path, output_path, built_at) "
        "VALUES ('lossless', ?, ?, ?, '2026-10-01')",
        (_arrived(cfg), str(song), str(target)),
    )
    conn.commit()
    conn.close()
    mod.main()
    assert target.exists() and song.exists()


def test_a_downmix_runs_beside_todays_wanted_list(vault):
    """First real run, 2026-10-25: TuneMyMusic.csv is now Title,Artist,Album, and the
    script read a "path" column from it even in --downmix mode, which never writes
    it -- KeyError before any song was touched."""
    mod, cfg, song = vault
    cfg.tunemymusic_csv_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.tunemymusic_csv_path.write_text(
        "Title,Artist,Album\nShotgun,Jr. Walker,Shotgun\n", encoding="utf-8"
    )
    assert mod.main() == 0
    assert _probe(song, "stream=channels").split()[0].strip(",") == "2"
    assert cfg.tunemymusic_csv_path.read_text(encoding="utf-8").startswith("Title,Artist,Album")


def test_the_delete_mode_refuses_todays_wanted_list(vault, monkeypatch):
    """Codex review of #238 (reproduced): with a Title,Artist,Album list the delete
    mode wrote an incompatible row and deleted the surround song."""
    mod, cfg, song = vault
    cfg.tunemymusic_csv_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.tunemymusic_csv_path.write_text(
        "Title,Artist,Album\nShotgun,Jr. Walker,Shotgun\n", encoding="utf-8"
    )
    monkeypatch.setattr(sys, "argv", ["prog", "--apply"])
    assert mod.main() != 0
    assert song.exists() and _row(cfg) is not None
    assert (
        cfg.tunemymusic_csv_path.read_text(encoding="utf-8")
        == "Title,Artist,Album\nShotgun,Jr. Walker,Shotgun\n"
    )
