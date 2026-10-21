#!/bin/sh
# Purpose: Build a local Debian package for the MUSAEUS CLI.
# Inputs: Source checkout, DEB_MAINTAINER_NAME, DEB_MAINTAINER_EMAIL, optional output directory.
# Outputs: Architecture-independent .deb and build metadata.
# Modifies files: Writes only to the selected output directory and a temporary staging directory.
set -eu

project_dir=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
output_dir=${1:-"$project_dir/dist"}
mkdir -p "$output_dir"
output_dir=$(CDPATH='' cd -- "$output_dir" && pwd)

: "${DEB_MAINTAINER_NAME:?Set DEB_MAINTAINER_NAME for package metadata}"
: "${DEB_MAINTAINER_EMAIL:?Set DEB_MAINTAINER_EMAIL for package metadata}"
python3 - "$DEB_MAINTAINER_NAME" "$DEB_MAINTAINER_EMAIL" <<'PY'
import os
import re
import sys

name, email = sys.argv[1:]
if not name.strip() or any(char in name for char in "\r\n<>"):
    raise SystemExit("DEB_MAINTAINER_NAME must be a single plain-text line")
if not re.fullmatch(r"[^\s<>@]+@[^\s<>@]+", email):
    raise SystemExit("DEB_MAINTAINER_EMAIL must be one email address")
if email.endswith(".invalid") and os.environ.get("DEB_LOCAL_BUILD") != "1":
    raise SystemExit("Replace the placeholder email for a release build")
PY

version=$(python3 - "$project_dir/pyproject.toml" <<'PY'
import sys
import tomllib
from pathlib import Path

print(tomllib.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))["project"]["version"])
PY
)
package_version="${version}-1"
if [ "${DEB_LOCAL_BUILD:-0}" = 1 ]; then
  package_version="${package_version}~local1"
fi
stage=$(mktemp -d "${TMPDIR:-/tmp}/musaeus-deb.XXXXXX")
trap 'rm -rf "$stage"' EXIT HUP INT TERM
root="$stage/root"

install -d "$root/usr/lib/musaeus/scripts" "$root/usr/bin" "$root/usr/share/doc/musaeus" "$root/usr/share/man/man1" "$root/DEBIAN"
cp -a "$project_dir/musaeus" "$root/usr/lib/musaeus/"
for helper in convergence_report musaeus_canon_review musaeus_notify musaeus_report musaeus_spec_scout musaeus_upgrade_check; do
  install -m 0644 "$project_dir/scripts/$helper.py" "$root/usr/lib/musaeus/scripts/$helper.py"
done
cp "$project_dir/LICENSE" "$root/usr/share/doc/musaeus/copyright"
install -m 0644 "$project_dir/packaging/musaeus.1" "$root/usr/share/man/man1/musaeus.1"
gzip -9n "$root/usr/share/man/man1/musaeus.1"
cat > "$root/usr/share/doc/musaeus/changelog.Debian" <<CHANGELOG
musaeus ($package_version) unstable; urgency=medium

  * Initial Debian package for the MUSAEUS command-line application.

 -- $DEB_MAINTAINER_NAME <$DEB_MAINTAINER_EMAIL>  $(date -R)
CHANGELOG
gzip -9n "$root/usr/share/doc/musaeus/changelog.Debian"
find "$root/usr/lib/musaeus" -type d -name tests -prune -exec rm -rf {} +
find "$root/usr/lib/musaeus" -type d -name __pycache__ -prune -exec rm -rf {} +
find "$root/usr/lib/musaeus" -type f -name '*.pyc' -delete
if grep -R -nE '/(home|media)/grey|/mnt/(FORGE2TB|NUC8TB_BACKUP)' "$root/usr/lib/musaeus"; then
  echo 'Refusing to package workstation-specific paths' >&2
  exit 1
fi

cat > "$root/usr/bin/musaeus" <<'SH'
#!/bin/sh
PYTHONPATH="/usr/lib/musaeus${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH
exec /usr/bin/python3 -m musaeus "$@"
SH
chmod 755 "$root/usr/bin/musaeus"

cat > "$root/usr/share/doc/musaeus/README.Debian" <<'DOC'
MUSAEUS Debian package
======================

This package installs the MUSAEUS application and the helpers used by its CLI.
Other standalone scripts from the source checkout are not included. It does not create a vault, copy music,
create a database, start the pipeline, or install API credentials. On first
use, MUSAEUS asks the user to configure their own vault under their account.

Start with:
  musaeus setup
  musaeus preflight
  musaeus run --dry-run

Review the project safety documentation before running any command that writes
to a music library. Optional integrations require user-supplied credentials.
The BPM command requires the optional Essentia Python package.
The car and iPhone editions encode with fdkaac (Debian non-free);
"musaeus preflight" reports it when it is missing.
"musaeus run" can move and retag music. Keep a backup of source files.
Set MUSAEUS_BACKUP_ROOTS to your backup folders before using automatic
bit-rot repair; no backup locations are chosen by the package.
DOC

cat > "$root/DEBIAN/control" <<CONTROL
Package: musaeus
Version: $package_version
Section: sound
Priority: optional
Architecture: all
Maintainer: $DEB_MAINTAINER_NAME <$DEB_MAINTAINER_EMAIL>
Depends: python3 (>= 3.10), python3-mutagen (>= 1.46), ffmpeg
Recommends: libchromaprint-tools, git, procps, curl
Suggests: fdkaac
Description: music library management pipeline
 MUSAEUS ingests and catalogs audio files, measures loudness, manages tags,
 and builds selected library exports. It is a command-line application.
 .
 The package does not include music, a vault database, personal settings,
 workstation-specific utility scripts, or API credentials. First-run setup
 is per-user.
CONTROL

dpkg-deb --build --root-owner-group "$root" "$output_dir/musaeus_${package_version}_all.deb"
