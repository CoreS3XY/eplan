"""
gui_modelle.py – Qt-Tabellenmodelle (Model/View) für Fehlerliste und Datensätze.

Die Modelle enthalten keine Fachlogik, sondern stellen nur Daten aus
``modelle.py`` für die Tabellen dar.
"""

from __future__ import annotations

import re
from typing import Any, Sequence

from PyQt6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt

from .modelle import EplanDatensatz, Fehlertyp, Pruefungsfehler

SORTIER_ROLLE = Qt.ItemDataRole.UserRole + 1
_ZAHLENTEIL = re.compile(r"(\d+)")


def natuerlicher_schluessel(text: str) -> tuple:
    """Sortierschlüssel, der Zahlen numerisch vergleicht (Seite 2 vor Seite 12)."""
    teile = _ZAHLENTEIL.split((text or "").casefold())
    return tuple((0, int(t), "") if t.isdigit() else (1, 0, t) for t in teile if t != "")


class _TabellenModell(QAbstractTableModel):
    """Gemeinsame Grundfunktionen."""

    SPALTEN: tuple[str, ...] = ()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else self._zeilenanzahl()

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self.SPALTEN)

    def headerData(self, abschnitt: int, ausrichtung: Qt.Orientation,
                   rolle: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if rolle == Qt.ItemDataRole.DisplayRole and ausrichtung == Qt.Orientation.Horizontal:
            return self.SPALTEN[abschnitt]
        return None

    def _zeilenanzahl(self) -> int:  # pragma: no cover - wird überschrieben
        raise NotImplementedError

    def _text(self, zeile: int, spalte: int) -> str:  # pragma: no cover - wird überschrieben
        raise NotImplementedError

    def data(self, index: QModelIndex, rolle: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or not 0 <= index.row() < self._zeilenanzahl():
            return None
        text = self._text(index.row(), index.column())
        if rolle in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            return text
        if rolle == SORTIER_ROLLE:
            return natuerlicher_schluessel(text)
        return None


class FehlerModell(_TabellenModell):
    """Fehlerliste: Fehlertyp, Symbol, Funktionstext, Adresse, Seite, Beschreibung."""

    SPALTEN = ("Fehlertyp", "Symbol", "Funktionstext", "Adresse", "Seite", "Beschreibung")

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._fehler: list[Pruefungsfehler] = []

    def setze_fehler(self, fehler: Sequence[Pruefungsfehler]) -> None:
        self.beginResetModel()
        self._fehler = list(fehler)
        self.endResetModel()

    def fehler_an(self, zeile: int) -> Pruefungsfehler | None:
        return self._fehler[zeile] if 0 <= zeile < len(self._fehler) else None

    def _zeilenanzahl(self) -> int:
        return len(self._fehler)

    def _text(self, zeile: int, spalte: int) -> str:
        f = self._fehler[zeile]
        return (f.fehlertyp.bezeichnung, f.symbol, f.funktionstext, f.adresse,
                f.seite, f.beschreibung)[spalte]


class FehlerFilterModell(QSortFilterProxyModel):
    """Filtert nach Fehlertyp und Suchtext und sortiert Seitennummern natürlich."""

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._typ: Fehlertyp | None = None
        self._suchtext = ""
        self.setSortRole(SORTIER_ROLLE)
        self.setDynamicSortFilter(True)

    @property
    def filter_aktiv(self) -> bool:
        return self._typ is not None or bool(self._suchtext)

    def setze_typ(self, typ: Fehlertyp | None) -> None:
        self._typ = typ
        self.invalidateFilter()

    def setze_suchtext(self, text: str) -> None:
        self._suchtext = text.strip().casefold()
        self.invalidateFilter()

    def filterAcceptsRow(self, quellzeile: int, quellindex: QModelIndex) -> bool:
        modell: FehlerModell = self.sourceModel()  # type: ignore[assignment]
        fehler = modell.fehler_an(quellzeile)
        if fehler is None:
            return False
        if self._typ is not None and fehler.fehlertyp is not self._typ:
            return False
        if self._suchtext:
            heuhaufen = " ".join((fehler.symbol, fehler.funktionstext, fehler.adresse,
                                  fehler.seite, fehler.beschreibung)).casefold()
            return self._suchtext in heuhaufen
        return True

    def lessThan(self, links: QModelIndex, rechts: QModelIndex) -> bool:
        a, b = links.data(SORTIER_ROLLE), rechts.data(SORTIER_ROLLE)
        if a is None or b is None:
            return False
        return a < b

    def sichtbare_fehler(self) -> list[Pruefungsfehler]:
        """Die aktuell sichtbaren Fehler in Anzeigereihenfolge."""
        modell: FehlerModell = self.sourceModel()  # type: ignore[assignment]
        ergebnis = []
        for zeile in range(self.rowCount()):
            quelle = self.mapToSource(self.index(zeile, 0))
            fehler = modell.fehler_an(quelle.row())
            if fehler is not None:
                ergebnis.append(fehler)
        return ergebnis


class DatensatzModell(_TabellenModell):
    """Alle eingelesenen SPS-Datensätze."""

    SPALTEN = ("Nr.", "Symbolische Adresse", "Funktionstext", "SPS-Adresse", "Seite",
               "Seitenname", "Betriebsmittelkennzeichen", "Fundstelle")

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._daten: list[EplanDatensatz] = []

    def setze_daten(self, daten: Sequence[EplanDatensatz]) -> None:
        self.beginResetModel()
        self._daten = list(daten)
        self.endResetModel()

    def datensatz_an(self, zeile: int) -> EplanDatensatz | None:
        return self._daten[zeile] if 0 <= zeile < len(self._daten) else None

    def _zeilenanzahl(self) -> int:
        return len(self._daten)

    def _text(self, zeile: int, spalte: int) -> str:
        d = self._daten[zeile]
        return (str(d.laufende_nummer), d.symbolische_adresse, d.funktionstext, d.sps_adresse,
                d.seite_anzeige, d.seitenname, d.betriebsmittelkennzeichen,
                d.fundstelle.als_text())[spalte]
