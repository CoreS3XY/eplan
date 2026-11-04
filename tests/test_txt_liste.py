"""Tabulatorgetrennte Textlisten (Symbolische Adresse, Adresse, Funktionstext, BMK) und Seitenverweis aus dem BMK."""

from __future__ import annotations

from pathlib import Path

import pytest

from eplan_pruefer.eplan_parser import EplanDateiLeser
from eplan_pruefer.modelle import Einstellungen
from eplan_pruefer.pruefer import Pruefmanager, standard_pruefungen
from eplan_pruefer.modelle import PruefEinstellungen

ZEILEN = [
    ("H1ETempMesSt3", "PEW138", "Temperatur Messstelle 3, Achse Z Gfk", "=U4+E2/2.2", "=U4+D12-R1"),
    ("C3EProzDat1PSU8600????", "ED1028-1057", "Sitop PSU8600 Prozessdaten 1", "=C3+E2/3.4", "=+"),
    ("C3AProzDat1PSU8600????", "AD1028-1067", "Sitop PSU8600 Prozessdaten 1", "=C3+E2/3.5", ""),
    ("E1AJog", "A3.4", "Jog", "=D1+E2/5.1", ""),
    ("E1EJog", "E7.4", "Jog", "=D1+E2/5.1", ""),
]
KOPF = ("Symbolische Adresse", "Adresse", "Funktionstext", "Sichtbares BMK", "Vollständiges BMK")


def doppelt_getrennt() -> str:
    """Wie im Beispiel: zwei Tabulatoren hinter Symbol und Adresse."""
    zeilen = ["\t\t".join(KOPF[:2]) + "\t\t" + KOPF[2] + "\t" + KOPF[3] + "\t" + KOPF[4]]
    for s, a, t, b1, b2 in ZEILEN:
        zeilen.append(f"{s}\t\t{a}\t\t{t}\t{b1}\t{b2}")
    return "\n".join(zeilen) + "\n"


def ausgerichtet() -> str:
    """Tabulatoren nach Textbreite (Tab = 8 Zeichen): lange Werte bekommen weniger Tabulatoren."""
    def zelle(wert: str, breite: int) -> str:
        return wert + "\t" * max(1, (breite - len(wert) + 7) // 8)
    zeilen = [zelle(KOPF[0], 32) + zelle(KOPF[1], 16) + zelle(KOPF[2], 48) + KOPF[3] + "\t" + KOPF[4]]
    for s, a, t, b1, b2 in ZEILEN:
        zeilen.append(zelle(s, 32) + zelle(a, 16) + zelle(t, 48) + b1 + "\t" + b2)
    return "\n".join(zeilen) + "\n"


@pytest.fixture(params=["doppelt", "ausgerichtet"])
def txt(request, tmp_path: Path) -> Path:
    pfad = tmp_path / "SPS_Liste.txt"
    pfad.write_text(doppelt_getrennt() if request.param == "doppelt" else ausgerichtet(), encoding="cp1252")
    return pfad


def lese(pfad):
    return EplanDateiLeser(Einstellungen()).lese(pfad)


def test_felder_werden_korrekt_zugeordnet(txt):
    ergebnis = lese(txt)
    assert len(ergebnis.datensaetze) == 5
    d = ergebnis.datensaetze[0]
    assert (d.symbolische_adresse, d.sps_adresse, d.funktionstext) == \
        ("H1ETempMesSt3", "PEW138", "Temperatur Messstelle 3, Achse Z Gfk")
    assert d.betriebsmittelkennzeichen == "=U4+E2/2.2" and d.vollstaendiges_bmk == "=U4+D12-R1"
    assert ergebnis.datensaetze[1].vollstaendiges_bmk == "=+"
    assert ergebnis.datensaetze[2].vollstaendiges_bmk == ""
    assert ergebnis.datensaetze[4].sps_adresse == "E7.4"
    assert not ergebnis.warnungen


def test_seite_wird_aus_dem_sichtbaren_bmk_abgeleitet(txt):
    d = lese(txt).datensaetze
    assert (d[0].seitenreferenz, d[0].seitennummer) == ("=U4+E2/2", "2")
    assert (d[1].seitenreferenz, d[1].seitennummer) == ("=C3+E2/3", "3")
    assert (d[3].seitenreferenz, d[3].seitennummer) == ("=D1+E2/5", "5")
    assert d[3].betriebsmittelkennzeichen == "=D1+E2/5.1"          # Spalte bleibt im BMK sichtbar
    assert d[1].seite_anzeige == "3" and d[1].fundstelle.zeile == 3


def test_pruefungen_auf_dieser_liste(txt):
    daten = lese(txt).datensaetze
    bericht = Pruefmanager(standard_pruefungen(PruefEinstellungen(datenbankabgleich=False))).pruefe(daten)
    typen = sorted(f.fehlertyp.name for f in bericht.fehler)
    # Platzhalter (????) in zwei Symbolen; Funktionstexte „Prozessdaten 1" und „Jog" je doppelt
    assert typen.count("PLATZHALTER") == 2 and typen.count("DOPPELTER_FUNKTIONSTEXT") == 2
    assert "DOPPELTE_ADRESSE" not in typen and "DOPPELTES_SYMBOL" not in typen


def test_utf16_mit_bom(tmp_path):
    pfad = tmp_path / "liste.txt"
    pfad.write_bytes(doppelt_getrennt().encode("utf-16"))
    assert len(lese(pfad).datensaetze) == 5


def test_anfuehrungszeichen_in_tabulatorlisten_stoeren_nicht(tmp_path):
    pfad = tmp_path / "liste.txt"
    pfad.write_text('Symbol\tAdresse\tFunktionstext\nA\tE1.0\tSchalter 5" breit\nB\tE1.1\t"Text mit Rand"\n',
                    encoding="utf-8")
    d = lese(pfad).datensaetze
    assert [x.funktionstext for x in d] == ['Schalter 5" breit', "Text mit Rand"]


def test_bmk_ohne_seitenverweis_erzeugt_keine_seite(tmp_path):
    pfad = tmp_path / "l.csv"
    pfad.write_text("Symbol;Adresse;BMK\nA;E1.0;=G2+E2-KA42.0\n", encoding="utf-8")
    d = lese(pfad).datensaetze[0]
    assert d.seitenreferenz == "" and d.seitennummer == ""


def test_alte_konfiguration_erhaelt_neue_standardbezeichnungen(tmp_path):
    from eplan_pruefer.einstellungen import EinstellungsVerwalter
    pfad = tmp_path / "e.ini"
    pfad.write_text("[feldzuordnung]\nbetriebsmittelkennzeichen = MeinBMK\nsps_adresse = SPS-Adresse|Klemme\n",
                    encoding="utf-8")
    z = EinstellungsVerwalter(pfad).laden().feldzuordnung
    assert z["betriebsmittelkennzeichen"][0] == "MeinBMK" and "Sichtbares BMK" in z["betriebsmittelkennzeichen"]
    assert "Klemme" in z["sps_adresse"] and "EPLAN426" in z["sps_adresse"]
    assert "Vollständiges BMK" in z["vollstaendiges_bmk"]
    # gespeichert mit Version: spätere Entfernung einer Bezeichnung durch den Anwender bleibt bestehen
    e = EinstellungsVerwalter(pfad).laden()
    e.feldzuordnung["sps_adresse"] = ("SPS-Adresse",)
    EinstellungsVerwalter(pfad).speichern(e)
    assert EinstellungsVerwalter(pfad).laden().feldzuordnung["sps_adresse"] == ("SPS-Adresse",)
