"""A running process must not be able to load a module edited after it started.

Python caches modules in ``sys.modules``, so a *deferred* import inside a
function body resolves the first time that function is called — possibly hours
into a run — and binds fresh code against startup-era dependencies. That is a
mixture of two versions, and on 2026-09-05 it lost a handoff document: a
42-hour-old process loaded new ``handoff`` code against a stale
``musaeus.context``.

Pinning every module at startup removes the mechanism. These tests assert the
pin is real, that it covers a non-trivial share of the package, that it is
wired into the *unskippable* startup path, and that it does not fall into the
one trap the technique has.
"""

from __future__ import annotations

import inspect
import pkgutil
import sys

import musaeus
from musaeus.cli import _pin_modules, main


def _package_module_names() -> set[str]:
    return {mi.name for mi in pkgutil.walk_packages(musaeus.__path__, "musaeus.")}


class TestThePinItself:
    def test_it_imports_the_package_and_reports_no_failures(self):
        """Coverage, not just a green tick.

        A pin that imported two modules would pass a bare "did not raise"
        assertion while leaving the hazard entirely in place, so the count is
        asserted against the real package size rather than a constant that
        would rot. Measured 2026-09-05: 97 modules, 0.15 s, zero failures.
        """
        imported, failed = _pin_modules()

        assert failed == [], f"modules failed to import: {failed}"
        # Every module in the package except __main__, which is deliberately skipped.
        assert imported == len(_package_module_names()) - 1
        assert imported > 50, "suspiciously few modules — is walk_packages reaching the package?"

    def test_every_module_is_resident_afterwards(self):
        """The actual property: nothing is left to be read off disk later."""
        _pin_modules()

        missing = sorted(
            name
            for name in _package_module_names()
            if name.rsplit(".", 1)[-1] != "__main__" and name not in sys.modules
        )
        assert missing == [], f"still unpinned, so still re-readable mid-run: {missing}"

    def test_it_does_not_import_dunder_main(self):
        """Importing musaeus.__main__ RUNS the CLI — it opens the interactive
        console and blocks forever. Verified, not guessed. This is why the pin
        skips it, and why the failure handler reports instead of aborting:
        __main__ proves a module here can carry import-time side effects."""
        _pin_modules()

        assert "musaeus.__main__" not in sys.modules

    def test_a_failing_module_is_reported_not_raised(self, monkeypatch):
        """One broken module must not take down every run of every command.

        A pin that can refuse to start is worse than the staleness it prevents.
        """
        import importlib

        real = importlib.import_module

        def explode(name, *a, **kw):
            if name.endswith(".config"):
                raise RuntimeError("boom")
            return real(name, *a, **kw)

        monkeypatch.setattr(importlib, "import_module", explode)
        imported, failed = _pin_modules()

        assert imported > 50, "the rest of the package must still be pinned"
        assert any(name.endswith(".config") for name, _ in failed)
        assert all("RuntimeError: boom" in err for _, err in failed)


class TestWhereItIsWired:
    def test_main_pins_before_dispatching_a_command(self):
        """It has to be in the startup path, not in a stage.

        PreflightStage is skippable (`musaeus run --skip ...`), and a partial
        run is exactly when somebody is most likely to be mid-edit. A guard
        living inside a skippable stage is absent precisely when it is needed.
        """
        src = inspect.getsource(main)

        assert "_pin_modules()" in src
        # Before the command dispatch, not after it.
        assert src.index("_pin_modules()") < src.index('command == "run"')

    def test_the_pin_is_not_delegated_to_preflight(self):
        from musaeus.stages import preflight

        assert "_pin_modules" not in inspect.getsource(preflight)
