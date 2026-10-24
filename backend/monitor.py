"""Prospekt-Check im Ablauf: wann abgefragt wird, was gemeldet wird.

Prueft an festen Tagen (Standard Mo, Mi, Fr, So um 08:00), ob Produkte der
Merkliste in den gewuenschten Geschaeften im Angebot sind - jetzt oder in den
naechsten Tagen. Neue Treffer gehen einmal als Push raus, die Seite zeigt alle
mit Geschaeft, Preis und Zeitraum.

check() laeuft jede Minute. Das kostet fast nichts: Es wird nur nachgesehen, ob
sich die Merkliste geaendert hat oder ein Termin faellig ist. Die Quellen
(marktguru, dazu LOGO direkt, wenn es in der Merkliste steht) werden nur dann
abgefragt:
- zum geplanten Termin (nach einem Neustart wird ein verpasster nachgeholt),
- sofort nach einer Aenderung der Merkliste,
- auf Knopfdruck ("Jetzt pruefen", hoechstens einmal pro Minute).
Schlaegt eine Quelle fehl, liefert die andere trotzdem. Fuer die fehlende gilt
ihr letzter Stand, sie wird stuendlich erneut versucht.

Letzter Stand und was schon gemeldet wurde, liegen im Docker-Volume
(angebote.json). Nach einem Neustart oder Neubau kommt so keine Meldung doppelt.

Uebernommen aus Sentinel 2.3.1 (AngeboteMonitor), ohne die Teile fuer die
Dienstueberwachung.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any

from backend.angebote import (SOURCE_LOGO, SOURCE_MARKTGURU, Match, Offer, OfferFormatError,
                              Watchlist, WatchlistError, find_matches, format_day, last_slot,
                              next_slot, offers_from_logo, offers_from_marktguru, offers_payload,
                              parse_watchlist, read_watchlist_text, summary_message)
from backend.files import write_atomic
from backend.logo import OFFERS_URL as LOGO_URL
from backend.logo import LogoClient, LogoError
from backend.marktguru import OFFER_URL, MarktguruClient, MarktguruError

logger = logging.getLogger("angebotsfinder.pruefung")

STATE_FILE = "angebote.json"
RETRY_AFTER = timedelta(minutes=60)
MANUAL_COOLDOWN = timedelta(seconds=60)
FETCH_TIMEOUT = 20.0
"""Je Quelle, beide laufen gleichzeitig. Unter dem Zeitlimit eines ganzen
Durchlaufs (30 s): so steht auf der Seite, was los war, statt nur "Zeitlimit"."""
SOURCE_NAMES = {SOURCE_MARKTGURU: "marktguru", SOURCE_LOGO: "LOGO"}


class ServiceStatus(str, Enum):
    OK = "ok"
    WARNING = "warning"
    ERROR = "error"
    UNKNOWN = "unknown"


@dataclass
class CheckResult:
    status: ServiceStatus
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    checked_at: datetime = field(default_factory=datetime.now)
    notifications: list[dict[str, Any]] = field(default_factory=list)
    """Push-Meldungen aus genau dieser Pruefung (neue Angebote). Sie werden
    einmal verschickt und gehoeren nicht zum angezeigten Stand."""


def _stamp(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


def _parse_stamp(value: Any) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def format_stamp(moment: datetime) -> str:
    return f"{format_day(moment)} {moment.hour:02d}:{moment.minute:02d}"


def sentence(text: str) -> str:
    """Mit Satzzeichen am Ende, damit mehrere Meldungen lesbar hintereinander stehen."""
    return text if text.endswith((".", "?", "!")) else f"{text}."


@dataclass
class OfferState:
    last_run_at: datetime | None = None
    """Letzter Abruf, bei dem mindestens eine Quelle geliefert hat."""
    last_attempt_at: datetime | None = None
    attempt_fingerprint: str | None = None
    run_fingerprint: str | None = None
    """Merkliste beim letzten erfolgreichen Abruf - weicht sie ab, wird sofort geprueft."""
    errors: dict[str, str] = field(default_factory=dict)
    """Fehler beim letzten Versuch je Quelle (marktguru, logo)."""
    fetched: dict[str, datetime] = field(default_factory=dict)
    """Letzter erfolgreicher Abruf je Quelle, fuer "Stand von ..."."""
    matches: list[Match] = field(default_factory=list)
    shops_seen: list[str] = field(default_factory=list)
    announced: dict[str, str] = field(default_factory=dict)
    """Schon gemeldete Treffer: Kennung -> Ende der Gueltigkeit (zum Aufraeumen)."""

    @property
    def last_error(self) -> str | None:
        return " ".join(sentence(e) for e in self.errors.values()) or None

    def to_json(self) -> dict[str, Any]:
        return {
            "version": 1,
            "last_run_at": _stamp(self.last_run_at),
            "last_attempt_at": _stamp(self.last_attempt_at),
            "attempt_fingerprint": self.attempt_fingerprint,
            "run_fingerprint": self.run_fingerprint,
            "errors": self.errors,
            "fetched": {source: _stamp(at) for source, at in self.fetched.items()},
            "matches": [m.to_dict() for m in self.matches],
            "shops_seen": self.shops_seen,
            "announced": self.announced,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "OfferState":
        return cls(
            last_run_at=_parse_stamp(data.get("last_run_at")),
            last_attempt_at=_parse_stamp(data.get("last_attempt_at")),
            attempt_fingerprint=data.get("attempt_fingerprint"),
            run_fingerprint=data.get("run_fingerprint"),
            errors={str(k): str(v) for k, v in (data.get("errors") or {}).items()},
            fetched={str(k): datetime.fromisoformat(v)
                     for k, v in (data.get("fetched") or {}).items() if v},
            matches=[Match.from_dict(m) for m in data.get("matches") or []],
            shops_seen=[str(s) for s in data.get("shops_seen") or []],
            announced={str(k): str(v) for k, v in (data.get("announced") or {}).items()},
        )


class AngeboteMonitor:
    def __init__(self, watchlist_path: Path, label: str | None = None,
                 client: Any = None, logo: Any = None):
        self.watchlist_path = Path(watchlist_path)
        self.watchlist_label = label or str(watchlist_path)
        self.client = client or MarktguruClient()
        self.logo = logo or LogoClient()
        self.clock = datetime.now
        self.state = OfferState()
        self.state_file: Path | None = None
        self.watchlist: Watchlist | None = None
        self.watchlist_error: str | None = None
        self._file_hash: str | None = None
        self._manual = False
        self._lock = asyncio.Lock()

    # --- Ablage im Volume --------------------------------------------------

    def use_data_dir(self, data_dir: Path) -> None:
        self.state_file = Path(data_dir) / STATE_FILE
        if not self.state_file.exists():
            return
        try:
            self.state = OfferState.from_json(json.loads(self.state_file.read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            # Schlimmstenfalls kommen die laufenden Angebote einmal erneut.
            logger.error("%s ist beschädigt (%s), es geht mit leerem Stand weiter",
                         self.state_file, exc)
            self.state = OfferState()

    def _save(self) -> None:
        if self.state_file is None:
            return
        data = json.dumps(self.state.to_json(), indent=1, ensure_ascii=False).encode("utf-8")
        try:
            write_atomic(self.state_file, data)
        except OSError as exc:
            # Ohne Ablage geht es im Speicher weiter. Nach einem Neustart kaeme
            # dann hoechstens eine Meldung doppelt.
            logger.error("%s lässt sich nicht speichern: %s", self.state_file, exc)

    # --- Merkliste ---------------------------------------------------------

    def _reload_watchlist(self) -> None:
        """Liest die Datei bei jeder Pruefung (wenige hundert Byte), uebernimmt sie
        aber nur, wenn sich der Inhalt geaendert hat. Kaputt oder geloescht: Es
        gilt der letzte gueltige Stand weiter, die Seite zeigt den Fehler."""
        try:
            raw = self.watchlist_path.read_bytes()
        except FileNotFoundError:
            self._file_hash = None
            self.watchlist_error = (f"{self.watchlist_label} fehlt. Bitte die Vorlage "
                                    f"config/angebote.example.yaml als angebote.yaml kopieren")
            return
        except OSError as exc:
            self._file_hash = None
            self.watchlist_error = f"{self.watchlist_label} nicht lesbar: {exc}"
            return

        digest = hashlib.sha256(raw).hexdigest()
        if digest == self._file_hash:
            return
        self._file_hash = digest
        try:
            self.watchlist = parse_watchlist(read_watchlist_text(raw))
        except WatchlistError as exc:
            self.watchlist_error = f"{self.watchlist_label}: {exc}"
            logger.warning("Merkliste %s abgelehnt: %s", self.watchlist_path, exc)
            return
        self.watchlist_error = None
        logger.info("Merkliste %s geladen: %d Produkt(e), Geschäfte: %s, PLZ %s",
                    self.watchlist_label, len(self.watchlist.products),
                    ", ".join(self.watchlist.shop_labels()) or "alle", self.watchlist.zip_code)

    # --- Wann wird abgefragt? ----------------------------------------------

    def due(self, now: datetime) -> str | None:
        """Grund fuer einen Abruf jetzt - oder None."""
        watchlist, state = self.watchlist, self.state
        if watchlist is None:
            return None
        if self._manual:
            return "manual"
        fingerprint = watchlist.fingerprint()
        if state.last_run_at is None:
            reason = "first"
        elif state.run_fingerprint != fingerprint:
            reason = "changed"
        elif state.last_run_at < last_slot(watchlist.schedule, now):
            reason = "schedule"
        elif state.errors:
            reason = "retry"            # eine Quelle hat beim letzten Abruf gefehlt
        else:
            return None
        # Mit genau dieser Merkliste eben gescheitert: erst nach einer Stunde
        # wieder. Eine neue Merkliste wird sofort versucht.
        if (state.errors and state.attempt_fingerprint == fingerprint
                and state.last_attempt_at and now - state.last_attempt_at < RETRY_AFTER):
            return None
        return reason

    def request_run(self, now: datetime) -> float:
        """Knopf "Jetzt pruefen": 0, wenn es losgehen kann, sonst die Sekunden bis
        zum naechsten erlaubten Versuch."""
        last = self.state.last_attempt_at
        if last is not None and now - last < MANUAL_COOLDOWN:
            return (MANUAL_COOLDOWN - (now - last)).total_seconds()
        self._manual = True
        return 0

    # --- Pruefen -----------------------------------------------------------

    async def check(self) -> CheckResult:
        async with self._lock:
            self._reload_watchlist()
            reason = self.due(self.clock())
            self._manual = False
            notifications = await self._run(reason) if reason else []
            return self._result(self.clock(), notifications)

    @staticmethod
    def _sources(watchlist: Watchlist) -> list[str]:
        sources = []
        if watchlist.uses_marktguru:
            sources.append(SOURCE_MARKTGURU)
        if watchlist.logo_shop:
            sources.append(SOURCE_LOGO)
        return sources

    async def _run(self, reason: str) -> list[dict]:
        watchlist, state = self.watchlist, self.state
        now = self.clock()
        fingerprint = watchlist.fingerprint()
        state.last_attempt_at, state.attempt_fingerprint = now, fingerprint
        sources = self._sources(watchlist)
        # Nachholen betrifft nur die Quelle, die gefehlt hat.
        wanted = [s for s in sources if s in state.errors] if reason == "retry" else []
        wanted = wanted or sources
        logger.info("Prüfe Angebote (%s): %d Produkt(e), PLZ %s, Quellen: %s", reason,
                    len(watchlist.products), watchlist.zip_code,
                    ", ".join(SOURCE_NAMES[s] for s in wanted))
        try:
            results = await asyncio.gather(*(self._fetch(s, watchlist) for s in wanted))
        except asyncio.CancelledError:
            # Abgebrochen vom Zeitlimit des ganzen Durchlaufs: Das zaehlt als Fehler
            # der Quellen, damit die Wartezeit von einer Stunde greift. Sonst
            # wuerde jede Minute neu angefragt (Befund der Review vom 24.10.2026).
            state.errors = {s: f"{SOURCE_NAMES[s]}: Abruf abgebrochen (Zeitlimit)" for s in wanted}
            self._save()
            raise
        fresh = {s: offers for s, (offers, error) in zip(wanted, results) if error is None}
        state.errors = {s: error for s, (_, error) in zip(wanted, results) if error is not None}
        for source, error in state.errors.items():
            logger.warning("Abruf bei %s fehlgeschlagen: %s", SOURCE_NAMES[source], error)
        if not fresh:
            self._save()
            return []

        # Was diesmal nicht frisch kam, gilt mit dem letzten Stand weiter.
        carried = [m.offer for m in state.matches
                   if m.offer.source in sources and m.offer.source not in fresh]
        marktguru = fresh.get(SOURCE_MARKTGURU, {})
        extra = fresh.get(SOURCE_LOGO, []) + carried
        offers = {query: marktguru.get(query, []) + extra for query in watchlist.queries}
        matches = find_matches(watchlist, offers, now)
        new = [m for m in matches if m.key not in state.announced]
        state.matches = matches
        if SOURCE_MARKTGURU in fresh:
            state.shops_seen = sorted({o.shop for found in marktguru.values() for o in found},
                                      key=str.casefold)
        elif SOURCE_MARKTGURU not in sources:
            state.shops_seen = []
        state.fetched = {s: at for s, at in state.fetched.items() if s in sources}
        state.fetched.update({s: now for s in fresh})
        state.last_run_at, state.run_fingerprint = now, fingerprint
        # Auch bei push: false merken - sonst kaeme beim Einschalten alles auf einmal.
        for match in matches:
            state.announced[match.key] = match.offer.valid_to.isoformat(timespec="minutes")
        cutoff = now - timedelta(days=1)
        state.announced = {k: v for k, v in state.announced.items()
                           if datetime.fromisoformat(v) >= cutoff}
        self._save()
        logger.info("Angebote: %d Treffer, davon %d neu", len(matches), len(new))
        if new and watchlist.push:
            return [offers_payload(new, now)]
        return []

    async def _fetch(self, source: str, watchlist: Watchlist) -> tuple[Any, str | None]:
        """Eine Quelle abrufen: (Angebote, None) oder (None, Meldung fuer die Seite).
        marktguru liefert je Suchbegriff, LOGO eine Liste fuer alle Produkte."""
        name = SOURCE_NAMES[source]
        try:
            async with asyncio.timeout(FETCH_TIMEOUT):
                if source == SOURCE_LOGO:
                    week = await self.logo.fetch(watchlist.logo_markets)
                else:
                    raw = await self.client.search_many(watchlist.queries, watchlist.zip_code)
            if source == SOURCE_LOGO:
                offers = offers_from_logo(watchlist.logo_shop, week)
                logger.info("LOGO-Woche %s-%s: %d Angebot(e), Märkte: %s",
                            week.valid_from.strftime("%d.%m."), week.valid_to.strftime("%d.%m.%Y"),
                            len(offers), ", ".join(m for m in week.by_market if m) or "alle")
                return offers, None
            return {query: offers_from_marktguru(results) for query, results in raw.items()}, None
        except (MarktguruError, LogoError, OfferFormatError) as exc:
            return None, str(exc)
        except TimeoutError:
            return None, f"{name} antwortet nicht (über {FETCH_TIMEOUT:.0f} s)"
        except Exception as exc:
            logger.exception("Abruf bei %s ist abgestürzt", name)
            return None, f"{name}: {type(exc).__name__}: {exc}"

    def current_matches(self, now: datetime) -> list[Match]:
        """Gespeicherte Treffer ohne die inzwischen abgelaufenen."""
        return [m for m in self.state.matches if m.offer.valid_to >= now]

    def _result(self, now: datetime, notifications: list[dict]) -> CheckResult:
        state, watchlist = self.state, self.watchlist
        matches = self.current_matches(now)
        details: dict[str, Any] = {"hits": len(matches)}
        if state.last_run_at:
            details["last_run"] = _stamp(state.last_run_at)
        if watchlist:
            details["next_run"] = _stamp(next_slot(watchlist.schedule, now))
            details["subtitle"] = (f"{watchlist.shops_text()} · PLZ {watchlist.zip_code} · "
                                   f"{watchlist.schedule.text()}")

        def result(status: ServiceStatus, message: str) -> CheckResult:
            return CheckResult(status=status, message=message, details=details,
                               notifications=notifications)

        if watchlist is None:
            return result(ServiceStatus.WARNING, self.watchlist_error or "Keine Merkliste")
        problems = []
        if self.watchlist_error:
            problems.append(f"{sentence(self.watchlist_error)} Es gilt der letzte gültige Stand.")
        for source, error in state.errors.items():
            problems.append(sentence(f"Abruf fehlgeschlagen: {error}"))
            if source in state.fetched:
                problems.append(f"Stand von {format_stamp(state.fetched[source])}.")
        if problems:
            return result(ServiceStatus.WARNING, " ".join(problems))
        if state.last_run_at is None:
            return result(ServiceStatus.UNKNOWN, "Noch nicht geprüft")
        return result(ServiceStatus.OK, summary_message(watchlist, matches, now))

    # --- Fuer die Seite ----------------------------------------------------

    def get_details(self) -> dict[str, Any]:
        now = self.clock()
        state, watchlist = self.state, self.watchlist
        matches = self.current_matches(now)
        configured = {s.casefold() for s in watchlist.shops} if watchlist else set()
        counts: dict[str, int] = {}
        for match in matches:
            counts[match.product] = counts.get(match.product, 0) + 1
        return {
            "summary": {
                "hits": len(matches),
                "current": sum(m.is_current(now) for m in matches),
                "upcoming": sum(not m.is_current(now) for m in matches),
                "last_run": _stamp(state.last_run_at),
                "next_run": _stamp(next_slot(watchlist.schedule, now)) if watchlist else None,
                "last_error": state.last_error,
                "watchlist_error": self.watchlist_error,
            },
            "matches": [
                {
                    "product": m.product,
                    "shop": m.offer.shop_label,
                    "price": m.offer.price,
                    "old_price": m.offer.old_price,
                    "reference_price": m.offer.reference_price,
                    "unit": m.offer.unit,
                    "valid_from": m.offer.valid_from.isoformat(timespec="minutes"),
                    "valid_to": m.offer.valid_to.isoformat(timespec="minutes"),
                    "is_current": m.is_current(now),
                    "app_only": m.offer.app_only,
                    "regional": m.offer.regional,
                    "condition": m.offer.condition,
                    "description": m.offer.description,
                    "source": m.offer.source,
                    "url": offer_url(m.offer),
                }
                for m in matches
            ],
            "products": [{"label": p.label, "hits": counts.get(p.label, 0)}
                         for p in watchlist.products] if watchlist else [],
            "settings": {
                "zip_code": watchlist.zip_code,
                "shops": watchlist.shop_labels(),
                "schedule": watchlist.schedule.text(),
                "days_ahead": watchlist.days_ahead,
                "push": watchlist.push,
            } if watchlist else None,
            "watchlist_file": self.watchlist_label,
            "other_shops": [s for s in state.shops_seen if s.casefold() not in configured],
            "source": source_text(watchlist),
        }


def offer_url(offer: Offer) -> str | None:
    if offer.source == SOURCE_LOGO:
        return LOGO_URL
    return OFFER_URL.format(id=offer.id) if offer.id.isdigit() else None


def source_text(watchlist: Watchlist | None) -> str:
    parts = []
    if watchlist is None or watchlist.uses_marktguru:
        parts.append("marktguru.de (Prospekte der Region)")
    if watchlist is not None and watchlist.logo_shop:
        parts.append("logo-getraenke.de (LOGO direkt)")
    return " und ".join(parts)
