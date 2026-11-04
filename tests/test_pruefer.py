"""Tests der fünf Prüfungen und des Prüfmanagers (Datenbankabgleich mit Attrappe)."""

from __future__ import annotations

import threading

import pytest

from eplan_pruefer.datenbank import ReferenzRepository
from eplan_pruefer.modelle import (
    DatenbankFehler,
    Fehlertyp,
    PruefEinstellungen,
    Verbindungstest,
    VorgangAbgebrochenFehler,
)
from eplan_pruefer.pruefer import (
    DatenbankAbgleichPruefung,
    DoppelteAdressenPruefung,
    DoppelteFunktionstextePruefung,
    DoppelteSymbolePruefung,
    PlatzhalterPruefung,
    Pruefkontext,
    Pruefmanager,
    Textnormalisierer,
    standard_pruefungen,
)

from .conftest import baue_datensatz


class AttrappenReferenz(ReferenzRepository):
    """Referenz im Speicher."""

    def __init__(self, paare, fehler=None):
        self.paare = list(paare)
        self.fehler = fehler

    def lade_referenzdaten(self, abbruch=None):
        if self.fehler:
            raise self.fehler
        return list(self.paare)

    def pruefe_vorhandensein(self, symbol, funktionstext):  # pragma: no cover
        raise NotImplementedError

    def uebernehmen(self, paare, konflikt_erlauben=False):  # pragma: no cover
        raise NotImplementedError

    def teste_verbindung(self):  # pragma: no cover
        return Verbindungstest(True, ())


def kontext(referenz=None, beachten=False):
    return Pruefkontext(Textnormalisierer(beachten), referenz)


def test_doppelte_symbole_verweist_auf_erste_verwendung():
    daten = [baue_datensatz("Motor_Start", seite="5", nummer=1),
             baue_datensatz("Motor_Stop", seite="6", nummer=2),
             baue_datensatz("Motor_Start", seite="12", nummer=3)]
    fehler = DoppelteSymbolePruefung().pruefe(daten, kontext())
    assert len(fehler) == 1
    f = fehler[0]
    assert f.fehlertyp is Fehlertyp.DOPPELTES_SYMBOL and f.datensatz.laufende_nummer == 3
    assert f.bezug.laufende_nummer == 1 and "bereits auf Seite 5 verwendet" in f.beschreibung


def test_drei_vorkommen_ergeben_zwei_fehler():
    daten = [baue_datensatz("A", nummer=i) for i in (1, 2, 3)]
    assert [f.datensatz.laufende_nummer for f in DoppelteSymbolePruefung().pruefe(daten, kontext())] == [2, 3]


def test_symbole_gross_kleinschreibung_und_leerzeichen():
    daten = [baue_datensatz("Motor_Start", nummer=1), baue_datensatz("  MOTOR_START ", nummer=2)]
    assert len(DoppelteSymbolePruefung().pruefe(daten, kontext())) == 1
    assert DoppelteSymbolePruefung().pruefe(daten, kontext(beachten=True)) == []


def test_leere_symbole_sind_keine_duplikate():
    daten = [baue_datensatz("", "T1", "E0.0", nummer=1), baue_datensatz("", "T2", "E0.1", nummer=2)]
    assert DoppelteSymbolePruefung().pruefe(daten, kontext()) == []


def test_doppelte_funktionstexte():
    daten = [baue_datensatz(text="Förderband Start", nummer=1), baue_datensatz(text="Förderband  Start", nummer=2),
             baue_datensatz(text="Förderband Stop", nummer=3)]
    fehler = DoppelteFunktionstextePruefung().pruefe(daten, kontext())
    assert [f.datensatz.laufende_nummer for f in fehler] == [2]
    assert fehler[0].fehlertyp is Fehlertyp.DOPPELTER_FUNKTIONSTEXT


def test_doppelte_adressen_beispiel_der_aufgabenstellung():
    daten = [baue_datensatz("A", adresse="E0.0", seite="5", nummer=1),
             baue_datensatz("B", adresse="E0.1", seite="6", nummer=2),
             baue_datensatz("C", adresse="e 0.0", seite="12", nummer=3)]
    fehler = DoppelteAdressenPruefung().pruefe(daten, kontext())
    assert len(fehler) == 1
    assert fehler[0].beschreibung == "Adresse bereits auf Seite 5 verwendet."
    assert fehler[0].seite == "12" and fehler[0].adresse == "e 0.0"


def test_adressen_unabhaengig_von_gross_kleinschreibung_auch_wenn_beachten_aktiv():
    daten = [baue_datensatz("A", adresse="%I0.0", nummer=1), baue_datensatz("B", adresse="%i0.0", nummer=2)]
    assert len(DoppelteAdressenPruefung().pruefe(daten, kontext(beachten=True))) == 1


@pytest.mark.parametrize("symbol,text,erwartet", [
    ("Motor_?", "ok", "Symbolische Adresse enthält"),
    ("ok", "? Förderband", "Funktionstext enthält"),
    ("M?", "T?", "Symbolische Adresse und Funktionstext"),
    ("Motor_？", "ok", "Symbolische Adresse enthält"),
])
def test_platzhalter(symbol, text, erwartet):
    fehler = PlatzhalterPruefung().pruefe([baue_datensatz(symbol, text)], kontext())
    assert len(fehler) == 1 and erwartet in fehler[0].beschreibung and fehler[0].fehlertyp is Fehlertyp.PLATZHALTER


def test_kein_platzhalter():
    assert PlatzhalterPruefung().pruefe([baue_datensatz("Motor_1", "Förderband", "E0.0")], kontext()) == []


