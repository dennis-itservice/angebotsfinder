"""Prospekt-Check: Merkliste, Abgleich mit den Angeboten, Zeitplan, Texte.

Reine Funktionen ohne Netz und Uhr - die Abfrage steckt in backend/marktguru.py
und backend/logo.py, der Ablauf in backend/monitor.py. Dadurch ist hier alles
vollstaendig testbar.

Die Merkliste (config/angebote.yaml) pflegt jeder selbst. Der Angebotsfinder
liest sie bei jeder Aenderung neu ein, ohne Neustart. Ein Tippfehler darin legt
nichts lahm: Die Seite meldet ihn, und geprueft wird mit dem letzten gueltigen
Stand weiter.

Uebernommen aus Sentinel 2.3.1 (Prospekt-Check), dort seit Oktober 2026 im Einsatz.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta, timezone
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

if TYPE_CHECKING:
    from backend.logo import LogoWeek


def _berlin() -> ZoneInfo | None:
    """Zeitzonendaten kommen aus dem Paket tzdata (requirements.txt). Fehlen sie
    doch einmal, gilt die Systemzeit des Containers - der Angebotsfinder muss
    trotzdem starten."""
    try:
        return ZoneInfo("Europe/Berlin")
    except ZoneInfoNotFoundError:
        return None


# Die Gueltigkeit kommt in UTC. Angezeigt und verglichen wird in lokaler Zeit,
# wie datetime.now() im Container (TZ=Europe/Berlin).
TZ = _berlin()

WEEKDAYS = {"mo": 0, "di": 1, "mi": 2, "do": 3, "fr": 4, "sa": 5, "so": 6}
DAY_NAMES = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")
DEFAULT_DAYS = ("mo", "mi", "fr", "so")
DEFAULT_TIME = "08:00"
DEFAULT_DAYS_AHEAD = 14
MAX_PRODUCTS = 30
MAX_SHOPS = 30
MAX_LOGO_MARKETS = 10
PUSH_LINES = 5

LOGO = "logo"
"""LOGO Getraenkemarkt kommt nicht ueber marktguru, sondern direkt (backend/logo.py)."""
SOURCE_MARKTGURU = "marktguru"
SOURCE_LOGO = "logo"


class WatchlistError(ValueError):
    """angebote.yaml unbrauchbar. Die Meldung erscheint auf der Seite."""


class OfferFormatError(ValueError):
    """Kein einziges Angebot der Antwort war lesbar - das Format hat sich geaendert."""


# --- Merkliste ---------------------------------------------------------------

def format_liters(liters: float) -> str:
    return f"{liters:g}".replace(".", ",")


@dataclass(frozen=True)
class Pack:
    count: int
    liters: float

    def label(self) -> str:
        menge = f"{format_liters(self.liters)} l"
        return f"{self.count}×{menge}" if self.count > 1 else menge


@dataclass(frozen=True)
class Product:
    name: str
    pack: Pack | None = None

    @property
    def label(self) -> str:
        return f"{self.name} {self.pack.label()}" if self.pack else self.name


@dataclass(frozen=True)
class Schedule:
    days: tuple[int, ...]
    """Wochentage, 0 = Montag."""
    hour: int
    minute: int

    def text(self) -> str:
        tage = ", ".join(DAY_NAMES[d] for d in self.days)
        return f"{tage} um {self.hour:02d}:{self.minute:02d}"


@dataclass(frozen=True)
class Watchlist:
    zip_code: str
    shops: tuple[str, ...]
    """Leer = alle Geschaefte bei marktguru."""
    products: tuple[Product, ...]
    schedule: Schedule
    days_ahead: int = DEFAULT_DAYS_AHEAD
    push: bool = True
    logo_markets: tuple[str, ...] = ()
    """Maerkte fuer LOGO (- LOGO: [Schweinfurt, ...]). Leer: alle Angebote der Woche."""

    def fingerprint(self) -> str:
        """Aendert sich nur mit dem Inhalt - Kommentare und Leerzeilen zaehlen nicht."""
        raw = json.dumps(asdict(self), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    @property
    def queries(self) -> list[str]:
        """Suchbegriffe fuer marktguru, jeder nur einmal (Produkte mit zwei Packungen)."""
        return list(dict.fromkeys(p.name for p in self.products))

    @property
    def logo_shop(self) -> str | None:
        """LOGO so geschrieben wie in der Merkliste. None: LOGO steht nicht drin."""
        return next((s for s in self.shops if s.casefold() == LOGO), None)

    @property
    def uses_marktguru(self) -> bool:
        """Eine leere Liste heisst: alle Geschaefte bei marktguru. LOGO allein braucht
        marktguru nicht."""
        return not self.shops or any(s.casefold() != LOGO for s in self.shops)

    def shops_text(self) -> str:
        return ", ".join(self.shops) if self.shops else "alle Geschäfte"

    def shop_labels(self) -> list[str]:
        """Fuer die Seite: LOGO mit seinen Maerkten."""
        return [f"{s} ({', '.join(self.logo_markets)})" if s.casefold() == LOGO and self.logo_markets
                else s for s in self.shops]


PACK = re.compile(r"^(?:(\d{1,3})[x×*])?(\d+(?:[.,]\d+)?)(ml|l|liter)$")
TIME = re.compile(r"^(\d{1,2}):(\d{2})$")
ZIP = re.compile(r"^\d{5}$")
TOP_LEVEL = ("zip_code", "shops", "products", "schedule", "days_ahead", "push")


def parse_pack(raw: Any) -> Pack:
    """'12x1l', '6 x 0,5 l', '24x330ml', '1,5l' -> Anzahl und Liter je Flasche."""
    text = re.sub(r"\s+", "", str(raw)).lower()
    match = PACK.match(text)
    if not match:
        raise WatchlistError(f"pack {raw!r}: erwartet z. B. 12x1l, 6x0,5l oder 24x330ml")
    count = int(match.group(1) or 1)
    amount = float(match.group(2).replace(",", "."))
    liters = amount / 1000 if match.group(3) == "ml" else amount
    if count < 1 or not 0 < liters <= 100:
        raise WatchlistError(f"pack {raw!r}: Anzahl oder Menge unplausibel")
    return Pack(count, round(liters, 4))


def _parse_time(raw: Any) -> tuple[int, int]:
    if isinstance(raw, int) and not isinstance(raw, bool):
        # YAML 1.1 liest 8:00 ohne Anfuehrungszeichen als Zahl zur Basis 60 (480).
        hour, minute = divmod(raw, 60)
    else:
        match = TIME.match(str(raw).strip())
        if not match:
            raise WatchlistError(f"schedule.time {raw!r}: erwartet z. B. \"08:00\"")
        hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        raise WatchlistError(f"schedule.time {raw!r}: keine gültige Uhrzeit")
    return hour, minute


def _parse_schedule(raw: Any) -> Schedule:
    raw = {} if raw is None else raw
    if not isinstance(raw, dict):
        raise WatchlistError("schedule: erwartet days: und time:")
    unknown = sorted(set(map(str, raw)) - {"days", "time"})
    if unknown:
        raise WatchlistError(f"schedule.{unknown[0]}: unbekannter Eintrag (erlaubt: days, time)")
    days_raw = raw.get("days", list(DEFAULT_DAYS))
    if isinstance(days_raw, str):
        days_raw = re.split(r"[,\s]+", days_raw.strip())
    if not isinstance(days_raw, list) or not days_raw:
        raise WatchlistError("schedule.days: mindestens ein Tag, z. B. [mo, mi, fr, so]")
    days = set()
    for day in days_raw:
        # "Mo", "mo" und "Montag" gehen alle - die ersten zwei Buchstaben reichen.
        key = str(day).strip().lower()[:2]
        if key not in WEEKDAYS:
            raise WatchlistError(f"schedule.days: {day!r} ist kein Wochentag (mo di mi do fr sa so)")
        days.add(WEEKDAYS[key])
    hour, minute = _parse_time(raw.get("time", DEFAULT_TIME))
    return Schedule(tuple(sorted(days)), hour, minute)


def _parse_product(item: Any, index: int) -> Product:
    if isinstance(item, str):
        item = {"name": item}
    if not isinstance(item, dict):
        raise WatchlistError(f"products, Eintrag {index}: erwartet name: und optional pack:")
    unknown = sorted(set(map(str, item)) - {"name", "pack"})
    if unknown:
        raise WatchlistError(f"products, Eintrag {index}: unbekannter Eintrag {unknown[0]!r} "
                             f"(erlaubt: name, pack)")
    name = " ".join(str(item.get("name") or "").split())
    if not 2 <= len(name) <= 60:
        raise WatchlistError(f"products, Eintrag {index}: name fehlt oder ist zu lang")
    pack = item.get("pack")
    return Product(name, parse_pack(pack) if pack not in (None, "") else None)


def _parse_markets(raw: Any, shop: str) -> tuple[str, ...]:
    """- LOGO: [Schweinfurt, Sennfeld], - LOGO: Schweinfurt oder - LOGO: (alle)."""
    if raw is None:
        return ()
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        raise WatchlistError(f"shops: Märkte für {shop} in eckigen Klammern, "
                             f"z. B. - {shop}: [Schweinfurt, Sennfeld]")
    markets: dict[str, str] = {}
    for item in raw:
        name = " ".join(str(item if item is not None else "").split())
        if not name:
            raise WatchlistError(f"shops: leerer Markt bei {shop}")
        markets.setdefault(name.casefold(), name)
    if len(markets) > MAX_LOGO_MARKETS:
        raise WatchlistError(f"shops: höchstens {MAX_LOGO_MARKETS} Märkte bei {shop}")
    return tuple(markets.values())


def _parse_shops(raw: Any) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(Geschaefte, LOGO-Maerkte). Ein Geschaeft ist ein Name, nur LOGO darf
    Maerkte haben: - LOGO: [Schweinfurt, Sennfeld]."""
    raw = raw or []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        raise WatchlistError("shops: Liste von Geschäften, z. B. [EDEKA, REWE]")
    shops: dict[str, str] = {}
    markets: dict[str, str] = {}
    for entry in raw:
        if isinstance(entry, dict):
            if len(entry) != 1:
                raise WatchlistError("shops: je Zeile ein Geschäft, z. B. - LOGO: [Schweinfurt]")
            key, value = next(iter(entry.items()))
            name = " ".join(str(key if key is not None else "").split())
            if name.casefold() != LOGO:
                raise WatchlistError(f"shops: Märkte gibt es nur bei LOGO, nicht bei {name!r}")
            for market in _parse_markets(value, name):
                markets.setdefault(market.casefold(), market)
        else:
            name = " ".join(str(entry if entry is not None else "").split())
            if not name:
                raise WatchlistError("shops: leerer Eintrag")
            if name.casefold().startswith(LOGO + " "):
                # "LOGO Schweinfurt" oder "Logo Getränkemarkt" landeten sonst still bei marktguru.
                raise WatchlistError(f"shops: {name!r}. LOGO-Märkte so angeben: "
                                     f"- LOGO: [Schweinfurt, Sennfeld]")
        shops.setdefault(name.casefold(), name)
    if len(shops) > MAX_SHOPS:
        raise WatchlistError(f"shops: höchstens {MAX_SHOPS} Geschäfte")
    return tuple(shops.values()), tuple(markets.values())


