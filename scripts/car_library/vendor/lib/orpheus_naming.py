#!/usr/bin/env python3
"""
ORPHEUS — Orpheus Naming
Library: lib/orpheus_naming.py — imported by cleanup_library_names.py, organize_only.py, post_intake_repair_pass.py
Purpose: Metadata, canon, path, and DB naming authority; provides build_track_filename, clean_metadata_from_tags, resolve_album_artist_for_path, sanitize_path_component, and logging helpers.
"""

from __future__ import annotations

import csv
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .orpheus_db import open_db, write_with_retry
from .orpheus_paths import DB_PATH, METADATA_ROOT

METADATA_DIR = METADATA_ROOT

UNKNOWN = "Unknown"

AUDIO_EXTENSIONS = {".mp3", ".flac", ".m4a", ".wav", ".aac", ".ogg", ".aiff", ".alac"}

WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    "COM1",
    "COM2",
    "COM3",
    "COM4",
    "COM5",
    "COM6",
    "COM7",
    "COM8",
    "COM9",
    "LPT1",
    "LPT2",
    "LPT3",
    "LPT4",
    "LPT5",
    "LPT6",
    "LPT7",
    "LPT8",
    "LPT9",
}

SMALL_WORDS = {
    "a",
    "an",
    "and",
    "as",
    "at",
    "but",
    "by",
    "for",
    "from",
    "in",
    "into",
    "nor",
    "of",
    "on",
    "onto",
    "or",
    "over",
    "the",
    "to",
    "up",
    "vs",
    "with",
}

BUILTIN_CANON_DISPLAY = {
    "98": "98°",
    "abba": "ABBA",
    "ac dc": "AC/DC",
    "ac-dc": "AC/DC",
    "a ha": "a-ha",
    "a-ha": "a-ha",
    "crosby stills and nash": "Crosby, Stills & Nash",
    "crosby stills nash and young": "Crosby, Stills, Nash & Young",
    "earth wind and fire": "Earth, Wind & Fire",
    "hall and oates": "Hall & Oates",
    "simon and garfunkel": "Simon & Garfunkel",
    "guns n roses": "Guns N' Roses",
    "kc and the sunshine band": "KC & The Sunshine Band",
    "kool and the gang": "Kool & The Gang",
    "huey lewis and the news": "Huey Lewis & The News",
    "tom petty and the heartbreakers": "Tom Petty & The Heartbreakers",
}

PROTECTED_FULL_ARTIST_NAMES = {
    "crosby, stills & nash",
    "crosby, stills, nash & young",
    "earth, wind & fire",
    "simon & garfunkel",
    "hall & oates",
    "sly & the family stone",  # ensemble — no solo "Sly Stone" folder
    "adam & the ants",
    "barney bentall & the legendary hearts",
    "of monsters and men",
    # Band names where the first two words are NOT a primary solo artist
    "big brother & the holding company",  # Janis Joplin fronted; no "Big Brother" artist
    "dr. hook & the medicine show",  # co-led band, no single "Dr. Hook" artist
}

PROTECTED_TOKENS = {
    "ABBA",
    "AC-DC",
    "AC/DC",
    "BTO",
    "CCR",
    "CSNY",
    "INXS",
    "R.E.M.",
    "REM",
    "U2",
}


@dataclass(frozen=True)
class ArtistPathResolution:
    raw_artist: str
    primary_artist: str
    canonical_artist: str
    path_artist: str
    matched: bool
    method: str
    confidence: float


def collapse_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_quotes_and_dashes(value: str) -> str:
    value = str(value or "")
    value = value.replace("\x00", "")
    value = value.replace("’", "'").replace("‘", "'").replace("`", "'")
    value = value.replace("“", '"').replace("”", '"')
    value = value.replace("–", "-").replace("—", "-").replace("−", "-")
    return value


def remove_control_chars(value: str) -> str:
    value = str(value or "")
    return "".join(ch for ch in value if ch == "\n" or ch == "\t" or ord(ch) >= 32)


def clean_metadata_text(value: object, fallback: str = "") -> str:
    """
    Rich metadata cleanup.

    This is NOT path sanitizing.
    This keeps AC/DC, 98°, apostrophes, ampersands, accents, etc.

    It does:
      - remove null/control garbage
      - normalize tabs/newlines into spaces
      - normalize quotes/dashes enough for scripts
      - trim/collapse whitespace
      - strip accidental trailing commas
    """
    text = str(value or "").strip()
    if not text:
        return fallback

    text = remove_control_chars(text)
    text = text.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    text = normalize_quotes_and_dashes(text)
    text = collapse_spaces(text)
    text = text.strip(" \u200b\ufeff")
    text = text.rstrip(", ").strip()

    return text or fallback


