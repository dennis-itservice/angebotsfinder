"""Prospekt-Check: Merkliste, Abgleich, Zeitplan und Texte - ohne Netz und Uhr.

Die Angebote stammen aus echten Antworten von marktguru (PLZ 97421, 10.10.2026,
gekuerzt in tests/fixtures). Darin stehen auch die Fallen: "Monster Jam"-Trucks
und Mon Chéri bei der Suche nach "monster", Pepsi-Cola und "ja! Cola" bei "cola",
und EDEKA Foodservice (Grosshandel) als einziger 12x1-l-Kasten Coca-Cola.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

import pytest

from backend.angebote import (Match, Offer, OfferFormatError, Pack, Product, Schedule, Watchlist,
                              WatchlistError, find_matches, last_slot, match_line, next_slot,
                              offers_from_logo, offers_from_marktguru, offers_payload,
                              pack_matches, parse_pack, parse_watchlist, read_watchlist_text,
                              summary_message, validity_text)
from backend.logo import LogoOffer, LogoWeek, parse_market_offers, parse_offers_page

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
JETZT = datetime(2026, 10, 10, 12, 0)   # Samstag


def fixture(name: str) -> list[dict]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))["results"]


COLA = fixture("marktguru_cola.json")
MONSTER = fixture("marktguru_monster.json")


def merkliste(shops=("EDEKA", "REWE"), products=None, days_ahead=14) -> Watchlist:
    return Watchlist(
        zip_code="97421", shops=tuple(shops),
        products=tuple(products or (Product("Monster Energy"),
                                    Product("Coca-Cola", Pack(12, 1.0)))),
        schedule=Schedule((0, 2, 4, 6), 8, 0), days_ahead=days_ahead)


def angebote(monster=MONSTER, cola=COLA) -> dict[str, list[Offer]]:
    """So, wie der Monitor sie je Suchbegriff (= Produktname) bekommt."""
    return {"Monster Energy": offers_from_marktguru(monster),
            "Coca-Cola": offers_from_marktguru(cola)}


def roh(**ueber) -> dict:
    """Ein Angebot im Format von marktguru, Standard: REWE, 12 x 1 l Coca-Cola."""
    daten = {
        "id": 1, "brand": {"name": "Coca-Cola"}, "advertisers": [{"name": "REWE"}],
        "product": {"name": "Erfrischungsgetränk"},
        "description": "versch. Sorten, 12 x 1-l-Fl.-Kasten zzgl. 3.30 Pfand",
        "price": 9.99, "oldPrice": 13.49, "referencePrice": 0.83, "quantity": 12.0,
        "volume": 1.0, "unit": {"shortName": "l"},
        "validityDates": [{"from": "2026-10-11T22:00:00Z", "to": "2026-10-17T21:59:00Z"}],
        "requiresLoyalityMembership": False,
    }
    daten.update(ueber)
    return daten


# --- Merkliste ---------------------------------------------------------------

VORLAGE = (ROOT / "config" / "angebote.example.yaml").read_text(encoding="utf-8")


def test_vorlage_ist_gueltig():
    """Wer sie nur kopiert, hat eine funktionierende Merkliste."""
    w = parse_watchlist(VORLAGE)
    assert w.zip_code == "97421"
    assert w.shops == ("EDEKA", "REWE", "Lidl")
    assert (w.logo_shop, w.logo_markets) == (None, ())
    assert [p.label for p in w.products] == ["Monster Energy", "Coca-Cola 12×1 l"]
    assert w.schedule == Schedule((0, 2, 4, 6), 8, 0)
    assert w.schedule.text() == "Mo, Mi, Fr, So um 08:00"
    assert (w.days_ahead, w.push) == (14, True)


def test_logo_zeile_der_vorlage_laesst_sich_einschalten():
    """Die Vorlage sagt: das # vor der LOGO-Zeile entfernen. Dann muss es gehen."""
    zeile = "  # - LOGO: [Schweinfurt, Sennfeld]"
    assert zeile in VORLAGE.splitlines()
    w = parse_watchlist(VORLAGE.replace(zeile, "  - LOGO: [Schweinfurt, Sennfeld]"))
    assert w.shops == ("EDEKA", "REWE", "Lidl", "LOGO")
    assert w.logo_markets == ("Schweinfurt", "Sennfeld")


