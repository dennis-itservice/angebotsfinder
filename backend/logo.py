"""LOGO Getraenkemarkt: Wochenangebote direkt von logo-getraenke.de.

marktguru fuehrt LOGO fuer die Region nicht. Dort gibt es nur Maerkte in
Rheinhessen, die letzten Angebote sind vom Maerz 2026 (geprueft am 11.10.2026).
LOGO zeigt die Angebote selbst:

- /angebote/ enthaelt die Marktliste (155 Maerkte, je mit Nummer), den Zeitraum
  der Woche ("05.10.-10.10.2026") und alle Angebote der Woche. Die mit Sternchen
  gibt es "nicht in allen Filialen".
- Die Angebote eines Marktes liefert admin-ajax.php (action my_product_view,
  marketid), so wie der Knopf "Als mein Markt merken". Die Liste war bisher fuer
  jeden geprueften Markt dieselbe, auch fuer Alsfeld und Fulda in Hessen. Fuer die
  Woche ab 05.10.2026 waren es alle Angebote ohne die mit Sternchen, fuer die
  Woche ab 12.10. 57 von 64, darunter 7 mit Sternchen. Welche Filiale ein
  Sternchen-Angebot fuehrt, sagt LOGO also nicht. Ohne eigene Liste zeigt die
  Seite alle Angebote der Woche, das macht der Angebotsfinder genauso.
- Eine Mengenstaffel steht als ein Angebot je Stufe da, die Bedingung nur in der
  Beschreibung: "mehr kaufen - zusätzlich sparen! Ab 3 Kasten - Pro Kasten".

robots.txt erlaubt beides. Abgefragt wird mit demselben ehrlichen User-Agent wie
bei marktguru. Aendert LOGO die Seite, schlaegt der Abruf mit einer klaren
Meldung fehl, statt still "keine Angebote" zu liefern.

Uebernommen aus Sentinel 2.3.1 (Prospekt-Check).
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
from typing import Any

import aiohttp

from backend.marktguru import USER_AGENT

OFFERS_URL = "https://www.logo-getraenke.de/angebote/"
AJAX_URL = "https://www.logo-getraenke.de/wp-admin/admin-ajax.php"
PARALLEL_MARKETS = 3
MAX_WEEK_DAYS = 31

VOID_TAGS = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
                       "meta", "param", "source", "track", "wbr"})
# "05.10.-10.10.2026", ueber den Jahreswechsel "28.12.-02.01.2027"
DATE_RANGE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})?\s*[-–]\s*(\d{1,2})\.(\d{1,2})\.(\d{4})")
# Link zum Handzettel der Woche: /ressources/Handzettel/2026KW41/mobile/index.html
HANDZETTEL = re.compile(r"/Handzettel/(\d{4})KW(\d{1,2})/", re.I)
# "20/0,5 Ltr. Kasten", "1 Ltr. Flasche", "24/330 ml Dose"
PACK = re.compile(r"(?:(\d{1,3})\s*[/x×]\s*)?(\d+(?:,\d+)?)\s*(ml|ltr\b|liter\b|l\b)", re.I)
# Grundpreis am Ende: "... Pfd. Ltr. = € 1,40"
REFERENCE = re.compile(r"(?:ltr|liter|l)\.?\s*=\s*€\s*(\d+(?:[.,]\d+)?)", re.I)
IMAGE = re.compile(r"product_images/([\w-]+)\.")
# Mengenstaffel in der Beschreibung: "... sparen! Ab 3 Kasten - Pro Kasten", "2 Kasten - Pro Kasten"
TIER = re.compile(r"(?<!\w)(ab\s+)?(\d{1,2})\s+(\w+)\s*[-–]\s*pro\s+\w+", re.I)


class LogoError(Exception):
    """Abruf fehlgeschlagen. Die Meldung erscheint auf der Seite."""


@dataclass(frozen=True)
class LogoMarket:
    id: str
    name: str
    address: str


@dataclass(frozen=True)
class LogoOffer:
    """Ein Angebot, wie es auf der Seite steht."""
    name: str
    """'Krombacher Bier', ohne Sternchen."""
    description: str
    """'diverse Sorten, + Sixpack GRATIS!'"""
    content: str
    """'20/0,5 Ltr. Kasten + € 3,58 Pfd. Ltr. = € 1,40'"""
    price: float
    old_price: float | None
    image: str
    """Nummer des Produktbilds, z. B. '96721_z'."""
    regional: bool
    """Sternchen: nicht in allen Filialen verfuegbar."""

    @property
    def key(self) -> tuple:
        """Dasselbe Angebot in mehreren Maerkten."""
        return (self.name, self.description, self.content, self.price, self.old_price)

    @property
    def pack(self) -> tuple[int, float] | None:
        """(Anzahl, Liter je Flasche): '20/0,5 Ltr. Kasten' -> (20, 0.5)."""
        match = PACK.search(self.content)
        if not match:
            return None
        count = int(match.group(1) or 1)
        amount = float(match.group(2).replace(",", "."))
        liters = amount / 1000 if match.group(3).lower() == "ml" else amount
        if count < 1 or not 0 < liters <= 100:
            return None
        return count, round(liters, 4)

    @property
    def reference_price(self) -> float | None:
        """Grundpreis je Liter."""
        match = REFERENCE.search(self.content)
        return float(match.group(1).replace(",", ".")) if match else None

    @property
    def condition(self) -> str | None:
        """Stufe einer Mengenstaffel: 'ab 3 Kästen', 'bei 2 Kästen', 'bei 1 Kasten'.
        Der Preis gilt je Kasten."""
        match = TIER.search(self.description)
        if not match or int(match.group(2)) < 1:
            return None
        count, unit = int(match.group(2)), match.group(3)
        if count > 1 and unit.casefold() == "kasten":
            unit = "Kästen"
        return f"{'ab' if match.group(1) else 'bei'} {count} {unit}"


@dataclass(frozen=True)
class LogoPage:
    markets: list[LogoMarket]
    period: tuple[date, date] | None
    offers: list[LogoOffer]
    """Alle Angebote der Woche."""


@dataclass(frozen=True)
class LogoWeek:
    valid_from: date
    valid_to: date
    by_market: dict[str, list[LogoOffer]]
    """Angebote je Markt, Name wie auf der Seite. Ohne Marktwahl ein Eintrag ""
    mit allen Angeboten der Woche."""


# --- Lesen ------------------------------------------------------------------

class _Parser(HTMLParser):
    """Marktliste, Zeitraum und Angebote aus dem HTML. Haengt an den Klassen,
    nicht an Reihenfolge, Anfuehrungszeichen oder Einrueckung."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.markets: list[dict[str, str]] = []
        self.offers: list[dict[str, str]] = []
        self.dates: list[str] = []
        self.weeks: list[tuple[int, int]] = []
        self._stack: list[tuple[str, frozenset[str]]] = []
        self._capture: tuple[str, int] | None = None
        self._buffer: list[str] = []
        self._offer: dict[str, str] | None = None
        self._offer_depth = 0
        self._market: dict[str, str] | None = None
        self._market_depth = 0
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {key: value or "" for key, value in attrs}
        for key in ("href", "src"):
            week = HANDZETTEL.search(a.get(key, ""))
            if week:
                self.weeks.append((int(week.group(1)), int(week.group(2))))
        if tag in VOID_TAGS:
            image = IMAGE.search(a.get("src", "")) if tag == "img" else None
            if image and self._offer is not None:
                self._offer.setdefault("image", image.group(1))
            return

        classes = frozenset(a.get("class", "").split())
        parent = self._stack[-1][1] if self._stack else frozenset()
        self._stack.append((tag, classes))
        depth = len(self._stack)
        # "Mein Markt" fuellt die Seite erst im Browser. Was dort steht, zaehlt nicht
        # zu den Angeboten der Woche.
        if a.get("id") == "market-offer" and not self._skip_depth:
            self._skip_depth = depth
        if (tag == "div" and classes & {"prodcut-details", "product-details"}
                and self._offer is None and not self._skip_depth):
            self._offer, self._offer_depth = {}, depth
        elif tag == "div" and "logo-details" in classes and self._market is None:
            self._market, self._market_depth = {}, depth
        if tag == "button" and "button_cookie" in classes and self._market is not None:
            self._market.setdefault("id", a.get("name", "").strip())
        if self._capture:
            return                      # z. B. <span> in <h6>: Text gehoert zum Namen

        field = None
        if self._offer is not None:
            if tag == "h6":
                field = "name"
            elif tag == "div" and "product-desc" in classes:
                field = "description"
            elif tag == "div" and "product-content" in classes:
                field = "content"
            elif tag == "span" and "price" in parent:
                field = "price"
            elif tag == "del":
                field = "old_price"
        elif self._market is not None:
            if tag == "h5":
                field = "name"
            elif tag == "p" and "address" in parent:
                field = "address"
        if tag == "span" and "date" in classes:
            field = "date"
        if field:
            self._capture, self._buffer = (field, depth), []

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        # Ein Endtag schliesst bis zu seinem Element, auch wenn dazwischen ein
        # <p> oder <li> offen blieb. Ein Endtag ohne Gegenstueck zaehlt nicht.
        if tag in VOID_TAGS or all(open_tag != tag for open_tag, _ in self._stack):
            return
        while self._stack:
            depth = len(self._stack)
            open_tag, _ = self._stack.pop()
            self._closed(depth)
            if open_tag == tag:
                break

    def _closed(self, depth: int) -> None:
        if self._capture and self._capture[1] == depth:
            field = self._capture[0]
            text = " ".join("".join(self._buffer).split())
            self._capture = None
            if field == "date":
                self.dates.append(text)
            elif self._offer is not None:
                self._offer.setdefault(field, text)
            elif self._market is not None:
                self._market.setdefault(field, text)
        if self._offer is not None and depth == self._offer_depth:
            self.offers.append(self._offer)
            self._offer = None
        if self._market is not None and depth == self._market_depth:
            self.markets.append(self._market)
            self._market = None
        if depth == self._skip_depth:
            self._skip_depth = 0


