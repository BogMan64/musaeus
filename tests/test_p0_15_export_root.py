"""
P0-15 / P0-03 — typed Curator export root, resolved and validated before
anything is built or created.

Three findings sit behind these tests, all verified against the running
code rather than inferred from the spec:

  1. `CuratorStage._get_export_root` ended in
     `getattr(ctx.config, "car_export_root", None)` and `MusicConfig` has
     no such field. The "fall back to configuration" branch was dead and
     always had been -- OPEN_ITEMS finding #6's shape exactly. Anyone who
     set a configuration value watched it be ignored in silence.
  2. `validate()` accepted any non-None value and explicitly did not
     require it to exist; `run()` then called `mkdir(parents=True)`. A
     typo in --export-root silently built a new library tree somewhere
     nobody chose and reported success copying into it.
  3. `--big-kahuna` and `BIG_KAHUNA_PIPELINE` do not exist anywhere in
     this codebase. Half of P0-15 as written has no target; the live
     concern is `musaeus curator --export-root`.

A fourth was found while writing these: `CuratorStage.validate()` had no
test coverage at all. A NameError introduced into it passed the entire
1057-test suite.
"""

from __future__ import annotations

import inspect
import dataclasses
import os
import stat
from pathlib import Path

import pytest

from musaeus import cli as cli_mod
from musaeus.config import MusicConfig
from musaeus.exports import (
    CONFIG_KEY,
    ENV_VAR,
    SOURCE_CONFIG,
    SOURCE_OVERRIDE,
    ExportRootError,
    configured_export_root,
    resolve_export_root,
    validate_export_root,
)
from musaeus.stages.base import StageError
from musaeus.stages.curator import CuratorStage


@pytest.fixture
def export_root(tmp_path: Path) -> Path:
    root = tmp_path / "usb-export"
    root.mkdir()
    return root


# ── The dead configuration fallback ───────────────────────────────────────────


class TestConfigurationActuallyWorks:
    def test_the_typed_field_exists(self):
        """The regression for finding #1. `car_export_root` was never a
        field on MusicConfig, so the fallback that read it could not fire."""
        fields = {f.name for f in dataclasses.fields(MusicConfig)}
        assert "curator_export_root" in fields
        assert "car_export_root" not in fields, (
            "the old name is the one that never existed; do not resurrect it"
        )

    def test_configured_export_root_reads_the_typed_field(self, export_root):
        config = _config(tmp_path=export_root.parent, curator_export_root=export_root)
        assert configured_export_root(config) == str(export_root)

    def test_a_config_without_the_field_returns_none_explicitly(self):
        """hasattr, not getattr-with-default. The default form returns None
        both when the value is unset and when the attribute never existed,
        and those need different answers."""

        class OldConfig:
            pass

        assert configured_export_root(OldConfig()) is None

    def test_the_env_var_populates_the_field(self, export_root, monkeypatch):
        monkeypatch.setenv("MUSAEUS_VAULT_ROOT", str(export_root.parent / "vault"))
        monkeypatch.setenv(ENV_VAR, str(export_root))
        config = MusicConfig.from_env()
        assert config.curator_export_root == export_root


# ── Resolution order, and the absence of a default ────────────────────────────


class TestResolution:
    def test_the_invocation_override_wins(self, tmp_path):
        resolved = resolve_export_root(override=tmp_path / "a", configured=tmp_path / "b")
        assert resolved.path == tmp_path / "a"
        assert resolved.source == SOURCE_OVERRIDE

    def test_configuration_is_used_when_there_is_no_override(self, tmp_path):
        resolved = resolve_export_root(override=None, configured=tmp_path / "b")
        assert resolved.path == tmp_path / "b"
        assert resolved.source == SOURCE_CONFIG

    def test_nothing_configured_resolves_to_none_not_a_default(self):
        """A default export root is a directory the operator did not
        choose, holding a copy of their library."""
        assert resolve_export_root(override=None, configured=None) is None

    def test_an_empty_string_is_not_a_value(self, tmp_path):
        assert resolve_export_root(override="", configured="  ") is None
        assert resolve_export_root(override="", configured=tmp_path).source == SOURCE_CONFIG

    def test_the_resolved_value_is_recorded_as_a_redacted_identity(self, tmp_path):
        resolved = resolve_export_root(override=tmp_path / "usb", configured=None)
        assert resolved.identity.startswith("exportroot:")
        assert str(tmp_path) not in resolved.identity
        assert resolved.describe()["config_key"] == CONFIG_KEY

    def test_the_identity_is_stable_for_the_same_root(self, tmp_path):
        a = resolve_export_root(override=tmp_path / "usb", configured=None)
        b = resolve_export_root(override=None, configured=tmp_path / "usb")
        assert a.identity == b.identity, "the same root must correlate across runs"


