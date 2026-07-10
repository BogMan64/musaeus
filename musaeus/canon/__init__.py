"""
MUSAEUS — Canon subsystem.

Provides ArtistCanon and GenreCanon for resolving raw metadata strings
to their normalised canonical forms.
"""

from .artist import ArtistCanon
from .genre import GenreCanon

__all__ = ["ArtistCanon", "GenreCanon"]