def _parse(html: str) -> _Parser:
    parser = _Parser()
    parser.feed(html)
    parser.close()
    return parser


def euro(text: str) -> float | None:
    """'13.99 €', '13,99 €', '1.039,00 €' -> Betrag."""
    match = re.search(r"\d[\d.,]*", text)
    if not match:
        return None
    raw = match.group().rstrip(".,")
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+,\d{1,2}", raw):
        raw = raw.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d+,\d{1,2}", raw):
        raw = raw.replace(",", ".")
    elif not re.fullmatch(r"\d+(?:\.\d{1,2})?", raw):
        return None
    return float(raw)


def _offers(raw: list[dict[str, str]], where: str) -> list[LogoOffer]:
    offers: list[LogoOffer] = []
    broken = 0
    for item in raw:
        name = item.get("name", "")
        price = euro(item.get("price", ""))
        if not name.strip(" *") or price is None:
            broken += 1
            continue
        offers.append(LogoOffer(
            name=name.rstrip(" *"),
            description=item.get("description", ""),
            content=item.get("content", ""),
            price=price,
            old_price=euro(item["old_price"]) if item.get("old_price") else None,
            image=item.get("image", ""),
            regional=name.endswith("*"),
        ))
    if broken and not offers:
        raise LogoError(f"LOGO: keines von {broken} Angeboten lesbar ({where}). Seite geändert?")
    return offers


