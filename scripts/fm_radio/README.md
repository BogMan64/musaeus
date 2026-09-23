# fm-radio-lab — which pressing is the one your ears remember

**Built 2026-09-10 by Kiro, for Grey. Standalone: outside MUSAEUS and ORPHEUS,
imports neither, modifies neither.**

Wishlist item 2. Reads the MUSAEUS library, writes a CSV, and nothing else. No
database is modified, no tag is written, no file is moved.

```bash
python3 fm_radio_identifier.py --dry-run --limit 20     # cache only, no network
python3 fm_radio_identifier.py --limit 20               # pass 1 (ListenBrainz)
python3 fm_radio_identifier.py --limit 20 --pass2       # + MusicBrainz, one run
python3 fm_radio_identifier.py --screensaver --limit 500  # only while idle
python3 -m pytest -q                                     # 35 tests, all offline
```

---

## For the reviewer: the two things I would attack first

**1. Are the scoring weights defensible?** `score.py` has ten named constants
and I chose every number by judgement, not measurement. The one I would
challenge hardest is `W_MOST_PLAYED = 25` against a remaster penalty of 30:
that asymmetry encodes Grey's ruling that the original beats a more-played
remaster, and it is load-bearing but arbitrary in magnitude.

**2. The listen floor is now per artist. FIXED.** It was an absolute 100,
measured on The Beatles: 9,057 records returned, 2,249 with exactly one listen,
only 1,753 above 100. But the first live run hit `108 Music` and got **zero**
recordings over 100 — so for an obscure artist the floor silenced the source
entirely and the run reported "no ListenBrainz signal", which is also what an
outage looks like. A real defect for a library with 464 artists, many obscure.

The floor is now derived from each artist's own counts (`fmradio/popularity.py`,
`--quantile`, default top decile), and the fetch-time number is only a noise
gate at 5 listens.

Implementing it turned up a second problem worth knowing about. A rank-based
90th percentile **fails on exactly the distribution it was introduced for**:
when the junk tail is most of the catalogue, the 90th percentile by rank is
itself junk. A test with 2,249 one-listen records against two millionsellers
produced a floor of 5 — *lower* than the floor for an obscure artist whose best
track had 40 listens. The statistic inverted, handing the noisiest artist the
most permissive filter. So the floor is the higher of the quantile and 1% of the
artist's peak count, which cannot be dragged down by tail volume.

The floor can also be too aggressive: ask about a deep album cut and the
artist's top decile sits far above it. `apply_floor()` is therefore guaranteed
non-empty — if the floor would remove every pressing of the song being asked
about, it is waived and the row says so. A filter that can silently empty its
own input is the original bug one level down.

---

## What it cannot do, and why

The wishlist asks it to "prefer the pressing that actually charted".
**MusicBrainz holds no chart data.** The obvious substitute is worse than
nothing: `billboard-charts`' own PyPI page warns that Billboard returns HTTP
200 with plausible markup for weeks that never existed, stamped with whatever
date you asked for -- so you store fabricated history that looks complete.
That is the one output this project cannot tolerate.

So charting is approximated from three directions, and **the CSV says which of
them it actually had**:

1. **was it a single** -- release-group type, plus radio/single-edit markers
2. **was it first** -- the *recording's* own first-release-date
3. **is it the one played** -- ListenBrainz total listens

## The finding that matters most

**Pass 1 cannot answer the question on its own.** ListenBrainz returns no
release date and no release-group type, so a pass-1-only verdict scores
popularity and length and nothing else -- while "which pressing came first" is
the entire point. Confirmed on the first live run: all seven initially
"confident" rows carried `no first-release-date -- cannot judge whether it came
first`.

Pass 2 is therefore **required for a real answer**, not an occasional top-up.
Pass 1 still earns its place -- one request per artist returns the whole
catalogue ranked, so pass 2 asks a focused question instead of a broad one --
but the saving is smaller than the two-pass framing suggests.

## Layout

| file | what |
|---|---|
| `fmradio/model.py` | `Candidate`, `Verdict`, `Proposal`. No I/O, no API types. |
| `fmradio/score.py` | `rank(artist, title, candidates)`. Pure. All the judgement. |
| `fmradio/clients.py` | ListenBrainz + MusicBrainz, behind protocols, with fakes. |
| `fmradio/cache.py` | SQLite, resumable, commits per row. |
| `fmradio/idle.py` | The screensaver gate (X Screen Saver via ctypes). |
| `fmradio/secrets.py` | Token loading. Never from a command line. |
| `fmradio/report.py` | The proposal CSV. Decision column first. |
| `tests/` | 35 tests, no network, no clock. |

