"""Review of slice C (#147), findings 1-4: Enrich and MBEnrich.

1. MBEnrich's release search sent artist:"<MBID>" (artist is the credited
   name; the id field is arid): no release was ever found. A failed release
   lookup still closed the row for good.
2. Enrich opened genre_allowed.txt / genre_map.tsv, not the vault's
   Genre_Allowed.txt / Genre_Canonical_Map.txt: no tag ever resolved.
3. Enrich's Last.fm request skipped the network gateway.
4. MBEnrich's connectivity probe skipped the gateway.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from musaeus import network_policy
from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.network_policy import NetworkPolicy
from musaeus.stages import enrich, mb_enrich


@pytest.fixture
def ctx(tmp_path: Path) -> RunContext:
    cfg = MusicConfig(
        vault_root=tmp_path,
        inbox=tmp_path / "INBOX",
        staging=tmp_path / "STAGING",
        quarantine=tmp_path / "QUARANTINE",
        runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData",
        alac_library=tmp_path / "Libraries" / "ALAC_Library",
        db_path=tmp_path / "musaeus.db",
    )
    cfg.ensure_dirs()
    return RunContext.new(cfg, open_db(cfg.db_path), dry_run=False)


def test_enrich_resolves_through_the_vaults_genre_files(ctx):
    (ctx.config.meta_dir / "Genre_Allowed.txt").write_text("Rock\n")
    (ctx.config.meta_dir / "Genre_Canonical_Map.txt").write_text("classic rock => Rock\n")
    upsert_archive(ctx.conn, {"file_path": "/m/a.m4a", "status": "CATALOGUED", "artist": "The Who"})
    ctx.conn.commit()
    object.__setattr__(ctx.config, "lastfm_api_key", "fake-key")
    with (
        patch.object(enrich, "_lastfm_top_tags", return_value=["classic rock", "rock"]),
        patch.object(enrich.time, "sleep"),
    ):
        enrich.EnrichStage()._enrich(ctx, dry_run=False)
    assert ctx.conn.execute("SELECT genre FROM archive").fetchone()[0] == "Rock"


def test_enrichs_lastfm_call_asks_the_gateway(monkeypatch):
    def no_transport(*a, **k):
        raise AssertionError("urlopen was called under LOCAL_ONLY")

    monkeypatch.setattr(enrich, "urlopen", no_transport)
    with (
        network_policy.policy(NetworkPolicy.LOCAL_ONLY) as gateway,
        pytest.raises(network_policy.NetworkDenied),
    ):
        enrich._lastfm_top_tags("The Who", "fake-key")
    assert gateway.denials


def test_the_release_search_asks_by_artist_id(monkeypatch):
    seen = {}

    def fake_get(kind, params):
        seen.update(params)
        return {"releases": []}

    monkeypatch.setattr(mb_enrich, "_mb_get", fake_get)
    mb_enrich._search_release("9fdaa16b-a6c4-4831-b87c-bc9ca8ce7eaa", "Who's Next")
    assert seen["query"].startswith("arid:9fdaa16b"), seen["query"]


def test_a_release_lookup_that_could_not_ask_is_not_none(monkeypatch):
    def down(kind, params):
        raise TimeoutError("timed out")

    monkeypatch.setattr(mb_enrich, "_mb_get", down)
    with pytest.raises(mb_enrich.LookupUnavailable):
        mb_enrich._search_release("9fdaa16b-a6c4-4831-b87c-bc9ca8ce7eaa", "Who's Next")


def test_mbenrichs_probe_asks_the_gateway(ctx, monkeypatch):
    calls = []
    monkeypatch.setattr(mb_enrich, "urlopen", lambda *a, **k: calls.append(a))
    with network_policy.policy(NetworkPolicy.LOCAL_ONLY) as gateway:
        mb_enrich.MBEnrichStage().run(ctx)
    assert not calls, "the probe went out under LOCAL_ONLY"
    assert any("musicbrainz.org" in d for d in gateway.denials)


# ── Pre-merge review of #170 ──────────────────────────────────────────────────


def test_a_query_musicbrainz_refuses_is_an_answer(monkeypatch):
    import urllib.error

    def refused(kind, params):
        raise urllib.error.HTTPError("u", 400, "Bad Request", {}, None)

    monkeypatch.setattr(mb_enrich, "_mb_get", refused)
    assert mb_enrich._search_release("9fdaa16b", 'Music From "Fiddler on the Roof"') is None


def test_an_outage_is_asked_once_per_album_per_run(ctx, monkeypatch):
    calls = []

    def down(artist_mbid, album):
        calls.append(album)
        raise mb_enrich.LookupUnavailable("timed out")

    for n in range(3):
        upsert_archive(
            ctx.conn,
            {"file_path": f"/m/{n}.m4a", "status": "CATALOGUED", "artist": "The Who",
             "album": "Who's Next", "title": f"T{n}"},
        )  # fmt: skip
    ctx.conn.commit()
    monkeypatch.setattr(mb_enrich, "urlopen", lambda *a, **k: None)
    monkeypatch.setattr(mb_enrich, "_search_artist", lambda *a, **k: ("mbid-who", "The Who"),
                        raising=False)  # fmt: skip
    monkeypatch.setattr(mb_enrich, "_search_release", down)
    monkeypatch.setattr(mb_enrich.time, "sleep", lambda s: None)
    with network_policy.policy(NetworkPolicy.ALLOWED):
        mb_enrich.MBEnrichStage().run(ctx)
    assert len(calls) == 1, calls
    open_rows = ctx.conn.execute(
        "SELECT COUNT(*) FROM archive WHERE mb_enriched_at IS NULL"
    ).fetchone()[0]
    assert open_rows == 3, "an outage closed the rows"


def test_an_album_name_with_a_quote_is_escaped(monkeypatch):
    seen = {}
    monkeypatch.setattr(mb_enrich, "_mb_get", lambda kind, params: seen.update(params) or {})
    mb_enrich._search_release("9fdaa16b", 'Music From "Fiddler on the Roof"')
    assert 'release:"Music From \\"Fiddler on the Roof\\""' in seen["query"], seen["query"]