def _period(parser: _Parser) -> tuple[date, date] | None:
    """Zeitraum aus dem Kopf der Wochenangebote, sonst aus dem Handzettel (Mo-Sa)."""
    for text in parser.dates:
        match = DATE_RANGE.search(text)
        if not match:
            continue
        day1, month1, year1, day2, month2, year2 = match.groups()
        end_year = int(year2)
        start_year = int(year1) if year1 else end_year - (int(month1) > int(month2))
        try:
            start = date(start_year, int(month1), int(day1))
            end = date(end_year, int(month2), int(day2))
        except ValueError:
            continue
        if 0 <= (end - start).days <= MAX_WEEK_DAYS:
            return start, end
    for year, week in parser.weeks:
        try:
            return date.fromisocalendar(year, week, 1), date.fromisocalendar(year, week, 6)
        except ValueError:
            continue
    return None


def parse_offers_page(html: str) -> LogoPage:
    parser = _parse(html)
    markets = [LogoMarket(id=m["id"], name=" ".join(m["name"].split()), address=m.get("address", ""))
               for m in parser.markets if m.get("id") and m.get("name")]
    return LogoPage(markets=markets, period=_period(parser),
                    offers=_offers(parser.offers, "Angebotsseite"))


def parse_market_offers(payload: Any, market: str) -> list[LogoOffer] | None:
    """Antwort von admin-ajax.php. None: Der Markt hat keine eigene Liste."""
    if not isinstance(payload, dict) or not isinstance(payload.get("products"), str):
        raise LogoError(f"LOGO: keine gültige Antwort für den Markt {market}")
    html = payload["products"]
    # Dieselbe Grenze wie im Skript der Seite: bis 10 Zeichen gilt als leer.
    if len(html.strip()) <= 10:
        return None
    offers = _offers(_parse(html).offers, f"Markt {market}")
    if not offers:
        raise LogoError(f"LOGO: Angebote für den Markt {market} nicht lesbar. Seite geändert?")
    return offers