def normalize_the_suffix(value: str) -> str:
    text = clean_metadata_text(value)
    m = re.match(r"^(.*)\(\s*the\s*\)\s*$", text, flags=re.IGNORECASE)
    if not m:
        return text
    core = clean_metadata_text(m.group(1)).rstrip(",")
    return f"{core} (the)".strip()


def clean_metadata_artist(value: object, fallback: str = "Unknown Artist") -> str:
    return normalize_the_suffix(clean_metadata_text(value, fallback=fallback))


def clean_metadata_album(value: object, fallback: str = "Unknown Album") -> str:
    return clean_metadata_text(value, fallback=fallback)


def clean_metadata_title(value: object, fallback: str = "Unknown Title") -> str:
    return clean_metadata_text(value, fallback=fallback)


def clean_metadata_track_number(value: object) -> str:
    """
    Keep track/disc order metadata useful.
    Does not force a specific format.
    Examples preserved:
      1
      01
      1/10
      01/10
    """
    text = clean_metadata_text(value, fallback="")
    text = re.sub(r"[^0-9/]", "", text)
    return text


def artist_key(value: str) -> str:
    text = clean_metadata_artist(value, fallback="")
    text = text.replace("°", "")
    text = text.replace("&", " and ")
    # Strip article suffix "(the)" entirely — it's a display convention, not identity.
    # "Andrews Sisters (the)" and "Andrews Sisters" must produce the same key.
    text = re.sub(r"(?i)\(\s*the\s*\)\s*$", "", text)
    # Also strip leading "The " — "The Andrews Sisters" must match too.
    text = re.sub(r"(?i)^the\s+", "", text)
    text = text.casefold()
    text = re.sub(r"[^a-z0-9\s-]", " ", text)
    text = text.replace("-", " ")
    text = collapse_spaces(text)
    return text


def strip_collaborator_tail(artist: str) -> str:
    raw = clean_metadata_artist(artist, fallback="")
    if not raw:
        return raw

    # Protected ensemble/duo names — never stripped.
    if raw.casefold() in PROTECTED_FULL_ARTIST_NAMES:
        return raw

    # 1. Keyword separators: feat./featuring/ft./with/vs.
    raw = re.split(
        r"(?i)\s+(?:feat\.?|featuring|ft\.?|with|duet with|vs\.?|versus)\s+",
        raw,
        maxsplit=1,
    )[0].strip()

    if raw.casefold() in PROTECTED_FULL_ARTIST_NAMES:
        return raw

    # 2. Semicolons — "Antonio Vivaldi; Adrian Chandler" → "Antonio Vivaldi"
    if ";" in raw:
        raw = raw.split(";", 1)[0].strip()

    if raw.casefold() in PROTECTED_FULL_ARTIST_NAMES:
        return raw

    # 3. Ampersand — "Afrika Bambaataa & Soulsonic Force" → "Afrika Bambaataa"
    #    Protected duo/ensemble names (Simon & Garfunkel, Hall & Oates, etc.)
    #    are already returned above.
    if " & " in raw:
        raw = raw.split(" & ", 1)[0].strip()

    if raw.casefold() in PROTECTED_FULL_ARTIST_NAMES:
        return raw

    # 4. Space-dash-space — multi-artist separator; compact hyphens (A-ha, AC-DC) unaffected.
    if " - " in raw:
        parts = [p.strip() for p in raw.split(" - ") if p.strip()]
        if len(parts) >= 2:
            raw = parts[0]

    if raw.casefold() in PROTECTED_FULL_ARTIST_NAMES:
        return raw

    # 5. Comma — "Alice Cooper, Andrew Lloyd Webber" → "Alice Cooper"
    #    Any & was stripped in step 3, so this is safe without the old "&" guard.
    if "," in raw:
        raw = raw.split(",", 1)[0].strip()

    return raw or artist


def _clean_csv_value(value: object) -> str:
    text = clean_metadata_text(value, fallback="")
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        text = text[1:-1].strip()
    return text