# ── Validation blocks before anything is created ──────────────────────────────


class TestValidation:
    def test_a_missing_root_is_refused_and_names_the_config_key(self):
        with pytest.raises(ExportRootError) as exc:
            validate_export_root(None)
        assert exc.value.reason_code == "configuration_invalid"
        assert CONFIG_KEY in str(exc.value)
        assert ENV_VAR in str(exc.value)

    def test_a_nonexistent_root_is_refused_not_created(self, tmp_path):
        """Finding #2. run() called mkdir(parents=True), so a typo built a
        library tree somewhere nobody chose."""
        typo = tmp_path / "mnt" / "USBB"
        resolved = resolve_export_root(override=typo, configured=None)
        with pytest.raises(ExportRootError) as exc:
            validate_export_root(resolved)
        assert "does not exist" in str(exc.value)
        assert not typo.exists(), "the guard must not create the path it rejected"
        assert not typo.parent.exists()

    def test_an_existing_writable_root_passes(self, export_root):
        """Negative control."""
        resolved = resolve_export_root(override=export_root, configured=None)
        assert validate_export_root(resolved).path == export_root

    def test_a_relative_root_is_refused(self):
        resolved = resolve_export_root(override="relative/export", configured=None)
        with pytest.raises(ExportRootError) as exc:
            validate_export_root(resolved)
        assert "not absolute" in str(exc.value)

    def test_a_file_is_not_a_directory(self, tmp_path):
        target = tmp_path / "not-a-dir"
        target.write_text("x")
        resolved = resolve_export_root(override=target, configured=None)
        with pytest.raises(ExportRootError) as exc:
            validate_export_root(resolved)
        assert "not a directory" in str(exc.value)

    def test_an_unwritable_root_is_refused(self, export_root):
        original = stat.S_IMODE(export_root.stat().st_mode)
        export_root.chmod(0o555)
        try:
            if os.access(export_root, os.W_OK):
                pytest.skip("running with write override (root)")
            resolved = resolve_export_root(override=export_root, configured=None)
            with pytest.raises(ExportRootError) as exc:
                validate_export_root(resolved)
            assert "not writable" in str(exc.value)
        finally:
            export_root.chmod(original)

    def test_a_root_inside_the_library_is_refused(self, tmp_path):
        """Curator derives its export FROM the library; exporting into it
        would have Curator copying its own output back over its source."""
        library = tmp_path / "ALAC-Library"
        library.mkdir()
        inside = library / "export"
        inside.mkdir()
        resolved = resolve_export_root(override=inside, configured=None)
        with pytest.raises(ExportRootError) as exc:
            validate_export_root(resolved, protected_roots=(library,))
        assert exc.value.reason_code == "scope_overlap"

    def test_a_root_containing_the_library_is_refused(self, tmp_path):
        library = tmp_path / "vault" / "ALAC-Library"
        library.mkdir(parents=True)
        resolved = resolve_export_root(override=tmp_path / "vault", configured=None)
        with pytest.raises(ExportRootError) as exc:
            validate_export_root(resolved, protected_roots=(library,))
        assert exc.value.reason_code == "scope_overlap"

    def test_a_sibling_directory_is_not_an_overlap(self, tmp_path):
        """Negative control: a raw prefix match would wrongly reject this."""
        library = tmp_path / "ALAC-Library"
        library.mkdir()
        sibling = tmp_path / "ALAC-Library-Export"
        sibling.mkdir()
        resolved = resolve_export_root(override=sibling, configured=None)
        assert validate_export_root(resolved, protected_roots=(library,)).path == sibling

    def test_insufficient_capacity_is_refused_with_measurements(self, export_root):
        resolved = resolve_export_root(override=export_root, configured=None)
        with pytest.raises(ExportRootError) as exc:
            validate_export_root(resolved, required_bytes=10**18)
        assert exc.value.reason_code == "insufficient_capacity"
        assert exc.value.details["required"] == 10**18
        assert "measured" in exc.value.details


