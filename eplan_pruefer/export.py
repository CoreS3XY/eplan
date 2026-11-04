"""
export.py – Export der Fehlerliste als CSV und Excel (.xlsx).

Spalten: Fehlertyp, Symbol, Funktionstext, Adresse, Seite, Beschreibung.

Weitere Formate (z. B. PDF) werden durch eine weitere Unterklasse von
:class:`FehlerExporter` ergänzt und in :data:`EXPORTER` registriert.

Sicherheit: Texte stammen aus fremden Projektdateien. Damit Excel sie nie als
Formel ausführt (CSV-/Formel-Injektion), werden in der XLSX-Datei alle Zellen
ausdrücklich als Text geschrieben, in der CSV-Datei erhalten gefährliche
Anfangszeichen (``= + - @``) ein vorangestelltes Hochkomma.
"""

from __future__ import annotations

import csv
import os
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Iterator, Sequence

from .logging import hole_protokollierer
from .modelle import ExportFehler, Pruefungsfehler

_log = hole_protokollierer("export")

SPALTEN: tuple[str, ...] = ("Fehlertyp", "Symbol", "Funktionstext", "Adresse", "Seite", "Beschreibung")


def zeilen_der_fehler(fehler: Sequence[Pruefungsfehler]) -> Iterator[tuple[str, ...]]:
    """Wandelt Fehler in Exportzeilen (ohne Kopfzeile) um."""
    for eintrag in fehler:
        yield (eintrag.fehlertyp.bezeichnung, eintrag.symbol, eintrag.funktionstext,
               eintrag.adresse, eintrag.seite, eintrag.beschreibung)


class FehlerExporter(ABC):
    """Schnittstelle eines Exportformats."""

    endung: str = ""
    beschreibung: str = ""

    def exportiere(self, fehler: Sequence[Pruefungsfehler], ziel: str | Path) -> Path:
        """Schreibt die Datei atomar (erst temporär, dann ersetzen) und liefert den Zielpfad."""
        ziel = Path(ziel)
        if ziel.suffix.lower() != self.endung:
            ziel = ziel.with_suffix(self.endung)
        try:
            ziel.parent.mkdir(parents=True, exist_ok=True)
            deskriptor, temporaer = tempfile.mkstemp(prefix=".export-", suffix=self.endung,
                                                     dir=ziel.parent)
            os.close(deskriptor)
        except OSError as fehler_:
            raise ExportFehler(f"Die Zieldatei kann nicht angelegt werden:\n{ziel}\n{fehler_}") from fehler_
        try:
            self._schreibe(list(zeilen_der_fehler(fehler)), Path(temporaer))
            os.replace(temporaer, ziel)
        except ExportFehler:
            _loesche(temporaer)
            raise
        except OSError as fehler_:
            _loesche(temporaer)
            raise ExportFehler(f"Export fehlgeschlagen:\n{ziel}\n{fehler_}") from fehler_
        _log.info("Export: %d Fehler -> %s", len(fehler), ziel)
        return ziel

    @abstractmethod
    def _schreibe(self, zeilen: list[tuple[str, ...]], pfad: Path) -> None:
        """Schreibt Kopfzeile und Zeilen nach ``pfad``."""


def _loesche(pfad: str) -> None:
    try:
        os.unlink(pfad)
    except OSError:
        pass


class CsvExporter(FehlerExporter):
    """CSV für deutsches Excel: Semikolon, UTF-8 mit BOM, Windows-Zeilenenden."""

    endung = ".csv"
    beschreibung = "CSV-Datei (*.csv)"
    _GEFAEHRLICH = ("=", "+", "-", "@", "\t", "\r")

    def __init__(self, trennzeichen: str = ";", formelschutz: bool = True) -> None:
        self._trennzeichen = trennzeichen
        self._formelschutz = formelschutz

    def _schuetze(self, wert: str) -> str:
        if self._formelschutz and wert.startswith(self._GEFAEHRLICH):
            return "'" + wert
        return wert

    def _schreibe(self, zeilen: list[tuple[str, ...]], pfad: Path) -> None:
        with open(pfad, "w", encoding="utf-8-sig", newline="") as datei:
            schreiber = csv.writer(datei, delimiter=self._trennzeichen, quoting=csv.QUOTE_MINIMAL,
                                   lineterminator="\r\n")
            schreiber.writerow(SPALTEN)
            for zeile in zeilen:
                schreiber.writerow([self._schuetze(zelle) for zelle in zeile])


class ExcelExporter(FehlerExporter):
    """Excel-Arbeitsmappe (.xlsx) mit formatierter Kopfzeile, Filter und fixierter Kopfzeile."""

    endung = ".xlsx"
    beschreibung = "Excel-Arbeitsmappe (*.xlsx)"
    _SPALTENBREITEN = (34, 28, 40, 14, 10, 80)

    def _schreibe(self, zeilen: list[tuple[str, ...]], pfad: Path) -> None:
        try:
            from openpyxl import Workbook
            from openpyxl.styles import Alignment, Font, PatternFill
            from openpyxl.utils import get_column_letter
        except ImportError as fehler:
            raise ExportFehler("Das Paket „openpyxl“ fehlt (pip install openpyxl).") from fehler

        mappe = Workbook()
        blatt = mappe.active
        blatt.title = "Fehlerliste"
        blatt.append(list(SPALTEN))
        for zeile in zeilen:
            blatt.append(list(zeile))

        kopf_schrift = Font(bold=True, color="FFFFFF")
        kopf_fuellung = PatternFill("solid", fgColor="305496")
        for zelle in blatt[1]:
            zelle.font = kopf_schrift
            zelle.fill = kopf_fuellung
            zelle.alignment = Alignment(vertical="center")
        # Alles als Text erzwingen: Werte wie "=A1" oder "E0.0" dürfen nie interpretiert werden.
        for zeile in blatt.iter_rows(min_row=2):
            for zelle in zeile:
                zelle.data_type = "s"
                zelle.number_format = "@"
                zelle.alignment = Alignment(vertical="top", wrap_text=(zelle.column == 6))
        for index, breite in enumerate(self._SPALTENBREITEN, start=1):
            blatt.column_dimensions[get_column_letter(index)].width = breite
        blatt.freeze_panes = "A2"
        blatt.auto_filter.ref = f"A1:{get_column_letter(len(SPALTEN))}{max(1, len(zeilen) + 1)}"
        mappe.save(pfad)


#: Registrierte Formate nach Dateiendung
EXPORTER: dict[str, FehlerExporter] = {
    CsvExporter.endung: CsvExporter(),
    ExcelExporter.endung: ExcelExporter(),
}


def waehle_exporter(pfad: str | Path) -> FehlerExporter:
    """Wählt das Exportformat anhand der Dateiendung."""
    endung = Path(pfad).suffix.lower()
    try:
        return EXPORTER[endung]
    except KeyError:
        raise ExportFehler(
            f"Unbekanntes Exportformat „{endung}“. Unterstützt: {', '.join(EXPORTER)}") from None


def exportiere_fehlerliste(fehler: Sequence[Pruefungsfehler], pfad: str | Path) -> Path:
    """Exportiert die Fehlerliste in das durch die Endung bestimmte Format."""
    return waehle_exporter(pfad).exportiere(fehler, pfad)
