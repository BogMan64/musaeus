"""MUSAEUS — safety primitives (P0-10 onward).

Import-time side effects are forbidden here: importing a safety module
must never acquire a lock, create a directory, or probe a path.
"""
