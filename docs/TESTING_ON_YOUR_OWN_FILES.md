# Testing MUSAEUS on your own music

Thanks for trying this. Please read the next section before you run
anything — it is short, and it is the part that matters.

---

## Read this first

**MUSAEUS moves, renames, transcodes and de-duplicates your files.** That is
its job, not a side effect. It is a pipeline for reorganising a music
library into a canonical structure, and a normal successful run will leave
your files somewhere other than where they started, with different names and
in a different format.

**`musaeus run` executes immediately. It does not ask, and it does not
preview.** Previewing is a flag:

```bash
docker compose run --rm musaeus run --dry-run
```

So, three rules for a first test:

1. **Work on copies.** Put *copies* of some music into `vault/INBOX`. Never
   point MUSAEUS at your only copy of anything. It moves files out of INBOX
   rather than copying them.
2. **Start small.** Fifty files, not fifty thousand. You will learn the same
   things and a mistake costs a minute.
3. **Run `--dry-run` first**, every time, until the output stops surprising
   you.

Nothing here is theoretical caution. This project's own history is a list of
faults that no error message announced — an encoder silently resampling
masters, a "dry run" deleting a staging directory a live encode was reading
from, four files truncated behind intact headers. It is careful software
*because* those happened.

**What is on your side:** removals move, they do not delete. The recovery
module deliberately exposes no delete operation at all, and the duplicate
resolver writes a CSV manifest plus an executable restore script next to
whatever it moved. If a run does something you did not want, look for
`RUNS/` and `DUPES_MOVED_FOR_REVIEW/` before you panic.

---

## Getting started

You need Docker with Compose v2 (`docker compose`, which Docker
Desktop ships). Nothing else — Python, ffmpeg and fpcalc all live in the
image, pinned to the versions MUSAEUS was developed against.

```bash
git clone https://github.com/BogMan64/musaeus.git
cd musaeus

# On Linux/macOS, so new files belong to you and not to root.
# (MUSAEUS_UID, not UID -- UID is readonly in bash and cannot be set inline.)
MUSAEUS_UID=$(id -u) MUSAEUS_GID=$(id -g) docker compose build
# On Windows, plain `docker compose build` is fine.

mkdir -p vault/INBOX
cp /path/to/some/music/*.flac vault/INBOX/     # COPIES

docker compose run --rm musaeus preflight        # check the environment
docker compose run --rm musaeus run --dry-run    # see what it would do
docker compose run --rm musaeus run              # do it
```

After a run, look in:

| Path | What is there |
|---|---|
| `vault/Libraries/ALAC-Library/` | your music, transcoded and organised `Artist/Album/` |
| `vault/Libraries/ALAC_Archive/DUPES_MOVED_FOR_REVIEW/` | duplicates it set aside, with a restore script |
| `vault/RUNS/HANDOFFS/` | a written report of the run — start here |
| `vault/RUNS/FAILURES/` | JSON detail for any stage that failed |
| `vault/QUARANTINE/` | anything it refused to process |

The handoff document in `RUNS/HANDOFFS/` is the single most useful artefact.
It names every stage that failed, what it was doing, and the exact error.
If you report a problem, that file is what to send.

## What needs no API keys

Everything structural: ingest, integrity checking, duplicate detection,
transcoding, organising, the audit. Enrichment stages (genre from Last.fm,
identity from MusicBrainz) no-op with a warning when keys are absent — they
do not fail the run. MusicBrainz needs no key at all.

To add keys, put them in a `.env` file beside `docker-compose.yml`:

```
LASTFM_API_KEY=...
ACOUSTICID_API_KEY=...
```

## Known rough edges

- **The database is deliberately not in `vault/`.** It lives on a Docker
  named volume, because SQLite's WAL mode needs locking primitives that
  Docker Desktop's Windows and macOS bind mounts do not reliably provide.
  Moving it onto the bind mount will eventually corrupt it. `docker compose
  down -v` deletes that volume and therefore the catalogue — your *files*
  are untouched, but MUSAEUS forgets what it knows about them.
- **Small-file throughput on Windows and macOS is slow**, because every read
  crosses the bind-mount boundary. A few hundred files is comfortable;
  tens of thousands will be tedious. This is Docker Desktop, not MUSAEUS.
- **`PermissionsStage` chmods files and verifies the result.** On a
  filesystem without POSIX permission bits (an NTFS or exFAT drive passed
  through) it may report failures that mean nothing. `--skip permissions`
  if it gets noisy.
- **The interactive console** (`docker compose run --rm musaeus console`)
  works — verified, menu and all, with the container's paths reported
  correctly and no setup wizard. Two of its entries reach for locations
  that only exist on the maintainer's machine, though: the documentation
  browser defaults to `/mnt/FORGE2TB/DOCUMENTATION` unless you set
  `MUSAEUS_DOC_ROOT`, and "Transfer to USB" expects a real removable
  device. Nothing on the `run` path touches either.

## What has and has not been verified

Being explicit, because "should work" is how the interesting bugs get shipped:

**Verified by running it** — the image builds; the full test suite (2,123
tests) passes inside the container, matching the maintainer's host exactly;
a real run over six files transcoded and organised five, set the exact
duplicate aside with a restore script, quarantined the originals rather than
deleting them, reached MusicBrainz over the network, and wrote its handoff
document; `--dry-run` changed nothing; the interactive console runs.

**Not verified here** — the `docker compose` path itself. The maintainer's
machine has only Compose v1, which is broken against its installed
libraries, so everything above was proven with plain `docker build` and
`docker run`. The compose file's variable substitution was checked
(`docker-compose config` resolves `MUSAEUS_UID` correctly, and defaults to
1000 when unset), but no image has been built *through* compose. If
`docker compose build` misbehaves, that is the untested seam — fall back to:

```bash
docker build --build-arg UID=$(id -u) --build-arg GID=$(id -g) -t musaeus:local .
docker run --rm -it -v "$PWD/vault:/vault" -v musaeus-state:/state musaeus:local run --dry-run
```

**Windows and macOS specifically** — untested. The design accounts for
their bind-mount limitations (that is why the database is on a named
volume), but no one has run it there yet. You would be the first.

## Reporting something

Useful: the handoff doc from `RUNS/HANDOFFS/`, what you ran, roughly what
was in INBOX (formats, how many), and your OS. The handoff doc alone answers
most of it.
