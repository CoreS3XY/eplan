"""
gui_dialoge.py – Dialoge der Oberfläche.

* Meldungsfunktionen (zentral, damit sie in Tests ersetzt werden können)
* :class:`EinstellungsDialog` – Verwaltung von Datenbank, Prüfungen und Feldzuordnung
* :class:`SeitenDialog` – „Sprung zur Fehlerstelle“: Seite, Seitenname, Fundstelle, Quellauszug
* :class:`EinleseberichtDialog` – Teildateien, Warnungen und Strukturbericht
* :class:`TextDialog` / :class:`ProtokollDialog` – Textanzeigen
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Callable

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont, QFontDatabase
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .datenbank import erzeuge_repository, lese_spaltennamen, lese_tabellen
from .einstellungen import EinstellungsVerwalter
from .eplan_parser import QuellenAuszugLeser
from .gui_arbeiter import HintergrundAufgabe
from .logging import hole_protokollierer, hole_speicher_handler
from .modelle import (
    FELD_ANZEIGENAMEN,
    FELDNAMEN,
    STANDARD_FELDZUORDNUNG,
    EplanDatensatz,
    EplanPrueferFehler,
    Einleseergebnis,
    Einstellungen,
    Verbindungstest,
)

_log = hole_protokollierer("gui.dialoge")


# ---------------------------------------------------------------------------
# Meldungen (zentral, damit Tests sie ersetzen können)
# ---------------------------------------------------------------------------

def zeige_information(eltern: QWidget | None, titel: str, text: str) -> None:
    QMessageBox.information(eltern, titel, text)


def zeige_warnung(eltern: QWidget | None, titel: str, text: str) -> None:
    QMessageBox.warning(eltern, titel, text)


def zeige_fehler(eltern: QWidget | None, titel: str, text: str) -> None:
    QMessageBox.critical(eltern, titel, text)


def stelle_frage(eltern: QWidget | None, titel: str, text: str) -> bool:
    """Ja/Nein-Frage; Standardantwort ist „Nein“."""
    antwort = QMessageBox.question(
        eltern, titel, text,
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No)
    return antwort == QMessageBox.StandardButton.Yes


def _festbreitenschrift() -> QFont:
    return QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)


# ---------------------------------------------------------------------------
# Einstellungen
# ---------------------------------------------------------------------------

class EinstellungsDialog(QDialog):
    """Verwaltungsdialog für alle Einstellungen (Datenbank, Prüfungen, Feldzuordnung, Allgemein)."""

    def __init__(self, einstellungen: Einstellungen, konfigurationspfad: Path,
                 protokollpfad: Path | None, eltern: QWidget | None = None) -> None:
        super().__init__(eltern)
        self.setWindowTitle("Einstellungen")
        self.setMinimumWidth(620)
        self._ausgang = copy.deepcopy(einstellungen)
        self._test: HintergrundAufgabe | None = None
        self._ergebnis: Einstellungen | None = None

        tabs = QTabWidget()
        tabs.addTab(self._baue_datenbank_tab(), "Datenbank")
        tabs.addTab(self._baue_pruefungen_tab(), "Prüfungen")
        tabs.addTab(self._baue_feldzuordnung_tab(), "Feldzuordnung")
        tabs.addTab(self._baue_allgemein_tab(konfigurationspfad, protokollpfad), "Allgemein")

        tasten = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        tasten.button(QDialogButtonBox.StandardButton.Ok).setText("Speichern")
        tasten.button(QDialogButtonBox.StandardButton.Cancel).setText("Abbrechen")
        tasten.accepted.connect(self._speichern)
        tasten.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(tabs)
        layout.addWidget(tasten)
        self._fuelle(self._ausgang)

    # -- Aufbau ------------------------------------------------------------------

    def _baue_datenbank_tab(self) -> QWidget:
        seite = QWidget()
        form = QFormLayout()
        self.e_datei = QLineEdit()
        self.e_datei.setPlaceholderText("Pfad der SQLite-Datei, z. B. C:\\Daten\\symbole.db")
        self.b_durchsuchen = QPushButton("Durchsuchen …")
        self.b_durchsuchen.clicked.connect(self._datei_waehlen)
        dateizeile = QHBoxLayout()
        dateizeile.addWidget(self.e_datei, 1)
        dateizeile.addWidget(self.b_durchsuchen)
        self.e_datei.editingFinished.connect(self._lade_tabellen)

        # Tabelle und Felder: frei eintragbar, nach Auswahl der Datei aber aus der Datenbank wählbar
        self.c_tabelle = QComboBox()
        self.c_feld_symbol = QComboBox()
        self.c_feld_text = QComboBox()
        self.c_primaer = QComboBox()
        for combo in (self.c_tabelle, self.c_feld_symbol, self.c_feld_text, self.c_primaer):
            combo.setEditable(True)
            combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.c_primaer.lineEdit().setPlaceholderText("optional, z. B. id")
        self.c_tabelle.currentIndexChanged.connect(lambda _i: self._lade_spalten())
        self.c_tabelle.lineEdit().editingFinished.connect(self._lade_spalten)
        self.e_timeout = QSpinBox()
        self.e_timeout.setRange(1, 300)
        self.e_timeout.setSuffix(" s")
        self.e_timeout.setToolTip("Wartezeit, wenn die Datei gerade von einem anderen Programm gesperrt ist")

        form.addRow("Datenbankdatei (*.db):", dateizeile)
        form.addRow("Tabellenname:", self.c_tabelle)
        form.addRow("Feldname Symbol:", self.c_feld_symbol)
        form.addRow("Feldname Funktionstext:", self.c_feld_text)
        form.addRow("Primärschlüssel:", self.c_primaer)
        form.addRow("Wartezeit bei Sperre:", self.e_timeout)

        self.l_auswahlhinweis = QLabel("")
        self.l_auswahlhinweis.setWordWrap(True)
        self.b_test = QPushButton("Verbindung testen")
        self.b_test.clicked.connect(self._teste_verbindung)
        self.t_testergebnis = QPlainTextEdit()
        self.t_testergebnis.setReadOnly(True)
        self.t_testergebnis.setMaximumHeight(120)
        self.t_testergebnis.setPlaceholderText("Das Ergebnis des Verbindungstests erscheint hier.")

        layout = QVBoxLayout(seite)
        layout.addLayout(form)
        layout.addWidget(self.l_auswahlhinweis)
        layout.addWidget(self.b_test, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.t_testergebnis)
        hinweis = QLabel("Der Abgleich ist optional (siehe Reiter „Prüfungen“ oder Menü „Prüfung“). Die Datei wird "
                         "zum Abgleich nur gelesen; geschrieben wird ausschließlich über „In Datenbank übernehmen“.")
        hinweis.setWordWrap(True)
        layout.addWidget(hinweis)
        return seite

    def _datei_waehlen(self) -> None:
        start = self.e_datei.text().strip() or str(Path.home())
        pfad, _ = QFileDialog.getOpenFileName(
            self, "SQLite-Datenbank wählen", start,
            "SQLite-Datenbank (*.db *.sqlite *.sqlite3 *.db3);;Alle Dateien (*)")
        if pfad:
            self.e_datei.setText(pfad)
            self._lade_tabellen()

    @staticmethod
    def _fuelle_combo(combo: QComboBox, eintraege: list[str], bisher: str) -> None:
        combo.blockSignals(True)
        combo.clear()
        combo.addItems(eintraege)
        combo.setEditText(bisher)
        if bisher in eintraege:
            combo.setCurrentIndex(eintraege.index(bisher))
        combo.blockSignals(False)

    def _lade_tabellen(self) -> None:
        """Füllt die Tabellenauswahl aus der Datei (Fehler werden als Hinweis angezeigt, nicht als Dialog)."""
        pfad = self.e_datei.text().strip()
        bisher = self.c_tabelle.currentText()
        if not pfad:
            self.l_auswahlhinweis.setText("")
            return
        try:
            tabellen = lese_tabellen(pfad, self.e_timeout.value())
        except EplanPrueferFehler as fehler:
            self.l_auswahlhinweis.setText(f"Tabellen konnten nicht gelesen werden: {fehler}")
            return
        self.l_auswahlhinweis.setText(f"{len(tabellen)} Tabelle(n) in der Datei gefunden.")
        self._fuelle_combo(self.c_tabelle, tabellen, bisher if bisher in tabellen else
                           (tabellen[0] if len(tabellen) == 1 and not bisher else bisher))
        self._lade_spalten()

    def _lade_spalten(self) -> None:
        pfad, tabelle = self.e_datei.text().strip(), self.c_tabelle.currentText().strip()
        if not pfad or not tabelle:
            return
        try:
            spalten = lese_spaltennamen(pfad, tabelle, self.e_timeout.value())
        except EplanPrueferFehler:
            return
        if not spalten:
            return
        for combo, mit_leer in ((self.c_feld_symbol, False), (self.c_feld_text, False), (self.c_primaer, True)):
            bisher = combo.currentText()
            self._fuelle_combo(combo, ([""] if mit_leer else []) + spalten, bisher)

    def _baue_pruefungen_tab(self) -> QWidget:
        seite = QWidget()
        layout = QVBoxLayout(seite)
        gruppe = QGroupBox("Aktive Prüfungen")
        gl = QVBoxLayout(gruppe)
        self.c_symbole = QCheckBox("1  Doppelte Symbole")
        self.c_texte = QCheckBox("2  Doppelte Funktionstexte")
        self.c_adressen = QCheckBox("3  Doppelte SPS-Adressen")
        self.c_platzhalter = QCheckBox("4  Platzhalter (Fragezeichen)")
        self.c_datenbank = QCheckBox("5  Abgleich mit der Referenzdatenbank (optional)")
        for c in (self.c_symbole, self.c_texte, self.c_adressen, self.c_platzhalter, self.c_datenbank):
            gl.addWidget(c)
        layout.addWidget(gruppe)
        self.c_gross_klein = QCheckBox("Groß-/Kleinschreibung bei Symbolen und Funktionstexten beachten")
        self.c_automatisch = QCheckBox("Nach dem Einlesen automatisch prüfen")
        layout.addWidget(self.c_gross_klein)
        layout.addWidget(self.c_automatisch)
        layout.addStretch(1)
        return seite

    def _baue_feldzuordnung_tab(self) -> QWidget:
        seite = QWidget()
        layout = QVBoxLayout(seite)
        erklaerung = QLabel(
            "Unter welchen Bezeichnungen kommen die Felder in der EPLAN-Datei vor "
            "(Spaltenüberschrift, XML-Element oder -Attribut)? Mehrere Bezeichnungen mit Semikolon "
            "trennen. Groß-/Kleinschreibung, Leer- und Sonderzeichen spielen keine Rolle. "
            "Hier können auch EPLAN-Eigenschaftsnummern (z. B. P20400) eingetragen werden.")
        erklaerung.setWordWrap(True)
        layout.addWidget(erklaerung)
        form = QFormLayout()
        self.e_felder: dict[str, QLineEdit] = {}
        for feld in FELDNAMEN:
            edit = QLineEdit()
            self.e_felder[feld] = edit
            form.addRow(FELD_ANZEIGENAMEN[feld] + ":", edit)
        layout.addLayout(form)
        zuruecksetzen = QPushButton("Auf Standard zurücksetzen")
        zuruecksetzen.clicked.connect(self._felder_zuruecksetzen)
        layout.addWidget(zuruecksetzen, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addStretch(1)
        return seite

    def _baue_allgemein_tab(self, konfigurationspfad: Path, protokollpfad: Path | None) -> QWidget:
        seite = QWidget()
        form = QFormLayout(seite)
        self.c_stufe = QComboBox()
        self.c_stufe.addItems(["DEBUG", "INFO", "WARNING", "ERROR"])
        self.e_sprache = QLineEdit()
        self.e_sprache.setPlaceholderText("z. B. de_DE")
        form.addRow("Protokollstufe:", self.c_stufe)
        form.addRow("Sprache mehrsprachiger Texte:", self.e_sprache)
        for beschriftung, pfad in (("Konfigurationsdatei:", konfigurationspfad),
                                   ("Protokolldatei:", protokollpfad)):
            label = QLabel(str(pfad) if pfad else "(keine)")
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            label.setWordWrap(True)
            form.addRow(beschriftung, label)
        return seite

    # -- Daten <-> Widgets ------------------------------------------------------------

    def _fuelle(self, e: Einstellungen) -> None:
        d = e.datenbank
        self.e_datei.setText(d.dateipfad)
        self.c_tabelle.setEditText(d.tabellenname)
        self.c_feld_symbol.setEditText(d.feld_symbol)
        self.c_feld_text.setEditText(d.feld_funktionstext)
        self.c_primaer.setEditText(d.primaerschluessel)
        self.e_timeout.setValue(d.timeout_s)
        if d.dateipfad.strip() and Path(d.dateipfad.strip()).expanduser().is_file():
            self._lade_tabellen()
        p = e.pruefung
        self.c_symbole.setChecked(p.doppelte_symbole)
        self.c_texte.setChecked(p.doppelte_funktionstexte)
        self.c_adressen.setChecked(p.doppelte_adressen)
        self.c_platzhalter.setChecked(p.platzhalter)
        self.c_datenbank.setChecked(p.datenbankabgleich)
        self.c_gross_klein.setChecked(p.gross_kleinschreibung_beachten)
        self.c_automatisch.setChecked(p.automatisch_pruefen)
        for feld in FELDNAMEN:
            self.e_felder[feld].setText("; ".join(e.feldzuordnung.get(feld, ())))
        self.c_stufe.setCurrentText(e.allgemein.protokollstufe.upper())
        self.e_sprache.setText(e.allgemein.bevorzugte_sprache)

    def _lese(self) -> Einstellungen:
        e = copy.deepcopy(self._ausgang)
        d = e.datenbank
        d.dateipfad = self.e_datei.text().strip()
        d.tabellenname = self.c_tabelle.currentText().strip()
        d.feld_symbol = self.c_feld_symbol.currentText().strip()
        d.feld_funktionstext = self.c_feld_text.currentText().strip()
        d.primaerschluessel = self.c_primaer.currentText().strip()
        d.timeout_s = self.e_timeout.value()
        p = e.pruefung
        p.doppelte_symbole = self.c_symbole.isChecked()
        p.doppelte_funktionstexte = self.c_texte.isChecked()
        p.doppelte_adressen = self.c_adressen.isChecked()
        p.platzhalter = self.c_platzhalter.isChecked()
        p.datenbankabgleich = self.c_datenbank.isChecked()
        p.gross_kleinschreibung_beachten = self.c_gross_klein.isChecked()
        p.automatisch_pruefen = self.c_automatisch.isChecked()
        e.feldzuordnung = {
            feld: tuple(a.strip() for a in self.e_felder[feld].text().split(";") if a.strip())
            for feld in FELDNAMEN}
        e.allgemein.protokollstufe = self.c_stufe.currentText()
        e.allgemein.bevorzugte_sprache = self.e_sprache.text().strip() or "de_DE"
        return e

    def _felder_zuruecksetzen(self) -> None:
        for feld in FELDNAMEN:
            self.e_felder[feld].setText("; ".join(STANDARD_FELDZUORDNUNG[feld]))

    # -- Aktionen -------------------------------------------------------------------------

    def _teste_verbindung(self) -> None:
        einstellungen = self._lese()
        repository = erzeuge_repository(einstellungen.datenbank)
        self.b_test.setEnabled(False)
        self.t_testergebnis.setPlainText("Verbinde …")
        aufgabe = HintergrundAufgabe(lambda _melde, _abbruch: repository.teste_verbindung(),
                                     self, "Verbindungstest")
        aufgabe.fertig.connect(self._zeige_testergebnis)
        aufgabe.fehlgeschlagen.connect(lambda text: self._zeige_testergebnis(Verbindungstest(False, (text,))))
        aufgabe.beendet.connect(lambda: self.b_test.setEnabled(True))
        self._test = aufgabe
        aufgabe.starten()

    def _zeige_testergebnis(self, ergebnis: Verbindungstest) -> None:
        kopf = "✔ Test erfolgreich" if ergebnis.erfolgreich else "✘ Test fehlgeschlagen"
        self.t_testergebnis.setPlainText(kopf + "\n" + "\n".join(ergebnis.meldungen))

    def _speichern(self) -> None:
        neu = self._lese()
        try:
            EinstellungsVerwalter.pruefe(neu)
        except EplanPrueferFehler as fehler:
            zeige_warnung(self, "Einstellungen unvollständig", str(fehler))
            return
        self._ergebnis = neu
        self.accept()

    def ergebnis(self) -> Einstellungen | None:
        """Die bestätigten Einstellungen (nur nach ``accept``)."""
        return self._ergebnis

    def done(self, code: int) -> None:
        if self._test is not None and self._test.laeuft:
            self._test.abbrechen()
            self._test.warte_auf_ende(self._ausgang.datenbank.timeout_s * 1000 + 2000)
        super().done(code)


# ---------------------------------------------------------------------------
# Sprung zur Fehlerstelle
# ---------------------------------------------------------------------------

class SeitenDialog(QDialog):
    """
    Zeigt die Seite einer Fundstelle: Seitennummer, Seitenname, Referenz, Fundstelle,
    alle Datensätze der Seite (der betroffene hervorgehoben) und den Quellauszug.
    """

    def __init__(self, ergebnis: Einleseergebnis, stellen: list[tuple[str, EplanDatensatz]],
                 eltern: QWidget | None = None,
                 auszug_leser: QuellenAuszugLeser | None = None) -> None:
        super().__init__(eltern)
        self.setWindowTitle("Fundstelle")
        self.resize(900, 620)
        self._ergebnis = ergebnis
        self._stellen = stellen
        self._auszug_leser = auszug_leser or QuellenAuszugLeser()

        self.auswahl = QComboBox()
        for beschriftung, _ in stellen:
            self.auswahl.addItem(beschriftung)
        self.auswahl.setVisible(len(stellen) > 1)

        self.l_seite = QLabel()
        self.l_name = QLabel()
        self.l_referenz = QLabel()
        self.l_fundstelle = QLabel()
        for label in (self.l_seite, self.l_name, self.l_referenz, self.l_fundstelle):
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            label.setWordWrap(True)
        kopf = QFormLayout()
        kopf.addRow("Seitennummer:", self.l_seite)
        kopf.addRow("Seitenname:", self.l_name)
        kopf.addRow("Seitenreferenz:", self.l_referenz)
        kopf.addRow("Fundstelle:", self.l_fundstelle)

        self.tabelle = QTableWidget(0, 5)
        self.tabelle.setHorizontalHeaderLabels(
            ["SPS-Adresse", "Symbolische Adresse", "Funktionstext", "BMK", "Fundstelle"])
        self.tabelle.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tabelle.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tabelle.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.tabelle.horizontalHeader().setStretchLastSection(True)

        self.quelltext = QPlainTextEdit()
        self.quelltext.setReadOnly(True)
        self.quelltext.setFont(_festbreitenschrift())
        self.quelltext.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)

        tabs = QTabWidget()
        tabs.addTab(self.tabelle, "Inhalt der Seite")
        tabs.addTab(self.quelltext, "Quelle")

        schliessen = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        schliessen.button(QDialogButtonBox.StandardButton.Close).setText("Schließen")
        schliessen.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(self.auswahl)
        layout.addLayout(kopf)
        layout.addWidget(tabs, 1)
        layout.addWidget(schliessen)

        self.auswahl.currentIndexChanged.connect(self._zeige)
        self._zeige(0)

    def _zeige(self, index: int) -> None:
        if not 0 <= index < len(self._stellen):
            return
        datensatz = self._stellen[index][1]
        self.l_seite.setText(datensatz.seitennummer or "–")
        self.l_name.setText(datensatz.seitenname or "–")
        self.l_referenz.setText(datensatz.seitenreferenz or "–")
        self.l_fundstelle.setText(datensatz.fundstelle.als_text())

        seitenzeilen = self._ergebnis.datensaetze_der_seite(datensatz) or [datensatz]
        self.tabelle.setRowCount(len(seitenzeilen))
        markiert = -1
        for zeile, d in enumerate(seitenzeilen):
            werte = (d.sps_adresse, d.symbolische_adresse, d.funktionstext,
                     d.betriebsmittelkennzeichen, d.fundstelle.als_text())
            for spalte, wert in enumerate(werte):
                self.tabelle.setItem(zeile, spalte, QTableWidgetItem(wert))
            if d.laufende_nummer == datensatz.laufende_nummer:
                markiert = zeile
                for spalte in range(len(werte)):
                    schrift = self.tabelle.item(zeile, spalte).font()
                    schrift.setBold(True)
                    self.tabelle.item(zeile, spalte).setFont(schrift)
        self.tabelle.resizeColumnsToContents()
        if markiert >= 0:
            self.tabelle.selectRow(markiert)
            self.tabelle.scrollToItem(self.tabelle.item(markiert, 0))

        self.quelltext.setPlainText(self._baue_quelltext(datensatz))

    def _baue_quelltext(self, datensatz: EplanDatensatz) -> str:
        quelle = Path(self._ergebnis.quelle)
        auszug = self._auszug_leser.lese(quelle, datensatz.fundstelle)
        if auszug.hinweis:
            return auszug.hinweis
        breite = len(str(auszug.zeilen[-1][0])) if auszug.zeilen else 1
        zeilen = [f"{'▶' if nr == auszug.markierte_zeile else ' '} {nr:>{breite}} │ {text}"
                  for nr, text in auszug.zeilen]
        return datensatz.fundstelle.als_text() + "\n\n" + "\n".join(zeilen)


# ---------------------------------------------------------------------------
# Berichte und Texte
# ---------------------------------------------------------------------------

class TextDialog(QDialog):
    """Einfache Textanzeige mit Kopieren-Schaltfläche."""

    def __init__(self, titel: str, text: str, eltern: QWidget | None = None,
                 aktualisieren: Callable[[], str] | None = None) -> None:
        super().__init__(eltern)
        self.setWindowTitle(titel)
        self.resize(820, 520)
        self.anzeige = QPlainTextEdit()
        self.anzeige.setReadOnly(True)
        self.anzeige.setFont(_festbreitenschrift())
        self.anzeige.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.anzeige.setPlainText(text)
        layout = QVBoxLayout(self)
        layout.addWidget(self.anzeige)
        zeile = QHBoxLayout()
        if aktualisieren is not None:
            neu = QPushButton("Aktualisieren")
            neu.clicked.connect(lambda: self.anzeige.setPlainText(aktualisieren()))
            zeile.addWidget(neu)
        kopieren = QPushButton("In Zwischenablage kopieren")
        kopieren.clicked.connect(lambda: self.anzeige.selectAll() or self.anzeige.copy())
        zeile.addWidget(kopieren)
        zeile.addStretch(1)
        schliessen = QPushButton("Schließen")
        schliessen.clicked.connect(self.accept)
        zeile.addWidget(schliessen)
        layout.addLayout(zeile)


class ProtokollDialog(TextDialog):
    """Zeigt die letzten Protokollzeilen dieser Sitzung."""

    def __init__(self, eltern: QWidget | None = None) -> None:
        super().__init__("Protokoll", self._lese(), eltern, self._lese)
        self.anzeige.moveCursor(self.anzeige.textCursor().MoveOperation.End)

    @staticmethod
    def _lese() -> str:
        handler = hole_speicher_handler()
        return "\n".join(handler.zeilen()) if handler else "(Kein Speicherprotokoll aktiv)"


class EinleseberichtDialog(QDialog):
    """Ergebnis des Einlesens: Teildateien, Warnungen und Strukturbericht."""

    def __init__(self, ergebnis: Einleseergebnis, eltern: QWidget | None = None) -> None:
        super().__init__(eltern)
        self.setWindowTitle("Einleseprotokoll")
        self.resize(860, 560)
        tabs = QTabWidget()

        tabelle = QTableWidget(len(ergebnis.teildateien), 4)
        tabelle.setHorizontalHeaderLabels(["Teildatei", "Art", "Datensätze", "Hinweis"])
        tabelle.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        for zeile, t in enumerate(ergebnis.teildateien):
            for spalte, wert in enumerate((t.name, t.art, str(t.datensaetze), t.hinweis)):
                tabelle.setItem(zeile, spalte, QTableWidgetItem(wert))
        tabelle.resizeColumnsToContents()
        tabelle.horizontalHeader().setStretchLastSection(True)
        tabs.addTab(tabelle, f"Teildateien ({len(ergebnis.teildateien)})")

        warnungen = QPlainTextEdit("\n\n".join(ergebnis.warnungen) or "Keine Warnungen.")
        warnungen.setReadOnly(True)
        tabs.addTab(warnungen, f"Warnungen ({len(ergebnis.warnungen)})")

        struktur = QPlainTextEdit(ergebnis.strukturbericht or "(Keine Strukturanalyse verfügbar.)")
        struktur.setReadOnly(True)
        struktur.setFont(_festbreitenschrift())
        struktur.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        tabs.addTab(struktur, "Dateistruktur")

        if not ergebnis.datensaetze:
            tabs.setCurrentIndex(1)

        schliessen = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        schliessen.button(QDialogButtonBox.StandardButton.Close).setText("Schließen")
        schliessen.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(tabs)
        layout.addWidget(schliessen)