def parse_watchlist(text: str) -> Watchlist:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" in Zeile {mark.line + 1}" if mark is not None else ""
        problem = getattr(exc, "problem", None) or str(exc)
        raise WatchlistError(f"kein gültiges YAML{where} ({problem})") from exc
    if data is None:
        raise WatchlistError("Datei ist leer")
    if not isinstance(data, dict):
        raise WatchlistError("erwartet werden Einträge wie zip_code:, shops: und products:")
    unknown = sorted(set(map(str, data)) - set(TOP_LEVEL))
    if unknown:
        raise WatchlistError(f"unbekannter Eintrag {unknown[0]!r} (erlaubt: {', '.join(TOP_LEVEL)})")

    zip_raw = data.get("zip_code")
    zip_code = f"{zip_raw:05d}" if isinstance(zip_raw, int) else str(zip_raw or "").strip()
    if not ZIP.match(zip_code):
        raise WatchlistError(f"zip_code {zip_raw!r}: fünfstellige Postleitzahl in "
                             f"Anführungszeichen, z. B. \"97421\"")

    shops, logo_markets = _parse_shops(data.get("shops"))

    products_raw = data.get("products")
    if not isinstance(products_raw, list) or not products_raw:
        raise WatchlistError("products: mindestens ein Produkt, z. B. - name: Monster Energy")
    products: dict[tuple, Product] = {}
    for index, item in enumerate(products_raw, start=1):
        product = _parse_product(item, index)
        products.setdefault((product.name.casefold(), product.pack), product)
    if len(products) > MAX_PRODUCTS:
        raise WatchlistError(f"products: höchstens {MAX_PRODUCTS} Produkte")

    days_ahead = data.get("days_ahead", DEFAULT_DAYS_AHEAD)
    if isinstance(days_ahead, bool) or not isinstance(days_ahead, int) or not 0 <= days_ahead <= 60:
        raise WatchlistError(f"days_ahead {days_ahead!r}: ganze Zahl von 0 bis 60")
    push = data.get("push", True)
    if not isinstance(push, bool):
        raise WatchlistError(f"push {push!r}: true oder false")

    return Watchlist(zip_code=zip_code, shops=shops,
                     products=tuple(products.values()),
                     schedule=_parse_schedule(data.get("schedule")),
                     days_ahead=days_ahead, push=push, logo_markets=logo_markets)


