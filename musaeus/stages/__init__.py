"""
MUSAEUS — Pipeline stages package.

Available stages:
  PreflightStage — environment sanity checks (commands, packages, disk,
                   DB integrity) -- report-only, runs first, never mutates
  IngestStage    — scan inbox, register new files in archive
  SentinelStage  — compute audio + full hashes, detect exact duplicates
  CrossDupeStage — flag files matching ALAC-Library content from a prior
                   batch, via the persistent cross-batch hash index (Act 2)
  DupeResolverStage — physically relocate duplicate-group losers into
                   ALAC-Library/DUPES_MOVED_FOR_REVIEW/, mirroring
                   ALAC-Library's own Artist/Album/Track shape (Act 2)
  ScholarStage   — extract ffprobe metadata, populate archive fields
  NormalizeStage — article-suffix fix + ALL-CAPS repair on archived metadata
  OrganizeStage  — rename and reorganize files into Artist/Album/ structure
  SanitizeStage  — filesystem-safe metadata (Windows/ExFAT/Android compatible)
  CanonicalizeStage — lossless->ALAC / sub-lossless->AAC, both as .m4a,
                   based on real ffprobe codec, not file extension (Act 3)
  FinalizeStage  — move canonicalized files INBOX -> ALAC-Library, the
                   trusted canonical library (Act 3)
  AuditStage     — physical-presence verification gate before a batch's
                   DB can be snapshotted and wiped (Act 3)
  ForgeStage     — measure EBU R128 loudness, write ReplayGain tags
  TaggerStage    — write normalised metadata from DB back to file tags
  AuditorStage   — pre-forge LUFS audit (flags out-of-window files)
  CuratorStage   — build car-library export with optional noise profiles
  PlaylistStage  — build per-genre M3U8 playlists from the archive
  GhostStage     — mark archive entries whose files no longer exist on disk
  HealthStage    — library-wide consistency and quality checks
  PermissionsStage — fix file/folder permissions under inbox (Windows/ExFAT
                   sources land with wrong perms; 644 files / 755 dirs)
  EnrichStage    — Last.fm genre enrichment for tracks with missing genre
  MBEnrichStage  — MusicBrainz artist + release MBID enrichment
  NearDupeStage  — metadata-based near-duplicate detection (fuzzy title match)
  AcousticIDStage — acoustic fingerprint dedup via fpcalc + AcousticID API
  TranscodeStage  — lossless → 256k AAC export via ffmpeg
  ReviewerStage   — Groq AI metadata quality review

DEFAULT_PIPELINE (`musaeus run`) is the full Act 1/2/3 + Enrichment chain,
restructured 2026-08-17 (Grey's explicit call, reversing the original
dedup-before-canonicalize efficiency ordering -- see the reasoning block
below CANONICAL_PIPELINE for the accepted tradeoff this costs):
  Act 1 (Intake & Correction): Preflight → Ingest → Permissions → Sentinel
         → Scholar → Health → Corrupt → AlbumArt → Normalize → Sanitize →
         ArtistConsolidate
  Canonicalize (moved ahead of dedup): Canonicalize
  Act 2 (Dedup & Staging):     CrossDupe → NearDupe → DupeResolver
  Act 3 (Finalize):            Finalize → Forge → Tagger → Audit
  Enrichment (default-on, moved from on-demand-only): Enrich → MBEnrich
See ACT1_INTAKE_CORRECTION / PRE_DEDUP_CANONICALIZE / ACT2_DEDUP_STAGING /
ACT3_FINALIZE / ENRICHMENT below for the named building blocks and the
reasoning behind this order.
AlbumArt runs in Act 1, before Canonicalize, so any sidecar art it embeds
gets carried through Canonicalize/Finalize's container conversion (see
canonicalize.py's/transcode.py's _has_attached_picture()-gated art
preservation) rather than being embedded after the file's already in its
final container.
Permissions moved from on-demand-only into Act 1 (2026-08-17, Grey's
explicit call: "keep it is needed") -- positioned right after Ingest,
before anything else touches the batch's files, matching Grey's own
Phase 1 ordering (preflight, permissions, then hygiene/dedup/etc).
Enrich/MBEnrich moved from on-demand-only to default-on (2026-08-17,
Grey's explicit call), positioned LAST -- after Audit, not merely "after
dedup" -- deliberately isolated from the file-safety-critical Finalize/
Forge/Tagger/Audit stages above so a Last.fm/MusicBrainz network hiccup
here can never block or interfere with them. MBEnrichStage was fixed the
same day to degrade gracefully on an unreachable network (skip + report,
matching EnrichStage's existing missing-API-key pattern) rather than
hard-failing the stage, since it's no longer purely on-demand.
On-demand only (not part of the canonical chain): Auditor, Curator,
           Playlist, Ghost, AcousticID, Transcode, Reviewer, Organize,
           IntegrityStage.
"""

