"""
gui_filter.py – Tabellenkopf mit einer Filterzeile (ein Eingabefeld je Spalte).

Jede Spalte hat unter ihrer Überschrift ein Eingabefeld für einen Wildcard-Ausdruck mit positiven
und negativen Mustern (Syntax: :mod:`suchfilter`). Die Felder sitzen im Kopf selbst; deshalb bleiben
sie bei Änderung der Spaltenbreite, beim Verschieben und beim waagerechten Scrollen ausgerichtet.
"""

from __future__ import annotations

from PyQt6.QtCore import QRect, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QPainter
from PyQt6.QtWidgets import QHeaderView, QLineEdit, QTableView

from .suchfilter import HILFETEXT


class FilterKopf(QHeaderView):
    """Waagerechter Tabellenkopf mit Filterfeldern unter den Überschriften."""

    filter_geaendert = pyqtSignal(int, str)         # logische Spalte, Ausdruck

    _RAND = 2

    def __init__(self, tabelle: QTableView) -> None:
        super().__init__(Qt.Orientation.Horizontal, tabelle)
        self._tabelle = tabelle
        self._editoren: list[QLineEdit] = []
        self.setSectionsClickable(True)
        self.setSortIndicatorShown(True)
        self.setStretchLastSection(True)
        self.sectionResized.connect(self._positioniere)
        self.sectionMoved.connect(self._positioniere)
        self.geometriesChanged.connect(self._positioniere)
        tabelle.horizontalScrollBar().valueChanged.connect(self._positioniere)

    # -- Aufbau --------------------------------------------------------------------------

    def erzeuge_filterfelder(self, anzahl_spalten: int) -> None:
        """Legt je Spalte ein Eingabefeld an (nach ``setModel`` aufrufen)."""
        for editor in self._editoren:
            editor.deleteLater()
        self._editoren = []
        for spalte in range(anzahl_spalten):
            editor = QLineEdit(self)
            editor.setPlaceholderText("Filter")
            editor.setClearButtonEnabled(True)
            editor.setToolTip(HILFETEXT)
            editor.textChanged.connect(lambda text, s=spalte: self.filter_geaendert.emit(s, text))
            self._editoren.append(editor)
        self.updateGeometry()
        self._positioniere()

    def filtertext(self, spalte: int) -> str:
        return self._editoren[spalte].text() if 0 <= spalte < len(self._editoren) else ""

    def setze_filtertext(self, spalte: int, text: str) -> None:
        if 0 <= spalte < len(self._editoren):
            self._editoren[spalte].setText(text)

    def loesche_filterfelder(self) -> None:
        for editor in self._editoren:
            editor.clear()

    # -- Geometrie -----------------------------------------------------------------------------

    def _feldhoehe(self) -> int:
        return (self._editoren[0].sizeHint().height() + 2 * self._RAND) if self._editoren else 0

    def _beschriftungshoehe(self) -> int:
        return super().sizeHint().height()

    def sizeHint(self) -> QSize:  # noqa: N802 (Qt-Name)
        groesse = super().sizeHint()
        return QSize(groesse.width(), groesse.height() + self._feldhoehe())

    def paintSection(self, painter: QPainter, rect: QRect, logical_index: int) -> None:  # noqa: N802 (Qt-Name)
        # Die Überschrift nur im oberen Teil zeichnen; darunter sitzt das Filterfeld.
        super().paintSection(painter, rect.adjusted(0, 0, 0, -self._feldhoehe()), logical_index)

    def _positioniere(self, *_: object) -> None:
        oben = self._beschriftungshoehe() + self._RAND
        hoehe = self._feldhoehe() - 2 * self._RAND
        for spalte, editor in enumerate(self._editoren):
            if self.isSectionHidden(spalte):
                editor.hide()
                continue
            editor.setGeometry(self.sectionViewportPosition(spalte) + self._RAND, oben,
                               max(10, self.sectionSize(spalte) - 2 * self._RAND), hoehe)
            editor.show()

    def resizeEvent(self, ereignis) -> None:  # noqa: N802 (Qt-Name)
        super().resizeEvent(ereignis)
        self._positioniere()

    def showEvent(self, ereignis) -> None:  # noqa: N802 (Qt-Name)
        super().showEvent(ereignis)
        self._positioniere()
