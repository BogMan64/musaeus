# discography-lab

Studio albums your library holds **nothing** from.

Not "you are missing track 7" but *"you own five Springsteen records and
Nebraska is not one of them"*. ORPHEUS had `orpheus_discography_diff.py`; this is
the same idea rebuilt around one ruling from Grey and one measurement of the
actual library.

**Read-only on the library.** Opens the vault database `mode=ro`, writes one CSV,
touches no audio file and writes to no MUSAEUS database.

```bash
cd ~/Desktop/discography-lab

# 1. Always start here. No network at all.
python3 discography_gaps.py --dry-run

# 2. A few artists, to see real output before committing to a long run.
python3 discography_gaps.py --limit 3 --out /tmp/probe.csv

# 3. The lot.
python3 discography_gaps.py
```

Output defaults to `~/Desktop/DISCOGRAPHY_gaps.csv`.

---

## Status, honestly

**The live path has never returned data.** Three attempts: the first was refused
by MUSAEUS's own network policy (see below), and once that was granted properly
the next attempt gave two read timeouts and an HTTP 503 from MusicBrainz. So
the fetching, paging and 503 handling are **unproven against the real service**.

What *is* verified: 37 offline tests pass, `ruff` is clean, and the dry run works
against your real library and resolves all 63 eligible artists. Everything that
decides a *right or wrong answer* — the scope ruling and the album matcher — is
pure and tested. What is untested is the plumbing that fetches.

Treat the first real run as an experiment, not a report.

---

## The ruling

Grey's call, and it is the whole feature:

> primary-type = Album, with **no secondary types**.

Ask MusicBrainz for The Beatles' release groups and you get several hundred:
every compilation, regional variant, live album, box set, interview disc. "You
are missing 400 Beatles albums" is not a noisy report, it is a useless one.

Both halves are needed, and the second is the operative one. A live album has
primary-type `Album` **and** secondary-type `Live`, so filtering on primary type
alone lets every live album and greatest-hits collection straight through — which
is exactly the noise the ruling exists to remove.

`discography/scope.py` returns a *reason* rather than a boolean, so the CSV can
say "excluded: Live" instead of silently dropping things. This filter removes the
large majority of what MusicBrainz returns, which is precisely when being able to
check it matters.

### What the ruling deliberately gets wrong

- **A live album you own does not count as owning something from that artist.**
  It cannot — it is not in the candidate list at all. Harmless here, because the
  report only ever says "you own no *studio* album called X". You do collect live
  material: *The Last Waltz* and *Before the Flood* are both in the library.
- **Soundtracks are excluded.** Right for most artists, wrong for a film
  composer. None in the eligible 63 today, so a limitation rather than a defect.
- **Antonio Vivaldi is in the eligible list and should not be trusted.** A
  composer's "discography" in MusicBrainz is thousands of release groups by
  hundreds of performers. Studio-album gaps are meaningless for classical.

---

## The measurement that shaped this tool

Your library is **singles-oriented, not album-oriented**. Measured on 2,140
catalogued tracks:

| | |
|---|---|
| tracks with no album tag | **782 (37%)** |
| largest "album" | `My playlist B`, 183 tracks |
| artists with exactly one track | **277 of 464** |
| artists with 2+ real albums and 5+ tracks | **63** |

So a gap report that runs over every artist produces mostly nonsense. "You own
1 track by Artist X and are missing their 14 albums" is technically true, useless,
and it buries the rows that matter.

Eligibility is therefore a first-class part of the tool rather than a filter
bolted on, the thresholds are arguments (`--min-albums`, `--min-tracks`), and the
run states them — because they are a judgement about what you collect, not a fact
about music.

`My playlist B` and friends are not albums. Nine tracks all sitting on a playlist
export clears the track threshold but means there is no album collection to find
gaps in, so that artist is excluded too.

---

## Artist MBIDs: no network needed

Only **20 of the eligible artists** carry an `mb_artist_id` in the archive, and
the four largest collections are among the missing — The Beatles (31 albums),
Bob Dylan (20), Bruce Springsteen (18), Billy Joel (18) all have none. Resolving
those over the network would be 40+ searches before any real work started.

They are already resolved in MUSAEUS's own cache
(`_db_backups/mb_cache.db`, 2,646 found of 3,186), keyed on the stored artist
name lowercased. All four resolve from it. The dry run confirms: 25 from the
archive, 38 from the cache, **0 unresolvable**.

A `found=0` cache row is *not* used. It means "looked up, not there", and using
its empty mbid would send a browse request for an empty artist.

---

## Borrowed, not copied

Three things come from MUSAEUS rather than being reimplemented. Each falls back
to a standalone version when MUSAEUS is absent, and **each says which one is in
force** in the run header — a fallback nobody can see is one nobody can trust.

