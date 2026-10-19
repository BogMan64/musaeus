# MUSAEUS 0.1.0-1 for Debian

This release adds an architecture-independent `.deb` for Debian 12.
It installs the MUSAEUS command-line application and the helpers used by its
CLI. It does not start a service, create a vault, copy music, install API
credentials, or include workstation-specific standalone utilities.

## Install

Download `musaeus_0.1.0-1_all.deb` from this release and run:

```sh
sudo apt install ./musaeus_0.1.0-1_all.deb
musaeus setup
musaeus run --dry-run
```

Choose a new vault during setup. A real `musaeus run` can move and retag music;
keep a backup and review the preview before running it. The optional BPM
command needs Essentia. Fingerprinting needs `fpcalc`, supplied by Debian's
`libchromaprint-tools` package.
Automatic bit-rot repair needs user-supplied backup locations in
`MUSAEUS_BACKUP_ROOTS`; no machine-specific backup paths are built in.

The optional P0 `--safety-gate` remains a fixture-only rehearsal gate. It
classifies a configured vault as real data and refuses a real run. It is not a
replacement for a backup or for reviewing a dry run.
