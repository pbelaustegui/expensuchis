"""Tests for the historical USD series fetcher, cache, and staleness-bounded lookup.

No test in this module touches the network: every :class:`~expensuchis.fx.ArgentinaDatosSource`
is built with an injected transport, either a canned fixture or an inline malformed payload.
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from pathlib import Path

import pytest

from expensuchis import fx
from expensuchis.fx import (
    MAX_STALENESS_DAYS,
    ArgentinaDatosSource,
    CachedSeries,
    CacheError,
    FetchError,
    FxCache,
    PayloadError,
    Quote,
    RateNotFoundError,
    Series,
)
from expensuchis.ledger import ENV_VAR
from expensuchis.paths import LedgerPaths

FIXTURES = Path(__file__).parent / "fixtures" / "fx"

BOLSA_URL = "https://api.argentinadatos.com/v1/cotizaciones/dolares/bolsa"
CCL_URL = "https://api.argentinadatos.com/v1/cotizaciones/dolares/contadoconliqui"


@pytest.fixture
def ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "ledger"
    root.mkdir()
    monkeypatch.setenv(ENV_VAR, str(root))
    return root


def _fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class _FakeTransport:
    """Records every requested URL and always returns the same canned payload."""

    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.requested_urls: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.requested_urls.append(url)
        return self.payload


# --------------------------------------------------------------------------------- Series


def test_series_maps_to_the_argentinadatos_casa_slug() -> None:
    assert Series.MEP.value == "bolsa"
    assert Series.CCL.value == "contadoconliqui"


# --------------------------------------------------------------------------------- ArgentinaDatosSource


def test_source_fetches_and_parses_a_valid_payload() -> None:
    transport = _FakeTransport(_fixture_bytes("bolsa_sample.json"))
    source = ArgentinaDatosSource(transport=transport)

    quotes = source.fetch(Series.MEP)

    assert quotes == [
        Quote(date=dt.date(2024, 1, 2), buy=Decimal("800.0"), sell=Decimal("820.5")),
        Quote(date=dt.date(2024, 1, 3), buy=Decimal("805.25"), sell=Decimal("825.0")),
        Quote(date=dt.date(2024, 1, 5), buy=Decimal("810.0"), sell=Decimal("830.75")),
        Quote(date=dt.date(2024, 1, 8), buy=Decimal("812.5"), sell=Decimal("832.0")),
    ]
    assert transport.requested_urls == [BOLSA_URL]
    assert source.source_id == "argentinadatos"
    assert source.url_for(Series.CCL) == CCL_URL


def test_source_never_falls_back_to_the_default_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guards against a wiring mistake: an injected transport must fully replace urllib."""

    def boom(url: str) -> bytes:
        raise AssertionError(f"unexpected network call to {url}")

    monkeypatch.setattr(fx, "_urllib_get", boom)
    source = ArgentinaDatosSource(transport=_FakeTransport(_fixture_bytes("bolsa_sample.json")))
    source.fetch(Series.MEP)  # would raise AssertionError if it used the patched default


def test_source_wraps_a_transport_failure() -> None:
    def failing(url: str) -> bytes:
        raise OSError("connection refused")

    source = ArgentinaDatosSource(transport=failing)
    with pytest.raises(FetchError):
        source.fetch(Series.MEP)


REJECTIONS: dict[str, str] = {
    "non_list_payload": '{"not": "a list"}',
    "entry_not_an_object": "[1, 2, 3]",
    "missing_field": '[{"casa": "bolsa", "compra": 1, "venta": 2}]',
    "extra_field": (
        '[{"casa": "bolsa", "compra": 1, "venta": 2, "fecha": "2024-01-02", "extra": true}]'
    ),
    "wrong_casa": '[{"casa": "contadoconliqui", "compra": 1, "venta": 2, "fecha": "2024-01-02"}]',
    "non_string_casa": '[{"casa": 1, "compra": 1, "venta": 2, "fecha": "2024-01-02"}]',
    "unparseable_date": '[{"casa": "bolsa", "compra": 1, "venta": 2, "fecha": "02-01-2024"}]',
    "non_string_date": '[{"casa": "bolsa", "compra": 1, "venta": 2, "fecha": 20240102}]',
    "non_numeric_compra": '[{"casa": "bolsa", "compra": "x", "venta": 2, "fecha": "2024-01-02"}]',
    "zero_buy": '[{"casa": "bolsa", "compra": 0, "venta": 2, "fecha": "2024-01-02"}]',
    "negative_sell": '[{"casa": "bolsa", "compra": 1, "venta": -1, "fecha": "2024-01-02"}]',
    "buy_greater_than_sell": '[{"casa": "bolsa", "compra": 5, "venta": 2, "fecha": "2024-01-02"}]',
    "duplicate_dates": (
        '[{"casa": "bolsa", "compra": 1, "venta": 2, "fecha": "2024-01-02"}, '
        '{"casa": "bolsa", "compra": 1, "venta": 2, "fecha": "2024-01-02"}]'
    ),
    "not_valid_json": "not json at all",
    "top_level_not_a_list_or_object": "42",
}