def read_watchlist_text(raw: bytes) -> str:
    """UTF-8 (mit oder ohne BOM). Der Windows-Editor speichert manchmal noch ANSI."""
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


# --- Angebote aus der Antwort von marktguru ----------------------------------

@dataclass(frozen=True)
class Offer:
    id: str
    shop: str
    brand: str
    product: str
    description: str
    price: float
    old_price: float | None
    reference_price: float | None
    unit: str | None
    quantity: float | None
    volume: float | None
    valid_from: datetime
    """Lokale Zeit ohne Zeitzone, wie datetime.now()."""
    valid_to: datetime
    app_only: bool
    """Preis gilt nur mit Kundenkarte oder App (Lidl Plus, ...)."""
    source: str = SOURCE_MARKTGURU
    markets: tuple[str, ...] = ()
    """LOGO: die gewaehlten Maerkte mit diesem Angebot. Leer ohne Marktwahl."""
    regional: bool = False
    """LOGO: Sternchen, nicht in allen Maerkten verfuegbar."""
    condition: str = ""
    """LOGO: Stufe einer Mengenstaffel, z. B. 'ab 3 Kästen'. Der Preis gilt je Kasten."""

    @property
    def text(self) -> str:
        return f"{self.brand} {self.product} {self.description}"

    @property
    def shop_label(self) -> str:
        """'LOGO Schweinfurt, Sennfeld': wo es das Angebot gibt."""
        return f"{self.shop} {', '.join(self.markets)}" if self.markets else self.shop

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["valid_from"] = self.valid_from.isoformat(timespec="minutes")
        data["valid_to"] = self.valid_to.isoformat(timespec="minutes")
        data["markets"] = list(self.markets)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Offer":
        return cls(**{**data, "valid_from": datetime.fromisoformat(data["valid_from"]),
                      "valid_to": datetime.fromisoformat(data["valid_to"]),
                      "markets": tuple(data.get("markets") or ())})


