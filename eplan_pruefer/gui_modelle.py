"""
gui_modelle.py – Qt-Tabellenmodelle (Model/View) für Fehlerliste und Datensätze.

Die Modelle enthalten keine Fachlogik, sondern stellen nur Daten aus
``modelle.py`` für die Tabellen dar.
"""

from __future__ import annotations

import re
from typing import Any, Sequence

from PyQt6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt

from .modelle import DatenbankStatus, EplanDatensatz, Fehlertyp, Pruefungsfehler
from .suchfilter import WildcardFilter

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

    SPALTEN = ("Fehlertyp", "Symbol", "Funktionstext", "Adresse", "Seite", "Beschreibung", "Verweis")

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
                f.seite, f.beschreibung, f.verweis)[spalte]


class SpaltenFilterModell(QSortFilterProxyModel):
    """
    Filtert eine Tabelle je Spalte und über alle Spalten mit Wildcard-Ausdrücken
    (positiv und negativ, siehe :mod:`suchfilter`) und sortiert Seitennummern natürlich.
    """

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._spaltenfilter: dict[int, WildcardFilter] = {}
        self._gesamtfilter = WildcardFilter("")
        self.setSortRole(SORTIER_ROLLE)
        self.setDynamicSortFilter(True)

    @property
    def filter_aktiv(self) -> bool:
        return self._gesamtfilter.aktiv or any(f.aktiv for f in self._spaltenfilter.values())

    def setze_spaltenfilter(self, spalte: int, ausdruck: str) -> None:
        self._spaltenfilter[spalte] = WildcardFilter(ausdruck)
        self.invalidateFilter()

    def setze_gesamtfilter(self, ausdruck: str) -> None:
        """Gesamtsuche über alle Spalten."""
        self._gesamtfilter = WildcardFilter(ausdruck)
        self.invalidateFilter()

    def loesche_filter(self) -> None:
        self._spaltenfilter.clear()
        self._gesamtfilter = WildcardFilter("")
        self.invalidateFilter()

    def _zusatzfilter(self, quellzeile: int) -> bool:
        """Erweiterungspunkt für weitere Bedingungen (z. B. Fehlertyp)."""
        return True

    def filterAcceptsRow(self, quellzeile: int, quellindex: QModelIndex) -> bool:  # noqa: N802 (Qt-Name)
        if not self._zusatzfilter(quellzeile):
            return False
        modell = self.sourceModel()
        for spalte, filter_ in self._spaltenfilter.items():
            if filter_.aktiv and not filter_.trifft(modell.index(quellzeile, spalte).data() or ""):
                return False
        if self._gesamtfilter.aktiv:
            zellen = [modell.index(quellzeile, s).data() or "" for s in range(modell.columnCount())]
            return self._gesamtfilter.trifft_zeile(zellen)
        return True

    def lessThan(self, links: QModelIndex, rechts: QModelIndex) -> bool:  # noqa: N802 (Qt-Name)
        a, b = links.data(SORTIER_ROLLE), rechts.data(SORTIER_ROLLE)
        if a is None or b is None:
            return False
        return a < b

    def sichtbare_quellzeilen(self) -> list[int]:
        """Zeilennummern des Quellmodells in Anzeigereihenfolge."""
        return [self.mapToSource(self.index(z, 0)).row() for z in range(self.rowCount())]


class FehlerFilterModell(SpaltenFilterModell):
    """Fehlerliste: zusätzlich nach Fehlertyp filterbar."""

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._typ: Fehlertyp | None = None

    @property
    def filter_aktiv(self) -> bool:
        return self._typ is not None or super().filter_aktiv

    def setze_typ(self, typ: Fehlertyp | None) -> None:
        self._typ = typ
        self.invalidateFilter()

    def loesche_filter(self) -> None:
        self._typ = None
        super().loesche_filter()

    def _zusatzfilter(self, quellzeile: int) -> bool:
        modell: FehlerModell = self.sourceModel()  # type: ignore[assignment]
        fehler = modell.fehler_an(quellzeile)
        return fehler is not None and (self._typ is None or fehler.fehlertyp is self._typ)

    def sichtbare_fehler(self) -> list[Pruefungsfehler]:
        """Die aktuell sichtbaren Fehler in Anzeigereihenfolge."""
        modell: FehlerModell = self.sourceModel()  # type: ignore[assignment]
        return [f for f in (modell.fehler_an(z) for z in self.sichtbare_quellzeilen()) if f is not None]


class DatensatzModell(_TabellenModell):
    """Alle eingelesenen SPS-Datensätze."""

    SPALTEN = ("Nr.", "Symbolische Adresse", "Funktionstext", "SPS-Adresse", "Seite",
               "Seitenname", "BMK (sichtbar)", "Vollständiges BMK", "Datenbank", "Fundstelle")

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._daten: list[EplanDatensatz] = []
        self._status: dict[int, DatenbankStatus] = {}

    def setze_daten(self, daten: Sequence[EplanDatensatz],
                    status: dict[int, DatenbankStatus] | None = None) -> None:
        self.beginResetModel()
        self._daten = list(daten)
        self._status = dict(status or {})
        self.endResetModel()

    def setze_status(self, status: dict[int, DatenbankStatus] | None) -> None:
        """Ergebnis des Datenbankabgleichs je Datensatz (leer = nicht geprüft)."""
        self._status = dict(status or {})
        spalte = self.SPALTEN.index("Datenbank")
        if self._daten:
            self.dataChanged.emit(self.index(0, spalte), self.index(len(self._daten) - 1, spalte))

    @property
    def spalte_datenbank(self) -> int:
        return self.SPALTEN.index("Datenbank")

    def datensatz_an(self, zeile: int) -> EplanDatensatz | None:
        return self._daten[zeile] if 0 <= zeile < len(self._daten) else None

    def _zeilenanzahl(self) -> int:
        return len(self._daten)

    def _text(self, zeile: int, spalte: int) -> str:
        d = self._daten[zeile]
        return (str(d.laufende_nummer), d.symbolische_adresse, d.funktionstext, d.sps_adresse,
                d.seite_anzeige, d.seitenname, d.betriebsmittelkennzeichen,
                d.vollstaendiges_bmk,
                self._status.get(d.laufende_nummer, DatenbankStatus.NICHT_GEPRUEFT).bezeichnung,
                d.fundstelle.als_text())[spalte]
