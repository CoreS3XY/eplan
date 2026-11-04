"""
gui.py – Hauptfenster der Anwendung (PyQt6, deutsche Oberfläche).

Ablauf: Datei öffnen -> Einlesen (Hintergrundthread) -> Prüfen (Hintergrundthread)
-> Fehlerliste mit Fundstelle -> Export / „In Datenbank übernehmen“.

Das Fenster enthält keine Fachlogik. Es verbindet nur Parser, Prüfer, Export und
Datenbank-Repository mit den Widgets (Trennung von GUI, Datenmodell und Datenzugriff).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Callable, Sequence

from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtGui import QAction, QDragEnterEvent, QDropEvent, QKeySequence
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QProgressBar,
    QPushButton,
    QSplitter,
    QStatusBar,
    QTableView,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from .dateianalyse import DateiAnalysator
from .datenbank import ReferenzRepository, erzeuge_repository
from .einstellungen import EinstellungsVerwalter, konfigurationsverzeichnis
from .eplan_parser import EplanDateiLeser, erzeuge_diagnosebericht
from .export import EXPORTER, exportiere_fehlerliste
from .gui_arbeiter import Aufgabenfunktion, HintergrundAufgabe
from .gui_dialoge import (
    EinleseberichtDialog,
    EinstellungsDialog,
    ProtokollDialog,
    SeitenDialog,
    TextDialog,
    stelle_frage,
    zeige_fehler,
    zeige_information,
    zeige_warnung,
)
from .gui_modelle import DatensatzModell, FehlerFilterModell, FehlerModell
from .logging import (
    hole_protokollierer,
    installiere_ausnahmeprotokoll,
    richte_protokollierung_ein,
    setze_stufe,
)
from .modelle import (
    EplanDatensatz,
    EplanPrueferFehler,
    Einleseergebnis,
    Einstellungen,
    Fehlertyp,
    Pruefbericht,
    Pruefungsfehler,
    UebernahmeErgebnis,
)
from .pruefer import Pruefmanager, standard_pruefungen

_log = hole_protokollierer("gui")

ANWENDUNGSNAME = "EPLAN-Prüfer"
VERSION = "1.0.0"
DATEIFILTER = ("EPLAN-Projekte (*.elk *.zw1);;"
               "Exporte und Datenquellen (*.xml *.csv *.tsv *.txt *.xlsx *.db *.sqlite);;"
               "Alle Dateien (*)")


class Hauptfenster(QMainWindow):
    """Hauptfenster mit Fehlerliste, Datensatzliste und Fundstellenanzeige."""

    def __init__(self, verwalter: EinstellungsVerwalter, einstellungen: Einstellungen,
                 protokollpfad: Path | None = None) -> None:
        super().__init__()
        self._verwalter = verwalter
        self._einstellungen = einstellungen
        self._protokollpfad = protokollpfad
        self._ergebnis: Einleseergebnis | None = None
        self._bericht: Pruefbericht | None = None
        self._aufgabe: HintergrundAufgabe | None = None
        self._laufende: set[HintergrundAufgabe] = set()

        self.setWindowTitle(ANWENDUNGSNAME)
        self.resize(1280, 820)
        self.setAcceptDrops(True)

        self._fehler_modell = FehlerModell(self)
        self._filter_modell = FehlerFilterModell(self)
        self._filter_modell.setSourceModel(self._fehler_modell)
        self._datensatz_modell = DatensatzModell(self)

        self._baue_aktionen()
        self._baue_menue_und_werkzeugleiste()
        self._baue_zentrum()
        self._baue_statusleiste()
        self._aktualisiere_aktionen()
        self._aktualisiere_details()
        self._status("Bereit. Öffnen Sie eine EPLAN-Datei (*.elk, *.zw1) oder ziehen Sie sie in dieses Fenster.")

    # ------------------------------------------------------------------ Aufbau --

    def _baue_aktionen(self) -> None:
        def aktion(text: str, slot: Callable[[], Any], kurz: str | QKeySequence.StandardKey | None = None,
                   tip: str = "") -> QAction:
            a = QAction(text, self)
            a.triggered.connect(lambda _checked=False: slot())
            if kurz is not None:
                a.setShortcut(QKeySequence(kurz))
            if tip:
                a.setStatusTip(tip)
                a.setToolTip(tip)
            return a

        self.a_oeffnen = aktion("Datei öffnen …", self.datei_oeffnen, QKeySequence.StandardKey.Open,
                                "EPLAN-Projekt (*.elk, *.zw1) oder Export öffnen")
        self.a_csv = aktion("Fehlerliste als CSV exportieren …", lambda: self._exportieren(".csv"))
        self.a_xlsx = aktion("Fehlerliste als Excel exportieren …", lambda: self._exportieren(".xlsx"))
        self.a_beenden = aktion("Beenden", self.close, QKeySequence.StandardKey.Quit)
        self.a_pruefen = aktion("Prüfung starten", self._pruefung_starten, "F5",
                                "Alle aktivierten Prüfungen erneut ausführen")
        self.a_uebernehmen = aktion("In Datenbank übernehmen", self._in_datenbank_uebernehmen, "Ctrl+D",
                                    "Markierte Fehler „nicht in Datenbank vorhanden“ in die Referenz übernehmen")
        self.a_seite = aktion("Seite anzeigen …", self._zeige_fundstelle, "Ctrl+G",
                              "Seite, Seitenname und Quelle der markierten Fundstelle anzeigen")
        self.a_abbrechen = aktion("Vorgang abbrechen", self._abbrechen, "Esc")
        self.a_einstellungen = aktion("Einstellungen …", self._einstellungen_oeffnen, "Ctrl+,")
        self.a_struktur = aktion("Dateistruktur analysieren …", self._struktur_analysieren,
                                 tip="Untersucht den tatsächlichen Aufbau einer beliebigen Datei")
        self.a_einleseprotokoll = aktion("Einleseprotokoll …", self._zeige_einleseprotokoll)
        self.a_diagnose = aktion("Diagnosebericht speichern …", self._diagnose_speichern,
                                 tip="Speichert Teildateien, Warnungen und Dateistruktur der geöffneten Datei als Textdatei")
        self.a_protokoll = aktion("Protokoll anzeigen …", self._zeige_protokoll)
        self.a_ueber = aktion("Über …", self._ueber)

    def _baue_menue_und_werkzeugleiste(self) -> None:
        menue = self.menuBar()
        datei = menue.addMenu("&Datei")
        datei.addActions([self.a_oeffnen])
        datei.addSeparator()
        datei.addActions([self.a_csv, self.a_xlsx])
        datei.addSeparator()
        datei.addAction(self.a_beenden)
        pruefung = menue.addMenu("&Prüfung")
        pruefung.addActions([self.a_pruefen, self.a_seite, self.a_uebernehmen])
        pruefung.addSeparator()
        pruefung.addAction(self.a_abbrechen)
        extras = menue.addMenu("&Extras")
        extras.addActions([self.a_einstellungen, self.a_struktur, self.a_einleseprotokoll,
                           self.a_diagnose, self.a_protokoll])
        hilfe = menue.addMenu("&Hilfe")
        hilfe.addAction(self.a_ueber)

        leiste = QToolBar("Hauptleiste")
        leiste.setMovable(False)
        leiste.addActions([self.a_oeffnen, self.a_pruefen])
        leiste.addSeparator()
        leiste.addActions([self.a_csv, self.a_xlsx])
        leiste.addSeparator()
        leiste.addActions([self.a_uebernehmen, self.a_einstellungen])
        leiste.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.addToolBar(leiste)

    def _baue_tabelle(self, modell: Any, breiten: Sequence[int]) -> QTableView:
        tabelle = QTableView()
        tabelle.setModel(modell)
        tabelle.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        tabelle.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        tabelle.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        tabelle.setAlternatingRowColors(True)
        tabelle.setSortingEnabled(True)
        tabelle.setWordWrap(False)
        tabelle.verticalHeader().setVisible(False)
        tabelle.horizontalHeader().setStretchLastSection(True)
        for spalte, breite in enumerate(breiten):
            tabelle.setColumnWidth(spalte, breite)
        return tabelle

    def _baue_zentrum(self) -> None:
        self.l_hinweis = QLabel()
        self.l_hinweis.setWordWrap(True)
        self.l_hinweis.setVisible(False)
        self.l_hinweis.setStyleSheet(
            "QLabel { background: palette(alternate-base); border: 1px solid palette(mid); padding: 6px; }")

        # -- Fehlerliste mit Filter
        self.c_typ = QComboBox()
        self.c_typ.addItem("Alle Fehlertypen", None)
        for typ in Fehlertyp:
            self.c_typ.addItem(typ.bezeichnung, typ)
        self.c_typ.currentIndexChanged.connect(
            lambda _i: (self._filter_modell.setze_typ(self.c_typ.currentData()), self._aktualisiere_zaehler()))
        self.e_suche = QLineEdit()
        self.e_suche.setPlaceholderText("Suchen in Symbol, Funktionstext, Adresse, Seite, Beschreibung …")
        self.e_suche.setClearButtonEnabled(True)
        self.e_suche.textChanged.connect(
            lambda text: (self._filter_modell.setze_suchtext(text), self._aktualisiere_zaehler()))
        self.l_zaehler = QLabel()
        self.b_uebernehmen = QPushButton("In Datenbank übernehmen")
        self.b_uebernehmen.clicked.connect(self._in_datenbank_uebernehmen)
        self.b_seite = QPushButton("Seite anzeigen …")
        self.b_seite.clicked.connect(self._zeige_fundstelle)

        filterzeile = QHBoxLayout()
        filterzeile.addWidget(self.c_typ)
        filterzeile.addWidget(self.e_suche, 1)
        filterzeile.addWidget(self.l_zaehler)
        filterzeile.addWidget(self.b_seite)
        filterzeile.addWidget(self.b_uebernehmen)

        self.t_fehler = self._baue_tabelle(self._filter_modell, (190, 160, 240, 90, 70, 420))
        self.t_fehler.sortByColumn(-1, Qt.SortOrder.AscendingOrder)
        self.t_fehler.doubleClicked.connect(lambda _i: self._zeige_fundstelle())
        self.t_fehler.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.t_fehler.customContextMenuRequested.connect(self._kontextmenue)
        self.t_fehler.selectionModel().selectionChanged.connect(lambda *_: self._aktualisiere_details())

        fehlerseite = QWidget()
        fl = QVBoxLayout(fehlerseite)
        fl.setContentsMargins(0, 4, 0, 0)
        fl.addLayout(filterzeile)
        fl.addWidget(self.t_fehler, 1)

        self.t_daten = self._baue_tabelle(self._datensatz_modell, (50, 170, 240, 90, 70, 150, 150, 300))
        self.t_daten.doubleClicked.connect(lambda _i: self._zeige_fundstelle())
        self.t_daten.selectionModel().selectionChanged.connect(lambda *_: self._aktualisiere_details())

        self.tabs = QTabWidget()
        self.tabs.addTab(fehlerseite, "Fehlerliste")
        self.tabs.addTab(self.t_daten, "Eingelesene Datensätze")
        self.tabs.currentChanged.connect(lambda _i: (self._aktualisiere_details(), self._aktualisiere_aktionen()))

        # -- Fundstelle
        self.d_beschreibung = QLabel("–")
        self.d_seite = QLabel("–")
        self.d_name = QLabel("–")
        self.d_referenz = QLabel("–")
        self.d_fundstelle = QLabel("–")
        self.d_bezug = QLabel("–")
        for label in (self.d_beschreibung, self.d_seite, self.d_name, self.d_referenz,
                      self.d_fundstelle, self.d_bezug):
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        details = QGroupBox("Fundstelle")
        form = QFormLayout(details)
        form.addRow("Beschreibung:", self.d_beschreibung)
        form.addRow("Seitennummer:", self.d_seite)
        form.addRow("Seitenname:", self.d_name)
        form.addRow("Seitenreferenz:", self.d_referenz)
        form.addRow("Fundstelle:", self.d_fundstelle)
        form.addRow("Verweis:", self.d_bezug)

        teiler = QSplitter(Qt.Orientation.Vertical)
        teiler.addWidget(self.tabs)
        teiler.addWidget(details)
        teiler.setStretchFactor(0, 4)
        teiler.setStretchFactor(1, 1)

        zentrum = QWidget()
        layout = QVBoxLayout(zentrum)
        layout.addWidget(self.l_hinweis)
        layout.addWidget(teiler, 1)
        self.setCentralWidget(zentrum)

    def _baue_statusleiste(self) -> None:
        leiste = QStatusBar()
        self.l_status = QLabel()
        self.l_zusammenfassung = QLabel()
        self.fortschrittsbalken = QProgressBar()
        self.fortschrittsbalken.setMaximumWidth(220)
        self.fortschrittsbalken.setTextVisible(False)
        self.fortschrittsbalken.setVisible(False)
        self.b_abbrechen = QPushButton("Abbrechen")
        self.b_abbrechen.setVisible(False)
        self.b_abbrechen.clicked.connect(self._abbrechen)
        leiste.addWidget(self.l_status, 1)
        leiste.addPermanentWidget(self.fortschrittsbalken)
        leiste.addPermanentWidget(self.b_abbrechen)
        leiste.addPermanentWidget(self.l_zusammenfassung)
        self.setStatusBar(leiste)

    # --------------------------------------------------------- Zustandsanzeige --

    @property
    def beschaeftigt(self) -> bool:
        return self._aufgabe is not None

    def _status(self, text: str) -> None:
        self.l_status.setText(text)
        _log.debug("Status: %s", text)

    def _zeige_hinweis(self, texte: Sequence[str]) -> None:
        self.l_hinweis.setText("\n".join(texte))
        self.l_hinweis.setVisible(bool(texte))

    def _aktualisiere_aktionen(self) -> None:
        frei = not self.beschaeftigt
        hat_daten = self._ergebnis is not None and bool(self._ergebnis.datensaetze)
        hat_fehler = self._bericht is not None and bool(self._bericht.fehler)
        self.a_oeffnen.setEnabled(frei)
        self.a_pruefen.setEnabled(frei and hat_daten)
        self.a_csv.setEnabled(frei and hat_fehler)
        self.a_xlsx.setEnabled(frei and hat_fehler)
        self.a_uebernehmen.setEnabled(frei and hat_fehler)
        self.a_seite.setEnabled(hat_daten)
        self.a_einstellungen.setEnabled(frei)
        self.a_einleseprotokoll.setEnabled(self._ergebnis is not None)
        self.a_diagnose.setEnabled(self._ergebnis is not None)
        self.a_abbrechen.setEnabled(not frei)
        self.b_uebernehmen.setEnabled(frei and hat_fehler)
        self.b_seite.setEnabled(hat_daten)
        self.b_abbrechen.setVisible(not frei)
        self.fortschrittsbalken.setVisible(not frei)

    def _aktualisiere_zaehler(self) -> None:
        gesamt = self._fehler_modell.rowCount()
        sichtbar = self._filter_modell.rowCount()
        self.l_zaehler.setText(f"{sichtbar} von {gesamt} Fehlern" if self._filter_modell.filter_aktiv
                               else f"{gesamt} Fehler")

    def _aktualisiere_zusammenfassung(self) -> None:
        teile = []
        if self._ergebnis is not None:
            teile.append(f"{len(self._ergebnis.datensaetze)} Datensätze")
        if self._bericht is not None:
            teile.append(f"{len(self._bericht.fehler)} Fehler")
        self.l_zusammenfassung.setText(" · ".join(teile))

    def _zeige_fortschritt(self, meldung: str, aktuell: int, gesamt: int) -> None:
        self._status(meldung)
        if gesamt <= 0:
            self.fortschrittsbalken.setRange(0, 0)          # unbestimmt
        else:
            self.fortschrittsbalken.setRange(0, gesamt)
            self.fortschrittsbalken.setValue(min(aktuell, gesamt))

    # ------------------------------------------------------- Auswahl & Details --

    def _ausgewaehlte_fehler(self) -> list[Pruefungsfehler]:
        ergebnis = []
        for index in self.t_fehler.selectionModel().selectedRows():
            fehler = self._fehler_modell.fehler_an(self._filter_modell.mapToSource(index).row())
            if fehler is not None:
                ergebnis.append(fehler)
        return ergebnis

    def _aktueller_datensatz(self) -> tuple[EplanDatensatz | None, Pruefungsfehler | None]:
        """Datensatz (und ggf. Fehler) der aktuellen Auswahl im sichtbaren Tab."""
        if self.tabs.currentIndex() == 0:
            index = self.t_fehler.currentIndex()
            if index.isValid():
                fehler = self._fehler_modell.fehler_an(self._filter_modell.mapToSource(index).row())
                if fehler is not None:
                    return fehler.datensatz, fehler
        else:
            index = self.t_daten.currentIndex()
            if index.isValid():
                return self._datensatz_modell.datensatz_an(index.row()), None
        return None, None

    def _aktualisiere_details(self) -> None:
        datensatz, fehler = self._aktueller_datensatz()
        if datensatz is None:
            for label in (self.d_beschreibung, self.d_seite, self.d_name, self.d_referenz,
                          self.d_fundstelle, self.d_bezug):
                label.setText("–")
            return
        self.d_beschreibung.setText(f"{fehler.fehlertyp.bezeichnung}: {fehler.beschreibung}" if fehler else "–")
        self.d_seite.setText(datensatz.seitennummer or "–")
        self.d_name.setText(datensatz.seitenname or "–")
        self.d_referenz.setText(datensatz.seitenreferenz or "–")
        self.d_fundstelle.setText(datensatz.fundstelle.als_text())
        if fehler is not None and fehler.bezug is not None:
            self.d_bezug.setText(f"Erste Verwendung auf Seite {fehler.bezug.seite_ausfuehrlich} – "
                                 f"{fehler.bezug.fundstelle.als_text()}")
        else:
            self.d_bezug.setText("–")

    def _kontextmenue(self, position: QPoint) -> None:
        menue = QMenu(self)
        menue.addAction(self.a_seite)
        menue.addAction(self.a_uebernehmen)
        menue.exec(self.t_fehler.viewport().mapToGlobal(position))

    # ------------------------------------------------------- Hintergrundaufgaben --

    def _starte(self, name: str, funktion: Aufgabenfunktion, bei_fertig: Callable[[Any], None]) -> None:
        aufgabe = HintergrundAufgabe(funktion, self, name)
        self._laufende.add(aufgabe)
        self._aufgabe = aufgabe
        self.fortschrittsbalken.setRange(0, 0)
        aufgabe.fortschritt.connect(self._zeige_fortschritt)
        aufgabe.fertig.connect(lambda ergebnis, a=aufgabe: self._aufgabe_fertig(a, bei_fertig, ergebnis))
        aufgabe.fehlgeschlagen.connect(lambda text, n=name: self._aufgabe_fehlgeschlagen(n, text))
        aufgabe.abgebrochen.connect(lambda: self._status("Vorgang abgebrochen."))
        aufgabe.beendet.connect(lambda a=aufgabe: self._aufgabe_beendet(a))
        aufgabe.aufgeraeumt.connect(lambda a=aufgabe: self._laufende.discard(a))
        self._aktualisiere_aktionen()
        self._status(f"{name} …")
        aufgabe.starten()

    def _aufgabe_fertig(self, aufgabe: HintergrundAufgabe, bei_fertig: Callable[[Any], None],
                        ergebnis: Any) -> None:
        """Die Aufgabe gilt ab jetzt als erledigt, damit der Ergebnis-Handler Folgeaufgaben starten darf."""
        if self._aufgabe is aufgabe:
            self._aufgabe = None
        bei_fertig(ergebnis)
        self._aktualisiere_aktionen()

    def _aufgabe_fehlgeschlagen(self, name: str, text: str) -> None:
        self._status(f"{name} fehlgeschlagen.")
        zeige_fehler(self, name, text)

    def _aufgabe_beendet(self, aufgabe: HintergrundAufgabe) -> None:
        if self._aufgabe is aufgabe:        # eine Folgeaufgabe darf nicht überschrieben werden
            self._aufgabe = None
        self._aktualisiere_aktionen()

    def _abbrechen(self) -> None:
        if self._aufgabe is not None:
            self._aufgabe.abbrechen()
            self._status("Breche ab …")

    # ----------------------------------------------------------------- Einlesen --

    def datei_oeffnen(self) -> None:
        start = self._einstellungen.allgemein.letztes_verzeichnis or str(Path.home())
        pfad, _ = QFileDialog.getOpenFileName(self, "EPLAN-Datei öffnen", start, DATEIFILTER)
        if pfad:
            self.datei_laden(Path(pfad))

    def datei_laden(self, pfad: Path) -> None:
        """Liest eine Datei im Hintergrund ein und startet danach (optional) die Prüfung."""
        if self.beschaeftigt:
            zeige_information(self, ANWENDUNGSNAME, "Es läuft bereits ein Vorgang.")
            return
        self._einstellungen.allgemein.letztes_verzeichnis = str(pfad.parent)
        leser = EplanDateiLeser(self._einstellungen)
        _log.info("Datei öffnen: %s", pfad)
        self._starte(f"Lese {pfad.name}",
                     lambda melde, abbruch: leser.lese(pfad, melde, abbruch),
                     self._nach_einlesen)

    def _nach_einlesen(self, ergebnis: Einleseergebnis) -> None:
        self._ergebnis = ergebnis
        self._bericht = None
        self._datensatz_modell.setze_daten(ergebnis.datensaetze)
        self._fehler_modell.setze_fehler([])
        self._aktualisiere_zaehler()
        self._aktualisiere_zusammenfassung()
        self._aktualisiere_details()
        self.setWindowTitle(f"{ANWENDUNGSNAME} – {Path(ergebnis.quelle).name}")
        hinweise = [f"{len(ergebnis.warnungen)} Warnung(en) beim Einlesen – siehe Extras ▸ Einleseprotokoll."] \
            if ergebnis.warnungen else []
        self._zeige_hinweis(hinweise)
        self._aktualisiere_aktionen()

        if not ergebnis.datensaetze:
            self._status("Keine SPS-Datensätze gefunden.")
            zeige_warnung(self, "Keine Datensätze",
                          "In der Datei wurden keine SPS-Datensätze gefunden.\n\n"
                          "Das Einleseprotokoll zeigt, wie die Datei aufgebaut ist und was erkannt wurde.\n"
                          "Über Extras ▸ Diagnosebericht speichern lässt sich das als Textdatei sichern.\n\n"
                          + (ergebnis.warnungen[-1] if ergebnis.warnungen else ""))
            EinleseberichtDialog(ergebnis, self).exec()
            return
        self._status(f"{len(ergebnis.datensaetze)} Datensätze auf {len(ergebnis.seiten)} Seiten eingelesen.")
        if self._einstellungen.pruefung.automatisch_pruefen:
            self._pruefung_starten()

    # ------------------------------------------------------------------ Prüfen --

    def _pruefung_starten(self) -> None:
        if self.beschaeftigt or self._ergebnis is None or not self._ergebnis.datensaetze:
            return
        pe = self._einstellungen.pruefung
        db = self._einstellungen.datenbank
        verwalter = Pruefmanager(standard_pruefungen(pe), pe.gross_kleinschreibung_beachten)
        referenz: ReferenzRepository | None = None
        if pe.datenbankabgleich and db.ist_vollstaendig:
            referenz = erzeuge_repository(db)
        daten = list(self._ergebnis.datensaetze)
        self._starte("Prüfung",
                     lambda melde, abbruch: verwalter.pruefe(daten, referenz, melde, abbruch),
                     self._nach_pruefung)

    def _nach_pruefung(self, bericht: Pruefbericht) -> None:
        self._bericht = bericht
        self._fehler_modell.setze_fehler(bericht.fehler)
        self._aktualisiere_zaehler()
        self._aktualisiere_zusammenfassung()
        self._aktualisiere_details()
        hinweise = []
        if self._ergebnis is not None and self._ergebnis.warnungen:
            hinweise.append(f"{len(self._ergebnis.warnungen)} Warnung(en) beim Einlesen – "
                            "siehe Extras ▸ Einleseprotokoll.")
        for name, grund in bericht.uebersprungen.items():
            hinweise.append(f"Nicht ausgeführt: {name} – {grund}")
        self._zeige_hinweis(hinweise)
        self._status(f"Prüfung beendet: {len(bericht.fehler)} Fehler in {bericht.anzahl_datensaetze} Datensätzen.")
        self._aktualisiere_aktionen()
        if bericht.fehler:
            self.tabs.setCurrentIndex(0)
            self.t_fehler.selectRow(0)

    # --------------------------------------------------------- Sprung zur Fundstelle --

    def _zeige_fundstelle(self) -> None:
        if self._ergebnis is None:
            return
        datensatz, fehler = self._aktueller_datensatz()
        if datensatz is None:
            zeige_information(self, "Seite anzeigen", "Bitte zuerst einen Eintrag markieren.")
            return
        stellen = [("Betroffene Stelle" if fehler else "Datensatz", datensatz)]
        if fehler is not None and fehler.bezug is not None:
            stellen.append((f"Erste Verwendung (Seite {fehler.bezug.seite_anzeige or 'unbekannt'})", fehler.bezug))
        SeitenDialog(self._ergebnis, stellen, self).exec()

    # ------------------------------------------------------------------- Export --

    def _exportieren(self, endung: str) -> None:
        if self._bericht is None or not self._bericht.fehler:
            zeige_information(self, "Export", "Es gibt keine Fehler zum Exportieren.")
            return
        exporter = EXPORTER[endung]
        quelle = Path(self._ergebnis.quelle).stem if self._ergebnis else "Projekt"
        start = str(Path(self._einstellungen.allgemein.letztes_verzeichnis or Path.home())
                    / f"Fehlerliste_{quelle}{endung}")
        pfad, _ = QFileDialog.getSaveFileName(self, "Fehlerliste exportieren", start, exporter.beschreibung)
        if not pfad:
            return
        fehler = self._bericht.fehler
        if self._filter_modell.filter_aktiv and stelle_frage(
                self, "Export", f"Ein Filter ist aktiv. Nur die {self._filter_modell.rowCount()} sichtbaren "
                                f"Fehler exportieren?\n(Nein = alle {len(fehler)} Fehler)"):
            fehler = self._filter_modell.sichtbare_fehler()
        self.exportiere_nach(Path(pfad), fehler)

    def exportiere_nach(self, pfad: Path, fehler: Sequence[Pruefungsfehler]) -> Path | None:
        """Schreibt die Fehlerliste; Fehler werden dem Anwender gemeldet."""
        try:
            ziel = exportiere_fehlerliste(fehler, pfad)
        except EplanPrueferFehler as problem:
            zeige_fehler(self, "Export fehlgeschlagen", str(problem))
            return None
        self._status(f"{len(fehler)} Fehler exportiert nach {ziel}")
        zeige_information(self, "Export", f"{len(fehler)} Fehler wurden exportiert:\n{ziel}")
        return ziel

    # ----------------------------------------------------- In Datenbank übernehmen --

    def _in_datenbank_uebernehmen(self) -> None:
        if self.beschaeftigt:
            return
        db = self._einstellungen.datenbank
        if not db.ist_vollstaendig:
            zeige_warnung(self, "Datenbank nicht konfiguriert",
                          "Bitte zuerst die Datenbankverbindung unter Extras ▸ Einstellungen eintragen.\n"
                          "Es fehlt: " + ", ".join(db.fehlende_angaben()))
            return
        auswahl = self._ausgewaehlte_fehler()
        fehler = [f for f in auswahl if f.fehlertyp.uebernehmbar]
        if not fehler:
            zeige_information(
                self, "In Datenbank übernehmen",
                "Bitte mindestens einen Fehler vom Typ „Symbol nicht in Datenbank vorhanden“ oder "
                "„Funktionstext nicht in Datenbank vorhanden“ markieren.")
            return
        paare = list(dict.fromkeys((f.symbol.strip(), f.funktionstext.strip()) for f in fehler))
        vorschau = "\n".join(f"  {s}  –  {t}" for s, t in paare[:8])
        if len(paare) > 8:
            vorschau += f"\n  … und {len(paare) - 8} weitere"
        if not stelle_frage(self, "In Datenbank übernehmen",
                            f"{len(paare)} Datensatz/-sätze in die Tabelle „{db.tabellenname}“ der "
                            f"Datenbank „{db.datenbankname}“ übernehmen?\n\n{vorschau}"):
            return
        repository = erzeuge_repository(db)
        self._starte("Übernahme in die Datenbank",
                     lambda _melde, _abbruch: repository.uebernehmen(paare, False),
                     lambda ergebnisse: self._nach_uebernahme(repository, ergebnisse))

    def _nach_uebernahme(self, repository: ReferenzRepository, ergebnisse: list[UebernahmeErgebnis]) -> None:
        konflikte = [e for e in ergebnisse if e.benoetigt_bestaetigung]
        if konflikte:
            zeilen = "\n".join(f"  {e.symbol} – {e.funktionstext}\n    {e.meldung}" for e in konflikte[:6])
            if len(konflikte) > 6:
                zeilen += f"\n  … und {len(konflikte) - 6} weitere"
            if stelle_frage(self, "Konflikt mit vorhandenen Einträgen",
                            "Für folgende Datensätze existiert Symbol oder Funktionstext bereits "
                            f"in der Datenbank:\n\n{zeilen}\n\nTrotzdem als zusätzliche Zeile speichern?"):
                paare = [(e.symbol, e.funktionstext) for e in konflikte]
                self._starte("Übernahme (bestätigte Konflikte)",
                             lambda _m, _a: repository.uebernehmen(paare, True),
                             lambda nachtrag: self._abschluss_uebernahme(
                                 [e for e in ergebnisse if not e.benoetigt_bestaetigung] + nachtrag))
                return
        self._abschluss_uebernahme(ergebnisse)

    def _abschluss_uebernahme(self, ergebnisse: list[UebernahmeErgebnis]) -> None:
        gespeichert = [e for e in ergebnisse if e.uebernommen]
        nicht = [e for e in ergebnisse if not e.uebernommen]
        text = f"{len(gespeichert)} Datensatz/-sätze in die Datenbank übernommen."
        if nicht:
            text += f"\n\n{len(nicht)} nicht übernommen:\n" + "\n".join(
                f"  {e.symbol} – {e.funktionstext}: {e.meldung}" for e in nicht[:8])
            if len(nicht) > 8:
                text += f"\n  … und {len(nicht) - 8} weitere"
        self._status(f"{len(gespeichert)} übernommen, {len(nicht)} nicht übernommen.")
        (zeige_warnung if nicht and not gespeichert else zeige_information)(self, "In Datenbank übernehmen", text)
        if gespeichert:
            self._pruefung_starten()

    # ---------------------------------------------------------------- Extras --

    def _einstellungen_oeffnen(self) -> None:
        dialog = EinstellungsDialog(self._einstellungen, self._verwalter.pfad, self._protokollpfad, self)
        if dialog.exec() != EinstellungsDialog.DialogCode.Accepted:
            return
        neu = dialog.ergebnis()
        if neu is None:
            return
        neu.allgemein.letztes_verzeichnis = self._einstellungen.allgemein.letztes_verzeichnis
        try:
            self._verwalter.speichern(neu)
        except EplanPrueferFehler as fehler:
            zeige_fehler(self, "Einstellungen", str(fehler))
            return
        self._einstellungen = neu
        setze_stufe(neu.allgemein.protokollstufe)
        self._status("Einstellungen gespeichert.")
        if self._ergebnis is not None and self._ergebnis.datensaetze and stelle_frage(
                self, "Einstellungen", "Prüfung mit den neuen Einstellungen jetzt erneut ausführen?\n"
                                       "(Geänderte Feldzuordnungen wirken erst beim erneuten Öffnen der Datei.)"):
            self._pruefung_starten()

    def _struktur_analysieren(self) -> None:
        pfad, _ = QFileDialog.getOpenFileName(
            self, "Datei analysieren", self._einstellungen.allgemein.letztes_verzeichnis or str(Path.home()),
            "Alle Dateien (*)")
        if not pfad:
            return
        try:
            bericht = DateiAnalysator().analysiere(pfad)
        except EplanPrueferFehler as fehler:
            zeige_fehler(self, "Dateistruktur", str(fehler))
            return
        TextDialog(f"Dateistruktur – {Path(pfad).name}", bericht.als_text(), self).exec()

    def _zeige_einleseprotokoll(self) -> None:
        if self._ergebnis is not None:
            EinleseberichtDialog(self._ergebnis, self).exec()

    def _diagnose_speichern(self) -> None:
        if self._ergebnis is None:
            return
        start = str(Path(self._einstellungen.allgemein.letztes_verzeichnis or Path.home())
                    / f"Diagnose_{Path(self._ergebnis.quelle).stem}.txt")
        pfad, _ = QFileDialog.getSaveFileName(self, "Diagnosebericht speichern", start, "Textdatei (*.txt)")
        if pfad:
            self.diagnose_speichern_nach(Path(pfad))

    def diagnose_speichern_nach(self, pfad: Path) -> bool:
        """Schreibt den Diagnosebericht (enthält Dateipfade, aber keine Passwörter)."""
        if self._ergebnis is None:
            return False
        try:
            pfad.write_text(erzeuge_diagnosebericht(self._ergebnis), encoding="utf-8")
        except OSError as fehler:
            zeige_fehler(self, "Diagnosebericht", f"Die Datei konnte nicht geschrieben werden:\n{pfad}\n{fehler}")
            return False
        _log.info("Diagnosebericht gespeichert: %s", pfad)
        zeige_information(self, "Diagnosebericht", f"Der Bericht wurde gespeichert:\n{pfad}\n\n"
                          "Er enthält Dateipfade und Dateinamen Ihres Projekts. Bitte vor der Weitergabe prüfen.")
        return True

    def _zeige_protokoll(self) -> None:
        ProtokollDialog(self).exec()

    def _ueber(self) -> None:
        zeige_information(
            self, "Über", f"{ANWENDUNGSNAME} {VERSION}\n\n"
                          "Analysiert SPS-Anschlüsse aus EPLAN-Projekten und prüft sie auf doppelte "
                          "Symbole, Funktionstexte und Adressen, Platzhalter sowie gegen eine "
                          "MySQL-Referenzdatenbank.\n\n"
                          f"Konfiguration: {self._verwalter.pfad}\nProtokoll: {self._protokollpfad or '–'}")

    # -------------------------------------------------- Drag & Drop, Schließen --

    def dragEnterEvent(self, ereignis: QDragEnterEvent) -> None:  # noqa: N802 (Qt-Name)
        if ereignis.mimeData().hasUrls():
            ereignis.acceptProposedAction()

    def dropEvent(self, ereignis: QDropEvent) -> None:  # noqa: N802 (Qt-Name)
        urls = [u.toLocalFile() for u in ereignis.mimeData().urls() if u.isLocalFile()]
        if urls:
            self.datei_laden(Path(urls[0]))

    def closeEvent(self, ereignis: Any) -> None:  # noqa: N802 (Qt-Name)
        for aufgabe in list(self._laufende):
            aufgabe.abbrechen()
        for aufgabe in list(self._laufende):
            aufgabe.warte_auf_ende(8000)
        try:
            self._verwalter.speichern(self._einstellungen)
        except EplanPrueferFehler as fehler:
            _log.warning("Einstellungen beim Beenden nicht gespeichert: %s", fehler)
        _log.info("Anwendung beendet")
        super().closeEvent(ereignis)


# ---------------------------------------------------------------------------
# Programmstart
# ---------------------------------------------------------------------------

def _argumente(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="eplan_pruefer", description="Prüft SPS-Anschlüsse aus EPLAN-Projekten.")
    parser.add_argument("datei", nargs="?", help="EPLAN-Datei (*.elk, *.zw1) oder Export, die sofort geöffnet wird")
    parser.add_argument("--konfiguration", type=Path, help="Pfad der Konfigurationsdatei (INI)")
    parser.add_argument("--protokollstufe", choices=("DEBUG", "INFO", "WARNING", "ERROR"),
                        help="überschreibt die konfigurierte Protokollstufe")
    return parser.parse_args(list(argv))


def starte(argv: Sequence[str] | None = None) -> int:
    """Startet die Anwendung und liefert den Exit-Code."""
    argumente = _argumente(sys.argv[1:] if argv is None else argv)
    verwalter = EinstellungsVerwalter(argumente.konfiguration)
    protokollverzeichnis = verwalter.pfad.parent / "protokoll" if argumente.konfiguration \
        else konfigurationsverzeichnis() / "protokoll"
    protokollpfad = richte_protokollierung_ein(protokollverzeichnis, argumente.protokollstufe or "INFO")
    installiere_ausnahmeprotokoll()
    _log.info("%s %s gestartet (Python %s)", ANWENDUNGSNAME, VERSION, sys.version.split()[0])

    einstellungen = verwalter.laden()
    setze_stufe(argumente.protokollstufe or einstellungen.allgemein.protokollstufe)

    app = QApplication(sys.argv[:1])
    app.setApplicationName(ANWENDUNGSNAME)
    fenster = Hauptfenster(verwalter, einstellungen, protokollpfad)
    fenster.show()
    if argumente.datei:
        fenster.datei_laden(Path(argumente.datei))
    return app.exec()