## Design decisions worth knowing about

**`rank()` takes a SET, not one candidate.** Earliest, most-played and shortest
are only meaningful relative to the other pressings of the same song.

**An error is never data.** 401/403 raise and stop the run; an unreachable
source raises; `[]` is returned only when the source answered and had nothing.
This exists because of a real trap: an unauthenticated request returned HTTP
200 with 9,057 genuine Beatles records *from an edge cache*, while every other
artist returned 401. Had the client shrugged at 401, every artist would have
scored as "no ListenBrainz signal" and the run would have reported success
having measured nothing.

**`listen_count=None` is unknown, never zero.** Zero would rank an unmeasured
pressing identically to one nobody plays. Same conflation that made
`musaeus_report.py` say nothing at all about 2,862 pending duplicate groups.

**Instrumentals are penalised RELATIVELY.** −70 when a vocal pressing of the
same song exists, 0 when it does not. A flat penalty was my first attempt and
a test caught it: instrumentals genuinely were radio hits -- *Green Onions*,
*Telstar*, and Grey's own MasterLaw keeps `Mort Stevens & His Orchestra —
Hawaii Five-O` under Surf Rock.

**`"edit"` is a POSITIVE marker.** ORPHEUS's `orpheus_fm_radio_identifier.py`
listed it under `exclude_keywords`, which filtered out its own best signal --
a *radio edit* is frequently the exact version wanted.

**Single length is 100–330s, not ORPHEUS's 120–360.** Section 5 measured a
120-second floor flagging 397 complete recordings -- *Hit the Road Jack* at
2:00, *All Shook Up* at 1:58.

**The recording's date beats the release group's.** MetaBrainz's own docs warn
these differ: a 1969 recording on a 1990 *Greatest Hits* has a release-group
date of 1990. Taking the minimum is what MUSAEUS's `original_year.py` does.

**Four report states, not two.** `CONFIDENT`, `ONLY ONE CANDIDATE (no choice
was made)`, `POPULARITY ONLY (no release date)`, `NEEDS A RULING`. The first
live run labelled seven rows `CONFIDENT` when four of them had a single
candidate -- confidence in a decision never made. That was my bug and these
states are the fix.

## Credentials

One only: a **ListenBrainz user token** from `listenbrainz.org/settings/`
(signs in via MusicBrainz). Read from `LISTENBRAINZ_USER_TOKEN` or
`/mnt/NUC8TB_BACKUP/SECRETS/listenbrainz.env`, never from an argument.

It is **not** a MetaBrainz OAuth client id/secret and **not** a MetaBrainz
access token; those are different credentials for different services and
neither works here. MusicBrainz itself needs no token, only a User-Agent.

Both services allow **one call per second**. `clients.py` also reads
`X-RateLimit-Remaining` and `X-RateLimit-Reset-In` and obeys the server's own
accounting, which their docs recommend over a fixed sleep because it survives a
client with a wrong clock.

## Known gaps

- **Pass 2 still has no live MusicBrainz call.** It now fetches *and* re-scores
  in one run (`score_songs()` is pure, so it is simply called twice), and the
  "re-run to score the newly cached pressings" message is gone. Covered by
  `tests/test_two_pass.py`, but no network run yet.
- **`base_title()` now borrows from MUSAEUS.** `fmradio/titles.py` imports
  `musaeus.stages.neardupe._normalise` and falls back to the crude bracket-split
  only when MUSAEUS is absent. Which one is in force is **printed in the run
  header**, because the first attempt at this imported a `base_title` that does
  not exist from a `musaeus.neardupe` that is not the right path — a plain
  `try/except ImportError` would have used the crude version forever while the
  README claimed an upgrade.
- **It imports a private name.** `_normalise` can move without warning. That is
  the accepted cost of not keeping a second copy of the rule, and the failure is
  loud rather than silent: `tests/test_titles.py` fails if MUSAEUS is importable
  but the borrowing is dead.
- **`idle.py` duplicates ~20 lines of `musaeus/idle_throttle.py`** deliberately,
  so this stays standalone. Documented in that file; a real divergence would be
  a problem.
- **No live run larger than 12 songs.** Behaviour at 2,140 is untested.
