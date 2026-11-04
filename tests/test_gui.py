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
from eplan_pruefer.datenbank import MySqlReferenzRepository  # noqa: E402
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
    dialog.e_server.setText("db.firma.de")
    dialog.e_port.setValue(3307)
    dialog.e_datenbank.setText("ref")
    dialog.e_benutzer.setText("u")
    dialog.e_passwort.setText("pw")
    dialog.e_tabelle.setText("t")
    dialog.e_feld_symbol.setText("sym")
    dialog.e_feld_text.setText("txt")
    dialog.e_primaer.setText("id")
    dialog.c_adressen.setChecked(False)
    dialog.e_felder["symbolische_adresse"].setText("Kurzname; P20404")
    dialog._speichern()
    neu = dialog.ergebnis()
    assert neu is not None
    d = neu.datenbank
    assert (d.server, d.port, d.datenbankname, d.benutzer, d.passwort) == ("db.firma.de", 3307, "ref", "u", "pw")
    assert (d.tabellenname, d.feld_symbol, d.feld_funktionstext, d.primaerschluessel) == ("t", "sym", "txt", "id")
    assert neu.pruefung.doppelte_adressen is False
    assert neu.feldzuordnung["symbolische_adresse"] == ("Kurzname", "P20404")
    assert e.datenbank.server == "localhost"          # Ausgangsobjekt bleibt unverändert


def test_einstellungsdialog_lehnt_ungueltiges_ab(app, tmp_path, monkeypatch):
    gemeldet = []
    monkeypatch.setattr("eplan_pruefer.gui_dialoge.zeige_warnung", lambda *a: gemeldet.append(a[-1]))
    dialog = EinstellungsDialog(Einstellungen(), tmp_path / "e.ini", None)
    dialog.e_tabelle.setText("a`b")
    dialog._speichern()
    assert dialog.ergebnis() is None and gemeldet and "Backticks" in gemeldet[0]


def test_beenden_speichert_einstellungen(fenster):
    fenster._einstellungen.datenbank.server = "gespeichert.example"
    fenster.close()
    assert EinstellungsVerwalter(fenster._verwalter.pfad).laden().datenbank.server == "gespeichert.example"


# -- mit echtem Datenbankserver -----------------------------------------------------

@pytest.mark.mysql
def test_verbindungstest_im_einstellungsdialog(app, tmp_path, datenbank_einstellungen, sql):
    sql("DROP TABLE IF EXISTS symbole; CREATE TABLE symbole (id INT AUTO_INCREMENT PRIMARY KEY, "
        "symbol VARCHAR(100), funktionstext VARCHAR(200));")
    e = Einstellungen()
    e.datenbank = datenbank_einstellungen
    dialog = EinstellungsDialog(e, tmp_path / "e.ini", None)
    dialog.b_test.click()
    assert warte_bis(lambda: dialog.b_test.isEnabled(), 15000)
    assert dialog.t_testergebnis.toPlainText().startswith("✔ Test erfolgreich")
    dialog.e_feld_text.setText("falsch")
    dialog.b_test.click()
    assert warte_bis(lambda: dialog.b_test.isEnabled(), 15000)
    assert dialog.t_testergebnis.toPlainText().startswith("✘")


@pytest.mark.mysql
def test_datenbankabgleich_und_uebernahme_im_ganzen_ablauf(app, tmp_path, meldungen, datenbank_einstellungen, sql):
    sql("DROP TABLE IF EXISTS symbole; CREATE TABLE symbole (id INT AUTO_INCREMENT PRIMARY KEY, "
        "symbol VARCHAR(100) NOT NULL, funktionstext VARCHAR(200) NOT NULL) CHARACTER SET utf8mb4; "
        "INSERT INTO symbole (symbol, funktionstext) VALUES ('Motor_Start','Förderband Start'),"
        "('Motor_Stop','Förderband Stop'),('Lampe','Meldeleuchte');")
    csv = tmp_path / "anlage.csv"
    csv.write_text(
        "Symbolische Adresse;Funktionstext;SPS-Adresse;Seite\n"
        "Lampe;Meldeleuchte;E0.0;5\n"                  # ok
        "Neu_1;Neuer Text;E0.1;5\n"                    # Symbol fehlt
        "Motor_Stop;Anderer Text;E0.2;6\n"             # Funktionstext fehlt (Symbol existiert)
        "Motor_Start;Förderband Stop;E0.3;7\n",        # Kombination weicht ab
        encoding="utf-8")
    e = Einstellungen()
    e.datenbank = datenbank_einstellungen
    f = gui.Hauptfenster(EinstellungsVerwalter(tmp_path / "k.ini"), e, None)
    f.show()
    try:
        f.datei_laden(csv)
        warte(f)
        typen = [x.fehlertyp for x in f._bericht.fehler]
        assert typen == [Fehlertyp.SYMBOL_NICHT_IN_DB, Fehlertyp.FUNKTIONSTEXT_NICHT_IN_DB,
                         Fehlertyp.KOMBINATION_ABWEICHEND]

        # Ohne passende Auswahl: Hinweis, nichts passiert
        f.t_fehler.clearSelection()
        f._in_datenbank_uebernehmen()
        assert meldungen.protokoll[-1][0] == "info"

        # Kombination ist nicht übernehmbar
        f.t_fehler.selectRow(2)
        meldungen.protokoll.clear()
        f._in_datenbank_uebernehmen()
        assert meldungen.arten() == ["info"]

        # Beide übernehmbaren Fehler markieren: Rückfrage 1 = ja; Konflikt (Symbol existiert) = ja
        meldungen.antworten[:] = [True, True]
        meldungen.protokoll.clear()
        f.t_fehler.selectRow(0)
        f.t_fehler.selectionModel().select(f._filter_modell.index(1, 0),
                                           f.t_fehler.selectionModel().SelectionFlag.Select
                                           | f.t_fehler.selectionModel().SelectionFlag.Rows)
        f._in_datenbank_uebernehmen()
        warte(f, 30000)
        reihenfolge = meldungen.arten()
        assert reihenfolge.count("frage") == 2 and reihenfolge[-1] == "info"
        assert "2 Datensatz/-sätze in die Datenbank übernommen" in meldungen.protokoll[-1][2]

        paare = MySqlReferenzRepository(datenbank_einstellungen).lade_referenzdaten()
        assert ("Neu_1", "Neuer Text") in paare and ("Motor_Stop", "Anderer Text") in paare
        # Automatische Neuprüfung: nur die Kombinationsabweichung bleibt
        assert [x.fehlertyp for x in f._bericht.fehler] == [Fehlertyp.KOMBINATION_ABWEICHEND]
    finally:
        f.close()


@pytest.mark.mysql
def test_datenbank_nicht_erreichbar_zeigt_hinweis_und_andere_pruefungen_laufen(app, tmp_path, meldungen,
                                                                               datenbank_einstellungen):
    csv = tmp_path / "a.csv"
    csv.write_text(CSV_MIT_FEHLERN, encoding="utf-8")
    e = Einstellungen()
    e.datenbank = dataclasses.replace(datenbank_einstellungen, passwort="falsch")
    f = gui.Hauptfenster(EinstellungsVerwalter(tmp_path / "k.ini"), e, None)
    f.show()
    try:
        f.datei_laden(csv)
        warte(f)
        assert "Datenbankabgleich" in f._bericht.uebersprungen
        assert "Nicht ausgeführt: Datenbankabgleich" in f.l_hinweis.text() and f.l_hinweis.isVisible()
        assert len(f._bericht.fehler) == 3          # die übrigen Prüfungen liefern weiterhin Ergebnisse
    finally:
        f.close()