**The album matcher** — `neardupe._normalise(strip_qualifiers=True)`, the same
function fm-radio-lab borrows for track titles.

**The MusicBrainz client** — `mb_enrich._mb_get`. This matters more than
convenience: MusicBrainz identifies and blocks clients by User-Agent, and the
rate limit is per IP, not per process. Two polite clients each waiting 1.1s still
send two requests a second between them.

**The network gateway** — inherited by borrowing `_mb_get`, and it stopped the
first live run dead:

```
NetworkDenied: network access to 'https://musicbrainz.org/ws/2' refused:
policy is local-only.
```

That is the gateway working. Its default is `LOCAL_ONLY` — "preview and any
unattended default". So permission is now **requested rather than routed
around**, via `network_policy.policy()` in a `with` block, and the run announces
it. Scoped deliberately: that module's own docstring records a P0-14 test that
passed alone and failed in the full suite because a caller set `ALLOWED` and
never put it back.

### One place I did not borrow

MUSAEUS's `STRIP_WORDS` is tuned for **track** version qualifiers. Measured:

```
"Nebraska (2015 Remaster)"       -> "nebraska"                  matched
"Nebraska [Deluxe Edition]"      -> "nebraska deluxe edition"    MISSED
"Nebraska (Expanded)"            -> "nebraska expanded"          MISSED
"Nebraska (Anniversary Edition)" -> "nebraska anniversary ..."   MISSED
```

Every miss is a **false gap** — telling you an album is missing when it is
sitting in the library. Do that once and the report stops being believed.

So album editions are stripped first and the shared fold is still delegated.
Adding these words to MUSAEUS's list would be wrong: they describe a *packaging
of a record*, not a *version of a performance*.

The strip is **bracket-scoped**, and only fires when the bracket contains nothing
but edition words. The opposite failure is worse: a false match reports an album
as owned when it is not, so the gap silently disappears. *Special Beat Service*
and *Mono* are real album titles, and stripping those words wherever they
appeared would fold genuinely different records together.

---

## The compilation caveat

Own *Bob Dylan's Greatest Hits* and you own Blowin' in the Wind — so "you own
nothing from The Freewheelin' Bob Dylan" is misleading even though, at album
level, it is true.

Resolving it properly means fetching every release group's tracklist and
comparing recordings: one extra request per album against a source limited to one
request per second. So the gap is reported at album level, as you framed it, and
the row carries `may_be_covered_by_compilation`. A caveat you can see beats a
silent inaccuracy.

Be aware it fires on most rows — most of these artists own a hits collection.

---

## Unavailable is not empty

An artist whose lookup failed has an **unknown** discography, which is the
opposite of a complete one. Reporting zero gaps would say "you own everything"
about an artist nobody managed to ask about — the most damaging thing this tool
could say.

So `mb.Unavailable` is raised rather than returning `[]`, failures are counted
separately from complete discographies, and the summary labels them
`UNKNOWN, lookup failed ... (not 'complete')`. The three failed probe runs
correctly wrote **0 rows** and claimed **0 complete**.

The run also asserts its own arithmetic: every studio album must be either owned
or missing.

---

## Layout

| file | what it is |
|---|---|
| `discography/scope.py` | the ruling, and what it excludes |
| `discography/library.py` | the vault, eligibility, MBID resolution |
| `discography/match.py` | owned album ↔ release group |
| `discography/mb.py` | MusicBrainz client, paging, throttle, policy gate |
| `discography_gaps.py` | CLI, CSV, summary |
| `tests/test_discography.py` | 37 tests, all offline |

```bash
python3 -m pytest tests/ -q
```

---

## Known gaps

- **No successful live run.** See "Status" above. Timeouts and a 503 on the only
  attempt that got past the policy gate. Paging (`MAX_PAGES = 12`, 100 per page)
  is written but unexercised, and the timeout is MUSAEUS's 15s, which may simply
  be too short for a browse of The Beatles.
- **The dry-run time estimate is optimistic.** It assumes one request per artist.
  Large artists page repeatedly, so 63 artists is well over 63 requests.
- **Classical composers are in scope and should not be.** Vivaldi, above.
- **`may_be_covered_by_compilation` fires on most rows**, which makes it weak as
  a per-row signal.
- **No caching.** ORPHEUS cached its discography results, which tells you it was
  slow. Every run re-fetches everything.
- **It imports two private names** (`_normalise`, `_mb_get`). They can move
  without warning. That is the accepted cost of not keeping second copies of
  those rules, and the failure is loud: the tests fail if MUSAEUS is importable
  but a borrowing is dead.