from .acousticid import AcousticIDStage
from .albumart import AlbumArtStage
from .artist_consolidate import ArtistConsolidateStage
from .audit import AuditStage
from .auditor import AuditorStage
from .canonicalize import CanonicalizeStage
from .corrupt import CorruptStage
from .cross_dupe import CrossDupeStage
from .curator import CuratorStage
from .dupe_resolver import DupeResolverStage
from .enrich import EnrichStage
from .finalize import FinalizeStage
from .forge import ForgeStage
from .ghost import GhostStage
from .health import HealthStage
from .ingest import IngestStage
from .integrity import IntegrityStage
from .mb_enrich import MBEnrichStage
from .neardupe import NearDupeStage
from .normalize import NormalizeStage
from .organize import OrganizeStage
from .permissions import PermissionsStage
from .playlist import PlaylistStage
from .preflight import PreflightStage
from .reviewer import ReviewerStage
from .sanitize import SanitizeStage
from .scholar import ScholarStage
from .sentinel import SentinelStage
from .tagger import TaggerStage
from .transcode import TranscodeStage

__all__ = [
    "PreflightStage",
    "IngestStage",
    "SentinelStage",
    "ScholarStage",
    "NormalizeStage",
    "OrganizeStage",
    "SanitizeStage",
    "CrossDupeStage",
    "DupeResolverStage",
    "CanonicalizeStage",
    "FinalizeStage",
    "AuditStage",
    "ForgeStage",
    "TaggerStage",
    "AuditorStage",
    "CuratorStage",
    "PlaylistStage",
    "GhostStage",
    "HealthStage",
    "PermissionsStage",
    "CorruptStage",
    "ArtistConsolidateStage",
    "EnrichStage",
    "MBEnrichStage",
    "NearDupeStage",
    "AcousticIDStage",
    "TranscodeStage",
    "ReviewerStage",
    "IntegrityStage",
    "AlbumArtStage",
]

# ── Act 1/2/3 + Enrichment pipeline (canonical, dependency-respecting order) ─
#
# Original Act 1/2/3 ordering confirmed with Grey (2026-08-09/10 session)
# and verified end-to-end via a real full-chain dry run against a scratch
# vault (Preflight through Audit, covering PASSTHROUGH/CONVERT/TRANSCODE,
# an EXACT duplicate, and a flagged file) before being written here. Two
# real bugs were found and fixed during that verification run rather than
# assumed away -- see git log for "archive.file_path didn't follow files
# moved by DupeResolver/Corrupt" and "keeper selection ignored codec,
# could discard the lossless copy".
#
# RESTRUCTURED 2026-08-17 (Grey's explicit call): Canonicalize moved ahead
# of dedup (previously ran last, in Act 3), and Enrich/MBEnrich moved from
# on-demand-only to default-on, positioned last. Reasoning below.
#
# Act 1 - Intake & Correction. Corrupt/Health/Normalize/Sanitize/
#   ArtistConsolidate all read fields Scholar populates (status=
#   'CATALOGUED', codec, bitrate, duration), so despite being conceptually
#   "intake", they run AFTER Sentinel+Scholar, not before -- a real data
#   dependency, not a stylistic choice. Permissions added 2026-08-17,
#   right after Ingest -- no data dependency on the stages after it,
#   fixing permissions before anything else touches the batch's files.
# Canonicalize (PRE_DEDUP_CANONICALIZE) - moved ahead of Act 2, 2026-08-17.
#   Previously ran in Act 3, specifically so a confirmed duplicate never
#   wasted ffmpeg conversion time (see DupeResolverStage's own module
#   docstring for the original reasoning). Grey's explicit call reverses
#   this: standardization (including format conversion) now happens
#   before dedup for every file, no exceptions. ACCEPTED TRADEOFF,
#   confirmed with Grey: a sub-lossless file that gets TRANSCODED here and
#   *then* turns out to be a duplicate has already lost its pristine
#   INBOX original (Canonicalize deletes it once the STAGING copy is
#   verified) -- DupeResolverStage will move the already-re-encoded .m4a
#   into DUPES_MOVED_FOR_REVIEW, not the true original. Grey confirmed a
#   separate curated RAW-files backup exists (/media/grey/USB2/
#   Curated.RAW.Files) that mitigates this, and explicitly chose to accept
#   the tradeoff as-is over the two more conservative alternatives offered
#   (delay the INBOX delete until dedup clears the row; or keep the old
#   post-dedup order for sub-lossless files specifically).
# Act 2 - Dedup & Staging. CrossDupe needs audio_hash (Sentinel), so it
#   can't literally run before Sentinel either -- it runs as early after
#   Sentinel as a hash-based check can. NearDupe benefits from running
#   after ArtistConsolidate (canon-resolved artist names). DupeResolver
#   runs LAST in this Act, once every dedup check has had a chance to flag
#   something for this batch.
# Act 3 - Finalize, Forge, Tagger, Audit (no longer includes Canonicalize,
#   see above). Finalize runs BEFORE Forge/Tagger per Grey's explicit
#   request: this lets an external archival copy be made of the
#   canonicalized-but-not-yet-loudness-tagged file, straight out of its
#   permanent ALAC-Library location, before Forge's ReplayGain tags get
#   burned into it. Audit runs last, as the gate before a future
#   DB-snapshot-and-wipe step.
# Enrichment (ENRICHMENT) - Enrich (Last.fm genre) + MBEnrich (MusicBrainz
#   MBID), moved from on-demand-only to default-on, 2026-08-17, Grey's
#   explicit call ("default-on every run... after dedup"). Positioned
#   LAST, after Audit rather than merely after dedup -- deliberately
#   isolated from the file-safety-critical stages above so a network
#   hiccup here can never block or interfere with them. MBEnrichStage was
#   fixed the same day to degrade gracefully on an unreachable network
#   (matching EnrichStage's existing pattern) rather than hard-failing,
#   since it's no longer purely on-demand.