def _pick_field(row: dict, names: list[str]) -> str:
    lower_map = {str(k).strip().casefold(): k for k in row}
    for name in names:
        key = lower_map.get(name.casefold())
        if key is not None:
            value = _clean_csv_value(row.get(key))
            if value:
                return value
    return ""


_ARTIST_CANON_CACHE: dict[str, str] | None = None


def load_artist_canon_map() -> dict[str, str]:
    """
    Flexible loader for ORPHEUS artist canon CSVs.

    Supports common shapes:
      artist,canonical_artist
      alias,canonical_artist
      artist,display
      alias,display
      first_two_columns fallback
    """
    global _ARTIST_CANON_CACHE
    if _ARTIST_CANON_CACHE is not None:
        return _ARTIST_CANON_CACHE

    mapping: dict[str, str] = {}

    # Priority rule:
    #   Artist_Canon.generated.csv = suggestions / generated seed
    #   artist_seed.csv            = user seed additions
    #   Artist_Canon.csv           = live approved canon and must win
    #
    # Later files overwrite earlier files.
    files = [
        METADATA_DIR / "Artist_Canon.generated.csv",
        METADATA_DIR / "artist_seed.csv",
        METADATA_DIR / "Artist_Canon.csv",
    ]

    alias_fields = [
        "artist",
        "alias",
        "raw_artist",
        "source_artist",
        "input_artist",
        "name",
    ]
    canonical_fields = [
        "canonical_artist",
        "canonical",
        "display",
        "display_artist",
        "artist_display",
        "target_artist",
        "preferred_artist",
    ]

    for csv_path in files:
        if not csv_path.exists():
            continue

        try:
            with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                fieldnames = reader.fieldnames or []

                for row in reader:
                    alias = _pick_field(row, alias_fields)
                    canonical = _pick_field(row, canonical_fields)

                    if not alias and fieldnames:
                        alias = _clean_csv_value(row.get(fieldnames[0]))
                    if not canonical and len(fieldnames) >= 2:
                        canonical = _clean_csv_value(row.get(fieldnames[1]))

                    if alias and not canonical:
                        canonical = alias

                    if not alias or not canonical:
                        continue

                    canonical = clean_metadata_artist(canonical)
                    mapping[artist_key(alias)] = canonical
                    mapping[artist_key(canonical)] = canonical

        except Exception as e:
            import sys as _sys

            print(
                f"  [warn] load_artist_canon_map: skipped {csv_path.name}: {e}",
                file=_sys.stderr,
            )
            continue

    for key, canonical in BUILTIN_CANON_DISPLAY.items():
        canonical = clean_metadata_artist(canonical)
        mapping.setdefault(artist_key(key), canonical)
        mapping.setdefault(artist_key(canonical), canonical)

    _ARTIST_CANON_CACHE = mapping
    return mapping


def ascii_friendly(value: str) -> str:
    text = normalize_quotes_and_dashes(value)
    text = text.replace("°", "")
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    return text


def sanitize_path_component(
    value: str, unknown: str = UNKNOWN, force_ascii: bool = True
) -> str:
    """
    Filesystem sanitizer.

    Stronger than metadata cleaning:
      - Windows safe
      - exFAT safe
      - Android/CarOS friendly
      - ASCII friendly by default

    Important hyphen policy:
      AC/DC -> AC-DC
      a-ha  -> a-ha
      AC-DC -> AC-DC

    Do not turn compact artist hyphens into spaced separators.
    """
    text = clean_metadata_text(value, fallback=unknown)

    # Slashes become compact hyphens for artist/path safety.
    text = text.replace("/", "-").replace("\\", "-")

    # Colons are Windows-illegal; use a readable spaced dash.
    text = text.replace(":", " -")

    # Remove Windows-illegal characters and control characters.
    text = re.sub(r"[<>\"|?*\x00-\x1F]", "", text)

    # Collapse already-spaced separators only.
    # This preserves compact artist hyphens:
    #   AC-DC stays AC-DC
    #   a-ha stays a-ha
    text = re.sub(r"\s+-\s+", " - ", text)
    text = collapse_spaces(text)

    if force_ascii:
        text = ascii_friendly(text)

    text = collapse_spaces(text)
    text = text.rstrip(" .,_-").strip()

    if not text:
        text = unknown

    if text.upper() in WINDOWS_RESERVED_NAMES:
        text = f"_{text}"

    if len(text) > 180:
        text = text[:180].rstrip(" .,_-").strip()

    return text or unknown