def _norm(name: str) -> str:
    return " ".join(name.split()).casefold()


def resolve_markets(available: list[LogoMarket], wanted: tuple[str, ...]) -> list[LogoMarket]:
    """Maerkte aus der Merkliste, gross/klein egal. Unbekannt: Fehler mit Vorschlaegen."""
    if not available:
        raise LogoError("LOGO: Marktliste nicht gefunden. Seite geändert?")
    by_name = {_norm(m.name): m for m in available}
    chosen: list[LogoMarket] = []
    for name in wanted:
        market = by_name.get(_norm(name))
        if market is None:
            similar = [m.name for m in available
                       if _norm(name) in _norm(m.name) or _norm(m.name) in _norm(name)]
            hint = (f" Ähnlich: {', '.join(similar[:5])}." if similar else
                    " Die Namen stehen in der Marktliste auf logo-getraenke.de/angebote.")
            raise LogoError(f"LOGO: Markt {name!r} nicht gefunden.{hint}")
        if market not in chosen:
            chosen.append(market)
    return chosen


# --- Abruf ------------------------------------------------------------------

class LogoClient:
    def __init__(self, offers_url: str = OFFERS_URL, ajax_url: str = AJAX_URL,
                 timeout: float = 10.0):
        self.offers_url = offers_url
        self.ajax_url = ajax_url
        self.timeout = timeout

    async def fetch(self, markets: tuple[str, ...] = ()) -> LogoWeek:
        """Angebote der Woche. Ohne Maerkte alle von der Seite, sonst je Markt."""
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        try:
            async with aiohttp.ClientSession(timeout=timeout,
                                             headers={"User-Agent": USER_AGENT}) as session:
                async with session.get(self.offers_url) as resp:
                    if resp.status != 200:
                        raise LogoError(f"LOGO: Angebotsseite antwortet mit HTTP {resp.status}")
                    html = await resp.text()
                page = parse_offers_page(html)
                if page.period is None:
                    raise LogoError("LOGO: Zeitraum der Wochenangebote nicht gefunden. "
                                    "Seite geändert?")
                if not page.offers:
                    raise LogoError("LOGO: keine Angebote auf der Seite gefunden. Seite geändert?")
                if not markets:
                    return LogoWeek(*page.period, {"": page.offers})
                chosen = resolve_markets(page.markets, markets)
                limiter = asyncio.Semaphore(PARALLEL_MARKETS)

                async def one(market: LogoMarket) -> list[LogoOffer] | None:
                    async with limiter:
                        return await self._market_offers(session, market)

                lists = await asyncio.gather(*(one(m) for m in chosen))
        except LogoError:
            raise
        except TimeoutError as exc:
            raise LogoError("LOGO antwortet nicht (Zeitlimit)") from exc
        except aiohttp.ClientError as exc:
            raise LogoError(f"LOGO nicht erreichbar: {exc}") from exc
        # Wie die Seite selbst: Ohne eigene Liste gelten alle Angebote der Woche.
        return LogoWeek(*page.period, {m.name: offers if offers is not None else page.offers
                                       for m, offers in zip(chosen, lists)})

    async def _market_offers(self, session: aiohttp.ClientSession,
                             market: LogoMarket) -> list[LogoOffer] | None:
        data = {"action": "my_product_view", "marketid": market.id}
        async with session.post(self.ajax_url, data=data) as resp:
            if resp.status != 200:
                raise LogoError(f"LOGO: HTTP {resp.status} bei den Angeboten für {market.name}")
            try:
                payload = await resp.json(content_type=None)
            except ValueError as exc:
                raise LogoError(f"LOGO: keine gültige Antwort für den Markt {market.name}") from exc
        return parse_market_offers(payload, market.name)