def test_kurzform_und_standardwerte():
    w = parse_watchlist('zip_code: "97424"\nproducts:\n  - Kaffee\n  - name: Bier\n    pack: 20x0,5l\n')
    assert w.shops == ()                       # leer = alle Geschaefte
    assert w.shops_text() == "alle Geschäfte"
    assert [p.label for p in w.products] == ["Kaffee", "Bier 20×0,5 l"]
    assert w.schedule.text() == "Mo, Mi, Fr, So um 08:00"


@pytest.mark.parametrize("roh_,erwartet", [
    ("12x1l", Pack(12, 1.0)),
    ("12 x 1 l", Pack(12, 1.0)),
    ("12×1,0l", Pack(12, 1.0)),
    ("6x0,5l", Pack(6, 0.5)),
    ("24x330ml", Pack(24, 0.33)),
    ("1,5l", Pack(1, 1.5)),
    ("20 X 0.5 Liter", Pack(20, 0.5)),
])
def test_packungen(roh_, erwartet):
    assert parse_pack(roh_) == erwartet


@pytest.mark.parametrize("roh_", ["12x1", "zwölf", "0x1l", "12x0l", "12x1kg"])
def test_ungueltige_packungen(roh_):
    with pytest.raises(WatchlistError, match="pack"):
        parse_pack(roh_)


def test_packung_als_text():
    assert Pack(12, 1.0).label() == "12×1 l"
    assert Pack(24, 0.33).label() == "24×0,33 l"
    assert Pack(1, 1.5).label() == "1,5 l"


@pytest.mark.parametrize("text,muster", [
    ("", "leer"),
    ("- a\n- b\n", "Einträge"),
    ('zip_code: "9742"\nproducts: [x1]', "zip_code"),
    ('zip_code: "97421"\n', "products"),
    ('zip_code: "97421"\nproducts: []', "products"),
    ('zip_code: "97421"\nshop: [EDEKA]\nproducts: [Cola]', "unbekannter Eintrag 'shop'"),
    ('zip_code: "97421"\nproducts: [{name: Cola, packung: 12x1l}]', "packung"),
    ('zip_code: "97421"\nproducts: [{name: Cola, pack: 12x1}]', "pack"),
    ('zip_code: "97421"\nproducts: [Cola]\nschedule: {days: [mo, xy]}', "xy"),
    ('zip_code: "97421"\nproducts: [Cola]\nschedule: {days: []}', "mindestens ein Tag"),
    ('zip_code: "97421"\nproducts: [Cola]\nschedule: {time: "25:00"}', "Uhrzeit"),
    ('zip_code: "97421"\nproducts: [Cola]\ndays_ahead: 100', "days_ahead"),
    ('zip_code: "97421"\nproducts: [Cola]\npush: ja bitte', "push"),
    ('zip_code: "97421"\nproducts: [Cola\n', "Zeile"),
])
def test_fehler_in_der_merkliste_sind_verstaendlich(text, muster):
    with pytest.raises(WatchlistError, match=muster):
        parse_watchlist(text)


def test_uhrzeit_ohne_anfuehrungszeichen():
    """YAML 1.1 liest 8:00 als Zahl zur Basis 60 (480) - das muss trotzdem 08:00 sein."""
    w = parse_watchlist('zip_code: "97421"\nproducts: [Cola]\nschedule:\n  time: 8:00\n')
    assert (w.schedule.hour, w.schedule.minute) == (8, 0)
    w = parse_watchlist('zip_code: 97421\nproducts: [Cola]\nschedule:\n  time: 18:30\n')
    assert (w.schedule.hour, w.schedule.minute, w.zip_code) == (18, 30, "97421")


def test_wochentage_auch_ausgeschrieben():
    w = parse_watchlist('zip_code: "97421"\nproducts: [Cola]\n'
                        'schedule: {days: [Montag, Mittwoch, Freitag, Sonntag]}')
    assert w.schedule.days == (0, 2, 4, 6)


def test_doppelte_eintraege_zaehlen_einmal():
    w = parse_watchlist('zip_code: "97421"\nshops: [REWE, rewe, EDEKA]\n'
                        'products: [Cola, cola, {name: Cola, pack: 12x1l}]')
    assert w.shops == ("REWE", "EDEKA")
    assert [p.label for p in w.products] == ["Cola", "Cola 12×1 l"]
    assert w.queries == ["Cola"]               # eine Suche fuer beide Packungen