ACT1_INTAKE_CORRECTION: list[type] = [
    PreflightStage,
    IngestStage,
    PermissionsStage,
    SentinelStage,
    ScholarStage,
    HealthStage,
    CorruptStage,
    AlbumArtStage,
    NormalizeStage,
    SanitizeStage,
    ArtistConsolidateStage,
]

PRE_DEDUP_CANONICALIZE: list[type] = [
    CanonicalizeStage,
]

ACT2_DEDUP_STAGING: list[type] = [
    CrossDupeStage,
    NearDupeStage,
    DupeResolverStage,
]

ACT3_FINALIZE: list[type] = [
    FinalizeStage,
    ForgeStage,
    TaggerStage,
    AuditStage,
]

ENRICHMENT: list[type] = [
    EnrichStage,
    MBEnrichStage,
]

# The full canonical pipeline: Act 1 + Canonicalize + Act 2 + Act 3 +
# Enrichment, in order. This is what `musaeus run` executes by default
# going forward, and what musaeus_overnight.sh's cron entry point should
# call stage-by-stage in this same order.
CANONICAL_PIPELINE: list[type] = (
    ACT1_INTAKE_CORRECTION
    + PRE_DEDUP_CANONICALIZE
    + ACT2_DEDUP_STAGING
    + ACT3_FINALIZE
    + ENRICHMENT
)

# Canonical run order for the default pipeline -- the full chain above.
# `musaeus run --dry-run` remains the safe way to preview this before
# ever running it live (subject to P0-02's current fail-closed guard --
# see consumer-readiness safety spec).
DEFAULT_PIPELINE: list[type] = CANONICAL_PIPELINE

# Extended pipeline (run with `musaeus run --full`)
FULL_PIPELINE: list[type] = [
    PreflightStage,
    IngestStage,
    SentinelStage,
    ScholarStage,
    NormalizeStage,
    SanitizeStage,
    ForgeStage,
    TaggerStage,
]

# Archive pipeline (run with `musaeus run --archive`) — full minus LUFS/ReplayGain
ARCHIVE_PIPELINE: list[type] = [
    PreflightStage,
    IngestStage,
    SentinelStage,
    ScholarStage,
    NormalizeStage,
    SanitizeStage,
    GhostStage,
    HealthStage,
    IntegrityStage,
    EnrichStage,
    MBEnrichStage,
    NearDupeStage,
    AcousticIDStage,
    AlbumArtStage,
    TaggerStage,
    ReviewerStage,
]

# Big Kahuna (run with `musaeus run --big-kahuna`) — everything including LUFS
BIG_KAHUNA_PIPELINE: list[type] = [
    PreflightStage,
    IngestStage,
    SentinelStage,
    ScholarStage,
    NormalizeStage,
    SanitizeStage,
    GhostStage,
    HealthStage,
    IntegrityStage,
    EnrichStage,
    MBEnrichStage,
    NearDupeStage,
    AcousticIDStage,
    AlbumArtStage,
    ForgeStage,
    TaggerStage,
    CuratorStage,
    PlaylistStage,
    ReviewerStage,
]

# Maintenance pipeline (run with `musaeus run --maintain`)
MAINTAIN_PIPELINE: list[type] = [
    PreflightStage,
    GhostStage,
    HealthStage,
    NormalizeStage,
    SanitizeStage,
    ArtistConsolidateStage,
    EnrichStage,
    MBEnrichStage,
    NearDupeStage,
]

# Enrichment pipeline (run with `musaeus run --enrich`)
ENRICH_PIPELINE: list[type] = [
    EnrichStage,
    MBEnrichStage,
    AcousticIDStage,
    ReviewerStage,
]
