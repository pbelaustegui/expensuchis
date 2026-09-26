"""Historical USD series (MEP, CCL): fetch, local cache, source attribution, gap handling.

**Deflation is a view, never a stored value** (see ``odd/tasks/family-ledger.md``,
Decisions): the ledger stores nominal ARS facts and dates; the USD series is
separate data fetched independently; a report computes the USD value at read time
by picking a series and a date. This module owns only that separate data — nothing
here writes to the ledger, and nothing in the ledger depends on it existing.

**Source, decided by the project owner (2026-09-26): ArgentinaDatos**
(``https://api.argentinadatos.com/v1/cotizaciones/dolares/{casa}``), public, no
auth, MIT-licensed, fed by DolarApi. It is **unofficial** — the BCRA publishes
neither MEP (``bolsa``, series from 2018-10-29) nor CCL (``contadoconliqui``,
series from 2013-01-02) itself. The mitigation is structural, not a disclaimer:
every cached quote records its source id and source URL, and fetching sits behind
the :class:`FxSource` protocol, so replacing the source is a new implementation of
that protocol, never a migration of stored data.

One call returns the *entire* series as a JSON array of
``{"casa": str, "compra": number, "venta": number, "fecha": "YYYY-MM-DD"}``. There
is no incremental endpoint, so :meth:`FxCache.refresh` always re-fetches and
rewrites the whole cache file for one series.

**Validation is strict, not best-effort.** A statement's amount is guessed at the
project's own peril elsewhere (see :mod:`expensuchis.numbers`); a market rate is
worse to get wrong, because it is silently multiplied into every USD-denominated
report. :func:`ArgentinaDatosSource.fetch` therefore rejects anything the payload
does not prove, rather than tolerating it: a non-list payload, an entry with
missing or wrongly typed fields, a ``casa`` that does not match the requested
series, an unparseable date, a non-positive rate, ``compra > venta``, or a
duplicate date. Quotes are returned sorted by date.

**The cache never refetches on its own.** :meth:`FxCache.load` reads the local
JSON file and raises :class:`CacheError` on anything it cannot trust — a missing
file, invalid JSON, a missing field, or an internally inconsistent quote. Only
:meth:`FxCache.refresh` talks to the network, and only when the caller asks for
it. Writes are atomic (temporary file in the same directory, then ``os.replace``),
so a crash mid-write can never leave a half-written cache file that a later
``load`` would trust.

**Staleness is bounded, not indefinite.** Argentina's markets are closed on
weekends and holidays, so :meth:`CachedSeries.rate_at` falls back to the most
recent earlier quote, but only within :data:`MAX_STALENESS_DAYS`. Beyond that
bound — including a date before the series starts, or past the last cached date
by more than the bound — it raises :class:`RateNotFoundError` rather than
returning a rate that is quietly too old to mean anything. Buy and sell are both
kept on every :class:`Quote`; which side a report uses is the caller's choice.
"""

from __future__ import annotations

import bisect
import dataclasses
import datetime as dt
import json
import os
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Protocol

from .paths import LedgerPaths

__all__ = [
    "MAX_STALENESS_DAYS",
    "ArgentinaDatosSource",
    "CacheError",
    "CachedSeries",
    "FetchError",
    "FxCache",
    "FxSource",
    "PayloadError",
    "Quote",
    "RateLookup",
    "RateNotFoundError",
    "Series",
]

_BASE_URL = "https://api.argentinadatos.com/v1/cotizaciones/dolares/{casa}"
_SOURCE_ID = "argentinadatos"
_FETCH_TIMEOUT_SECONDS = 10
_CACHE_VERSION = 1

#: How many calendar days a quote may be reused for a later date with no quote of
#: its own (weekends, holidays). Beyond this, :meth:`CachedSeries.rate_at` refuses
#: rather than returning a rate too old to be meaningful.
MAX_STALENESS_DAYS = 7

_EXPECTED_QUOTE_KEYS = frozenset({"casa", "compra", "venta", "fecha"})


class Series(Enum):
    """The two USD series the owner decided to fetch, mapped to ArgentinaDatos' ``casa``."""

    MEP = "bolsa"
    """Dollar MEP ("bolsa"); ArgentinaDatos carries this series from 2018-10-29."""

    CCL = "contadoconliqui"
    """Contado con liquidación; ArgentinaDatos carries this series from 2013-01-02."""