REFERENZ = [("Motor_Start", "Förderband Start"), ("Motor_Stop", "Förderband Stop"), ("Lampe", "Meldeleuchte")]


@pytest.mark.parametrize("symbol,text,erwarteter_typ", [
    ("Motor_Start", "Förderband Start", None),                                  # Fall 1: OK
    ("motor_start", "FÖRDERBAND  START", None),                                 # Schreibweise egal
    ("Unbekannt", "Meldeleuchte", Fehlertyp.SYMBOL_NICHT_IN_DB),               # Fall 2
    ("Unbekannt", "Auch unbekannt", Fehlertyp.SYMBOL_NICHT_IN_DB),             # Fall 2 (beides fehlt)
    ("Lampe", "Gibt es nicht", Fehlertyp.FUNKTIONSTEXT_NICHT_IN_DB),           # Fall 3
    ("Lampe", "Förderband Stop", Fehlertyp.KOMBINATION_ABWEICHEND),            # Fall 4
    ("", "Meldeleuchte", Fehlertyp.SYMBOL_NICHT_IN_DB),                        # leeres Symbol
    ("Lampe", "", Fehlertyp.FUNKTIONSTEXT_NICHT_IN_DB),                        # leerer Text
])
def test_datenbankabgleich_alle_faelle(symbol, text, erwarteter_typ):
    fehler = DatenbankAbgleichPruefung().pruefe([baue_datensatz(symbol, text, "E0.0")],
                                                kontext(AttrappenReferenz(REFERENZ)))
    if erwarteter_typ is None:
        assert fehler == []
    else:
        assert [f.fehlertyp for f in fehler] == [erwarteter_typ]


def test_datenbankabgleich_beschreibung_beide_fehlen():
    f = DatenbankAbgleichPruefung().pruefe([baue_datensatz("Neu", "Neuer Text")],
                                           kontext(AttrappenReferenz(REFERENZ)))[0]
    assert "Symbol „Neu“ fehlt" in f.beschreibung and "Funktionstext ist in der Datenbank nicht vorhanden" in f.beschreibung


def test_datenbankabgleich_gross_klein_beachten():
    ref = AttrappenReferenz(REFERENZ)
    d = [baue_datensatz("motor_start", "Förderband Start")]
    assert DatenbankAbgleichPruefung().pruefe(d, kontext(ref)) == []
    assert DatenbankAbgleichPruefung().pruefe(d, kontext(ref, beachten=True))[0].fehlertyp \
        is Fehlertyp.SYMBOL_NICHT_IN_DB


def test_datenbankabgleich_ohne_referenz_wird_uebersprungen():
    bericht = Pruefmanager([DoppelteSymbolePruefung(), DatenbankAbgleichPruefung()]).pruefe(
        [baue_datensatz("A", nummer=1), baue_datensatz("A", nummer=2)], referenz=None)
    assert "Datenbankabgleich" in bericht.uebersprungen and len(bericht.fehler) == 1
    assert bericht.ausgefuehrt == ["Doppelte Symbole"]


def test_datenbank_nicht_erreichbar_stoppt_nicht_die_anderen_pruefungen():
    ref = AttrappenReferenz([], fehler=DatenbankFehler("Server nicht erreichbar"))
    bericht = Pruefmanager(standard_pruefungen(PruefEinstellungen(datenbankabgleich=True))).pruefe(
        [baue_datensatz("A?", "T", "E0.0", nummer=1), baue_datensatz("A?", "T", "E0.0", nummer=2)], referenz=ref)
    assert bericht.uebersprungen["Datenbankabgleich"] == "Server nicht erreichbar"
    typen = {f.fehlertyp for f in bericht.fehler}
    assert {Fehlertyp.DOPPELTES_SYMBOL, Fehlertyp.DOPPELTER_FUNKTIONSTEXT,
            Fehlertyp.DOPPELTE_ADRESSE, Fehlertyp.PLATZHALTER} <= typen


def test_datenbankabgleich_ist_standardmaessig_aus():
    assert [type(p).__name__ for p in standard_pruefungen(PruefEinstellungen())] == [
        "DoppelteSymbolePruefung", "DoppelteFunktionstextePruefung", "DoppelteAdressenPruefung", "PlatzhalterPruefung"]


def test_einzelne_pruefungen_abschaltbar():
    e = PruefEinstellungen(doppelte_symbole=False, platzhalter=False, datenbankabgleich=False)
    assert [type(p).__name__ for p in standard_pruefungen(e)] == [
        "DoppelteFunktionstextePruefung", "DoppelteAdressenPruefung"]


def test_ergebnis_ist_nach_reihenfolge_im_projekt_sortiert():
    daten = [baue_datensatz("X?", "T", "E0.0", nummer=1), baue_datensatz("Y", "T", "E0.1", nummer=2)]
    bericht = Pruefmanager(standard_pruefungen(PruefEinstellungen(datenbankabgleich=False))).pruefe(daten)
    assert [(f.datensatz.laufende_nummer, f.fehlertyp) for f in bericht.fehler] == [
        (1, Fehlertyp.PLATZHALTER), (2, Fehlertyp.DOPPELTER_FUNKTIONSTEXT)]


def test_abbruch():
    abbruch = threading.Event()
    abbruch.set()
    with pytest.raises(VorgangAbgebrochenFehler):
        Pruefmanager([PlatzhalterPruefung()]).pruefe([baue_datensatz("A")], abbruch=abbruch)


def test_uebernehmbare_fehlertypen():
    assert {t for t in Fehlertyp if t.uebernehmbar} == {
        Fehlertyp.SYMBOL_NICHT_IN_DB, Fehlertyp.FUNKTIONSTEXT_NICHT_IN_DB}