def standardize_title_case(value: str) -> str:
    text = clean_metadata_text(value)
    if not text:
        return text

    letters = [c for c in text if c.isalpha()]
    if not letters:
        return text

    has_lower = any(c.islower() for c in letters)
    has_upper = any(c.isupper() for c in letters)

    # Preserve intentional mixed case.
    if has_lower and has_upper:
        return text

    parts = re.split(r"(\s+|-)", text)
    word_indexes = [i for i, part in enumerate(parts) if part.strip() and part != "-"]

    for pos, idx in enumerate(word_indexes):
        raw = parts[idx]
        low = raw.lower()

        if raw.upper() in PROTECTED_TOKENS:
            parts[idx] = raw.upper()
            continue

        if pos not in {0, len(word_indexes) - 1} and low in SMALL_WORDS:
            parts[idx] = low
        else:
            if "'" in low:
                bits = low.split("'")
                parts[idx] = "'".join(b[:1].upper() + b[1:] if b else b for b in bits)
            else:
                parts[idx] = low[:1].upper() + low[1:]

    return "".join(parts)


def strip_track_number_prefix(value: str) -> str:
    original = clean_metadata_text(value)
    text = original

    patterns = [
        r"^\s*(?:disc|cd)\s*\d+\s*[-_. ]+\s*\d{1,3}\s*[-_. ]+",
        r"^\s*\d{1,2}\s*[-_.]\s*\d{1,3}\s+",
        r"^\s*\d{2,3}\s*[-_.]\s+",
        r"^\s*0\d\s+",
        r"^\s*\d{2,3}[._]\s*",
    ]

    for pattern in patterns:
        new_text = re.sub(pattern, "", text, count=1, flags=re.IGNORECASE).strip()
        if new_text != text and new_text:
            return new_text

    return original


def resolve_artist_for_path(raw_artist: str) -> ArtistPathResolution:
    raw = clean_metadata_artist(raw_artist)
    primary = strip_collaborator_tail(raw) or raw

    canon_map = load_artist_canon_map()
    key = artist_key(primary)

    if key in canon_map:
        canonical = clean_metadata_artist(canon_map[key])
        matched = True
        method = "artist_canon_csv_or_builtin"
        confidence = 0.98 if canonical.casefold() != primary.casefold() else 1.0
    else:
        canonical = clean_metadata_artist(primary)
        matched = False
        method = "artist_raw_fallback"
        confidence = 0.0

    path_artist = sanitize_path_component(canonical)

    return ArtistPathResolution(
        raw_artist=raw,
        primary_artist=primary,
        canonical_artist=canonical,
        path_artist=path_artist,
        matched=matched,
        method=method,
        confidence=confidence,
    )


def resolve_album_artist_for_path(
    album_artist: object, artist: object = ""
) -> ArtistPathResolution:
    """
    Official intake rule:
      album_artist first
      artist second only if album_artist is missing
    """
    chosen = clean_metadata_artist(album_artist, fallback="")
    if not chosen:
        chosen = clean_metadata_artist(artist, fallback="Unknown Artist")
    return resolve_artist_for_path(chosen)


def clean_artist_for_path(value: str) -> str:
    return resolve_artist_for_path(value).path_artist


def clean_album_for_path(value: str) -> str:
    text = clean_metadata_album(value)
    text = normalize_the_suffix(text)
    text = standardize_title_case(text)
    return sanitize_path_component(text)


def clean_title_for_path(value: str) -> str:
    text = clean_metadata_title(value)
    text = strip_track_number_prefix(text)
    text = normalize_the_suffix(text)
    text = standardize_title_case(text)
    return sanitize_path_component(text)


def clean_file_stem_for_path(stem: str) -> str:
    text = clean_metadata_text(stem, fallback=UNKNOWN)
    text = strip_track_number_prefix(text)
    text = normalize_the_suffix(text)
    text = standardize_title_case(text)
    return sanitize_path_component(text)


def clean_folder_name_for_path(name: str) -> str:
    text = clean_metadata_text(name, fallback=UNKNOWN)
    text = normalize_the_suffix(text)
    text = standardize_title_case(text)
    return sanitize_path_component(text)


def build_track_filename(album_artist: str, title: str, ext: str) -> str:
    artist_safe = clean_artist_for_path(album_artist)
    title_safe = clean_title_for_path(title)

    suffix = str(ext or "").strip()
    if not suffix.startswith("."):
        suffix = f".{suffix}"
    suffix = suffix.lower()

    return f"{artist_safe} - {title_safe}{suffix}"