@dataclasses.dataclass(frozen=True)
class Quote:
    """One day's quote for one series. Both sides are kept; the caller picks one."""

    date: dt.date
    buy: Decimal
    sell: Decimal


class FetchError(RuntimeError):
    """Raised when the HTTP transport fails (network, timeout, non-2xx handled by it)."""


class PayloadError(ValueError):
    """Raised when a fetched payload is malformed, incomplete, or internally inconsistent."""


class CacheError(RuntimeError):
    """Raised when the local cache file is missing, corrupt, or internally inconsistent."""


class RateNotFoundError(LookupError):
    """Raised when no quote exists for a date within :data:`MAX_STALENESS_DAYS`."""


class FxSource(Protocol):
    """A source of USD quotes for one series, with attribution for the cache."""

    @property
    def source_id(self) -> str:
        """A short, stable identifier for this source (recorded in the cache)."""
        ...

    def url_for(self, series: Series) -> str:
        """The URL this source would fetch ``series`` from (recorded in the cache)."""
        ...

    def fetch(self, series: Series) -> list[Quote]:
        """Return every known quote for ``series``, sorted by date.

        Raises:
            FetchError: the transport failed.
            PayloadError: the payload does not prove itself trustworthy.
        """
        ...


def _urllib_get(url: str) -> bytes:
    """The default HTTP transport: stdlib ``urllib``, no third-party dependency."""
    with urllib.request.urlopen(url, timeout=_FETCH_TIMEOUT_SECONDS) as response:
        return response.read()


def _to_decimal(value: object, where: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (Decimal, int)):
        raise PayloadError(f"{where}: expected a number, found {value!r}")
    return value if isinstance(value, Decimal) else Decimal(value)


def _parse_quote(item: object, series: Series, index: int) -> Quote:
    if not isinstance(item, dict):
        raise PayloadError(f"Entry {index} is not an object: {item!r}")

    keys = set(item.keys())
    if keys != _EXPECTED_QUOTE_KEYS:
        raise PayloadError(
            f"Entry {index} has fields {sorted(keys)}, expected exactly "
            f"{sorted(_EXPECTED_QUOTE_KEYS)}."
        )

    casa = item["casa"]
    if not isinstance(casa, str) or casa != series.value:
        raise PayloadError(
            f"Entry {index} has casa {casa!r}, expected {series.value!r} for series {series.name}."
        )

    fecha = item["fecha"]
    if not isinstance(fecha, str):
        raise PayloadError(f"Entry {index} has a non-string fecha: {fecha!r}")
    try:
        quote_date = dt.date.fromisoformat(fecha)
    except ValueError as exc:
        raise PayloadError(f"Entry {index} has an unparseable fecha {fecha!r}: {exc}") from exc

    buy = _to_decimal(item["compra"], f"Entry {index} ({fecha}) compra")
    sell = _to_decimal(item["venta"], f"Entry {index} ({fecha}) venta")
    if buy <= 0 or sell <= 0:
        raise PayloadError(
            f"Entry {index} ({fecha}) has a non-positive rate: compra={buy} venta={sell}."
        )
    if buy > sell:
        raise PayloadError(
            f"Entry {index} ({fecha}) has compra > venta: compra={buy} venta={sell}."
        )

    return Quote(date=quote_date, buy=buy, sell=sell)


def _parse_payload(raw: bytes, series: Series, url: str) -> list[Quote]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PayloadError(f"Payload from {url} is not valid UTF-8: {exc}") from exc
    try:
        payload = json.loads(text, parse_float=Decimal)
    except json.JSONDecodeError as exc:
        raise PayloadError(f"Payload from {url} is not valid JSON: {exc}") from exc
    if not isinstance(payload, list):
        raise PayloadError(
            f"Payload from {url} is not a JSON array, found {type(payload).__name__}."
        )

    quotes = [_parse_quote(item, series, index) for index, item in enumerate(payload)]

    seen: set[dt.date] = set()
    for quote in quotes:
        if quote.date in seen:
            raise PayloadError(f"Duplicate fecha {quote.date} in payload from {url}.")
        seen.add(quote.date)

    return sorted(quotes, key=lambda quote: quote.date)


