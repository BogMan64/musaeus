"""
MUSAEUS — checkpoint manifests (P0-12)

A manifest is the record of what a checkpoint covers, entry by entry, so
a rollback can restore exactly what was captured and can tell when it
has not. It is ordered and immutable: entries sort by their relative
path, and the digest is taken over that ordered content, so the same tree
always produces the same digest and any change to any covered attribute
changes it.

MCR-003 requires coverage of identity, hash, metadata, artwork and
database references. Each is a separate field rather than one blob,
because "the file changed" and "only its artwork changed" call for
different responses, and a single digest cannot tell them apart.

`item_ref` is a stable internal identifier, not a path. DR-02 is explicit
that shareable reports carry the reference only, while a local restricted
manifest maps it to a path -- so the reference has to exist as its own
thing from the start rather than being retrofitted by redacting strings
later.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

_CHUNK = 1024 * 1024

KIND_FILE = "file"
KIND_DATABASE = "database"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def item_ref_for(relative_path: str) -> str:
    """Stable, path-derived, non-reversible reference.

    Derived rather than random so the same item gets the same reference
    across runs, and hashed so a shareable report carrying the reference
    does not leak the directory layout of someone's music library."""
    return hashlib.sha256(relative_path.encode("utf-8")).hexdigest()[:24]


def _tag_digests(path: Path) -> tuple[str | None, str | None]:
    """(metadata_digest, artwork_digest), best effort.

    Returns (None, None) for anything mutagen cannot parse -- most test
    fixtures, and any non-audio file. None means "not captured", which is
    deliberately distinct from a digest of empty tags: the first is a
    limitation, the second is a fact about the file.
    """
    try:
        import mutagen  # type: ignore[import-untyped]

        audio = mutagen.File(str(path))
        if audio is None or audio.tags is None:
            return (None, None)
        items = {str(k): str(v) for k, v in audio.tags.items() if not str(k).startswith("covr")}
        metadata = hashlib.sha256(json.dumps(items, sort_keys=True).encode("utf-8")).hexdigest()
        artwork = None
        covers = audio.tags.get("covr") if hasattr(audio.tags, "get") else None
        if covers:
            art = hashlib.sha256()
            for cover in covers:
                art.update(bytes(cover))
            artwork = art.hexdigest()
        return (metadata, artwork)
    except Exception:
        return (None, None)


@dataclass(frozen=True)
class ManifestEntry:
    item_ref: str
    relative_path: str
    kind: str
    sha256: str
    size_bytes: int
    mtime_ns: int
    metadata_digest: str | None = None
    artwork_digest: str | None = None


@dataclass(frozen=True)
class Manifest:
    checkpoint_id: str
    created_at: str
    source_root: str
    entries: tuple[ManifestEntry, ...]

    @property
    def digest(self) -> str:
        """Digest over the ordered entries and the checkpoint identity."""
        material = json.dumps(
            {
                "checkpoint_id": self.checkpoint_id,
                "source_root": self.source_root,
                "entries": [asdict(e) for e in self.entries],
            },
            sort_keys=True,
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    @property
    def total_bytes(self) -> int:
        return sum(e.size_bytes for e in self.entries)

    def entry(self, item_ref: str) -> ManifestEntry:
        for entry in self.entries:
            if entry.item_ref == item_ref:
                return entry
        raise KeyError(item_ref)

    def coverage(self) -> dict[str, int]:
        """What this checkpoint covers, for the `coverage` payload field."""
        return {
            "items": len(self.entries),
            "files": sum(1 for e in self.entries if e.kind == KIND_FILE),
            "databases": sum(1 for e in self.entries if e.kind == KIND_DATABASE),
            "bytes": self.total_bytes,
            "with_metadata": sum(1 for e in self.entries if e.metadata_digest is not None),
            "with_artwork": sum(1 for e in self.entries if e.artwork_digest is not None),
        }

    def to_json(self) -> str:
        return json.dumps(
            {
                "checkpoint_id": self.checkpoint_id,
                "created_at": self.created_at,
                "source_root": self.source_root,
                "entries": [asdict(e) for e in self.entries],
            },
            sort_keys=True,
            indent=2,
        )

    @classmethod
    def from_json(cls, text: str) -> Manifest:
        data = json.loads(text)
        return cls(
            checkpoint_id=data["checkpoint_id"],
            created_at=data["created_at"],
            source_root=data["source_root"],
            entries=tuple(ManifestEntry(**e) for e in data["entries"]),
        )


def build_manifest(
    source_root: Path,
    *,
    checkpoint_id: str,
    created_at: str,
    database_path: Path | None = None,
) -> Manifest:
    """
    Capture *source_root* as an ordered manifest.

    Entries are sorted by relative path so the manifest -- and therefore
    its digest -- is a function of the tree's content, not of the order
    the filesystem happened to hand back.
    """
    entries: list[ManifestEntry] = []
    if source_root.is_dir():
        for path in sorted(source_root.rglob("*")):
            if not path.is_file():
                continue
            relative = str(path.relative_to(source_root))
            stat = path.stat()
            metadata_digest, artwork_digest = _tag_digests(path)
            entries.append(
                ManifestEntry(
                    item_ref=item_ref_for(relative),
                    relative_path=relative,
                    kind=KIND_FILE,
                    sha256=sha256_file(path),
                    size_bytes=stat.st_size,
                    mtime_ns=stat.st_mtime_ns,
                    metadata_digest=metadata_digest,
                    artwork_digest=artwork_digest,
                )
            )

    if database_path is not None and database_path.is_file():
        stat = database_path.stat()
        reference = f"::database::{database_path.name}"
        entries.append(
            ManifestEntry(
                item_ref=item_ref_for(reference),
                relative_path=reference,
                kind=KIND_DATABASE,
                sha256=sha256_file(database_path),
                size_bytes=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
            )
        )

    entries.sort(key=lambda e: e.relative_path)
    return Manifest(
        checkpoint_id=checkpoint_id,
        created_at=created_at,
        source_root=str(source_root),
        entries=tuple(entries),
    )