def build_clean_audio_metadata(
    *,
    album_artist: object = "",
    artist: object = "",
    album: object = "",
    title: object = "",
    track: object = "",
    disc: object = "",
    fallback_title: str = "Unknown Title",
) -> dict[str, str]:
    """
    Rich embedded metadata cleaner.

    This is meant for writing tags into exported files.
    It does NOT path-flatten AC/DC, 98°, accents, apostrophes, etc.
    """
    clean_album_artist = clean_metadata_artist(album_artist, fallback="")
    clean_artist_value = clean_metadata_artist(artist, fallback="")

    if not clean_album_artist:
        clean_album_artist = clean_artist_value or "Unknown Artist"
    if not clean_artist_value:
        clean_artist_value = clean_album_artist

    clean_album_value = clean_metadata_album(album, fallback="Unknown Album")
    clean_title_value = clean_metadata_title(
        title, fallback=fallback_title or "Unknown Title"
    )
    clean_track_value = clean_metadata_track_number(track)
    clean_disc_value = clean_metadata_track_number(disc)

    result = {
        "album_artist": clean_album_artist,
        "artist": clean_artist_value,
        "album": clean_album_value,
        "title": clean_title_value,
    }

    if clean_track_value:
        result["track"] = clean_track_value
    if clean_disc_value:
        result["disc"] = clean_disc_value

    return result


def clean_metadata_from_tags(
    tags: dict[str, str], fallback_title: str = "Unknown Title"
) -> dict[str, str]:
    lowered = {str(k).lower(): str(v) for k, v in (tags or {}).items()}

    album_artist = (
        lowered.get("album_artist")
        or lowered.get("albumartist")
        or lowered.get("album artist")
        or ""
    )
    artist = lowered.get("artist") or ""
    album = lowered.get("album") or ""
    title = lowered.get("title") or fallback_title

    track = lowered.get("track") or lowered.get("tracknumber") or ""
    disc = lowered.get("disc") or lowered.get("discnumber") or ""

    return build_clean_audio_metadata(
        album_artist=album_artist,
        artist=artist,
        album=album,
        title=title,
        track=track,
        disc=disc,
        fallback_title=fallback_title,
    )


def ffmpeg_metadata_args(clean_tags: dict[str, str]) -> list[str]:
    """
    Args appended after -map_metadata 0 so clean core tags override dirty originals.
    """
    args: list[str] = []

    field_map = {
        "album_artist": "album_artist",
        "artist": "artist",
        "album": "album",
        "title": "title",
        "track": "track",
        "disc": "disc",
    }

    for key, ffmpeg_key in field_map.items():
        value = clean_tags.get(key)
        if value:
            args += ["-metadata", f"{ffmpeg_key}={value}"]

    return args


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path

    parent = path.parent
    stem = path.stem
    suffix = path.suffix
    n = 2

    while True:
        candidate = parent / f"{stem} [{n}]{suffix}"
        if not candidate.exists():
            return candidate
        n += 1


