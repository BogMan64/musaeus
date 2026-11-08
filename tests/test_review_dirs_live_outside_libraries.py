"""A review queue must never resolve inside Libraries/.

Libraries/ is machine-managed and wipeable -- it was wiped on 2026-09-18.
The review queues hold files awaiting Grey's judgement and CANNOT be rebuilt
from Curated.RAW.Files, so they live outside it.

    Anything MUSAEUS can rebuild lives in Libraries/.
    Anything it cannot lives outside it.

TuneMyMusic.csv is the precedent: it sat in Libraries/ALAC-Archival/ and the
wipe took it. 305 rows came back off the NUC backup by luck, not design.
"""

from pathlib import Path

import pytest

from musaeus.config import MusicConfig


@pytest.fixture
def cfg(tmp_path) -> MusicConfig:
    return MusicConfig(
        vault_root=tmp_path,
        inbox=tmp_path / "INBOX",
        staging=tmp_path / "STAGING",
        quarantine=tmp_path / "QUARANTINE",
        runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData",
        alac_library=tmp_path / "ALAC-Library",
        db_path=tmp_path / "musaeus.db",
    )


@pytest.mark.parametrize("attr", ["dupes_review_dir", "tribute_review_dir"])
def test_review_dir_is_not_inside_libraries(cfg, attr):
    review = getattr(cfg, attr)
    for wipeable in (cfg.libraries, cfg.alac_archive, cfg.alac_library):
        with pytest.raises(ValueError):
            review.relative_to(wipeable)


@pytest.mark.parametrize("attr", ["dupes_review_dir", "tribute_review_dir"])
def test_review_dir_is_inside_the_vault(cfg, attr):
    # outside Libraries/, but still in the vault -- not loose on a Desktop.
    getattr(cfg, attr).relative_to(cfg.vault_root)


def test_doctor_excludes_review_dirs_without_hardcoding_their_names():
    """The exclusion must follow the folders if they move again.

    Checks the whole exclusion path, not one expression: the config lookup
    lives in the _under_any() helper, so asserting only against the orphan
    comprehension would pass a name-based implementation hidden one call away.
    """
    src = (Path(__file__).resolve().parent.parent / "musaeus" / "doctor.py").read_text()

    body = src.split("orphans = [", 1)[1].split("]", 1)[0]
    assert "DUPES_MOVED_FOR_REVIEW" not in body, "name-based exclusion reintroduced"
    assert "_under_any" in body, "orphan scan no longer routes through the helper"

    helper = src.split("def _under_any(", 1)[1].split("\ndef ", 1)[0]
    assert "DUPES_MOVED_FOR_REVIEW" not in helper, "helper hardcodes the old name"
    assert "dupes_review_dir" in helper and "tribute_review_dir" in helper
