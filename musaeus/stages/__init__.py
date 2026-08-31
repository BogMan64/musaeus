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
  BPMStage       — BPM/key/energy/danceability extraction + tag write
                   (Essentia, optional `bpm` extra), ported from ORPHEUS's
                   orpheus_audio_analyzer.py -- wired into DEFAULT_PIPELINE
                   2026-08-19, near Forge, after Finalize (see below)
  TributeQuarantineStage — detect + quarantine tribute-band/karaoke/
                   meditation content, ported from ORPHEUS's
                   orpheus_junk_quarantine.py
  VariousArtistsFixStage — resolve the real artist for "Various Artists"
                   tagged rows + relocate the file, ported from
                   ORPHEUS's fix_various_artists.py -- wired into
                   DEFAULT_PIPELINE 2026-08-19, end of Act 1, MusicBrainz
                   lookups forced off (see below)
  BitRotStage    — verify ALAC_Archive against a directory-scan baseline
                   (archive_tier_hashes) to catch silent bit rot, ported
                   from ORPHEUS's orpheus_integrity_check.py
  EnrichStage    — Last.fm genre enrichment for tracks with missing genre
  DenyListStage  — refuse re-ingest of audio deliberately removed before.
                   Sits immediately after Sentinel, the first point an
                   audio_hash exists, and before any stage invests work in
                   a file about to be refused. Quarantines, never deletes.
                   Added 2026-08-24 after tracing that the finalized-hash
                   ledger cannot prevent re-acquisition by design
  ClassicalComposerStage — refile Classical under the composer rather than
                   the performer, resolved from a thematic catalogue
                   number, Composer_Canon.tsv, or a title prefix. Added to
                   Act 1 2026-08-24, after GenreValidate
  MBEnrichStage  — MusicBrainz artist + release MBID enrichment
  OriginalYearStage — recover a recording's FIRST release year from
                   MusicBrainz into original_year, leaving `year` (the
                   edition year) untouched. Deliberately NOT in
                   DEFAULT_PIPELINE: it is one rate-limited network
                   call per track (~3h for the library), so it is run
                   on demand via `musaeus original-year`
  NearDupeStage  — metadata-based near-duplicate detection (fuzzy title match)
  AcousticIDStage — acoustic fingerprint dedup via fpcalc + AcousticID API
  TranscodeStage  — lossless → 256k AAC export via ffmpeg
  ReviewerStage   — Groq AI metadata quality review