def _local(value: str) -> datetime:
    moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    # TZ None (ohne Zeitzonendaten): astimezone(None) nimmt die Systemzeit.
    return moment.astimezone(TZ).replace(tzinfo=None)


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def offers_from_marktguru(results: list[dict[str, Any]]) -> list[Offer]:
    """Ein Offer je Angebot und Geschaeft. Einzelne kaputte Eintraege werden
    uebersprungen. Ist keiner lesbar, hat sich das Format geaendert - dann
    lieber laut scheitern als still "keine Angebote" melden."""
    offers: list[Offer] = []
    broken = 0
    for raw in results:
        try:
            dates = [(_local(v["from"]), _local(v["to"])) for v in raw["validityDates"]]
            price = float(raw["price"])
            shops = [str(a["name"]).strip() for a in raw.get("advertisers") or [] if a.get("name")]
            if not dates or not shops:
                raise ValueError("ohne Zeitraum oder Geschäft")
        except (KeyError, TypeError, ValueError, AttributeError):
            broken += 1
            continue
        for shop in shops:
            offers.append(Offer(
                id=str(raw.get("id", "")),
                shop=shop,
                brand=str((raw.get("brand") or {}).get("name") or "").strip(),
                product=str((raw.get("product") or {}).get("name") or "").strip(),
                description=" ".join(str(raw.get("description") or "").split()),
                price=price,
                old_price=_number(raw.get("oldPrice")),
                reference_price=_number(raw.get("referencePrice")),
                unit=(raw.get("unit") or {}).get("shortName"),
                quantity=_number(raw.get("quantity")),
                volume=_number(raw.get("volume")),
                valid_from=min(d[0] for d in dates),
                valid_to=max(d[1] for d in dates),
                app_only=bool(raw.get("requiresLoyalityMembership")),
            ))
    if results and not offers and broken:
        raise OfferFormatError(f"marktguru: keines von {broken} Angeboten lesbar. Format geändert?")
    return offers


