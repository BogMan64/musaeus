# Album names

Two programs. One proposes, one writes, and they are separate on purpose.

    propose_album_names.py   read-only; asks three services, writes a CSV
    apply_album_names.py     writes the catalogue; dry run unless --live

## The usual run

```bash
# 1. see the size of the job without touching anything
python3 scripts/album_names/propose_album_names.py --dry-run

# 2. ask. Resumable -- answers are cached, so an interrupted run
#    costs nothing to restart. --path-prefix narrows it to one tree.
python3 scripts/album_names/propose_album_names.py --rate 2.5

# 3. review the CSV, then apply only the tier you trust
python3 scripts/album_names/apply_album_names.py ~/Desktop/MUSAEUS_album_names_PROPOSED.csv
python3 scripts/album_names/apply_album_names.py ~/Desktop/MUSAEUS_album_names_PROPOSED.csv --live
```

## Confidence tiers

| tier | meaning |
|---|---|
| `1-AGREED` | two or more sources named the same album — the only tier applied by default |
| `2-<SOURCE> ONLY` | one source answered; plausible, unreviewed |
| `3-SOURCES DISAGREE` | they named different albums; a person has to choose |
| `4-NO ANSWER` | nobody knew |

## Two things this file exists to stop you re-learning

**Ask in the natural form.** The library stores `Beatles, The`; no service has
heard of that string. Measured 2026-09-14: 1,072 rows in the proposal CSV had
a trailing-article artist and 1,071 of them — 99.9% — came back
`4-NO ANSWER`. Re-asking 25 of them as `The Beatles` resolved 19, five
straight to `1-AGREED`. This is why the script is in the repository: it can
import `musaeus.artist_form`, and on the Desktop it could not.

**A proposal never overwrites an album that is already set.** It answers
"what goes in this empty field", not "what should this be". Re-running a CSV
against rows a later pass already filled would otherwise replace real album
names with guesses.

## Sources

iTunes, Deezer, MusicBrainz. No API key is needed for any of them.

Spotify was the third source until 2026-09-14 and cannot be: it issues a
token and then answers `/v1/search` with HTTP 403 *"Active premium
subscription required for the owner of the app"*. A token is not permission
to search, and no code change fixes an account requirement.

MusicBrainz's own relevance score is not trusted — a search for Fleetwood
Mac's "Dreams" returns "Mac dreams" at score 100 — so all three sources are
matched by the same strict artist/title folding.
