"""
MUSAEUS — typed export-root resolution for Curator (P0-15, P0-03)

DR-07 asks for a typed `exports.curator.root`, a one-invocation
`--export-root` override, a redacted configuration identity, and a
fail-closed block before Curator builds anything or creates a directory.

What was actually there, verified against the running code:

  * `CuratorStage._get_export_root` falls back to
    `getattr(ctx.config, "car_export_root", None)`. `MusicConfig` has no
    such field -- confirmed by inspecting its dataclass fields -- so the
    fallback is dead and has always been dead. The code reads as though
    the export root can be configured; it cannot. Every invocation must
    pass `--export-root`, and anyone who set a config value would have
    watched it be ignored in silence. This is OPEN_ITEMS finding #6
    exactly: a `getattr` default standing in for an attribute that does
    not exist.
  * `validate()` accepts any non-None value and explicitly does not
    require it to exist, and `run()` then calls
    `export_root.mkdir(parents=True, exist_ok=True)`. A typo in the flag
    creates a new directory tree and reports success at copying files
    into it.
  * `--big-kahuna` and `BIG_KAHUNA_PIPELINE` do not exist anywhere in
    this codebase. The half of P0-15 addressed to them has no target;
    what remains live is `musaeus curator --export-root`.

Resolution order here is explicit and has no hidden default: the
one-invocation override, then the typed configuration value, then
nothing. "Nothing" is a refusal, never an invented path.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

CONFIG_KEY = "exports.curator.root"
ENV_VAR = "MUSAEUS_CURATOR_EXPORT_ROOT"

SOURCE_OVERRIDE = "invocation_override"
SOURCE_CONFIG = "configuration"
SOURCE_ABSENT = "absent"

# Held back before calling free space usable, matching preflight's default.
DEFAULT_RESERVE_BYTES = 2 * 10**9


class ExportRootError(RuntimeError):
    """The export root cannot be used. Carries a reason code and a
    remediation, and is raised before anything is built or created."""

    def __init__(self, message: str, *, reason_code: str, remediation: str, **details: object):
        super().__init__(message)
        self.reason_code = reason_code
        self.remediation = remediation
        self.details = details


@dataclass(frozen=True)
class ResolvedExportRoot:
    path: Path
    source: str

    @property
    def identity(self) -> str:
        """Redacted configuration identity.

        DR-07 asks for the resolved value to be recorded as a redacted
        identity. A digest correlates two runs that used the same root
        without publishing where someone's USB stick is mounted."""
        return "exportroot:" + hashlib.sha256(str(self.path).encode("utf-8")).hexdigest()[:16]

    def describe(self) -> dict[str, object]:
        return {"config_key": CONFIG_KEY, "source": self.source, "identity": self.identity}


def resolve_export_root(
    *, override: str | Path | None, configured: str | Path | None
) -> ResolvedExportRoot | None:
    """
    Resolve the export root, or return None.

    None means "not configured", and the caller must refuse. This function
    deliberately cannot invent a path: there is no default branch, no
    `or Path.home() / ...`, and no environment read hidden inside it. A
    default export root is a directory the operator did not choose,
    holding a copy of their library.
    """
    if override is not None and str(override).strip():
        return ResolvedExportRoot(path=Path(str(override)).expanduser(), source=SOURCE_OVERRIDE)
    if configured is not None and str(configured).strip():
        return ResolvedExportRoot(path=Path(str(configured)).expanduser(), source=SOURCE_CONFIG)
    return None


def configured_export_root(config: object) -> str | None:
    """
    Read the typed configuration value.

    Explicit `hasattr`, not `getattr(config, name, None)`. The default
    form is what made the previous fallback dead and undetectable: it
    returns None both when the value is unset and when the attribute has
    never existed, and those need different answers.
    """
    if not hasattr(config, "curator_export_root"):
        return None
    value = config.curator_export_root
    return None if value is None else str(value)


def validate_export_root(
    resolved: ResolvedExportRoot | None,
    *,
    protected_roots: tuple[Path, ...] = (),
    required_bytes: int = 0,
    reserve_bytes: int = DEFAULT_RESERVE_BYTES,
) -> ResolvedExportRoot:
    """
    Check the root before Curator constructs anything.

    Non-empty, accessible, non-overlapping with any protected root, and
    capacious enough. Raises rather than returning a verdict, and creates
    nothing -- P0-03's requirement is that the guard "must not invent a
    default, create a directory, start Curator, or mutate configuration",
    and the way to guarantee that is for this function to have no code
    that could.
    """
    if resolved is None:
        raise ExportRootError(
            f"no export root is set. Pass --export-root, or set {CONFIG_KEY} "
            f"({ENV_VAR}) in your configuration.",
            reason_code="configuration_invalid",
            remediation=f"pass --export-root PATH or set {ENV_VAR}",
            config_key=CONFIG_KEY,
        )

    path = resolved.path
    if not path.is_absolute():
        raise ExportRootError(
            f"export root {path} is not absolute; a relative export root depends on the "
            f"working directory the command happened to be run from",
            reason_code="configuration_invalid",
            remediation="give an absolute path",
        )

    for protected in protected_roots:
        protected_str = os.path.normpath(str(protected))
        candidate = os.path.normpath(str(path))
        if candidate == protected_str or candidate.startswith(protected_str + os.sep):
            raise ExportRootError(
                f"export root {path} lies inside {protected}; exporting a library into "
                f"itself would have Curator copying its own output back over its source",
                reason_code="scope_overlap",
                remediation="choose an export root outside the library",
            )
        if protected_str.startswith(candidate + os.sep):
            raise ExportRootError(
                f"export root {path} contains {protected}; the export would be written "
                f"around the library it is derived from",
                reason_code="scope_overlap",
                remediation="choose an export root that does not contain the library",
            )

    if not path.exists():
        raise ExportRootError(
            f"export root {path} does not exist. It is not created for you: a typo in "
            f"--export-root would otherwise silently build a new library somewhere "
            f"nobody chose.",
            reason_code="configuration_invalid",
            remediation=f"create {path} first, or correct the path",
        )
    if not path.is_dir():
        raise ExportRootError(
            f"export root {path} is not a directory",
            reason_code="configuration_invalid",
            remediation="give a directory path",
        )
    if not os.access(path, os.W_OK):
        raise ExportRootError(
            f"export root {path} is not writable",
            reason_code="configuration_invalid",
            remediation=f"grant write permission on {path}",
        )

    if required_bytes:
        usage = shutil.disk_usage(str(path))
        usable = max(0, usage.free - reserve_bytes)
        if required_bytes > usable:
            raise ExportRootError(
                f"export root {path} has {usable} safely usable bytes; this export needs "
                f"{required_bytes}",
                reason_code="insufficient_capacity",
                remediation="free space on the export target or reduce the export",
                measured=usable,
                required=required_bytes,
            )

    return resolved