def test_fingerabdruck_ignoriert_kommentare():
    a = parse_watchlist('zip_code: "97421"\nproducts: [Cola]\n')
    b = parse_watchlist('# Kommentar\nzip_code: "97421"   # Schweinfurt\n\nproducts:\n  - Cola\n')
    c = parse_watchlist('zip_code: "97421"\nproducts: [Cola, Bier]\n')
    assert a.fingerprint() == b.fingerprint() != c.fingerprint()


def test_ansi_datei_aus_dem_windows_editor():
    assert read_watchlist_text("shops: [ALDI SÜD]".encode("cp1252")) == "shops: [ALDI SÜD]"
    assert read_watchlist_text("﻿a: ü".encode("utf-8")) == "a: ü"


def mit_shops(zeilen: str) -> Watchlist:
    return parse_watchlist(f'zip_code: "97422"\nshops:\n{zeilen}\nproducts: [Cola]\n')


def test_logo_mit_maerkten():
    w = mit_shops("  - EDEKA\n  - LOGO: [Dittelbrunn, schweinfurt, Sennfeld, dittelbrunn]")
    assert w.shops == ("EDEKA", "LOGO")
    assert w.logo_markets == ("Dittelbrunn", "schweinfurt", "Sennfeld")
    assert (w.logo_shop, w.uses_marktguru) == ("LOGO", True)
    assert w.shop_labels() == ["EDEKA", "LOGO (Dittelbrunn, schweinfurt, Sennfeld)"]
    assert w.shops_text() == "EDEKA, LOGO"


@pytest.mark.parametrize("zeile,maerkte", [
    ("  - LOGO", ()),
    ("  - LOGO:", ()),
    ("  - Logo: Dittelbrunn", ("Dittelbrunn",)),
])
def test_logo_schreibweisen(zeile, maerkte):
    w = mit_shops(zeile)
    assert w.logo_shop is not None and w.logo_markets == maerkte
    assert not w.uses_marktguru                     # LOGO allein braucht marktguru nicht


def test_logo_in_der_kurzschreibweise():
    w = parse_watchlist('zip_code: "97422"\nshops: [REWE, LOGO: [Dittelbrunn, Sennfeld]]\n'
                        'products: [Cola]\n')
    assert (w.shops, w.logo_markets) == (("REWE", "LOGO"), ("Dittelbrunn", "Sennfeld"))


def test_ohne_logo():
    w = mit_shops("  - REWE")
    assert (w.logo_shop, w.logo_markets, w.uses_marktguru) == (None, (), True)
    assert parse_watchlist('zip_code: "97422"\nproducts: [Cola]\n').uses_marktguru   # alle


def test_andere_maerkte_aendern_den_fingerabdruck():
    a = mit_shops("  - LOGO: [Dittelbrunn]")
    b = mit_shops("  - LOGO: [Dittelbrunn, Sennfeld]")
    assert a.fingerprint() != b.fingerprint()


@pytest.mark.parametrize("zeilen,muster", [
    ("  - LOGO Dittelbrunn", "LOGO-Märkte so angeben: - LOGO: \\[Schweinfurt"),
    ("  - Logo Getränkemarkt", "LOGO-Märkte so angeben"),
    ("  - REWE: [Schweinfurt]", "nur bei LOGO, nicht bei 'REWE'"),
    ("  - {LOGO: [Dittelbrunn], REWE: x}", "je Zeile ein Geschäft"),
    ("  - LOGO: {Dittelbrunn: 1}", "eckigen Klammern"),
    ("  - LOGO: [Dittelbrunn, '']", "leerer Markt bei LOGO"),
    ("  - LOGO: [a, b, c, d, e, f, g, h, i, j, k]", "höchstens 10 Märkte"),
])
def test_fehler_bei_logo_sind_verstaendlich(zeilen, muster):
    with pytest.raises(WatchlistError, match=muster):
        mit_shops(zeilen)


# --- Angebote lesen ----------------------------------------------------------

