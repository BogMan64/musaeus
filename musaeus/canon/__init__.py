"""
MUSAEUS — Canon subsystem.

Provides ArtistCanon and GenreCanon for resolving raw metadata strings,
and GenreLaw for the artist->genre authority (MasterLaw.csv)
to their normalised canonical forms.
"""

from .artist import ArtistCanon
from .genre import GenreCanon
from .genre_law import GenreLaw

__all__ = ["ArtistCanon", "GenreCanon", "GenreLaw"]
