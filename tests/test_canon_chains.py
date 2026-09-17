"""A canon entry must never point at a name that is itself a canon key.

resolve_exact does NOT follow a chain. So if the file already says
`X -> old` and you then add `old -> new`, X lands on `old` and stops --
half-way, at a name nothing else uses. doctor's "authorities agree" check
calls that a FAIL, and it is right to.

It happened for real on 2026-09-16, the first time consolidate_artist_
folders.py ran. Merging ELO into Electric Light Orchestra turned a
pre-existing "Jeff Lynne's ELO" -> "ELO" into a chain and doctor went red on
the next run. Nothing was mis-filed -- both names held zero tracks -- but the
ruling file was inconsistent, and the next artist arriving under that name
would have landed on a dead middle name.

A chain can be created from either end, so both are tested.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from consolidate_artist_folders import _write_canon_entry  # noqa: E402


@pytest.fixture
def canon(tmp_path) -> Path:
    p = tmp_path / "artist_canon.tsv"
    p.write_text("# canon\nAlias A\tTarget One\n", encoding="utf-8")
    return p


def rows(p: Path) -> dict[str, str]:
    out = {}
    for ln in p.read_text(encoding="utf-8").splitlines():
        if ln.strip() and not ln.startswith("#") and "\t" in ln:
            k, v = ln.split("\t", 1)
            out[k.strip()] = v.strip()
    return out


class TestTheOrdinaryCase:
    def test_a_new_entry_is_appended(self, canon):
        _write_canon_entry(canon, "Old Name", "New Name")
        assert rows(canon)["Old Name"] == "New Name"

    def test_existing_entries_survive(self, canon):
        _write_canon_entry(canon, "Old Name", "New Name")
        assert rows(canon)["Alias A"] == "Target One"

    def test_an_entry_that_already_exists_is_not_duplicated(self, canon):
        _write_canon_entry(canon, "Alias A", "Target One")
        text = canon.read_text(encoding="utf-8")
        assert text.count("Alias A\t") == 1

    def test_the_comment_header_is_kept(self, canon):
        _write_canon_entry(canon, "Old", "New")
        assert canon.read_text(encoding="utf-8").startswith("# canon")


class TestAChainFromTheTargetSide:
    """`X -> old` already exists, and we now add `old -> new`."""

    def test_the_existing_entry_is_repointed(self, canon):
        canon.write_text("Jeff Lynne's ELO\tELO\n", encoding="utf-8")
        n = _write_canon_entry(canon, "ELO", "Electric Light Orchestra")
        r = rows(canon)
        assert n == 1
        assert r["Jeff Lynne's ELO"] == "Electric Light Orchestra", \
            "it would land on 'ELO' and stop"
        assert r["ELO"] == "Electric Light Orchestra"

    def test_several_are_repointed(self, canon):
        canon.write_text("A\tELO\nB\tELO\nC\tOther\n", encoding="utf-8")
        n = _write_canon_entry(canon, "ELO", "Electric Light Orchestra")
        r = rows(canon)
        assert n == 2
        assert r["A"] == r["B"] == "Electric Light Orchestra"
        assert r["C"] == "Other", "an unrelated entry must not move"

    def test_no_chain_remains(self, canon):
        canon.write_text("Jeff Lynne's ELO\tELO\n", encoding="utf-8")
        _write_canon_entry(canon, "ELO", "Electric Light Orchestra")
        r = rows(canon)
        assert not (set(r.values()) & set(r.keys()) - {"Electric Light Orchestra"}) or \
            all(r.get(v, v) == v for v in r.values()), "a canonical is still a key"


class TestAChainFromTheSourceSide:
    """`new` is itself already a key -- the same chain, built the other way."""

    def test_it_writes_the_final_destination_instead(self, canon):
        canon.write_text("ELO\tElectric Light Orchestra\n", encoding="utf-8")
        _write_canon_entry(canon, "Jeff Lynne's ELO", "ELO")
        r = rows(canon)
        assert r["Jeff Lynne's ELO"] == "Electric Light Orchestra", \
            "writing -> 'ELO' would have created the chain"

    def test_the_intermediate_entry_is_left_alone(self, canon):
        canon.write_text("ELO\tElectric Light Orchestra\n", encoding="utf-8")
        _write_canon_entry(canon, "Jeff Lynne's ELO", "ELO")
        assert rows(canon)["ELO"] == "Electric Light Orchestra"


class TestTheFileStaysReadable:
    def test_it_ends_with_a_newline(self, canon):
        """Appending to a file with no trailing newline is how two ids became
        one on 2026-09-16 -- 13516 and 8377 became 135168377."""
        canon.write_text("A\tB", encoding="utf-8")       # no trailing newline
        _write_canon_entry(canon, "C", "D")
        text = canon.read_text(encoding="utf-8")
        assert text.endswith("\n")
        assert rows(canon) == {"A": "B", "C": "D"}
