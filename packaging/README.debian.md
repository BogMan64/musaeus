# Debian package

`build-deb.sh` builds an architecture-independent MUSAEUS package for Debian
12. The package contains the CLI and its required helper scripts.
Workstation-specific standalone utilities, music, vault databases, personal
settings, API credentials, and background services are excluded. Installation
does not run MUSAEUS or create a vault.

## Build

Use a real maintainer name and email for a package intended for GitHub:

```sh
DEB_MAINTAINER_NAME='MUSAEUS Maintainers' \
DEB_MAINTAINER_EMAIL='maintainer@example.org' \
  ./packaging/build-deb.sh
```

The script writes to `dist/` and a temporary staging directory. It does not
install the package. For an isolated local test with placeholder metadata, set
`DEB_LOCAL_BUILD=1`; that adds `~local1` to the package version so the file
cannot be mistaken for a release build.

The package depends on Debian's Python 3, `python3-mutagen` (Debian 12 ships
1.46), and `ffmpeg`. `libchromaprint-tools` supplies the optional `fpcalc`
command. The BPM command additionally requires Essentia, which is not bundled.
Automatic bit-rot repair has no backup source until the user sets
`MUSAEUS_BACKUP_ROOTS` to a colon-separated list of their backup folders.

## First use

Install a released `.deb` with `sudo apt install ./musaeus_VERSION_all.deb`.
Then run `musaeus setup` as your normal user, choose your own vault, and review
`musaeus run --dry-run` before a real run. `musaeus run` can move and retag
music, so keep a backup of the source files.

## Release checks

Before uploading a package to GitHub Releases:

1. Verify package metadata and contents with `dpkg-deb --info` and
   `dpkg-deb --contents`.
2. Extract it into a temporary directory and run `--help`, `--version`, and
   `run --dry-run` with an isolated `HOME` and disposable vault path.
3. Check that a gated real run against a new vault is refused without creating
   a directory or lock file.
4. Run the safety and packaging tests, and record the artifact's SHA-256 hash.

Do not test against an existing music vault when preparing a release.
