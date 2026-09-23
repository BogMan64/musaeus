"""`_choose` displays 1-based, returns 0-based.

Nothing tested this helper directly, and on 2026-09-10 it was changed from
0-based to 1-based display. Every caller indexes a list with the value it
returns, so the two halves of that conversion have to stay in agreement --
get it wrong and the console silently runs the menu item next to the one
the operator picked. That is not a crash, it is a wrong action taken
confidently, which is the worst shape of bug this project has.

The change itself is right: people count menus from 1. Grey confirmed it
stays on 2026-09-14. These tests exist so the NEXT person to touch it finds
out from a red test rather than from a mis-fired menu item.
"""

from __future__ import annotations

import pytest

from musaeus.console import _choose

LABELS = ["Lossless", "Car", "iPhone", "Back"]


class TestChooseIsOneBased:
    @pytest.mark.parametrize(
        "typed,expected_index",
        [("1", "0"), ("2", "1"), ("3", "2"), ("4", "3")],
    )
    def test_typed_number_maps_to_the_label_the_operator_saw(
        self, monkeypatch, capsys, typed, expected_index
    ) -> None:
        monkeypatch.setattr("builtins.input", lambda *a, **k: typed)
        got = _choose("Which edition", LABELS)
        assert got == expected_index

        # and the label that number sat next to on screen is the one the
        # returned index selects -- the actual contract, not just the arithmetic
        out = capsys.readouterr().out
        assert f"{typed}  {LABELS[int(got)]}" in out

    def test_every_label_is_displayed_with_a_reachable_number(self, monkeypatch, capsys) -> None:
        """No label may be shown as 0, and none may be unreachable."""
        monkeypatch.setattr("builtins.input", lambda *a, **k: "1")
        _choose("Which edition", LABELS)
        out = capsys.readouterr().out
        for i, label in enumerate(LABELS):
            assert f"{i + 1}  {label}" in out
        assert f"0  {LABELS[0]}" not in out

    def test_the_range_hint_matches_the_numbers_shown(self, monkeypatch) -> None:
        """The hint is part of input()'s prompt, not printed, so it is read
        from the call rather than from captured stdout. It said [0-3] while
        the menu listed 1..4 during the 09-10 change -- the operator was being
        told to type a number that no longer selected anything."""
        seen: list[str] = []

        def _capture(prompt: str = "") -> str:
            seen.append(prompt)
            return "1"

        monkeypatch.setattr("builtins.input", _capture)
        _choose("Which edition", LABELS)
        assert seen, "_choose must prompt"
        assert f"[1-{len(LABELS)}]" in seen[0]
        assert "[0-" not in seen[0]

    def test_blank_returns_the_default_untouched(self, monkeypatch) -> None:
        """The default is already a 0-based index -- it must NOT be decremented."""
        monkeypatch.setattr("builtins.input", lambda *a, **k: "")
        assert _choose("Which edition", LABELS, default="0") == "0"
        assert _choose("Which edition", LABELS, default="2") == "2"

    def test_eof_returns_the_default(self, monkeypatch) -> None:
        def _eof(*a, **k):
            raise EOFError

        monkeypatch.setattr("builtins.input", _eof)
        assert _choose("Which edition", LABELS, default="0") == "0"

    @pytest.mark.parametrize("typed", ["0", "5", "-1", "99"])
    def test_out_of_range_never_returns_an_index(self, monkeypatch, typed) -> None:
        """The dangerous one.

        Callers index a list with this result, and Python indexes backwards
        from a negative number instead of raising. A typed "0" became -1,
        `["alac", "car"][-1]` selected "car", and _usb_menu walked on into a
        flow that WIPES A DEVICE -- on input the operator never meant as a
        selection. An out-of-range reply must come back as something
        `int()` refuses, so the caller's ValueError path returns instead.
        """
        monkeypatch.setattr("builtins.input", lambda *a, **k: typed)
        got = _choose("Which library", LABELS)
        with pytest.raises(ValueError):
            int(got)

    def test_non_numeric_is_passed_through_not_guessed(self, monkeypatch) -> None:
        """A caller int()s the result and returns on ValueError. Passing the
        raw string through keeps that path reachable; guessing an index would
        turn a typo into an action."""
        monkeypatch.setattr("builtins.input", lambda *a, **k: "loads")
        assert _choose("Which edition", LABELS) == "loads"
