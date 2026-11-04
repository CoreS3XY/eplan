"""Tests der PyQt6-Oberfläche (offscreen) einschließlich Datenbank-Übernahme gegen echten Server."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

pytest.importorskip("PyQt6")
from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtTest import QTest  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from eplan_pruefer import gui  # noqa: E402
from eplan_pruefer.datenbank import SqliteReferenzRepository  # noqa: E402
from eplan_pruefer.einstellungen import EinstellungsVerwalter  # noqa: E402
from eplan_pruefer.gui_dialoge import EinstellungsDialog, SeitenDialog  # noqa: E402
from eplan_pruefer.gui_modelle import natuerlicher_schluessel  # noqa: E402
from eplan_pruefer.modelle import Einstellungen, Fehlertyp  # noqa: E402

pytestmark = pytest.mark.gui


def warte_bis(bedingung, ms=15000) -> bool:
    """Verarbeitet Qt-Ereignisse, bis die Bedingung gilt oder die Zeit abgelaufen ist."""
    import time
    ende = time.monotonic() + ms / 1000
    while time.monotonic() < ende:
        QApplication.processEvents()
        if bedingung():
            return True
        QTest.qWait(10)
    return bool(bedingung())

CSV_MIT_FEHLERN = (
    "Symbolische Adresse;Funktionstext;SPS-Adresse;Seite;Seitenname\n"
    "Motor_Start;Förderband Start;E0.0;5;Antriebe\n"
    "Motor_Start;Förderband Start 2;E0.1;12;Sensorik\n"
    "Lampe_?;Meldeleuchte;E0.0;13;Meldungen\n"
)


@pytest.fixture(scope="session")
def app():
    return QApplication.instance() or QApplication([])


class Meldungen:
    """Ersetzt modale Dialoge und merkt sich, was angezeigt wurde."""

    def __init__(self, antworten=None):
        self.antworten = list(antworten or [])
        self.protokoll: list[tuple[str, str, str]] = []

    def information(self, _eltern, titel, text):
        self.protokoll.append(("info", titel, text))

    def warnung(self, _eltern, titel, text):
        self.protokoll.append(("warnung", titel, text))

    def fehler(self, _eltern, titel, text):
        self.protokoll.append(("fehler", titel, text))

    def frage(self, _eltern, titel, text):
        self.protokoll.append(("frage", titel, text))
        return self.antworten.pop(0) if self.antworten else False

    def arten(self):
        return [art for art, *_ in self.protokoll]


@pytest.fixture
def meldungen(monkeypatch):
    m = Meldungen()
    monkeypatch.setattr(gui, "zeige_information", m.information)
    monkeypatch.setattr(gui, "zeige_warnung", m.warnung)
    monkeypatch.setattr(gui, "zeige_fehler", m.fehler)
    monkeypatch.setattr(gui, "stelle_frage", m.frage)
    monkeypatch.setattr(gui.EinleseberichtDialog, "exec", lambda self: 0)
    monkeypatch.setattr(gui.SeitenDialog, "exec", lambda self: 0)
    return m


@pytest.fixture
def fenster(app, tmp_path, meldungen):
    verwalter = EinstellungsVerwalter(tmp_path / "konfig" / "einstellungen.ini")
    e = Einstellungen()
    e.pruefung.datenbankabgleich = False          # nur in den Datenbanktests aktiv
    f = gui.Hauptfenster(verwalter, e, None)
    f.show()
    yield f
    f.close()


def warte(fenster, ms=15000):
    assert warte_bis(lambda: not fenster.beschaeftigt, ms), "Hintergrundaufgabe wurde nicht fertig"
    QTest.qWait(50)         # nachfolgende Signale (z. B. Folgeaufgaben) abarbeiten
    assert warte_bis(lambda: not fenster.beschaeftigt, ms)


@pytest.fixture
def csv_fehler(tmp_path) -> Path:
    pfad = tmp_path / "anlage.csv"
    pfad.write_text(CSV_MIT_FEHLERN, encoding="utf-8")
    return pfad


def test_einlesen_und_automatische_pruefung(fenster, csv_fehler):
    assert not fenster.a_pruefen.isEnabled() and not fenster.a_csv.isEnabled()
    fenster.datei_laden(csv_fehler)
    warte(fenster)
    assert fenster._datensatz_modell.rowCount() == 3
    typen = {f.fehlertyp for f in fenster._bericht.fehler}
    assert typen == {Fehlertyp.DOPPELTE_ADRESSE, Fehlertyp.DOPPELTES_SYMBOL, Fehlertyp.PLATZHALTER}
    assert fenster._fehler_modell.rowCount() == 3 and "3 Fehler" in fenster.l_zaehler.text()
    assert "anlage.csv" in fenster.windowTitle()
    assert fenster.a_csv.isEnabled() and fenster.a_xlsx.isEnabled() and fenster.a_pruefen.isEnabled()
    assert "3 Datensätze" in fenster.l_zusammenfassung.text()
    assert any("Datenbankabgleich" in t for t in [fenster.l_hinweis.text()]) is False   # abgeschaltet, kein Hinweis


def test_detailanzeige_zeigt_seite_name_und_fundstelle(fenster, csv_fehler):
    fenster.datei_laden(csv_fehler)
    warte(fenster)
    modell = fenster._filter_modell
    zeile = next(r for r in range(modell.rowCount()) if modell.index(r, 0).data() == "Doppeltes Symbol")
    fenster.t_fehler.selectRow(zeile)
    fenster.t_fehler.setCurrentIndex(modell.index(zeile, 0))
    assert fenster.d_seite.text() == "12" and fenster.d_name.text() == "Sensorik"
    assert "anlage.csv › Zeile 3" in fenster.d_fundstelle.text()
    assert "Erste Verwendung auf Seite 5 – Antriebe" in fenster.d_bezug.text()
    assert "bereits auf Seite 5 verwendet" in fenster.d_beschreibung.text()


def test_seitendialog_zeigt_seite_inhalt_und_quelle(fenster, csv_fehler, app):
    fenster.datei_laden(csv_fehler)
    warte(fenster)
    fehler = next(f for f in fenster._bericht.fehler if f.fehlertyp is Fehlertyp.DOPPELTES_SYMBOL)
    dialog = SeitenDialog(fenster._ergebnis, [("Betroffene Stelle", fehler.datensatz),
                                              ("Erste Verwendung", fehler.bezug)])
    assert dialog.l_seite.text() == "12" and dialog.l_name.text() == "Sensorik"
    assert dialog.tabelle.rowCount() == 1 and dialog.tabelle.item(0, 1).text() == "Motor_Start"
    assert "▶" in dialog.quelltext.toPlainText() and "Förderband Start 2" in dialog.quelltext.toPlainText()
    dialog.auswahl.setCurrentIndex(1)
    assert dialog.l_seite.text() == "5" and "Zeile 2" in dialog.l_fundstelle.text()


def test_sprung_oeffnet_dialog_fuer_markierten_fehler(fenster, csv_fehler, monkeypatch):
    geoeffnet = []
    monkeypatch.setattr(gui.SeitenDialog, "exec", lambda self: geoeffnet.append(self.l_seite.text()) or 0)
    fenster.datei_laden(csv_fehler)
    warte(fenster)
    fenster.t_fehler.setCurrentIndex(fenster._filter_modell.index(0, 0))
    fenster._zeige_fundstelle()
    assert len(geoeffnet) == 1 and geoeffnet[0] != "–"


def test_filter_und_suche(fenster, csv_fehler):
    fenster.datei_laden(csv_fehler)
    warte(fenster)
    fenster.c_typ.setCurrentIndex(fenster.c_typ.findData(Fehlertyp.PLATZHALTER))
    assert fenster._filter_modell.rowCount() == 1 and "1 von 3" in fenster.l_zaehler.text()
    fenster.c_typ.setCurrentIndex(0)
    fenster.e_suche.setText("sensorik")
    assert fenster._filter_modell.rowCount() == 0           # Seitenname steht nicht in der Fehlerliste
    fenster.e_suche.setText("seite 5")                       # Duplikat-Symbol und Duplikat-Adresse verweisen auf Seite 5
    assert fenster._filter_modell.rowCount() == 2
    fenster.e_suche.setText("")
    assert fenster._filter_modell.rowCount() == 3


def test_sortierung_der_seiten_ist_natuerlich(fenster, csv_fehler):
    fenster.datei_laden(csv_fehler)
    warte(fenster)
    fenster.t_fehler.sortByColumn(4, Qt.SortOrder.DescendingOrder)
    seiten = [fenster._filter_modell.index(r, 4).data() for r in range(fenster._filter_modell.rowCount())]
    assert seiten == ["13", "12", "12"] or seiten == ["13", "12", "5"] or seiten == sorted(seiten, key=natuerlicher_schluessel, reverse=True)
    assert natuerlicher_schluessel("2") < natuerlicher_schluessel("12") < natuerlicher_schluessel("12a")


def test_export_aus_der_oberflaeche(fenster, csv_fehler, tmp_path, meldungen):
    fenster.datei_laden(csv_fehler)
    warte(fenster)
    ziel = fenster.exportiere_nach(tmp_path / "out.xlsx", fenster._bericht.fehler)
    assert ziel is not None and ziel.exists() and "info" in meldungen.arten()
    assert fenster.exportiere_nach(tmp_path / "x.pdf", fenster._bericht.fehler) is None
    assert "fehler" in meldungen.arten()


def test_datei_ohne_datensaetze_zeigt_einleseprotokoll(fenster, tmp_path, meldungen):
    leer = tmp_path / "leer.csv"
    leer.write_text("Name;Wert\na;b\n", encoding="utf-8")
    fenster.datei_laden(leer)
    warte(fenster)
    assert "warnung" in meldungen.arten() and not fenster.a_pruefen.isEnabled()
    assert fenster.a_einleseprotokoll.isEnabled()


def test_fehlende_datei_wird_gemeldet_und_oberflaeche_bleibt_bedienbar(fenster, tmp_path, meldungen):
    fenster.datei_laden(tmp_path / "gibt_es_nicht.zw1")
    warte(fenster)
    assert "fehler" in meldungen.arten() and fenster.a_oeffnen.isEnabled()


def test_einstellungsdialog_uebernimmt_werte(app, tmp_path):
    e = Einstellungen()
    dialog = EinstellungsDialog(e, tmp_path / "e.ini", None)
    dialog.e_datei.setText(str(tmp_path / "symbole.db"))
    dialog.c_tabelle.setEditText("t")
    dialog.c_feld_symbol.setEditText("sym")
    dialog.c_feld_text.setEditText("txt")
    dialog.c_primaer.setEditText("id")
    dialog.e_timeout.setValue(20)
    dialog.c_adressen.setChecked(False)
    dialog.c_datenbank.setChecked(True)
    dialog.e_felder["symbolische_adresse"].setText("Kurzname; P20404")
    dialog._speichern()
    neu = dialog.ergebnis()
    assert neu is not None
    d = neu.datenbank
    assert d.dateipfad == str(tmp_path / "symbole.db") and d.timeout_s == 20
    assert (d.tabellenname, d.feld_symbol, d.feld_funktionstext, d.primaerschluessel) == ("t", "sym", "txt", "id")
    assert neu.pruefung.doppelte_adressen is False and neu.pruefung.datenbankabgleich is True
    assert neu.feldzuordnung["symbolische_adresse"] == ("Kurzname", "P20404")
    assert e.datenbank.dateipfad == ""                 # Ausgangsobjekt bleibt unverändert


def test_einstellungsdialog_lehnt_ungueltiges_ab(app, tmp_path, monkeypatch):
    gemeldet = []
    monkeypatch.setattr("eplan_pruefer.gui_dialoge.zeige_warnung", lambda *a: gemeldet.append(a[-1]))
    dialog = EinstellungsDialog(Einstellungen(), tmp_path / "e.ini", None)
    dialog.c_tabelle.setEditText("a\x00b")
    dialog._speichern()
    assert dialog.ergebnis() is None and gemeldet and "Steuerzeichen" in gemeldet[0]


def test_beenden_speichert_einstellungen(fenster):
    fenster._einstellungen.datenbank.dateipfad = "C:/gespeichert.db"
    fenster.close()
    assert EinstellungsVerwalter(fenster._verwalter.pfad).laden().datenbank.dateipfad == "C:/gespeichert.db"


# -- Spalte „Verweis" ------------------------------------------------------------------------------

def test_fehlerliste_hat_spalte_verweis_mit_erster_verwendung(fenster, csv_fehler):
    fenster.datei_laden(csv_fehler)
    warte(fenster)
    modell = fenster._filter_modell
    assert fenster._fehler_modell.SPALTEN[-1] == "Verweis"
    spalte = fenster._fehler_modell.SPALTEN.index("Verweis")
    zeilen = {modell.index(r, 0).data(): modell.index(r, spalte).data() for r in range(modell.rowCount())}
    assert zeilen["Doppeltes Symbol"] == "Erste Verwendung auf Seite 5 – Antriebe – anlage.csv › Zeile 2"
    assert zeilen["Platzhalter (?)"] == ""                       # kein Verweis ohne Duplikat
    # Dieselbe Formulierung wie im Fundstellen-Panel
    modell_zeile = next(r for r in range(modell.rowCount()) if modell.index(r, 0).data() == "Doppeltes Symbol")
    fenster.t_fehler.setCurrentIndex(modell.index(modell_zeile, 0))
    assert fenster.d_bezug.text() == zeilen["Doppeltes Symbol"]


# -- Filter je Spalte (positiv und negativ) und Gesamtsuche ------------------------------------

def spalte_der_fehlerliste(fenster, name: str) -> int:
    return fenster._fehler_modell.SPALTEN.index(name)


def sichtbar(proxy, spalte: int) -> list[str]:
    return [proxy.index(r, spalte).data() for r in range(proxy.rowCount())]


@pytest.fixture
def viele_fehler(tmp_path) -> Path:
    zeilen = ["Symbolische Adresse;Funktionstext;SPS-Adresse;Seite"]
    zeilen += [f"Motor_{i};Text {i % 2};E{i}.0;{i}" for i in range(1, 7)]       # Texte wiederholen sich: Fehler
    zeilen += ["Motor_1;Eigener Text;E99.0;9", "Lampe_?;Anderer Text;E98.0;8"]       # doppeltes Symbol, Platzhalter
    pfad = tmp_path / "viele.csv"
    pfad.write_text("\n".join(zeilen), encoding="utf-8")
    return pfad


def test_spaltenfilter_positiv_und_negativ_ueber_die_filterzeile(fenster, viele_fehler):
    fenster.datei_laden(viele_fehler)
    warte(fenster)
    proxy = fenster._filter_modell
    gesamt = proxy.rowCount()
    kopf = fenster.t_fehler.horizontalHeader()
    typ, sym = spalte_der_fehlerliste(fenster, "Fehlertyp"), spalte_der_fehlerliste(fenster, "Symbol")

    kopf.setze_filtertext(typ, "Doppelter*")                              # positiv
    assert 0 < proxy.rowCount() < gesamt
    assert all(t == "Doppelter Funktionstext" for t in sichtbar(proxy, typ))
    kopf.setze_filtertext(sym, "!Motor_5;!Motor_6")                        # zusätzlich negativ
    assert "Motor_5" not in sichtbar(proxy, sym) and "Motor_6" not in sichtbar(proxy, sym)
    kopf.setze_filtertext(typ, "!Doppelter*")                              # nur negativ in der Typspalte
    assert all(not t.startswith("Doppelter") for t in sichtbar(proxy, typ))
    kopf.setze_filtertext(typ, "")
    kopf.setze_filtertext(sym, "")
    assert proxy.rowCount() == gesamt and not proxy.filter_aktiv


def test_filter_in_jeder_spalte_der_fehlerliste(fenster, viele_fehler):
    fenster.datei_laden(viele_fehler)
    warte(fenster)
    proxy, kopf = fenster._filter_modell, fenster.t_fehler.horizontalHeader()
    beispiel = {"Fehlertyp": "Platzhalter*", "Symbol": "Lampe*", "Funktionstext": "Anderer*", "Adresse": "E98.?",
                "Seite": "8", "Beschreibung": "*Platzhalter*", "Verweis": "!?*"}
    assert set(beispiel) == set(fenster._fehler_modell.SPALTEN)           # wirklich alle Spalten
    for name, ausdruck in beispiel.items():
        spalte = spalte_der_fehlerliste(fenster, name)
        kopf.setze_filtertext(spalte, ausdruck)
        assert proxy.rowCount() >= 1, name
        assert proxy.filter_aktiv
        kopf.setze_filtertext(spalte, "")
    kopf.setze_filtertext(spalte_der_fehlerliste(fenster, "Verweis"), "?*")        # nur Fehler MIT Verweis
    assert proxy.rowCount() >= 1 and all(sichtbar(proxy, spalte_der_fehlerliste(fenster, "Verweis")))


def test_fragezeichen_selbst_suchen_mit_maskierung(fenster, viele_fehler):
    fenster.datei_laden(viele_fehler)
    warte(fenster)
    kopf = fenster.t_fehler.horizontalHeader()
    sym = spalte_der_fehlerliste(fenster, "Symbol")
    kopf.setze_filtertext(sym, "*\\?")
    assert set(sichtbar(fenster._filter_modell, sym)) == {"Lampe_?"}


def test_gesamtsuche_positiv_und_negativ(fenster, viele_fehler):
    fenster.datei_laden(viele_fehler)
    warte(fenster)
    proxy = fenster._filter_modell
    gesamt = proxy.rowCount()
    fenster.e_suche.setText("Motor_1")
    assert 0 < proxy.rowCount() < gesamt
    fenster.e_suche.setText("!Motor_1")
    assert 0 < proxy.rowCount() < gesamt
    assert all("Motor_1" not in " ".join(proxy.index(r, c).data() for c in range(proxy.columnCount()))
               for r in range(proxy.rowCount()))
    fenster.e_suche.setText("Motor_1;Lampe*;!*Platzhalter*")
    assert "Platzhalter (?)" not in sichtbar(proxy, 0)
    assert "e" in fenster.l_zaehler.text() and " von " in fenster.l_zaehler.text()


def test_filter_zuruecksetzen(fenster, viele_fehler):
    fenster.datei_laden(viele_fehler)
    warte(fenster)
    proxy, kopf = fenster._filter_modell, fenster.t_fehler.horizontalHeader()
    gesamt = proxy.rowCount()
    fenster.c_typ.setCurrentIndex(fenster.c_typ.findData(Fehlertyp.PLATZHALTER))
    fenster.e_suche.setText("Lampe")
    kopf.setze_filtertext(spalte_der_fehlerliste(fenster, "Seite"), "8")
    assert proxy.rowCount() == 1 and proxy.filter_aktiv
    fenster.b_filter_zuruecksetzen.click()
    assert proxy.rowCount() == gesamt and not proxy.filter_aktiv
    assert fenster.e_suche.text() == "" and kopf.filtertext(spalte_der_fehlerliste(fenster, "Seite")) == ""


def test_export_beruecksichtigt_spaltenfilter(fenster, viele_fehler, tmp_path, monkeypatch):
    fenster.datei_laden(viele_fehler)
    warte(fenster)
    fenster.t_fehler.horizontalHeader().setze_filtertext(spalte_der_fehlerliste(fenster, "Fehlertyp"), "Platzhalter*")
    sichtbare = fenster._filter_modell.sichtbare_fehler()
    assert sichtbare and all(f.fehlertyp is Fehlertyp.PLATZHALTER for f in sichtbare)
    assert len(sichtbare) < len(fenster._bericht.fehler)


def test_filterzeile_sitzt_unter_der_ueberschrift_und_folgt_der_spaltenbreite(fenster, csv_fehler):
    fenster.datei_laden(csv_fehler)
    warte(fenster)
    kopf = fenster.t_fehler.horizontalHeader()
    kopf._positioniere()
    editoren = kopf._editoren
    assert len(editoren) == fenster._fehler_modell.columnCount() == 7
    assert kopf.sizeHint().height() > kopf._beschriftungshoehe()          # Platz für die Filterzeile
    for spalte in range(3):
        assert editoren[spalte].x() >= kopf.sectionViewportPosition(spalte)
        assert editoren[spalte].width() <= kopf.sectionSize(spalte)
        assert editoren[spalte].y() >= kopf._beschriftungshoehe()
    fenster.t_fehler.setColumnWidth(0, 300)
    kopf._positioniere()
    assert editoren[0].width() > 250 and editoren[1].x() >= 300


def test_listen_starten_in_dateireihenfolge(fenster, viele_fehler):
    fenster.datei_laden(viele_fehler)
    warte(fenster)
    nummern = [fenster._datensatz_filter.index(r, 0).data() for r in range(fenster._datensatz_filter.rowCount())]
    assert nummern == [str(n) for n in range(1, 9)]


def test_filter_der_datensatzliste_und_gemeinsame_syntax(fenster, viele_fehler):
    fenster.datei_laden(viele_fehler)
    warte(fenster)
    proxy, kopf = fenster._datensatz_filter, fenster.t_daten.horizontalHeader()
    assert proxy.rowCount() == 8 and "8 Datensätze" in fenster.l_zaehler_daten.text()
    kopf.setze_filtertext(fenster._datensatz_modell.SPALTEN.index("Symbolische Adresse"), "Motor_?;!Motor_1")
    assert proxy.rowCount() == 5 and "5 von 8" in fenster.l_zaehler_daten.text()
    fenster.b_daten_filter_zuruecksetzen.click()
    assert proxy.rowCount() == 8
    fenster.e_suche_daten.setText("!Motor*")
    assert proxy.rowCount() == 1
    # Auswahl in der gefilterten/sortierten Liste liefert den richtigen Datensatz
    fenster.tabs.setCurrentIndex(1)
    fenster.t_daten.setCurrentIndex(proxy.index(0, 0))
    assert fenster._aktueller_datensatz()[0].symbolische_adresse == "Lampe_?"


# -- Optionaler Datenbankabgleich mit SQLite -----------------------------------------------------

def test_datenbankabgleich_ist_standardmaessig_aus_und_zeigt_nichts_an(fenster, csv_fehler):
    assert not fenster.a_dbabgleich.isChecked()
    fenster.datei_laden(csv_fehler)
    warte(fenster)
    spalte = fenster._datensatz_modell.spalte_datenbank
    assert {fenster._datensatz_modell.index(r, spalte).data() for r in range(3)} == {"–"}
    assert "Datenbank:" not in fenster.l_zusammenfassung.text() and not fenster.l_hinweis.isVisible()


def test_datenbankabgleich_zeigt_je_datensatz_ob_symbol_mit_funktionstext_vorhanden_ist(
        app, tmp_path, meldungen, datenbank_einstellungen):
    csv = tmp_path / "anlage.csv"
    csv.write_text("Symbolische Adresse;Funktionstext;SPS-Adresse;Seite\n"
                   "Motor_Start;Förderband Start;E0.0;5\n"
                   "Neu_1;Neuer Text;E0.1;5\n"
                   "Motor_Stop;Anderer Text;E0.2;6\n"
                   "Motor_Start;Förderband Stop;E0.3;7\n", encoding="utf-8")
    e = Einstellungen()
    e.datenbank = datenbank_einstellungen
    f = gui.Hauptfenster(EinstellungsVerwalter(tmp_path / "k.ini"), e, None)
    f.show()
    try:
        f.datei_laden(csv)
        warte(f)
        assert f._bericht.datenbankstatus == {}                              # optional: nicht eingeschaltet
        f.a_dbabgleich.setChecked(True)                                      # Menüschalter prüft sofort neu
        warte(f)
        spalte = f._datensatz_modell.spalte_datenbank
        assert [f._datensatz_modell.index(r, spalte).data() for r in range(4)] == [
            "In Datenbank vorhanden", "Symbol fehlt in Datenbank", "Funktionstext fehlt in Datenbank",
            "Kombination weicht ab"]
        assert "Datenbank: 1 von 4 vorhanden" in f.l_zusammenfassung.text()
        # nach Datenbankstatus filtern (positiv / negativ)
        kopf = f.t_daten.horizontalHeader()
        kopf.setze_filtertext(spalte, "In Datenbank vorhanden")
        assert f._datensatz_filter.rowCount() == 1
        kopf.setze_filtertext(spalte, "!In Datenbank vorhanden")
        assert f._datensatz_filter.rowCount() == 3
        # Einstellung wurde gespeichert
        assert EinstellungsVerwalter(tmp_path / "k.ini").laden().pruefung.datenbankabgleich is True
        f.a_dbabgleich.setChecked(False)
        warte(f)
        assert f._bericht.datenbankstatus == {} and all(
            f._datensatz_modell.index(r, spalte).data() == "–" for r in range(4))
    finally:
        f.close()


def test_uebernahme_in_die_datenbank_im_ganzen_ablauf(app, tmp_path, meldungen, datenbank_einstellungen):
    csv = tmp_path / "anlage.csv"
    csv.write_text(
        "Symbolische Adresse;Funktionstext;SPS-Adresse;Seite\n"
        "Lampe;Meldeleuchte;E0.0;5\n"                 # ok
        "Neu_1;Neuer Text;E0.1;5\n"                    # Symbol fehlt
        "Motor_Stop;Anderer Text;E0.2;6\n"             # Funktionstext fehlt (Symbol existiert)
        "Motor_Start;Förderband Stop;E0.3;7\n",        # Kombination weicht ab
        encoding="utf-8")
    e = Einstellungen()
    e.pruefung.datenbankabgleich = True
    e.datenbank = datenbank_einstellungen
    f = gui.Hauptfenster(EinstellungsVerwalter(tmp_path / "k.ini"), e, None)
    f.show()
    try:
        f.datei_laden(csv)
        warte(f)
        assert [x.fehlertyp for x in f._bericht.fehler] == [
            Fehlertyp.SYMBOL_NICHT_IN_DB, Fehlertyp.FUNKTIONSTEXT_NICHT_IN_DB, Fehlertyp.KOMBINATION_ABWEICHEND]

        f.t_fehler.clearSelection()                       # ohne Auswahl: Hinweis, nichts passiert
        f._in_datenbank_uebernehmen()
        assert meldungen.protokoll[-1][0] == "info"
        f.t_fehler.selectRow(2)                           # Kombination ist nicht übernehmbar
        meldungen.protokoll.clear()
        f._in_datenbank_uebernehmen()
        assert meldungen.arten() == ["info"]

        meldungen.antworten[:] = [True, True]             # Rückfrage 1 = ja; Konflikt (Symbol existiert) = ja
        meldungen.protokoll.clear()
        f.t_fehler.selectRow(0)
        f.t_fehler.selectionModel().select(f._filter_modell.index(1, 0),
                                           f.t_fehler.selectionModel().SelectionFlag.Select
                                           | f.t_fehler.selectionModel().SelectionFlag.Rows)
        f._in_datenbank_uebernehmen()
        warte(f, 30000)
        assert meldungen.arten().count("frage") == 2 and meldungen.arten()[-1] == "info"
        assert "2 Datensatz/-sätze in die Datenbank übernommen" in meldungen.protokoll[-1][2]

        paare = SqliteReferenzRepository(datenbank_einstellungen).lade_referenzdaten()
        assert ("Neu_1", "Neuer Text") in paare and ("Motor_Stop", "Anderer Text") in paare
        assert [x.fehlertyp for x in f._bericht.fehler] == [Fehlertyp.KOMBINATION_ABWEICHEND]      # automatische Neuprüfung
        spalte = f._datensatz_modell.spalte_datenbank
        assert [f._datensatz_modell.index(r, spalte).data() for r in range(4)] == [
            "In Datenbank vorhanden", "In Datenbank vorhanden", "In Datenbank vorhanden", "Kombination weicht ab"]
    finally:
        f.close()


def test_fehlende_datenbankdatei_zeigt_hinweis_und_andere_pruefungen_laufen(app, tmp_path, meldungen,
                                                                           datenbank_einstellungen):
    csv = tmp_path / "a.csv"
    csv.write_text(CSV_MIT_FEHLERN, encoding="utf-8")
    e = Einstellungen()
    e.pruefung.datenbankabgleich = True
    e.datenbank = dataclasses.replace(datenbank_einstellungen, dateipfad=str(tmp_path / "weg.db"))
    f = gui.Hauptfenster(EinstellungsVerwalter(tmp_path / "k.ini"), e, None)
    f.show()
    try:
        f.datei_laden(csv)
        warte(f)
        assert "Datenbankabgleich" in f._bericht.uebersprungen
        assert "existiert nicht" in f._bericht.uebersprungen["Datenbankabgleich"]
        assert "Nicht ausgeführt: Datenbankabgleich" in f.l_hinweis.text() and f.l_hinweis.isVisible()
        assert len(f._bericht.fehler) == 3          # die übrigen Prüfungen liefern weiterhin Ergebnisse
    finally:
        f.close()


def test_abgleich_ohne_konfiguration_meldet_nicht_konfiguriert(fenster, csv_fehler):
    fenster._einstellungen.pruefung.datenbankabgleich = True
    fenster.datei_laden(csv_fehler)
    warte(fenster)
    assert "nicht konfiguriert" in fenster._bericht.uebersprungen["Datenbankabgleich"]


# -- Einstellungsdialog: SQLite-Datei, Tabellen- und Feldauswahl -------------------------------------

def test_dialog_liest_tabellen_und_felder_aus_der_datei(app, tmp_path, datenbank_einstellungen):
    e = Einstellungen()
    dialog = EinstellungsDialog(e, tmp_path / "e.ini", None)
    dialog.e_datei.setText(datenbank_einstellungen.dateipfad)
    dialog._lade_tabellen()
    assert [dialog.c_tabelle.itemText(i) for i in range(dialog.c_tabelle.count())] == ["symbole"]
    assert dialog.c_tabelle.currentText() == "symbole"                        # einzige Tabelle wird vorgeschlagen
    assert [dialog.c_feld_symbol.itemText(i) for i in range(dialog.c_feld_symbol.count())] == ["id", "symbol", "funktionstext"]
    assert dialog.c_primaer.itemText(0) == ""                                 # Primärschlüssel ist optional
    assert "1 Tabelle" in dialog.l_auswahlhinweis.text()


def test_dialog_zeigt_fehler_beim_lesen_als_hinweis_statt_als_dialog(app, tmp_path):
    dialog = EinstellungsDialog(Einstellungen(), tmp_path / "e.ini", None)
    dialog.e_datei.setText(str(tmp_path / "gibt_es_nicht.db"))
    dialog._lade_tabellen()
    assert "existiert nicht" in dialog.l_auswahlhinweis.text()


def test_dialog_behaelt_gespeicherte_werte_beim_oeffnen(app, tmp_path, datenbank_einstellungen):
    e = Einstellungen()
    e.datenbank = datenbank_einstellungen
    dialog = EinstellungsDialog(e, tmp_path / "e.ini", None)
    assert (dialog.c_tabelle.currentText(), dialog.c_feld_symbol.currentText(),
            dialog.c_feld_text.currentText(), dialog.c_primaer.currentText()) == ("symbole", "symbol", "funktionstext", "id")
    dialog._speichern()
    assert dialog.ergebnis().datenbank == datenbank_einstellungen


def test_verbindungstest_im_einstellungsdialog(app, tmp_path, datenbank_einstellungen):
    e = Einstellungen()
    e.datenbank = datenbank_einstellungen
    dialog = EinstellungsDialog(e, tmp_path / "e.ini", None)
    dialog.b_test.click()
    assert warte_bis(lambda: dialog.b_test.isEnabled(), 15000)
    assert dialog.t_testergebnis.toPlainText().startswith("✔ Test erfolgreich")
    dialog.c_feld_text.setEditText("falsch")
    dialog.b_test.click()
    assert warte_bis(lambda: dialog.b_test.isEnabled(), 15000)
    assert dialog.t_testergebnis.toPlainText().startswith("✘") and "falsch" in dialog.t_testergebnis.toPlainText()