def offers_from_logo(shop: str, week: LogoWeek) -> list[Offer]:
    """Ein Offer je Angebot, auch wenn es in mehreren Maerkten gilt. Dabei stehen
    die gewaehlten Maerkte, in denen es gilt. Zeitraum: die Woche von der Seite,
    ganztags wie bei marktguru."""
    valid_from = datetime.combine(week.valid_from, time(0, 0))
    valid_to = datetime.combine(week.valid_to, time(23, 59))
    first: dict[tuple, Any] = {}
    where: dict[tuple, list[str]] = {}
    for market, items in week.by_market.items():
        for item in items:
            first.setdefault(item.key, item)
            present = where.setdefault(item.key, [])
            if market not in present:
                present.append(market)
    offers = []
    for key, item in first.items():
        count, liters = item.pack or (None, None)
        present = where[key]
        offers.append(Offer(
            id=f"logo-{item.image}" if item.image else "logo",
            shop=shop,
            brand="",
            product=item.name,
            description=" · ".join(part for part in (item.description, item.content) if part),
            price=item.price,
            old_price=item.old_price,
            reference_price=item.reference_price,
            unit="l",
            quantity=count,
            volume=liters,
            valid_from=valid_from,
            valid_to=valid_to,
            app_only=False,
            source=SOURCE_LOGO,
            markets=tuple(market for market in present if market),
            regional=item.regional,
            condition=item.condition or "",
        ))
    return offers


# --- Abgleich ----------------------------------------------------------------

def normalize(text: str) -> str:
    """Kleinschreibung, Bindestriche wie Leerzeichen: COCA-COLA -> coca cola."""
    return " ".join(text.casefold().replace("-", " ").split())


def name_pattern(name: str) -> re.Pattern:
    """Der Name als ganzes Wort: 'Monster Energy' passt nicht auf 'Monster Jam Truck',
    'Coca-Cola' nicht auf 'Pepsi Cola'."""
    return re.compile(r"(?<!\w)" + re.escape(normalize(name)) + r"(?!\w)")


def _number_variants(liters: float) -> list[str]:
    base = f"{liters:g}"                          # 1, 0.5, 0.33, 1.5
    variants = {base, base.replace(".", ",")}
    if "." in base:
        variants |= {base + "0", base.replace(".", ",") + "0"}            # 0,50
    else:
        variants |= {f"{base},0", f"{base}.0", f"{base},00", f"{base}.00"}
    return sorted(variants, key=len, reverse=True)


def pack_pattern(pack: Pack) -> re.Pattern:
    """Packung im Beschreibungstext: '12 x 1 l', '12x1,0-l', '24 x 330 ml', ..."""
    liters = "|".join(re.escape(v) for v in _number_variants(pack.liters))
    ml = round(pack.liters * 1000)
    menge = rf"(?:(?:{liters})\s*l(?:iter|tr)?|{ml}\s*ml)(?![a-zäöü])"
    if pack.count > 1:
        return re.compile(rf"(?<![\d,.]){pack.count}\s*[x×*]\s*{menge}")
    # Einzelne Flasche: nicht in "6 x 1,5 l" hineinlesen.
    return re.compile(rf"(?<![\d,.x×*])(?<![x×*]\s){menge}")


def pack_matches(pack: Pack, offer: Offer) -> bool:
    if (offer.quantity is not None and offer.volume is not None
            and (offer.unit or "l").lower() == "l"
            and round(offer.quantity) == pack.count and abs(offer.volume - pack.liters) < 0.005):
        return True
    return bool(pack_pattern(pack).search(normalize(offer.description)))


@dataclass(frozen=True)
class Match:
    product: str
    """Bezeichnung aus der Merkliste, z. B. 'Coca-Cola 12×1 l'."""
    offer: Offer

    @property
    def key(self) -> str:
        """Kennung fuer "schon gemeldet". Haengt nicht an der id von marktguru:
        dasselbe Angebot steht dort oft zweimal (z. B. "Cola" und "Original Taste")."""
        o = self.offer
        return "|".join((normalize(self.product), o.shop.casefold(), f"{o.price:.2f}",
                         o.valid_from.isoformat(timespec="minutes"),
                         o.valid_to.isoformat(timespec="minutes")))

    def is_current(self, now: datetime) -> bool:
        return self.offer.valid_from <= now

    def to_dict(self) -> dict[str, Any]:
        return {"product": self.product, "offer": self.offer.to_dict()}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Match":
        return cls(product=str(data["product"]), offer=Offer.from_dict(data["offer"]))