class ArgentinaDatosSource:
    """Fetches one series from ArgentinaDatos' public, unofficial, no-auth endpoint.

    The HTTP transport is injected so tests never touch the network: pass any
    ``Callable[[str], bytes]`` (a fake, or a real one built on another library).
    The default transport is stdlib ``urllib``, so no new dependency is required.
    """

    source_id = _SOURCE_ID

    def __init__(self, transport: Callable[[str], bytes] = _urllib_get) -> None:
        self._transport = transport

    def url_for(self, series: Series) -> str:
        """The ArgentinaDatos URL for ``series``."""
        return _BASE_URL.format(casa=series.value)

    def fetch(self, series: Series) -> list[Quote]:
        """Fetch and strictly validate the whole series. See the module docstring."""
        url = self.url_for(series)
        try:
            raw = self._transport(url)
        except (OSError, urllib.error.URLError) as exc:
            raise FetchError(f"Could not fetch {series.name} from {url}: {exc}") from exc
        return _parse_payload(raw, series, url)


@dataclasses.dataclass(frozen=True)
class CachedSeries:
    """A loaded FX series: its attribution plus the sorted quotes."""

    series: Series
    source_id: str
    source_url: str
    fetched_at: dt.datetime
    quotes: tuple[Quote, ...]

    def rate_at(self, requested: dt.date) -> RateLookup:
        """Return the quote to use for ``requested``.

        Returns the exact date's quote when one exists. Otherwise falls back to
        the most recent *earlier* quote, to cover weekends and holidays, but only
        within :data:`MAX_STALENESS_DAYS` calendar days.

        Raises:
            RateNotFoundError: the series is empty, ``requested`` is before the
                series starts, or the nearest available quote (including the last
                cached date, when ``requested`` is beyond it) is more than
                :data:`MAX_STALENESS_DAYS` days away.
        """
        if not self.quotes:
            raise RateNotFoundError(
                f"The {self.series.name} series is empty; no quote exists for {requested}."
            )

        dates = [quote.date for quote in self.quotes]
        if requested < dates[0]:
            raise RateNotFoundError(
                f"{requested} is before the {self.series.name} series start ({dates[0]}); "
                f"no quote exists."
            )

        index = bisect.bisect_right(dates, requested) - 1
        candidate = self.quotes[index]
        staleness = (requested - candidate.date).days
        if staleness > MAX_STALENESS_DAYS:
            raise RateNotFoundError(
                f"No {self.series.name} quote within {MAX_STALENESS_DAYS} days of "
                f"{requested}; the nearest is {candidate.date}, {staleness} days earlier."
            )
        return RateLookup(requested=requested, used=candidate)


@dataclasses.dataclass(frozen=True)
class RateLookup:
    """The quote used to answer a :meth:`CachedSeries.rate_at` query.

    ``used.date`` may differ from ``requested`` (a weekend or holiday fallback),
    so a caller that reports a value can also report which date it actually used.
    """

    requested: dt.date
    used: Quote


def _quote_to_json(quote: Quote) -> dict[str, str]:
    return {"fecha": quote.date.isoformat(), "compra": str(quote.buy), "venta": str(quote.sell)}


def _write_cache_atomic(path: Path, cached: CachedSeries) -> None:
    """Write ``cached`` to ``path`` atomically: temp file in the same directory, then rename."""
    payload = {
        "version": _CACHE_VERSION,
        "series": cached.series.name,
        "casa": cached.series.value,
        "source_id": cached.source_id,
        "source_url": cached.source_url,
        "fetched_at": cached.fetched_at.isoformat(),
        "quotes": [_quote_to_json(quote) for quote in cached.quotes],
    }

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.remove(tmp_name)
        except OSError:
            pass
        raise