@pytest.mark.parametrize("payload", REJECTIONS.values(), ids=REJECTIONS.keys())
def test_source_rejects_malformed_payloads(payload: str) -> None:
    source = ArgentinaDatosSource(transport=lambda url: payload.encode("utf-8"))
    with pytest.raises(PayloadError):
        source.fetch(Series.MEP)


def test_source_sorts_out_of_order_quotes_by_date() -> None:
    payload = (
        '[{"casa": "bolsa", "compra": 1, "venta": 2, "fecha": "2024-01-05"}, '
        '{"casa": "bolsa", "compra": 1, "venta": 2, "fecha": "2024-01-02"}]'
    )
    source = ArgentinaDatosSource(transport=lambda url: payload.encode("utf-8"))
    quotes = source.fetch(Series.MEP)
    assert [quote.date for quote in quotes] == [dt.date(2024, 1, 2), dt.date(2024, 1, 5)]


# --------------------------------------------------------------------------------- FxCache


def test_refresh_writes_an_attributed_cache_file(ledger: Path) -> None:
    paths = LedgerPaths()
    transport = _FakeTransport(_fixture_bytes("bolsa_sample.json"))
    cache = FxCache(paths, source=ArgentinaDatosSource(transport=transport))

    cached = cache.refresh(Series.MEP)

    assert cached.series is Series.MEP
    assert cached.source_id == "argentinadatos"
    assert cached.source_url == BOLSA_URL
    assert len(cached.quotes) == 4

    cache_path = paths.fx_series("bolsa")
    assert cache_path.is_file()
    on_disk = json.loads(cache_path.read_text(encoding="utf-8"))
    assert on_disk["source_id"] == "argentinadatos"
    assert on_disk["source_url"] == BOLSA_URL
    assert on_disk["casa"] == "bolsa"
    assert len(on_disk["quotes"]) == 4


def test_refresh_write_leaves_no_temp_file_behind(ledger: Path) -> None:
    paths = LedgerPaths()
    cache = FxCache(
        paths,
        source=ArgentinaDatosSource(transport=_FakeTransport(_fixture_bytes("bolsa_sample.json"))),
    )
    cache.refresh(Series.MEP)
    assert list(paths.fx_dir().glob("*.tmp")) == []
    assert sorted(entry.name for entry in paths.fx_dir().iterdir()) == ["bolsa.json"]


def test_load_reads_back_a_refreshed_cache_without_network(ledger: Path) -> None:
    paths = LedgerPaths()
    transport = _FakeTransport(_fixture_bytes("contadoconliqui_sample.json"))
    FxCache(paths, source=ArgentinaDatosSource(transport=transport)).refresh(Series.CCL)

    def boom(url: str) -> bytes:
        raise AssertionError("load must never touch the network")

    reloaded = FxCache(paths, source=ArgentinaDatosSource(transport=boom)).load(Series.CCL)

    assert reloaded.series is Series.CCL
    assert reloaded.source_id == "argentinadatos"
    assert reloaded.source_url == CCL_URL
    assert [quote.date for quote in reloaded.quotes] == [
        dt.date(2024, 1, 2),
        dt.date(2024, 1, 3),
        dt.date(2024, 1, 5),
        dt.date(2024, 1, 8),
    ]
    assert reloaded.quotes[0].buy == Decimal("795.0")


def test_load_without_a_prior_refresh_raises(ledger: Path) -> None:
    paths = LedgerPaths()
    with pytest.raises(CacheError):
        FxCache(paths).load(Series.MEP)


def test_load_refuses_a_corrupt_cache_instead_of_silently_refetching(ledger: Path) -> None:
    paths = LedgerPaths()
    paths.fx_dir().mkdir(parents=True, exist_ok=True)
    paths.fx_series("bolsa").write_text("not json", encoding="utf-8")

    def boom(url: str) -> bytes:
        raise AssertionError("a corrupt cache must never trigger a silent refetch")

    with pytest.raises(CacheError):
        FxCache(paths, source=ArgentinaDatosSource(transport=boom)).load(Series.MEP)