def find_matches(watchlist: Watchlist, offers_by_query: dict[str, list[Offer]],
                 now: datetime) -> list[Match]:
    """Alle Treffer der Merkliste: Produkt passt, Geschaeft passt, gilt jetzt oder
    beginnt innerhalb von days_ahead Tagen. Reihenfolge wie in der Merkliste,
    laufende vor kommenden, dann nach Preis."""
    shops = {s.casefold() for s in watchlist.shops}
    horizon = now + timedelta(days=watchlist.days_ahead)
    found: dict[str, tuple[int, Match]] = {}
    for order, product in enumerate(watchlist.products):
        pattern = name_pattern(product.name)
        for offer in offers_by_query.get(product.name, []):
            if shops and offer.shop.casefold() not in shops:
                continue
            if offer.valid_to < now or offer.valid_from > horizon:
                continue
            if not pattern.search(normalize(offer.text)):
                continue
            if product.pack and not pack_matches(product.pack, offer):
                continue
            match = Match(product.label, offer)
            found.setdefault(match.key, (order, match))
    ordered = sorted(found.values(), key=lambda om: (
        om[0], not om[1].is_current(now), om[1].offer.price, om[1].offer.valid_from,
        om[1].offer.shop.casefold()))
    return [match for _, match in ordered]


# --- Zeitplan ----------------------------------------------------------------

def last_slot(schedule: Schedule, now: datetime) -> datetime:
    """Juengster geplanter Termin, der nicht in der Zukunft liegt."""
    for back in range(8):
        slot = (now - timedelta(days=back)).replace(hour=schedule.hour, minute=schedule.minute,
                                                    second=0, microsecond=0)
        if slot.weekday() in schedule.days and slot <= now:
            return slot
    raise ValueError("Zeitplan ohne Wochentag")


def next_slot(schedule: Schedule, now: datetime) -> datetime:
    for ahead in range(8):
        slot = (now + timedelta(days=ahead)).replace(hour=schedule.hour, minute=schedule.minute,
                                                     second=0, microsecond=0)
        if slot.weekday() in schedule.days and slot > now:
            return slot
    raise ValueError("Zeitplan ohne Wochentag")


# --- Texte -------------------------------------------------------------------

def format_price(value: float) -> str:
    return f"{value:.2f} €".replace(".", ",")


def format_day(moment: datetime) -> str:
    return f"{DAY_NAMES[moment.weekday()]} {moment.day:02d}.{moment.month:02d}."


def validity_text(offer: Offer, now: datetime) -> str:
    if offer.valid_from <= now:
        return f"bis {format_day(offer.valid_to)}"
    return f"{format_day(offer.valid_from)}–{format_day(offer.valid_to)}"


def match_line(match: Match, now: datetime, with_product: bool = True) -> str:
    o = match.offer
    price = f"{format_price(o.price)} {o.condition}" if o.condition else format_price(o.price)
    line = f"{o.shop_label} {price} ({validity_text(o, now)})"
    if o.app_only:
        line += " mit App"
    if o.regional:
        line += " nicht in allen Märkten"
    return f"{match.product}: {line}" if with_product else line


def offers_payload(matches: list[Match], now: datetime) -> dict[str, Any]:
    """Push fuer neue Treffer. Ein Produkt: 'Angebot: Monster Energy', darunter je
    Geschaeft eine Zeile. Mehrere Produkte: Produkt vorn in jeder Zeile."""
    products = list(dict.fromkeys(m.product for m in matches))
    mehrere = len(products) > 1
    lines = [match_line(m, now, with_product=mehrere) for m in matches[:PUSH_LINES]]
    if len(matches) > PUSH_LINES:
        lines.append(f"… und {len(matches) - PUSH_LINES} weitere")
    if not mehrere:
        title = f"Angebot: {products[0]}"
    elif len(products) <= 3:
        title = f"Angebote: {', '.join(products)}"
    else:
        title = f"Angebote: {', '.join(products[:2])} und {len(products) - 2} weitere"
    return {"title": title, "body": "\n".join(lines), "tag": "angebote", "url": "/",
            "kind": "info"}


def summary_message(watchlist: Watchlist, matches: list[Match], now: datetime) -> str:
    """Eine Zeile fuer die Statuszeile oben auf der Seite."""
    if not matches:
        namen = ", ".join(p.label for p in watchlist.products[:3])
        if len(watchlist.products) > 3:
            namen += f" und {len(watchlist.products) - 3} weitere"
        return f"Kein Angebot für {namen}"
    text = " · ".join(match_line(m, now) for m in matches[:2])
    if len(matches) > 2:
        text += f" und {len(matches) - 2} weitere"
    return text