def _quote_from_json(item: object, path: Path, index: int) -> Quote:
    if not isinstance(item, dict):
        raise CacheError(f"Corrupt fx cache {path}: quote {index} is not an object.")
    try:
        quote_date = dt.date.fromisoformat(item["fecha"])
        buy = Decimal(item["compra"])
        sell = Decimal(item["venta"])
    except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
        raise CacheError(f"Corrupt fx cache {path}: malformed quote {index}: {exc}") from exc
    if buy <= 0 or sell <= 0 or buy > sell:
        raise CacheError(
            f"Corrupt fx cache {path}: quote {index} ({quote_date}) has an invalid rate: "
            f"compra={buy} venta={sell}."
        )
    return Quote(date=quote_date, buy=buy, sell=sell)


def _read_cache(path: Path, expected_series: Series) -> CachedSeries:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CacheError(f"Could not read fx cache {path}: {exc}") from exc

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CacheError(f"Corrupt fx cache {path}: not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise CacheError(f"Corrupt fx cache {path}: expected a JSON object at the top level.")

    try:
        series_name = payload["series"]
        source_id = payload["source_id"]
        source_url = payload["source_url"]
        fetched_at_raw = payload["fetched_at"]
        raw_quotes = payload["quotes"]
    except KeyError as exc:
        raise CacheError(f"Corrupt fx cache {path}: missing field {exc}.") from exc

    if not isinstance(series_name, str) or series_name not in Series.__members__:
        raise CacheError(f"Corrupt fx cache {path}: unknown series {series_name!r}.")
    series = Series[series_name]
    if series is not expected_series:
        raise CacheError(
            f"Corrupt fx cache {path}: holds series {series.name}, expected {expected_series.name}."
        )

    if not isinstance(source_id, str) or not isinstance(source_url, str):
        raise CacheError(f"Corrupt fx cache {path}: source_id/source_url must be strings.")

    if not isinstance(fetched_at_raw, str):
        raise CacheError(f"Corrupt fx cache {path}: fetched_at must be a string.")
    try:
        fetched_at = dt.datetime.fromisoformat(fetched_at_raw)
    except ValueError as exc:
        raise CacheError(
            f"Corrupt fx cache {path}: unparseable fetched_at {fetched_at_raw!r}: {exc}"
        ) from exc

    if not isinstance(raw_quotes, list):
        raise CacheError(f"Corrupt fx cache {path}: 'quotes' is not a list.")

    quotes: list[Quote] = []
    previous_date: dt.date | None = None
    for index, item in enumerate(raw_quotes):
        quote = _quote_from_json(item, path, index)
        if previous_date is not None and quote.date <= previous_date:
            raise CacheError(f"Corrupt fx cache {path}: dates out of order at quote {index}.")
        previous_date = quote.date
        quotes.append(quote)

    return CachedSeries(
        series=series,
        source_id=source_id,
        source_url=source_url,
        fetched_at=fetched_at,
        quotes=tuple(quotes),
    )


class FxCache:
    """Local, per-series JSON cache of a fetched USD series, kept in the ledger directory.

    ``load`` never touches the network; only ``refresh`` does, and only when
    explicitly called. The path is obtained through
    :class:`~expensuchis.paths.LedgerPaths`, so it inherits the per-path
    repository guard (never inside this public repository).
    """

    def __init__(self, paths: LedgerPaths, source: FxSource | None = None) -> None:
        self._paths = paths
        self._source: FxSource = source if source is not None else ArgentinaDatosSource()

    def path(self, series: Series) -> Path:
        """The cache file for ``series`` (it may not exist yet)."""
        return self._paths.fx_series(series.value)

    def refresh(self, series: Series) -> CachedSeries:
        """Fetch ``series`` from the source and atomically rewrite its cache file.

        There is no incremental fetch (the source has no such endpoint), so this
        always replaces the previous cache contents wholesale.
        """
        quotes = self._source.fetch(series)
        cached = CachedSeries(
            series=series,
            source_id=self._source.source_id,
            source_url=self._source.url_for(series),
            fetched_at=dt.datetime.now(dt.UTC),
            quotes=tuple(quotes),
        )
        _write_cache_atomic(self.path(series), cached)
        return cached

    def load(self, series: Series) -> CachedSeries:
        """Load ``series`` from the local cache. Never fetches; never falls back.

        Raises:
            CacheError: no cache file exists yet, or it is corrupt.
        """
        path = self.path(series)
        if not path.is_file():
            raise CacheError(f"No cached {series.name} series at {path}; call refresh() first.")
        return _read_cache(path, series)
