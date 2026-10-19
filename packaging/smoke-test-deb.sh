#!/bin/sh
# Exercise an extracted package with an isolated HOME and disposable vault.
set -eu

package=${1:?Pass the .deb to test}
stage=$(mktemp -d "${TMPDIR:-/tmp}/musaeus-deb-smoke.XXXXXX")
trap 'rm -rf "$stage"' EXIT HUP INT TERM
mkdir -p "$stage/root" "$stage/home"
dpkg-deb --extract "$package" "$stage/root"

export HOME="$stage/home"
export MUSAEUS_VAULT_ROOT="$stage/new-user-vault"
export PYTHONPATH="$stage/root/usr/lib/musaeus"

python3 -m musaeus --help > "$stage/help.txt"
python3 -m musaeus --version > "$stage/version.txt"
python3 -m musaeus run --dry-run > "$stage/preview.txt"
test ! -e "$MUSAEUS_VAULT_ROOT"

if python3 -m musaeus run --safety-gate > "$stage/gated.txt" 2>&1; then
  echo "A gated run unexpectedly proceeded" >&2
  exit 1
else
  result=$?
  test "$result" -eq 2 || { cat "$stage/gated.txt" >&2; exit 1; }
fi
grep -q 'classification : canonical' "$stage/gated.txt"
test ! -e "$MUSAEUS_VAULT_ROOT"
printf 'Package smoke test passed: %s\n' "$package"
