# Changelog

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

`0.2.0` is the first tagged version. `pyproject.toml` said `0.1.0` from July
2026 while 302 pull requests were merged, so the sections below it are by
month, newest first, and name the pull requests. The full record is
`gh pr list --state merged --limit 500`.

## [0.2.0] - 2026-11-12

Wrap-up check of 2026-11-11: a first run in a clean home folder, on a fresh
clone, with 13 test files.

### Fixed

- `musaeus setup`: `~/MyVault` made a folder named `~` in the current
  directory; a relative path was accepted. The path is now expanded, and a
  relative one refused.
- `musaeus run` with no vault and no answer, and an abandoned
  `musaeus setup`, ended with exit code 0. Both now exit 1.
- `musaeus report`, `upgrade-check`, `spec-scout` and `canon-review` crashed
  with "No module named 'scripts'" when run from outside the checkout, or
  when another folder or package named `scripts` was found first.
- `hold_keep.txt` saved with a byte-order mark or a trailing space no longer
  loses its first (or that) name; an unmatched name is deleted after the
  30-day hold.

### Added

- `musaeus doctor` warns about rows set aside in REVIEW or QUARANTINE whose
  file is no longer on disk.
- `CHANGELOG.md`, `NOTES.md`, `docs/HOW_IT_WORKS.md`.

### Changed

- The version is `0.2.0` (`pyproject.toml`, `musaeus --version`, the man
  page, the Debian package name).
- `musaeus --help` says what `run` does: the whole pipeline, changing files
  at once. It said "Ingest→Sentinel→Scholar".
- Audit no longer ends "safe to snapshot and wipe the DB". The catalogue is
  never wiped and cannot be rebuilt.
- A missing API key points at `musaeus setup`, not at `settings.env`.
- README rewritten to match the program: install in a virtual environment,
  every setting, the real vault layout, the Acts, editions, the 30-day
  hold, known limitations.
- `docs/TESTING_ON_YOUR_OWN_FILES.md`: real folder names; the hold deletes
  after 30 days.

## 2026-11 (#269–#321)

- **30-day hold** (#286): REVIEW and QUARANTINE are no longer checked by
  hand; audio held 30 days is deleted and denied.
- **iPhone**: `musaeus iphone-sync` sized to the phone's free space (#310),
  in the menu (#314), by genre (#315), reconnects when the phone drops the
  link (#316); a real-phone check before iPhone changes merge (#318).
- **Lossless edition** writes its playlists (#317) and takes the lossy
  masters by default (#319). ALAC and AAC are written as Apple M4A (#283).
- **Keep rule**: mono beats a remix (#269); of two live recordings the
  earlier (#282); same song is one song whatever the brackets or guests
  (#313); MusicBrainz works find one song under two titles (#273–#277).
- **Windows port** (#289–#309, #311): one file lock for both systems,
  UTF-8 everywhere, path handling, a Windows CI job. Paused 2026-11-10.
- **Preflight** runs an ffmpeg self-test when the ffmpeg version changes
  (#305). The Debian package builds again (#281).
- `musaeus export itunes` writes the file the iTunes Companion reads (#321).
- Run problem lists judge machine failures by the decoder's words (#280).
  A duplicate group with no copy left is closed (#320).

## 2026-10 (#54–#268)

- **Whole-program review** in five slices (#143–#147, review only); every
  finding fixed (#148–#187), then a second audit (#196–#207).
- **Masters are never saved in place** (#148, #153): a copy beside the file,
  then a rename.
- **Protection**: monthly bit-rot check with repair from backup (#76),
  monthly verified music backup (#77), `doctor` checks for masters changed
  outside Musaeus, one act under two spellings, shared covers.
- **Editions on fdkaac** at full bandwidth; a copy that clicks is retried
  at a lower one (#54, #70). The stick transfer checks and repairs the
  filesystem first (#151).
- **Loudness**: ReplayGain is R128 + 5 dB (#73); measurements kept in the
  masters' tags.
- **Duplicates**: one keep rule for the resolver and the swap tool; a choice
  made by hand always stands (#239); `musaeus dedupe --sheet` (#222);
  classical is one copy per piece, filed by composer.
- **Names and genres**: MasterLaw is the genre authority (#171); one folder
  per act; person folders; `musaeus genre-suggest`; album fill from
  AcoustID, Discogs and Deezer with a MusicBrainz check (#223, #249).
- **Runs**: a resume skips only stages that finished in order (#166);
  secrets never shown by `musaeus setup` (#157); resets hold the masters
  lock (#158).
- Surround files are downmixed to stereo on arrival (#234).

## 2026-09 (#14–#53)

- **One file per track**: masters in `ALAC-Archival`, the row points at the
  master (#36). Finalize files by one shared rule (#34).
- **Act-by-act runs** with a problem list after each Act (#32).
- **Editions**: the lossless edition at −18 LUFS (#49–#52); car and iPhone
  on the same framework (#53).
- Damaged audio caught at intake (#42). An original always beats a levelled
  copy (#43–#45). AcoustID back in enrichment (#41, #46).
- The deny list: removed audio cannot return (#23–#25).
- `doctor` reads the deny list (#24); the review brief is committed (#18).

## 2026-07 and 2026-08 (#1–#13)

- First pipeline: ingest, sentinel, scholar, forge, tagger, curator.
- Phase 1 expansion and the ORPHEUS salvage (#6): the pipeline grows from
  six stages towards the Acts.
- `rebuild-db` disabled as unsafe; `rebuild-from-disk` added (#8, #10).
- A false duplicate cascade (11,160 files set aside) fixed and recovered
  (#12, #13).