def test_angebote_aus_der_echten_antwort():
    lidl = offers_from_marktguru(MONSTER)[0]
    assert (lidl.shop, lidl.brand, lidl.price, lidl.app_only) == ("Lidl", "Monster Energy", 0.75, True)
    assert (lidl.quantity, lidl.volume, lidl.unit, lidl.reference_price) == (1.0, 0.5, "l", 1.5)
    # 2026-10-11T22:00Z ist Montag 00:00 in Berlin (Sommerzeit), bis Samstag 23:59
    assert lidl.valid_from == datetime(2026, 10, 12, 0, 0)
    assert lidl.valid_to == datetime(2026, 10, 17, 23, 59)


def test_winterzeit_wird_beruecksichtigt():
    """Ab 25.10.2026 gilt MEZ: 23:00 UTC ist dann Mitternacht."""
    aldi = [o for o in offers_from_marktguru(MONSTER) if o.shop == "ALDI SÜD"][0]
    assert aldi.valid_from == datetime(2026, 10, 26, 0, 0)


def test_ein_angebot_je_geschaeft():
    offers = offers_from_marktguru([roh(advertisers=[{"name": "REWE"}, {"name": "nahkauf"}])])
    assert [o.shop for o in offers] == ["REWE", "nahkauf"]


def test_ohne_zeitzonendaten_gilt_die_systemzeit(monkeypatch):
    """Ohne tzdata startet der Angebotsfinder trotzdem und rechnet in Systemzeit."""
    import backend.angebote as angebote_modul
    from datetime import timezone
    monkeypatch.setattr(angebote_modul, "TZ", None)
    lidl = offers_from_marktguru(MONSTER)[0]
    erwartet = datetime(2026, 10, 11, 22, 0, tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
    assert lidl.valid_from == erwartet


def test_kaputte_eintraege_werden_uebersprungen():
    offers = offers_from_marktguru([roh(), {"id": 2, "price": None}, roh(validityDates=[])])
    assert len(offers) == 1


def test_ganz_unlesbare_antwort_ist_ein_fehler():
    """Lieber laut scheitern als still "keine Angebote" melden."""
    with pytest.raises(OfferFormatError):
        offers_from_marktguru([{"id": 1, "preis": 1}, {"id": 2}])
    assert offers_from_marktguru([]) == []


# --- Abgleich ----------------------------------------------------------------

def test_edeka_und_rewe_haben_heute_keinen_treffer():
    """Stand 10.10.2026: Monster gibt es nur bei Lidl, den 12x1-l-Kasten nur bei
    EDEKA Foodservice - das ist Grosshandel und nicht EDEKA."""
    assert find_matches(merkliste(), angebote(), JETZT) == []


def test_mit_allen_geschaeften_genau_die_richtigen():
    treffer = find_matches(merkliste(shops=()), angebote(), JETZT)
    assert [(m.product, m.offer.shop, m.offer.price) for m in treffer] == [
        ("Monster Energy", "Lidl", 0.75),
        ("Coca-Cola 12×1 l", "EDEKA Foodservice", 9.79),
    ]


def test_monster_jam_und_mon_cheri_sind_kein_monster_energy():
    nur_monster = merkliste(shops=(), products=[Product("Monster Energy")])
    treffer = find_matches(nur_monster, angebote(), JETZT)
    assert [m.offer.brand for m in treffer] == ["Monster Energy"]


def test_coca_cola_ohne_packung_ohne_pepsi_und_ja_cola():
    cola = merkliste(shops=(), products=[Product("Coca-Cola")])
    treffer = find_matches(cola, {"Coca-Cola": offers_from_marktguru(COLA)}, JETZT)
    marken = {m.offer.brand for m in treffer}
    assert marken == {"Coca-Cola"}
    assert len(treffer) == 8          # 10 Coca-Cola-Eintraege, zwei davon doppelt bei REWE
    assert not any("Pepsi" in m.offer.text or "ja!" in m.offer.brand for m in treffer)


def test_geschaeft_muss_genau_passen():
    """'EDEKA' ist nicht 'EDEKA Foodservice' - Gross- und Kleinschreibung egal."""
    w = merkliste(shops=("edeka foodservice",), products=[Product("Coca-Cola", Pack(12, 1.0))])
    treffer = find_matches(w, angebote(), JETZT)
    assert [m.offer.shop for m in treffer] == ["EDEKA Foodservice"]


def test_mehrere_geschaefte_werden_alle_gezeigt():
    rewe = roh(id=1)
    edeka = roh(id=2, advertisers=[{"name": "EDEKA"}], price=9.49)
    w = merkliste(products=[Product("Coca-Cola", Pack(12, 1.0))])
    treffer = find_matches(w, {"Coca-Cola": offers_from_marktguru([rewe, edeka])}, JETZT)
    assert [(m.offer.shop, m.offer.price) for m in treffer] == [("EDEKA", 9.49), ("REWE", 9.99)]


def test_doppelt_gelistetes_angebot_zaehlt_einmal():
    """REWE fuehrt dieselbe Cola als "Cola" und als "Original Taste"."""
    a = roh(id=1, product={"name": "Cola"})
    b = roh(id=2, product={"name": "Original Taste"})
    w = merkliste(products=[Product("Coca-Cola")])
    assert len(find_matches(w, {"Coca-Cola": offers_from_marktguru([a, b])}, JETZT)) == 1


@pytest.mark.parametrize("menge,beschreibung,passt", [
    ((12.0, 1.0), "", True),                                   # strukturiert
    ((1.0, 12.0), "12 x 1,0-l-Flasche je Kasten", True),       # nur im Text
    ((None, None), "Kasten 12 x 1 Ltr.", True),
    ((None, None), "je 12x1l Glas", True),
    ((6.0, 1.0), "6 x 1 l", False),
    ((12.0, 0.5), "12 x 0,5 l", False),
    ((None, None), "12 x 1,5 l", False),
    ((None, None), "112 x 1 l", False),
])
def test_packung_im_angebot(menge, beschreibung, passt):
    offer = offers_from_marktguru([roh(quantity=menge[0], volume=menge[1],
                                       description=beschreibung)])[0]
    assert pack_matches(Pack(12, 1.0), offer) is passt


def test_einzelflasche_ist_kein_sechserpack():
    einzeln = offers_from_marktguru([roh(quantity=None, volume=None, description="je 1,5-l-Fl.")])[0]
    sechser = offers_from_marktguru([roh(quantity=None, volume=None, description="6 x 1,5 l")])[0]
    assert pack_matches(Pack(1, 1.5), einzeln)
    assert not pack_matches(Pack(1, 1.5), sechser)


def test_milliliter():
    dose = offers_from_marktguru([roh(quantity=None, volume=None, description="24 x 330 ml Dose")])[0]
    assert pack_matches(Pack(24, 0.33), dose)


def test_laufend_kommend_und_zu_weit_weg():
    laeuft = roh(id=1, validityDates=[{"from": "2026-10-04T22:00:00Z", "to": "2026-10-10T21:59:00Z"}])
    abgelaufen = roh(id=2, price=8.88,
                     validityDates=[{"from": "2026-09-27T22:00:00Z", "to": "2026-10-03T21:59:00Z"}])
    bald = roh(id=3, price=9.49)
    zu_spaet = roh(id=4, price=7.77,
                   validityDates=[{"from": "2026-11-01T23:00:00Z", "to": "2026-11-07T22:59:00Z"}])
    w = merkliste(products=[Product("Coca-Cola")])
    treffer = find_matches(w, {"Coca-Cola": offers_from_marktguru([bald, zu_spaet, abgelaufen,
                                                                     laeuft])}, JETZT)
    assert [(m.offer.price, m.is_current(JETZT)) for m in treffer] == [(9.99, True), (9.49, False)]


def test_reihenfolge_wie_in_der_merkliste():
    w = merkliste(shops=(), products=[Product("Coca-Cola", Pack(12, 1.0)), Product("Monster Energy")])
    assert [m.product for m in find_matches(w, angebote(), JETZT)] == [
        "Coca-Cola 12×1 l", "Monster Energy"]


def test_kennung_haengt_nicht_an_der_marktguru_id():
    a = Match("Cola", offers_from_marktguru([roh(id=1)])[0])
    b = Match("Cola", offers_from_marktguru([roh(id=999)])[0])
    c = Match("Cola", offers_from_marktguru([roh(id=1, price=8.99)])[0])
    assert a.key == b.key != c.key
    assert Match.from_dict(a.to_dict()) == a


# --- LOGO --------------------------------------------------------------------
# Echte Woche vom 05.10. bis 10.10.2026 (tests/fixtures/logo_*). Einen Kasten
# Coca-Cola hatte LOGO in dieser Woche nicht, der kommt von Hand dazu.

LOGO_SEITE = parse_offers_page((FIXTURES / "logo_angebote.html").read_text(encoding="utf-8"))
LOGO_MARKT = parse_market_offers(
    json.loads((FIXTURES / "logo_markt.json").read_text(encoding="utf-8")), "Dittelbrunn")
MITTWOCH = datetime(2026, 10, 7, 12, 0)


def cola_kasten(preis=12.99) -> LogoOffer:
    return LogoOffer("Coca-Cola, Fanta, Sprite, Mezzo Mix", "diverse Sorten",
                     "12/1,0 Ltr. Kasten + € 3,30 Pfd. Ltr. = € 1,08", preis, 15.99, "12345", False)


def logo_woche(**je_markt) -> LogoWeek:
    return LogoWeek(date(2026, 10, 5), date(2026, 10, 10), je_markt)


def test_logo_angebote_aus_der_echten_woche():
    offers = offers_from_logo("LOGO", logo_woche(Dittelbrunn=LOGO_MARKT, Schweinfurt=LOGO_MARKT,
                                                 Sennfeld=LOGO_MARKT))
    assert len(offers) == 4                         # je Angebot eines, nicht je Markt
    guinness = offers[0]
    assert (guinness.shop, guinness.shop_label, guinness.product) == (
        "LOGO", "LOGO Dittelbrunn, Schweinfurt, Sennfeld", "Guinness Extra Stout")
    assert guinness.description == "11/0,5 Ltr. Kasten + € 2,38 Pfd. Ltr. = € 2,36"
    assert (guinness.price, guinness.old_price, guinness.reference_price) == (12.99, 16.49, 2.36)
    assert (guinness.quantity, guinness.volume, guinness.unit) == (11, 0.5, "l")
    assert (guinness.valid_from, guinness.valid_to) == (datetime(2026, 10, 5, 0, 0),
                                                        datetime(2026, 10, 10, 23, 59))
    assert (guinness.source, guinness.markets, guinness.regional, guinness.condition) == (
        "logo", ("Dittelbrunn", "Schweinfurt", "Sennfeld"), False, "")
    krombacher = offers[1]
    assert krombacher.description == ("diverse Sorten, + Sixpack GRATIS! · "
                                      "20/0,5 Ltr. Kasten + € 3,58 Pfd. Ltr. = € 1,40")


def test_logo_angebot_nur_in_einem_teil_der_maerkte():
    offers = offers_from_logo("LOGO", logo_woche(Dittelbrunn=[cola_kasten()], Schweinfurt=[],
                                                 Sennfeld=[cola_kasten()]))
    assert offers[0].markets == ("Dittelbrunn", "Sennfeld")
    assert offers[0].shop_label == "LOGO Dittelbrunn, Sennfeld"


def test_logo_ohne_maerkte_mit_sternchen():
    """Krombacher steht oben bei den Wochenangeboten und bei den Bieren: zaehlt einmal."""
    offers = offers_from_logo("LOGO", LogoWeek(date(2026, 10, 5), date(2026, 10, 10),
                                               {"": LOGO_SEITE.offers}))
    assert [o.product for o in offers].count("Krombacher Bier") == 1
    braustuebl = next(o for o in offers if o.product.startswith("Braust"))
    assert braustuebl.regional and braustuebl.markets == ()
    assert braustuebl.shop_label == "LOGO"


def test_logo_staffel_mit_maerkten_und_stufe():
    """Woche ab 12.10.2026: Coca-Cola Kombikasten fuer 13,49, 12,49 oder 11,49 € je
    Kasten, je nach Menge. In allen drei Maerkten gleich, REWE hat 11,99 €."""
    staffel = parse_market_offers(
        json.loads((FIXTURES / "logo_markt_staffel.json").read_text(encoding="utf-8")), "Dittelbrunn")
    sonntag = datetime(2026, 10, 11, 12, 0)
    woche = LogoWeek(date(2026, 10, 12), date(2026, 10, 17),
                     {markt: staffel for markt in ("Dittelbrunn", "Schweinfurt", "Sennfeld")})
    rewe = offers_from_marktguru([roh(price=11.99)])
    treffer = find_matches(merkliste(shops=("REWE", "LOGO")),
                           {"Coca-Cola": rewe + offers_from_logo("LOGO", woche)}, sonntag)
    assert [match_line(t, sonntag, with_product=False) for t in treffer] == [
        "LOGO Dittelbrunn, Schweinfurt, Sennfeld 11,49 € ab 3 Kästen (Mo 12.10.–Sa 17.10.)",
        "REWE 11,99 € (Mo 12.10.–Sa 17.10.)",
        "LOGO Dittelbrunn, Schweinfurt, Sennfeld 12,49 € bei 2 Kästen (Mo 12.10.–Sa 17.10.)",
        "LOGO Dittelbrunn, Schweinfurt, Sennfeld 13,49 € bei 1 Kasten (Mo 12.10.–Sa 17.10.)"]
    assert len({t.key for t in treffer}) == 4       # jede Stufe wird einmal gemeldet


def test_coca_cola_kasten_bei_logo():
    """Gentleman Jack "Coca-Cola, Sour" in der 0,33-l-Flasche ist kein Kasten,
    TWENTYFOUR 7 Energy kein Monster Energy."""
    angebote_ = offers_from_logo("LOGO", logo_woche(Dittelbrunn=LOGO_MARKT + [cola_kasten()]))
    treffer = find_matches(merkliste(shops=("LOGO",)),
                           {"Monster Energy": angebote_, "Coca-Cola": angebote_}, MITTWOCH)
    assert [(t.product, t.offer.product, t.offer.price) for t in treffer] == [
        ("Coca-Cola 12×1 l", "Coca-Cola, Fanta, Sprite, Mezzo Mix", 12.99)]


def test_ohne_packung_findet_coca_cola_auch_den_whiskey_mix():
    """Bewusst so: Der Name steht als ganzes Wort im Text. Wer nur den Kasten will,
    gibt die Packung an."""
    angebote_ = offers_from_logo("LOGO", logo_woche(Dittelbrunn=LOGO_MARKT))
    w = merkliste(shops=("logo",), products=[Product("Coca-Cola")])
    assert [t.offer.product for t in find_matches(w, {"Coca-Cola": angebote_}, MITTWOCH)] == [
        "Gentleman Jack"]


def test_logo_nur_wenn_es_in_der_merkliste_steht():
    angebote_ = offers_from_logo("LOGO", logo_woche(Dittelbrunn=[cola_kasten()]))
    w = merkliste(shops=("REWE",), products=[Product("Coca-Cola", Pack(12, 1.0))])
    assert find_matches(w, {"Coca-Cola": angebote_}, MITTWOCH) == []


def test_abgelaufene_logo_woche_bringt_keinen_treffer():
    """Sonntag frueh steht auf der Seite noch die Woche bis Samstag."""
    angebote_ = offers_from_logo("LOGO", logo_woche(Dittelbrunn=[cola_kasten()]))
    w = merkliste(shops=("LOGO",), products=[Product("Coca-Cola", Pack(12, 1.0))])
    assert find_matches(w, {"Coca-Cola": angebote_}, datetime(2026, 10, 11, 8, 0)) == []


def test_treffer_ueberstehen_das_speichern():
    """Treffer liegen als JSON im Volume und muessen nach einem Neustart gleich sein."""
    marktguru = offers_from_marktguru([roh()])[0]
    assert Offer.from_dict(json.loads(json.dumps(marktguru.to_dict()))) == marktguru
    stufe = replace(cola_kasten(), description="Ab 3 Kasten - Pro Kasten")
    logo = offers_from_logo("LOGO", logo_woche(A=[stufe], B=[]))[0]
    assert (logo.condition, logo.markets) == ("ab 3 Kästen", ("A",))
    assert Offer.from_dict(json.loads(json.dumps(logo.to_dict()))) == logo
    treffer = Match("Cola", logo)
    assert Match.from_dict(json.loads(json.dumps(treffer.to_dict()))) == treffer


# --- Zeitplan ----------------------------------------------------------------

PLAN = Schedule((0, 2, 4, 6), 8, 0)


@pytest.mark.parametrize("jetzt,letzter,naechster", [
    (datetime(2026, 10, 10, 12, 0), datetime(2026, 10, 9, 8, 0), datetime(2026, 10, 11, 8, 0)),   # Sa
    (datetime(2026, 10, 11, 7, 59), datetime(2026, 10, 9, 8, 0), datetime(2026, 10, 11, 8, 0)),   # So kurz vor 8
    (datetime(2026, 10, 11, 8, 0), datetime(2026, 10, 11, 8, 0), datetime(2026, 10, 12, 8, 0)),   # So um 8
    (datetime(2026, 10, 13, 9, 0), datetime(2026, 10, 12, 8, 0), datetime(2026, 10, 14, 8, 0)),   # Di
])
def test_termine(jetzt, letzter, naechster):
    assert last_slot(PLAN, jetzt) == letzter
    assert next_slot(PLAN, jetzt) == naechster


def test_einmal_pro_woche():
    sonntags = Schedule((6,), 18, 30)
    assert last_slot(sonntags, datetime(2026, 10, 11, 18, 29)) == datetime(2026, 10, 4, 18, 30)
    assert next_slot(sonntags, datetime(2026, 10, 11, 18, 30)) == datetime(2026, 10, 18, 18, 30)


# --- Texte -------------------------------------------------------------------

def treffer_fuer(*rohdaten, produkt="Coca-Cola 12×1 l") -> list[Match]:
    return [Match(produkt, o) for o in offers_from_marktguru(list(rohdaten))]


def test_zeitraum():
    laeuft, kommt = (offers_from_marktguru([roh(validityDates=[
        {"from": "2026-10-04T22:00:00Z", "to": "2026-10-10T21:59:00Z"}])])[0],
        offers_from_marktguru([roh()])[0])
    assert validity_text(laeuft, JETZT) == "bis Sa 10.10."
    assert validity_text(kommt, JETZT) == "Mo 12.10.–Sa 17.10."


def test_push_ein_produkt_zwei_geschaefte():
    treffer = treffer_fuer(roh(), roh(advertisers=[{"name": "EDEKA"}], price=9.49,
                                      requiresLoyalityMembership=True))
    payload = offers_payload(treffer, JETZT)
    assert payload == {
        "title": "Angebot: Coca-Cola 12×1 l",
        "body": "REWE 9,99 € (Mo 12.10.–Sa 17.10.)\nEDEKA 9,49 € (Mo 12.10.–Sa 17.10.) mit App",
        "tag": "angebote", "url": "/", "kind": "info",
    }


def test_push_mehrere_produkte_und_viele_treffer():
    treffer = (treffer_fuer(*[roh(id=i, price=9 + i / 100) for i in range(5)])
               + treffer_fuer(roh(id=9, price=0.99), produkt="Monster Energy"))
    payload = offers_payload(treffer, JETZT)
    assert payload["title"] == "Angebote: Coca-Cola 12×1 l, Monster Energy"
    zeilen = payload["body"].split("\n")
    assert zeilen[0] == "Coca-Cola 12×1 l: REWE 9,00 € (Mo 12.10.–Sa 17.10.)"
    assert zeilen[-1] == "… und 1 weitere"
    assert len(zeilen) == 6


def test_kacheltext():
    w = merkliste()
    assert summary_message(w, [], JETZT) == "Kein Angebot für Monster Energy, Coca-Cola 12×1 l"
    treffer = treffer_fuer(roh(), roh(advertisers=[{"name": "EDEKA"}]), roh(price=1.0))
    assert summary_message(w, treffer, JETZT) == (
        "Coca-Cola 12×1 l: REWE 9,99 € (Mo 12.10.–Sa 17.10.) · "
        "Coca-Cola 12×1 l: EDEKA 9,99 € (Mo 12.10.–Sa 17.10.) und 1 weitere")


def test_viele_produkte_ohne_treffer_werden_gekuerzt():
    w = replace(merkliste(), products=tuple(Product(n) for n in ("A1", "B2", "C3", "D4", "E5")))
    assert summary_message(w, [], JETZT) == "Kein Angebot für A1, B2, C3 und 2 weitere"


def test_logo_im_push_und_auf_der_kachel():
    nur_dittelbrunn = offers_from_logo("LOGO", logo_woche(Dittelbrunn=[cola_kasten()], Sennfeld=[]))
    payload = offers_payload([Match("Coca-Cola 12×1 l", nur_dittelbrunn[0])], MITTWOCH)
    assert payload["body"] == "LOGO Dittelbrunn 12,99 € (bis Sa 10.10.)"
    ueberall = replace(nur_dittelbrunn[0], markets=(), regional=True)
    assert match_line(Match("Coca-Cola 12×1 l", ueberall), MITTWOCH, with_product=False) == (
        "LOGO 12,99 € (bis Sa 10.10.) nicht in allen Märkten")
