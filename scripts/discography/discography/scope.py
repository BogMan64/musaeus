"""What counts as an album you should own. Grey's ruling, encoded and tested.

THE RULING, 2026-09-10:

    primary-type = Album, with NO secondary types.

That one line is the whole feature. Ask MusicBrainz for The Beatles' release
groups and you get several hundred: every compilation, every regional variant,
live albums, box sets, interview discs, karaoke editions. Reporting "you are
missing 400 Beatles albums" is not a noisy report, it is a useless one. The
filter is what turns that into roughly a dozen studio LPs.

MusicBrainz models this in two fields and BOTH are needed:

  primary-type      Album | Single | EP | Broadcast | Other
  secondary-types   Compilation, Soundtrack, Live, Remix, DJ-mix, Mixtape/Street,
                    Demo, Field recording, Interview, Audiobook, Audio drama,
                    Spokenword

A live album has primary-type Album AND secondary-type Live. So filtering on
primary-type alone lets every live album and greatest-hits through, which is
exactly the noise the ruling exists to remove. "No secondary types" is the
operative half.

WHAT THIS DELIBERATELY GETS WRONG, so it is not discovered later as a bug:

  - A live album you own does not count as "owning something from this artist".
    It cannot: it is not in the candidate list at all. That is harmless for
    THIS report -- we only ever say "you own no studio album called X" -- but it
    would be wrong for a report that claimed to describe your whole collection.
    Grey demonstrably does collect live material: The Last Waltz (13 tracks)
    and Before the Flood (12) are both live albums in the library.

  - Soundtracks are excluded. For most artists right; for a film composer,
    everything. No composer in the qualifying 42 today, so this is a known
    limitation rather than a present defect.

  - A studio album later reissued as "Deluxe Edition" is usually the SAME
    release group in MusicBrainz, so it is not double-counted. Where an editor
    has created a separate release group for a reissue, it will appear twice.
    Reported rather than silently merged: merging by title similarity would
    also merge Alice Cooper's "Killer" with "Killers".
"""

from __future__ import annotations

from dataclasses import dataclass

#: The ruling. An album is a studio album when its primary type is Album and it
#: carries no secondary type at all.
STUDIO_PRIMARY = "album"

#: Named rather than inlined so the report can explain WHY something was
#: excluded, and so a future ruling ("include Live after all") is one edit.
#: These are MusicBrainz's own secondary-type values, lowercased.
KNOWN_SECONDARY = (
    "compilation",
    "soundtrack",
    "live",
    "remix",
    "dj-mix",
    "mixtape/street",
    "demo",
    "field recording",
    "interview",
    "audiobook",
    "audio drama",
    "spokenword",
)


@dataclass(frozen=True)
class ReleaseGroup:
    """One album as a concept, not one pressing of it.

    Release GROUP is the right granularity and ORPHEUS's version had this
    right: a release group is "Nebraska", while a release is "Nebraska, 1982
    US vinyl" versus "Nebraska, 2015 Japanese CD". Asking about releases would
    report the same album missing a dozen times.
    """

    mbid: str
    title: str
    primary_type: str = ""
    secondary_types: tuple[str, ...] = ()
    first_release_date: str = ""

    @property
    def year(self) -> int | None:
        head = (self.first_release_date or "").strip()[:4]
        return int(head) if head.isdigit() else None


def excluded_because(rg: ReleaseGroup) -> str:
    """Why this release group is not a studio album, or "" if it is.

    Returns a REASON rather than a boolean, so the report can say "excluded:
    Live" instead of silently dropping it. A filter that cannot explain itself
    is one nobody can check, and this filter removes the large majority of
    everything MusicBrainz returns -- which is precisely when being checkable
    matters.
    """
    primary = (rg.primary_type or "").strip().lower()
    if not primary:
        return "no primary type recorded in MusicBrainz"
    if primary != STUDIO_PRIMARY:
        return f"primary type is {rg.primary_type}, not Album"
    secondaries = [s.strip() for s in rg.secondary_types if s and s.strip()]
    if secondaries:
        return "secondary type: " + ", ".join(secondaries)
    return ""


def is_studio_album(rg: ReleaseGroup) -> bool:
    return not excluded_because(rg)


def partition(groups: list[ReleaseGroup]) -> tuple[list[ReleaseGroup], list[tuple[ReleaseGroup, str]]]:
    """(studio albums, [(everything else, why)]).

    Both halves are returned because the excluded count is the number that
    tells you whether the ruling is behaving. If 400 Beatles release groups
    reduce to 13 studio albums, that is the filter working; if they reduce to 0,
    something is wrong with the field names and the report would otherwise
    just look like "you own everything".
    """
    keep: list[ReleaseGroup] = []
    drop: list[tuple[ReleaseGroup, str]] = []
    for rg in groups:
        why = excluded_because(rg)
        (drop.append((rg, why)) if why else keep.append(rg))
    return keep, drop