DEFAULT_PIPELINE (`musaeus run`) is the full Act 1/2/3 + Enrichment chain.
2026-08-17 briefly moved Canonicalize ahead of dedup (Grey's call at the
time); REVERTED 2026-08-18 after Claude(chat) challenged the motivating
case and Claude Code confirmed against sentinel.py's own docstring that
cross-format duplicate detection already works via PCM-based audio_hash
("Same audio, different container: audio_hash matches → EXACT
duplicate") -- the reversal was paying real ffmpeg cost for a
correctness problem that didn't actually exist. Canonicalize is back in
Act 3, its original position:
  Act 1 (Intake & Correction): Preflight → Ingest → Permissions → Sentinel
         → Scholar → Health → Corrupt → AlbumArt → Normalize → Sanitize →
         ArtistConsolidate → VariousArtistsFix
  Act 2 (Dedup & Staging):     CrossDupe → NearDupe → DupeResolver
  Act 3 (Canonicalize/Finalize): Canonicalize → Finalize → BPM → Forge →
         Tagger → Audit
  Enrichment (default-on, moved from on-demand-only 2026-08-17): Enrich →
         MBEnrich
See ACT1_INTAKE_CORRECTION / ACT2_DEDUP_STAGING / ACT3_CANONICALIZE_FINALIZE
/ ENRICHMENT below for the named building blocks and the reasoning behind
this order.
AlbumArt runs in Act 1, before Canonicalize, so any sidecar art it embeds
gets carried through Canonicalize/Finalize's container conversion (see
canonicalize.py's/transcode.py's _has_attached_picture()-gated art
preservation) rather than being embedded after the file's already in its
final container.
The pristine-original tradeoff the brief reversal introduced (a
sub-lossless duplicate-loser losing its true original to a wasted
TRANSCODE before dedup could flag it) no longer applies -- Canonicalize
runs after DupeResolver again, so a confirmed duplicate is pulled before
any conversion happens, exactly as originally designed.
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
BPM and VariousArtistsFix moved from on-demand-only to default-on
(2026-08-19, Grey's explicit call). BPM: positioned after Finalize, near
Forge, per Grey's original request -- Grey also corrected an initial
cost objection here (Essentia is heavy, but BPM's own tag-read-first
shortcut + bpm_analyzed_at resumability mean the real cost is "once per
new file, ever," not "every pipeline run," so wiring it in by default is
reasonable). VariousArtistsFix: positioned at the end of Act 1, right
after ArtistConsolidate -- both a natural continuation of "artist
correction" and, like ArtistConsolidate, beneficial to run before Act
2's dedup so CrossDupe/NearDupe see the resolved real artist rather than
a shared "Various Artists" tag on every candidate row. MusicBrainz
lookups are forced off (various_artists_no_mb=True) when run as part of
DEFAULT_PIPELINE -- bracket/filename-segment resolution only, no network
call -- so a Last.fm/MusicBrainz-style network hiccup early in Act 1
can't block or stall an otherwise file-safety-critical run the way
Enrich/MBEnrich's isolation-to-the-end already guards against for their
own network calls. `musaeus various-artists-fix` run standalone still
defaults to MB lookups on.
On-demand only (not part of the canonical chain): Auditor, Curator,
           Playlist, Ghost, AcousticID, Transcode, Reviewer, Organize,
           IntegrityStage, TributeQuarantineStage, BitRotStage.
"""

from .acousticid import AcousticIDStage
from .albumart import AlbumArtStage
from .artist_consolidate import ArtistConsolidateStage
from .audit import AuditStage
from .auditor import AuditorStage
from .bitrot import BitRotStage
from .bpm import BPMStage
from .canonicalize import CanonicalizeStage
from .classical_composer import ClassicalComposerStage
from .corrupt import CorruptStage
from .cross_dupe import CrossDupeStage
from .curator import CuratorStage
from .deny_list import DenyListStage
from .dupe_resolver import DupeResolverStage
from .enrich import EnrichStage
from .finalize import FinalizeStage
from .forge import ForgeStage
from .genre_validate import GenreValidateStage  # noqa: E402
from .ghost import GhostStage
from .health import HealthStage
from .identity_tag import IdentityTagStage
from .ingest import IngestStage
from .integrity import IntegrityStage
from .mb_enrich import MBEnrichStage
from .neardupe import NearDupeStage
from .normalize import NormalizeStage
from .organize import OrganizeStage
from .original_year import OriginalYearStage
from .permissions import PermissionsStage
from .playlist import PlaylistStage
from .preflight import PreflightStage
from .sanitize import SanitizeStage
from .scholar import ScholarStage
from .sentinel import SentinelStage
from .spellcheck import SpellCheckStage  # noqa: E402
from .tagger import TaggerStage
from .transcode import TranscodeStage
from .tribute_quarantine import TributeQuarantineStage
from .various_artists_fix import VariousArtistsFixStage

__all__ = [
    "PreflightStage",
    "IngestStage",
    "SentinelStage",
    "ScholarStage",
    "NormalizeStage",
    "OrganizeStage",
    "SanitizeStage",
    "CrossDupeStage",
    "DenyListStage",
    "DupeResolverStage",
    "CanonicalizeStage",
    "ClassicalComposerStage",
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
    "BPMStage",
    "CorruptStage",
    "ArtistConsolidateStage",
    "EnrichStage",
    "MBEnrichStage",
    "OriginalYearStage",
    "NearDupeStage",
    "AcousticIDStage",
    "IdentityTagStage",
    "TranscodeStage",
    "IntegrityStage",
    "AlbumArtStage",
    "TributeQuarantineStage",
    "GenreValidateStage",
    "SpellCheckStage",
    "VariousArtistsFixStage",
    "BitRotStage",
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
# RESTRUCTURED 2026-08-17, REVERTED 2026-08-18 (see module docstring
# above for the full reasoning): Canonicalize briefly moved ahead of
# dedup, then moved back to Act 3 once the motivating case (cross-format
# duplicate detection) was confirmed to already work via PCM-based
# audio_hash without needing the reversal. Enrich/MBEnrich's move from
# on-demand-only to default-on, positioned last, is unaffected and
# stands.
#
# Act 1 - Intake & Correction. Corrupt/Health/Normalize/Sanitize/
#   ArtistConsolidate all read fields Scholar populates (status=
#   'CATALOGUED', codec, bitrate, duration), so despite being conceptually
#   "intake", they run AFTER Sentinel+Scholar, not before -- a real data
#   dependency, not a stylistic choice. Permissions added 2026-08-17,
#   right after Ingest -- no data dependency on the stages after it,
#   fixing permissions before anything else touches the batch's files.
#   VariousArtistsFix added 2026-08-19, last in this Act, right after
#   ArtistConsolidate -- same "resolve the real artist before dedup"
#   logic, and MB lookups forced off here (bracket/filename-segment
#   resolution only) so a network hiccup can't stall Act 1.
# Act 2 - Dedup & Staging. CrossDupe needs audio_hash (Sentinel), so it
#   can't literally run before Sentinel either -- it runs as early after
#   Sentinel as a hash-based check can. NearDupe benefits from running
#   after ArtistConsolidate (canon-resolved artist names). DupeResolver
#   runs LAST in this Act, once every dedup check has had a chance to flag
#   something for this batch -- so a confirmed duplicate is physically
#   pulled out before Act 3 wastes any ffmpeg conversion or loudness
#   measurement on it.
# Act 3 - Canonicalize, Finalize, BPM, Forge, Tagger, Audit. Finalize runs
#   BEFORE Forge/Tagger per Grey's explicit request: this lets an
#   external archival copy be made of the canonicalized-but-not-yet-
#   loudness-tagged file, straight out of its permanent ALAC-Library
#   location, before Forge's ReplayGain tags get burned into it. BPM
#   added 2026-08-19 right after Finalize, near Forge, per Grey's
#   original request and bpm.py's own recommended placement -- its
#   tag-read-first shortcut + bpm_analyzed_at resumability mean the
#   Essentia cost is paid once per new file, not on every pipeline run.
#   Audit runs last, as the gate before a future DB-snapshot-and-wipe
#   step.
# Enrichment (ENRICHMENT) - Enrich (Last.fm genre) + MBEnrich (MusicBrainz
#   MBID), moved from on-demand-only to default-on, 2026-08-17, Grey's
#   explicit call ("default-on every run... after dedup"). Positioned
#   LAST, after Audit rather than merely after dedup -- deliberately
#   isolated from the file-safety-critical stages above so a network
#   hiccup here can never block or interfere with them. MBEnrichStage was
#   fixed the same day to degrade gracefully on an unreachable network
#   (matching EnrichStage's existing pattern) rather than hard-failing,
#   since it's no longer purely on-demand. Both stages' dry_run() were
#   further fixed 2026-08-18 to skip the real network call entirely
#   instead of only gating the DB write (see enrich.py/mb_enrich.py).

ACT1_INTAKE_CORRECTION: list[type] = [
    PreflightStage,
    IngestStage,
    PermissionsStage,
    SentinelStage,
    DenyListStage,
    ScholarStage,
    HealthStage,
    CorruptStage,
    AlbumArtStage,
    NormalizeStage,
    SpellCheckStage,
    SanitizeStage,
    ArtistConsolidateStage,
    VariousArtistsFixStage,
    # Genre is settled here, at the end of intake, once the artist is
    # canonical -- the law is keyed on artist, so it cannot be applied
    # before ArtistConsolidate and VariousArtistsFix have run. Added to the
    # pipeline 2026-08-24: it had only ever run on demand, which is why a
    # file's genre came from its own tags via Scholar and nothing corrected
    # it against MasterLaw.
    GenreValidateStage,
]

ACT2_DEDUP_STAGING: list[type] = [
    CrossDupeStage,
    NearDupeStage,
    DupeResolverStage,
    # Composer attribution runs AFTER dedup, not before. Filing classical
    # under the composer collapses dozens of performers into a handful of
    # artists -- Bach reached 60 tracks, Vivaldi 52 -- and NearDupeStage
    # compares titles WITHIN an artist. Classical titles share long work
    # prefixes, so inside those groups fuzzy matching produces false
    # positives at scale.
    #
    # Measured 2026-08-25: 15 distinct recordings were quarantined as
    # near-duplicates, among them "Water Music Suite No. 1 ... - Air"
    # against "... - VIII. Hornpipe" at 89% -- different movements of the
    # same suite. Under their original performer credits those tracks were
    # never compared to each other; the attribution created the surface
    # dedup then tripped on.
    #
    # So dedup sees the credits as they arrived, and only then is the
    # recording re-filed under whoever wrote it.
    ClassicalComposerStage,
]

ACT3_CANONICALIZE_FINALIZE: list[type] = [
    CanonicalizeStage,
    FinalizeStage,
    BPMStage,
    ForgeStage,
    TaggerStage,
    OrganizeStage,
    # Organize BEFORE Audit. Audit reconciles archive.file_path against what
    # is on disk and is documented above as "the gate before a future
    # DB-snapshot-and-wipe step"; with Organize after it, every audit result
    # described a layout Organize then rewrote, and the gate passed on
    # pre-move state.
    #
    # Wired 2026-08-30 at Grey's instruction, after the hazard that kept it
    # out was fixed and tested. It ran nowhere before: every target was built
    # under ctx.inbox while the query selects CATALOGUED, so it would have
    # moved 10,660 of 10,660 files OUT of ALAC-Library. A file now organizes
    # within the root it already lives in, and a finalized file's root is its
    # BATCH directory -- ALAC-Library itself as the root would have flattened
    # <batch>/ and moved the entire library, measured 2026-08-30.
    #
    # Last in Act 3, after Finalize has placed the file and Tagger has settled
    # the metadata the path is derived from. Organizing earlier would name
    # folders from tags that Tagger is about to change.
    AuditStage,
]

ENRICHMENT: list[type] = [
    EnrichStage,
    MBEnrichStage,
    # Wired 2026-08-30 at Grey's instruction; his earlier ruling was "after
    # the campaign, as a deliberate change", and the campaign is over.
    #
    # AFTER MBEnrich, not before: MusicBrainz by name is cheap and settles
    # most rows, and AcoustID is the answer for what text cannot identify --
    # 744 files as of 2026-08-30, down from 2,152 once the article-form
    # lookup bug was fixed. Fingerprinting costs ~0.8s/file, so it should ask
    # about the remainder, not the library.
    AcousticIDStage,
    # LAST. It writes identity to the FILES, so it must run after everything
    # that resolves identity -- otherwise it writes what the run is about to
    # learn. This is the stage whose absence let ~8,074 MBIDs live only in a
    # database that was later reset to 87 rows.
    IdentityTagStage,
]

# The full canonical pipeline: Act 1 + Act 2 + Act 3 + Enrichment, in
# order. This is what `musaeus run` executes by default going forward,
# and what musaeus_overnight.sh's cron entry point should call
# stage-by-stage in this same order.
CANONICAL_PIPELINE: list[type] = (
    ACT1_INTAKE_CORRECTION + ACT2_DEDUP_STAGING + ACT3_CANONICALIZE_FINALIZE + ENRICHMENT
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
]
