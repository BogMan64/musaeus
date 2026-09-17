#!/usr/bin/env python3
"""Walk through copying the iPhone edition onto the phone, and check it can work.

Grey, 2026-09-17: "can musaeus walk the enduser through it? even if that's
just a note saying cut'n'paste this into terminal."

So this PRINTS the commands rather than running them, the same contract the
console's Misc. Options menu already uses. Nothing here touches the phone.

WHY IT IS NOT AUTOMATED

Three of the steps need a human at the handset:

    idevicepair pair    the phone shows a Trust dialog that must be tapped,
                        and it only appears while the phone is UNLOCKED
    ifuse               mounts the app's Documents container; it fails with
                        a bare "No such file or directory" if VLC is not
                        installed, which reads like a bug and is not
    rm -rf ~/iphone/*   optional, and deleting what is on someone's phone is
                        not a thing to do because a script felt like it

What CAN be checked from here is every reason the paste would fail, which is
most of the value: a missing tool, a phone that is not plugged in, a pairing
that has lapsed. Those are checked and reported before the commands are
shown.

    python3 scripts/iphone_transfer.py            # check, then print the steps
    python3 scripts/iphone_transfer.py --wipe     # include the clear-first line
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from musaeus.config import MusicConfig  # noqa: E402

#: The app whose Documents folder the music lands in. VLC reads a folder tree
#: and plays m4a natively; iOS sandboxing means the built-in Music app can
#: never see these files, which is the single most common surprise here.
APP_ID = "org.videolan.vlc-ios"
APP_NAME = "VLC for iOS"

TOOLS = (
    ("idevicepair", "pairing with the phone", "libimobiledevice-utils"),
    ("ifuse", "mounting the app's Documents folder", "ifuse"),
    ("rsync", "copying without re-sending what is already there", "rsync"),
)


def _run(cmd: list[str], timeout: int = 10) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except Exception as exc:
        return 1, f"{type(exc).__name__}: {exc}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--wipe", action="store_true",
                    help="include the line that clears the phone folder first")
    ap.add_argument("--mount", default=str(Path.home() / "iphone"),
                    help="where to mount (default: ~/iphone)")
    args = ap.parse_args()

    cfg = MusicConfig.from_env()
    src = Path(cfg.iphone_library)
    mount = Path(args.mount)

    print("iPhone transfer\n" + "=" * 62)

    # ── what is being copied ────────────────────────────────────────────────
    if not src.is_dir():
        print(f"\n  The iPhone edition does not exist yet at {src}.")
        print("  Build it first:")
        print("     python3 scripts/car_library/build_car_library.py \\")
        print("             --edition iphone --source car --budget-gb 45")
        return 1
    files = list(src.rglob("*.m4a"))
    size = sum(f.stat().st_size for f in files)
    print(f"\n  to copy : {len(files):,} tracks, {size / 1e9:.1f} GB")
    print(f"  from    : {src}")
    print(f"  into    : {APP_NAME}'s Documents folder")

    # ── can it work at all? ─────────────────────────────────────────────────
    print("\n  Checks")
    blockers: list[str] = []
    for tool, why, pkg in TOOLS:
        if shutil.which(tool):
            print(f"     ok    {tool} ({why})")
        else:
            print(f"     MISSING {tool} -- {why}")
            print(f"             sudo apt install {pkg}")
            blockers.append(tool)

    rc, out = _run(["idevice_id", "-l"])
    udid = out.splitlines()[0].strip() if rc == 0 and out.strip() else ""
    if udid:
        print(f"     ok    a phone is attached ({udid[:12]}...)")
    else:
        print("     NOT ATTACHED -- plug the phone in with a data cable and unlock it")
        blockers.append("device")

    if udid and shutil.which("idevicepair"):
        rc, out = _run(["idevicepair", "validate"])
        if rc == 0:
            print("     ok    already paired")
        else:
            print("     NOT PAIRED YET -- the first command below fixes this;")
            print("           unlock the phone and tap Trust when it asks")

    if blockers:
        print(f"\n  {len(blockers)} thing(s) to sort out first. The steps are still below,")
        print("  so you can come back to them.")

    # ── the paste ───────────────────────────────────────────────────────────
    print("\n  Copy and paste this into a terminal:\n")
    print("  " + "-" * 58)
    lines = [
        "idevicepair pair",
        f"mkdir -p {mount}",
        f"ifuse --documents {APP_ID} {mount}",
    ]
    if args.wipe:
        lines.append(f"rm -rf {mount}/*")
    lines += [
        f"rsync -a --info=progress2 '{src}/' '{mount}/'",
        f"fusermount -u {mount}",
    ]
    for ln in lines:
        print(f"    {ln}")
    print("  " + "-" * 58)

    print(f"""
  Notes

    * {APP_NAME} must already be installed on the phone. Without it the
      ifuse line fails with "No such file or directory", which looks like a
      broken command and is really a missing app.

    * The iOS Music app will NOT see these tracks. Every iOS app has its own
      sandbox, so the files live inside {APP_NAME} and play there. That is
      normal, not a mistake in the copy.

    * rsync skips what is already on the phone, so it is safe to stop it and
      run it again -- it picks up where it left off rather than starting
      over. {size / 1e9:.0f} GB over USB is not quick.

    * --wipe adds a line that clears the folder first. Use it when you want
      to know exactly what is on the phone; leave it off to add to what is
      already there.
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