# ── The stage guard (P0-03) ───────────────────────────────────────────────────


def _config(tmp_path: Path, **over) -> MusicConfig:
    base = {
        "vault_root": tmp_path / "vault",
        "inbox": tmp_path / "vault" / "INBOX",
        "staging": tmp_path / "vault" / "STAGING",
        "quarantine": tmp_path / "vault" / "QUARANTINE",
        "runs_root": tmp_path / "vault" / "RUNS",
        "meta_dir": tmp_path / "vault" / "MetaData",
        "alac_library": tmp_path / "vault" / "ALAC-Library",
        "db_path": tmp_path / "vault" / "musaeus.db",
    }
    base.update(over)
    return MusicConfig(**base)


class _FakeContext:
    """Minimal RunContext stand-in: validate() only reads the stash and
    the config."""

    def __init__(self, config: MusicConfig, **stash):
        self.config = config
        self._stash = dict(stash)

    def get(self, key, default=None):
        return self._stash.get(key, default)

    def set(self, key, value):
        self._stash[key] = value


class TestCuratorStageGuard:
    def test_validate_blocks_with_no_export_root(self, tmp_path):
        """P0-03's guard: fail closed before the stage builds anything."""
        ctx = _FakeContext(_config(tmp_path))
        with pytest.raises(StageError) as exc:
            CuratorStage().validate(ctx)
        assert CONFIG_KEY in str(exc.value)
        assert "--export-root" in str(exc.value)

    def test_validate_blocks_a_nonexistent_root_without_creating_it(self, tmp_path):
        typo = tmp_path / "mnt" / "USBB"
        ctx = _FakeContext(_config(tmp_path), curator_export_root=typo)
        with pytest.raises(StageError):
            CuratorStage().validate(ctx)
        assert not typo.exists()

    def test_validate_blocks_an_export_root_inside_the_library(self, tmp_path):
        config = _config(tmp_path)
        config.alac_library.mkdir(parents=True)
        inside = config.alac_library / "export"
        inside.mkdir()
        ctx = _FakeContext(config, curator_export_root=inside)
        with pytest.raises(StageError) as exc:
            CuratorStage().validate(ctx)
        assert "inside" in str(exc.value)

    def test_validate_accepts_a_good_root_and_records_a_redacted_identity(
        self, tmp_path, export_root
    ):
        """Negative control, and the regression for the fourth finding:
        this path had NO test coverage -- a NameError introduced into
        validate() passed the entire suite."""
        ctx = _FakeContext(_config(tmp_path), curator_export_root=export_root)
        CuratorStage().validate(ctx)
        assert ctx.get("curator_export_root_identity").startswith("exportroot:")
        assert str(export_root) not in ctx.get("curator_export_root_identity")
        assert ctx.get("curator_export_root_source") == SOURCE_OVERRIDE

    def test_configuration_alone_is_enough_now(self, tmp_path, export_root):
        """The branch that was dead. No --export-root passed; the typed
        configuration value is used and accepted."""
        config = _config(tmp_path, curator_export_root=export_root)
        ctx = _FakeContext(config)
        CuratorStage().validate(ctx)
        assert ctx.get("curator_export_root_source") == SOURCE_CONFIG


# ── The obsolete half of P0-15 ────────────────────────────────────────────────


class TestBigKahunaIsGone:
    def test_no_big_kahuna_pipeline_exists(self):
        """P0-15 is addressed to `--big-kahuna`. It does not exist in this
        codebase -- neither the flag nor BIG_KAHUNA_PIPELINE. Recorded as a
        test so the spec's obsolescence is visible rather than argued."""
        import musaeus.stages as stages

        assert not [n for n in dir(stages) if "KAHUNA" in n.upper()]
        cli_source = inspect.getsource(cli_mod)
        assert "big-kahuna" not in cli_source
        assert "big_kahuna" not in cli_source
