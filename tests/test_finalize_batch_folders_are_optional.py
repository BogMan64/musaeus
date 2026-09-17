"""Finalize files flat by default; the dated batch folder is opt-in.

The measurement that changed the default: 1,493 of 2,773 catalogued
artists sat in more than one folder, and 1,466 of those were split by the
`<library>/<batch date>/` layer alone -- with a correct name in every
copy. Elvis Presley was in four folders. That defeats Grey's standing
rule ("group them into one folder") and no amount of name-fixing reaches
it, because nothing about the names is wrong.

The layer is kept rather than deleted. Grey wants it back for the RC, so
what changed is the DEFAULT, not the capability. These tests pin both
halves: flat unless asked, and unchanged when asked.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from musaeus.stages.finalize import FinalizeStage, _batch_folders_enabled


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("MUSAEUS_BATCH_FOLDERS", raising=False)


class TestTheFlag:
    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " on "])
    def test_recognised_spellings_turn_it_on(self, monkeypatch, value):
        monkeypatch.setenv("MUSAEUS_BATCH_FOLDERS", value)
        assert _batch_folders_enabled() is True

    @pytest.mark.parametrize("value", ["", "0", "no", "off", "false", "maybe"])
    def test_everything_else_leaves_it_off(self, monkeypatch, value):
        monkeypatch.setenv("MUSAEUS_BATCH_FOLDERS", value)
        assert _batch_folders_enabled() is False

    def test_unset_is_off(self):
        assert _batch_folders_enabled() is False

    def test_it_is_read_at_call_time_not_import_time(self, monkeypatch):
        """A run that sets the variable must not need the module reloaded.
        Reading it into a module-level constant would make the flag look
        settable while silently having no effect."""
        assert _batch_folders_enabled() is False
        monkeypatch.setenv("MUSAEUS_BATCH_FOLDERS", "1")
        assert _batch_folders_enabled() is True


class _Cfg:
    def __init__(self, meta_dir: Path) -> None:
        self.meta_dir = meta_dir


class _Ctx:
    """The attributes _target_path actually touches.

    `config.meta_dir` joined the list when filing names landed: the stage
    reads MetaData/artist_filing.tsv to decide the folder. Pointing it at
    an empty tmp_path keeps these tests about batch folders -- with no
    filing file, every artist is filed under their own tag.
    """

    def __init__(self, lib: Path) -> None:
        self.alac_library = lib
        self.config = _Cfg(lib.parent / "MetaData")
        self._d: dict = {}

    def get(self, key, default=None):
        return self._d.get(key, default)

    def set(self, key, value):
        self._d[key] = value


ROW = {"artist": "Tom Petty", "album": "Damn the Torpedoes", "title": "Even the Losers"}


class TestTargetPath:
    def test_flat_by_default(self, tmp_path):
        ctx = _Ctx(tmp_path / "ALAC-Library")
        target = FinalizeStage()._target_path(ctx, ROW, Path("/src/x.m4a"))
        rel = target.relative_to(ctx.alac_library)
        assert rel.parts[0] == "Tom Petty", (
            f"expected the artist directly under the library, got {rel.parts[0]!r}"
        )
        assert rel.parts[1] == "Damn the Torpedoes"

    def test_no_empty_directory_component_is_created(self, tmp_path):
        """The bug this guards: `lib / "" / artist` is easy to write and
        yields a path that LOOKS right, so only checking the parts catches
        a stray empty component."""
        ctx = _Ctx(tmp_path / "ALAC-Library")
        target = FinalizeStage()._target_path(ctx, ROW, Path("/src/x.m4a"))
        assert "" not in target.parts
        assert "//" not in str(target)

    def test_the_flag_restores_the_dated_folder(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MUSAEUS_BATCH_FOLDERS", "1")
        ctx = _Ctx(tmp_path / "ALAC-Library")
        target = FinalizeStage()._target_path(ctx, ROW, Path("/src/x.m4a"))
        rel = target.relative_to(ctx.alac_library)
        assert len(rel.parts[0]) == 10 and rel.parts[0][4] == "-", (
            f"expected a YYYY-MM-DD batch folder, got {rel.parts[0]!r}"
        )
        assert rel.parts[1] == "Tom Petty"

    def test_an_explicit_override_still_wins_with_the_flag_off(self, tmp_path):
        """Tests and one-off runs pin the stamp through the context. That
        has to keep working independently of the default, or every test
        that sets it starts writing flat without saying so."""
        ctx = _Ctx(tmp_path / "ALAC-Library")
        ctx.set("finalize_batch_date", "2026-01-01")
        target = FinalizeStage()._target_path(ctx, ROW, Path("/src/x.m4a"))
        assert target.relative_to(ctx.alac_library).parts[0] == "2026-01-01"

    def test_two_artists_land_in_two_folders_not_one(self, tmp_path):
        ctx = _Ctx(tmp_path / "ALAC-Library")
        a = FinalizeStage()._target_path(ctx, ROW, Path("/src/x.m4a"))
        b = FinalizeStage()._target_path(
            ctx, {**ROW, "artist": "Elvis Presley"}, Path("/src/y.m4a")
        )
        assert a.relative_to(ctx.alac_library).parts[0] != b.relative_to(ctx.alac_library).parts[0]