def ensure_naming_db_tables() -> None:
    try:
        METADATA_DIR.mkdir(parents=True, exist_ok=True)
        with open_db(DB_PATH) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS artist_path_resolution_cache (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    raw_artist TEXT,
                    primary_artist TEXT,
                    canonical_artist TEXT,
                    path_artist TEXT,
                    matched INTEGER,
                    method TEXT,
                    confidence REAL,
                    resolved_at TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS path_routing_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_script TEXT,
                    source_path TEXT,
                    destination_path TEXT,
                    raw_artist TEXT,
                    primary_artist TEXT,
                    canonical_artist TEXT,
                    path_artist TEXT,
                    album TEXT,
                    title TEXT,
                    filename TEXT,
                    method TEXT,
                    confidence REAL,
                    details_json TEXT,
                    created_at TEXT
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_path_routing_events_path
                    ON path_routing_events(source_path)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS metadata_cleaning_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_script TEXT,
                    source_path TEXT,
                    raw_album_artist TEXT,
                    raw_artist TEXT,
                    raw_album TEXT,
                    raw_title TEXT,
                    clean_album_artist TEXT,
                    clean_artist TEXT,
                    clean_album TEXT,
                    clean_title TEXT,
                    details_json TEXT,
                    created_at TEXT
                )
            """)
            conn.commit()
    except Exception as e:
        import sys as _sys

        print(f"  [!] ensure_naming_db_tables failed: {e}", file=_sys.stderr)


def log_artist_resolution(resolution: ArtistPathResolution) -> None:
    try:
        ensure_naming_db_tables()
        with open_db(DB_PATH) as conn:
            conn.execute(
                """
                INSERT INTO artist_path_resolution_cache (
                    raw_artist, primary_artist, canonical_artist, path_artist,
                    matched, method, confidence, resolved_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    resolution.raw_artist,
                    resolution.primary_artist,
                    resolution.canonical_artist,
                    resolution.path_artist,
                    1 if resolution.matched else 0,
                    resolution.method,
                    resolution.confidence,
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
            conn.commit()
    except Exception as e:
        import sys as _sys

        print(f"  [!] log_artist_resolution failed: {e}", file=_sys.stderr)


def log_naming_event(
    *,
    source_script: str,
    source_path: Path | str,
    destination_path: Path | str,
    raw_artist: str = "",
    album: str = "",
    title: str = "",
    filename: str = "",
    method: str = "",
    details: dict | None = None,
) -> None:
    try:
        ensure_naming_db_tables()

        resolution = (
            resolve_artist_for_path(raw_artist)
            if raw_artist
            else ArtistPathResolution(
                raw_artist="",
                primary_artist="",
                canonical_artist="",
                path_artist="",
                matched=False,
                method="no_artist",
                confidence=0.0,
            )
        )

        if raw_artist:
            log_artist_resolution(resolution)

        with open_db(DB_PATH) as conn:
            write_with_retry(
                conn,
                """
                INSERT INTO path_routing_events (
                    source_script, source_path, destination_path,
                    raw_artist, primary_artist, canonical_artist, path_artist,
                    album, title, filename, method, confidence, details_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_script,
                    str(source_path),
                    str(destination_path),
                    resolution.raw_artist,
                    resolution.primary_artist,
                    resolution.canonical_artist,
                    resolution.path_artist,
                    album,
                    title,
                    filename,
                    method or resolution.method,
                    resolution.confidence,
                    json.dumps(details or {}, ensure_ascii=False, sort_keys=True),
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
            conn.commit()

    except Exception as e:
        import sys as _sys

        print(f"  [!] log_naming_event failed: {e}", file=_sys.stderr)


def log_metadata_cleaning_event(
    *,
    source_script: str,
    source_path: Path | str,
    raw_tags: dict[str, str],
    clean_tags: dict[str, str],
    details: dict | None = None,
) -> None:
    try:
        ensure_naming_db_tables()
        raw = {str(k).lower(): str(v) for k, v in (raw_tags or {}).items()}

        with open_db(DB_PATH) as conn:
            write_with_retry(
                conn,
                """
                INSERT INTO metadata_cleaning_events (
                    source_script, source_path,
                    raw_album_artist, raw_artist, raw_album, raw_title,
                    clean_album_artist, clean_artist, clean_album, clean_title,
                    details_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_script,
                    str(source_path),
                    raw.get("album_artist")
                    or raw.get("albumartist")
                    or raw.get("album artist")
                    or "",
                    raw.get("artist") or "",
                    raw.get("album") or "",
                    raw.get("title") or "",
                    clean_tags.get("album_artist", ""),
                    clean_tags.get("artist", ""),
                    clean_tags.get("album", ""),
                    clean_tags.get("title", ""),
                    json.dumps(details or {}, ensure_ascii=False, sort_keys=True),
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
            conn.commit()

    except Exception as e:
        import sys as _sys

        print(f"  [!] log_metadata_cleaning_event failed: {e}", file=_sys.stderr)


# ORPHEUS filename policy:
# Audio file extensions are always lowercase.
# Examples:
#   .FLAC -> .flac
#   .M4A  -> .m4a
#   .MP3  -> .mp3
def lowercase_audio_extension(filename: str) -> str:
    """Return filename with audio extension normalized to lowercase."""
    audio_exts = {
        ".mp3",
        ".flac",
        ".m4a",
        ".aac",
        ".alac",
        ".wav",
        ".aiff",
        ".ogg",
        ".oga",
    }

    p = Path(filename)
    if p.suffix.lower() in audio_exts:
        return p.with_suffix(p.suffix.lower()).name

    return filename
