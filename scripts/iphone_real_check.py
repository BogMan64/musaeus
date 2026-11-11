#!/usr/bin/env python3
"""Check the iPhone copy against the REAL phone before an iPhone change is merged.

2026-11-09: every file failed on the phone ("Function not implemented (38)":
rsync -a chmods, ifuse does not) while every test passed, because the tests
only ever copied into plain folders. This runs this checkout's own mount and
copy code against the phone that is plugged in:

  1. exactly one phone, mounted the way `musaeus iphone-sync` mounts it;
  2. one small song from the iPhone edition copied with its rsync into a
     folder of its own, `_musaeus_check`, and the size compared;
  3. that folder removed again, and the phone unmounted.

Nothing else on the phone is touched. If `_musaeus_check` is already there
(a check that was cut short), it stops and asks for it to be removed by hand.

    python3 scripts/iphone_real_check.py      # phone plugged in and unlocked

Not covered: the copy of picked genres (--files-from), the reconnect, and
the clearing of VLC.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from musaeus import iphone_sync as s  # noqa: E402
from musaeus.config import MusicConfig  # noqa: E402

CHECK_DIR = "_musaeus_check"


def check(edition: Path, mount: Path) -> int:
    phones = s._phones()
    if len(phones) != 1:
        print(f"  Need exactly one phone plugged in and unlocked; found {len(phones)}.")
        return 1
    songs = sorted(edition.rglob("*.m4a"), key=lambda p: p.stat().st_size)
    if not songs:
        print(f"  No song in the iPhone edition at {edition}.")
        return 1
    song = songs[0]
    if not s._mount(mount, phones[0]):
        return 1
    try:
        target = mount / CHECK_DIR
        if target.exists():
            print(f"  {CHECK_DIR} is already on the phone (a check cut short?). Remove it in VLC.")
            return 1
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp)
            (src / CHECK_DIR).mkdir()
            shutil.copy2(song, src / CHECK_DIR / song.name)
            rc = s._rsync(src, mount)
        copied = target / song.name
        ok = rc == 0 and copied.is_file() and copied.stat().st_size == song.stat().st_size
        shutil.rmtree(target, ignore_errors=True)
        if target.exists():
            print(f"  Could not remove {CHECK_DIR} from the phone. Remove it in VLC.")
            return 1
    finally:
        s._unmount(mount)
    if not ok:
        print(f"  FAILED: rsync rc={rc}; the song did not arrive whole.")
        return 1
    print(f"  Real phone check passed: copied and removed {song.name}.")
    return 0


def main() -> int:
    # Not TemporaryDirectory: its cleanup deletes what is inside, which would
    # be VLC on the phone if the unmount failed. rmdir only takes an empty folder.
    mount = Path(tempfile.mkdtemp(prefix="musaeus_phone_"))
    try:
        return check(MusicConfig.from_env().iphone_library, mount)
    finally:
        try:
            mount.rmdir()
        except OSError:
            print(f"  Left {mount} in place (still mounted?). Run:  fusermount -u {mount}")


if __name__ == "__main__":
    sys.exit(main())