def test_load_refuses_a_cache_holding_the_wrong_series(ledger: Path) -> None:
    paths = LedgerPaths()
    FxCache(
        paths,
        source=ArgentinaDatosSource(transport=_FakeTransport(_fixture_bytes("bolsa_sample.json"))),
    ).refresh(Series.MEP)
    # Move the MEP cache bytes under the CCL cache path: contents disagree with the filename.
    paths.fx_series("bolsa").rename(paths.fx_series("contadoconliqui"))

    with pytest.raises(CacheError):
        FxCache(paths).load(Series.CCL)


def test_refresh_overwrites_a_previous_cache(ledger: Path) -> None:
    paths = LedgerPaths()
    FxCache(
        paths,
        source=ArgentinaDatosSource(transport=_FakeTransport(_fixture_bytes("bolsa_sample.json"))),
    ).refresh(Series.MEP)

    replacement = '[{"casa": "bolsa", "compra": 900, "venta": 910, "fecha": "2024-02-01"}]'
    FxCache(
        paths, source=ArgentinaDatosSource(transport=lambda url: replacement.encode("utf-8"))
    ).refresh(Series.MEP)

    reloaded = FxCache(paths).load(Series.MEP)
    assert len(reloaded.quotes) == 1
    assert reloaded.quotes[0].date == dt.date(2024, 2, 1)


# --------------------------------------------------------------------------------- rate_at


def _quotes(*rows: tuple[str, str, str]) -> tuple[Quote, ...]:
    return tuple(
        Quote(date=dt.date.fromisoformat(date), buy=Decimal(buy), sell=Decimal(sell))
        for date, buy, sell in rows
    )


def _cached(series: Series, quotes: tuple[Quote, ...]) -> CachedSeries:
    return CachedSeries(
        series=series,
        source_id="argentinadatos",
        source_url="https://example.invalid",
        fetched_at=dt.datetime(2024, 1, 9, tzinfo=dt.UTC),
        quotes=quotes,
    )


SAMPLE_QUOTES = _quotes(
    ("2024-01-02", "800", "820"),  # Tuesday
    ("2024-01-03", "805", "825"),  # Wednesday
    ("2024-01-05", "810", "830"),  # Friday
    ("2024-01-08", "812", "832"),  # Monday
)


def test_rate_at_returns_the_exact_date_when_present() -> None:
    lookup = _cached(Series.MEP, SAMPLE_QUOTES).rate_at(dt.date(2024, 1, 3))
    assert lookup.requested == dt.date(2024, 1, 3)
    assert lookup.used.date == dt.date(2024, 1, 3)
    assert lookup.used.buy == Decimal(805)


def test_rate_at_falls_back_to_the_most_recent_previous_quote_across_a_weekend() -> None:
    """2024-01-06/07 is a weekend with no quote; 01-05 (Friday) is the last one before it."""
    lookup = _cached(Series.MEP, SAMPLE_QUOTES).rate_at(dt.date(2024, 1, 7))
    assert lookup.requested == dt.date(2024, 1, 7)
    assert lookup.used.date == dt.date(2024, 1, 5)


def test_rate_at_before_the_series_start_raises() -> None:
    with pytest.raises(RateNotFoundError):
        _cached(Series.MEP, SAMPLE_QUOTES).rate_at(dt.date(2024, 1, 1))


def test_rate_at_beyond_the_staleness_bound_raises() -> None:
    stale_date = SAMPLE_QUOTES[-1].date + dt.timedelta(days=MAX_STALENESS_DAYS + 1)
    with pytest.raises(RateNotFoundError):
        _cached(Series.MEP, SAMPLE_QUOTES).rate_at(stale_date)


def test_rate_at_exactly_at_the_staleness_bound_succeeds() -> None:
    edge_date = SAMPLE_QUOTES[-1].date + dt.timedelta(days=MAX_STALENESS_DAYS)
    lookup = _cached(Series.MEP, SAMPLE_QUOTES).rate_at(edge_date)
    assert lookup.used.date == SAMPLE_QUOTES[-1].date
    assert lookup.requested == edge_date


def test_rate_at_on_an_empty_series_raises() -> None:
    with pytest.raises(RateNotFoundError):
        _cached(Series.MEP, ()).rate_at(dt.date(2024, 1, 3))
