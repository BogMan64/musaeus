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
#: An audio file whose TAGS are captured in the manifest instead of its
#: bytes being copied into the checkpoint.
KIND_TAGGED_AUDIO = "tagged_audio"

#: Extensions checkpointed by tag capture rather than byte copy.
#:
#: The library is 483 GB of audio and the recovery cap is 100 GB, so a
#: byte-for-byte checkpoint of it is arithmetically impossible. It is also
#: unnecessary for the stages that actually modify it: ForgeStage says so
#: in its own header -- "Never re-encodes audio. Tags only." -- and
#: TaggerStage only calls audio.save() after changing fields. What has to
#: be restorable for those is the TAG VALUES, and those measure ~1.5 KB a
#: track: 15 MB for the whole library against 483 GB of audio.
#:
#: This does NOT cover a stage that rewrites the audio stream. Anything
#: that re-encodes must checkpoint its inputs by copy.
TAG_CAPTURE_SUFFIXES: frozenset[str] = frozenset({".m4a", ".mp3", ".flac", ".m4b", ".aac"})


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tagged_identity(size: int, mtime_ns: int, metadata: str | None, artwork: str | None) -> str:
    """Cheap identity for a tag-captured entry. Deliberately not a content
    digest, and prefixed so it cannot be read as one."""
    material = f"{size}|{mtime_ns}|{metadata or ''}|{artwork or ''}"
    return "tagged:" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:40]


def item_ref_for(relative_path: str) -> str:
    """Stable, path-derived, non-reversible reference.

    Derived rather than random so the same item gets the same reference
    across runs, and hashed so a shareable report carrying the reference
    does not leak the directory layout of someone's music library."""
    return hashlib.sha256(relative_path.encode("utf-8")).hexdigest()[:24]


def read_tags(path: Path) -> dict[str, list] | None:
    """Every non-artwork tag on *path*, preserving VALUE TYPES.

    The manifest already recorded a metadata DIGEST, which is enough to
    detect a tag changed and useless for putting it back. This records the
    values so a rollback can restore them.

    Types are kept, and that is the whole point. An earlier version
    stringified everything, which round-tripped fine for text tags and
    threw MP4MetadataValueError on the first real track it met: MP4 stores
    `tmpo` (BPM) as an int and `trkn`/`disk` as int pairs, and mutagen
    refuses a str where it wants an int. A capture that cannot be written
    back is not a capture -- it just looks like one until a rollback needs
    it.
    """
    try:
        import mutagen  # type: ignore[import-untyped]

        audio = mutagen.File(str(path))
        if audio is None or audio.tags is None:
            return None
        out: dict[str, list] = {}
        for key, value in audio.tags.items():
            k = str(key)
            if k.startswith("covr"):
                continue
            values = value if isinstance(value, list) else [value]
            encoded = []
            for v in values:
                if isinstance(v, bool):
                    encoded.append({"t": "bool", "v": v})
                elif isinstance(v, int):
                    encoded.append({"t": "int", "v": v})
                elif isinstance(v, (tuple, list)):
                    encoded.append({"t": "seq", "v": [int(x) for x in v]})
                elif isinstance(v, bytes):
                    encoded.append({"t": "bytes", "v": v.hex()})
                else:
                    encoded.append({"t": "str", "v": str(v)})
            out[k] = encoded
        return out
    except Exception:
        return None


def decode_tag_values(encoded: list) -> list:
    """Turn read_tags() output back into values mutagen will accept."""
    out = []
    for item in encoded:
        if not isinstance(item, dict) or "t" not in item:
            out.append(item)  # tolerate a plain value
            continue
        kind, value = item["t"], item["v"]
        if kind == "int":
            out.append(int(value))
        elif kind == "bool":
            out.append(bool(value))
        elif kind == "seq":
            out.append(tuple(int(x) for x in value))
        elif kind == "bytes":
            out.append(bytes.fromhex(value))
        else:
            out.append(str(value))
    return out


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
    #: Captured tag values, present only for KIND_TAGGED_AUDIO entries.
    tags: dict[str, list[str]] | None = None


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

    @property
    def payload_bytes(self) -> int:
        """Bytes the checkpoint actually has to copy.

        Excludes tag-captured audio, whose restorable state is the tag
        values recorded here rather than a copy of the file.
        """
        return sum(e.size_bytes for e in self.entries if e.kind != KIND_TAGGED_AUDIO)

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
            "tagged_audio": sum(1 for e in self.entries if e.kind == KIND_TAGGED_AUDIO),
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
    capture_tags: bool = False,
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
            # Tag capture only applies if the tags can actually be READ.
            # An unreadable or untagged audio file cannot be tag-restored,
            # so it falls back to a byte copy rather than being recorded as
            # captured-with-nothing. Failing the whole checkpoint over one
            # such file would be worse: it would make a 10,000-track
            # library un-checkpointable because of a single odd file.
            captured = (
                read_tags(path)
                if capture_tags and path.suffix.lower() in TAG_CAPTURE_SUFFIXES
                else None
            )
            tag_capture = captured is not None
            entries.append(
                ManifestEntry(
                    item_ref=item_ref_for(relative),
                    relative_path=relative,
                    kind=KIND_TAGGED_AUDIO if tag_capture else KIND_FILE,
                    # A tag-captured entry gets an identity, not a content
                    # hash. sha256 of the file means reading it in full:
                    # 483 GB for this library, which took longer than a
                    # ten-minute timeout to compute for a checkpoint whose
                    # payload is 15 MB of tags. Since the bytes are not
                    # copied, a content hash of them buys nothing a
                    # rollback could use.
                    #
                    # Honest limit: this detects a rewrite that changes
                    # size, mtime or tags -- which is every write mutagen
                    # or ffmpeg makes -- but not a byte edit that preserves
                    # all three. It is prefixed so it can never be mistaken
                    # for a content digest.
                    sha256=(
                        tagged_identity(
                            stat.st_size, stat.st_mtime_ns, metadata_digest, artwork_digest
                        )
                        if tag_capture
                        else sha256_file(path)
                    ),
                    size_bytes=stat.st_size,
                    mtime_ns=stat.st_mtime_ns,
                    metadata_digest=metadata_digest,
                    artwork_digest=artwork_digest,
                    tags=captured,
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
