================================================================
  THIS DRIVE IS A BACKUP OF GREY'S MUSIC LIBRARY
  Written 2026-09-21
================================================================

WHAT IS HERE
    A complete copy of the ALAC music library, filed as
    Genre / Artist / Album / Track.m4a

    These are lossless ALAC files. They play in iTunes, Apple
    Music, VLC, foobar2000 and most other players.

WHY IT IS DISCONNECTED
    On purpose. A drive in a drawer cannot be hit by an
    accidental delete, a bad script, or ransomware. That is the
    whole point of it. Leave it unplugged.

ONCE A YEAR, PLEASE
    Plug it in and run:

        python3 verify_backup.py verify .

    from this folder. It reads every file and tells you whether
    the bytes are still the bytes. Takes a while -- it is
    reading the whole drive -- and needs nothing but Python 3.

    Two things are being checked at once:
      1. that the drive still spins up at all
      2. that nothing has quietly rotted

    A drive left unpowered for years can fail on the first spin.
    Finding that out on a quiet Sunday is much better than
    finding it out when you need the files.

IF IT REPORTS PROBLEMS
    "CHANGED" means the BACKUP is damaged, not the original.
    Re-copy those files from the live library. Do not overwrite
    anything until you have found a good copy.

WHERE THE OTHER COPIES ARE (as of 2026-09-21)
    - The live library: FORGE2TB, /Projects/MUSAEUS_VAULT
    - The raw source files it was all built from: NUC 8TB
      If this drive AND the live library were both lost, the
      library can be rebuilt from those raw files.

    Three copies. Two off the working drive. One offline --
    this one.

BACKUP_MANIFEST.sha256
    The list of what every file should be. Keep it with the
    backup. Without it, verify has nothing to compare against.
================================================================
