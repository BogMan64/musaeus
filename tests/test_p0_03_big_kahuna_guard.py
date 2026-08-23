"""
P0-03: Big Kahuna is refused when no export root is resolved.

Big Kahuna's nineteenth stage is Curator, which writes an export tree.
Without a root, the pipeline used to be constructed and run, discovering
the problem only once Ingest, Forge and Tagger had already mutated the
library -- an expensive way to learn a flag was missing.

Two things the guard deliberately does NOT do, both worse than refusing:
invent a default root (that is how an export lands somewhere nobody
chose), or create the directory (a guard that makes state is not a
guard). The full typed configuration flow is P0-15.
"""

from __future__ import annotations

from pathlib import Path

from musaeus.cli import check_big_kahuna_export_root as check


class TestBigKahunaGuard:
    def test_no_root_is_refused(self):
        assert check(None) is not None
        assert check("") is not None

    def test_a_resolved_root_proceeds(self):
        assert check("/mnt/USB/export") is None
        assert check(Path("/mnt/USB/export")) is None

    def test_the_refusal_says_what_to_do(self):
        """A refusal the user cannot act on is only half a refusal."""
        assert "--export-root" in check(None)

    def test_the_refusal_explains_why(self):
        msg = check(None).lower()
        assert "curator" in msg and "export tree" in msg

    def test_it_does_not_invent_a_default(self):
        """The message must not name a path it chose on the user's behalf."""
        msg = check(None)
        assert "/path/to/target" in msg  # a placeholder, not a real location
        assert "defaulting to" not in msg.lower()

    def test_it_creates_nothing(self, tmp_path):
        """A guard that makes state is not a guard."""
        before = list(tmp_path.iterdir())
        check(None)
        check(str(tmp_path / "nonexistent"))
        assert list(tmp_path.iterdir()) == before
