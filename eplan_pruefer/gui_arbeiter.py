"""
gui_arbeiter.py – Hintergrundaufgaben für die Oberfläche.

Einlesen, Prüfen und Datenbankzugriffe können lange dauern oder blockieren
(Netzwerk). Sie laufen deshalb nie im GUI-Thread, sondern in einem eigenen
``QThread``. Die Oberfläche bleibt bedienbar, zeigt Fortschritt und erlaubt
Abbrechen.

Verwendung::

    aufgabe = HintergrundAufgabe(lambda melde, abbruch: tue_etwas(melde, abbruch), self)
    aufgabe.fertig.connect(...)        # zuerst verbinden …
    aufgabe.starten()                  # … dann starten
"""

from __future__ import annotations

import threading
from typing import Any, Callable

from PyQt6.QtCore import QObject, QThread, pyqtSignal, pyqtSlot

from .logging import hole_protokollierer
from .modelle import EplanPrueferFehler, VorgangAbgebrochenFehler

_log = hole_protokollierer("gui.aufgabe")

#: Signatur der auszuführenden Funktion: (Fortschrittsmelder, Abbruch-Ereignis) -> Ergebnis
Aufgabenfunktion = Callable[[Callable[[str, int, int], None], threading.Event], Any]


class HintergrundAufgabe(QObject):
    """Führt eine Funktion in einem eigenen Thread aus und meldet das Ergebnis per Signal."""

    fortschritt = pyqtSignal(str, int, int)     # Meldung, aktuell, gesamt (0 = unbekannt)
    fertig = pyqtSignal(object)
    fehlgeschlagen = pyqtSignal(str)
    abgebrochen = pyqtSignal()
    beendet = pyqtSignal()                      # immer nach dem Ergebnis, egal wie die Aufgabe endete
    aufgeraeumt = pyqtSignal()                  # der Thread ist ausgelaufen; Objekt darf freigegeben werden

    def __init__(self, funktion: Aufgabenfunktion, besitzer: QObject | None = None,
                 name: str = "Aufgabe") -> None:
        super().__init__()                      # kein Elternobjekt: wird in den Thread verschoben
        self._funktion = funktion
        self._name = name
        self._abbruch = threading.Event()
        self._thread = QThread(besitzer)
        self._thread.setObjectName(name)
        self.moveToThread(self._thread)
        self._thread.started.connect(self._ausfuehren)
        self._thread.finished.connect(self.aufgeraeumt)

    @property
    def laeuft(self) -> bool:
        return self._thread.isRunning()

    def starten(self) -> None:
        _log.info("Aufgabe „%s“ gestartet", self._name)
        self._thread.start()

    def abbrechen(self) -> None:
        _log.info("Aufgabe „%s“: Abbruch angefordert", self._name)
        self._abbruch.set()

    def warte_auf_ende(self, millisekunden: int = 5000) -> bool:
        return self._thread.wait(millisekunden)

    def _melde(self, meldung: str, aktuell: int = 0, gesamt: int = 0) -> None:
        self.fortschritt.emit(meldung, aktuell, gesamt)

    @pyqtSlot()
    def _ausfuehren(self) -> None:
        try:
            ergebnis = self._funktion(self._melde, self._abbruch)
        except VorgangAbgebrochenFehler:
            _log.info("Aufgabe „%s“ abgebrochen", self._name)
            self.abgebrochen.emit()
        except EplanPrueferFehler as fehler:
            _log.error("Aufgabe „%s“ fehlgeschlagen: %s", self._name, fehler)
            self.fehlgeschlagen.emit(str(fehler))
        except Exception as fehler:  # noqa: BLE001 – darf den GUI-Thread nie erreichen
            _log.exception("Aufgabe „%s“: unerwarteter Fehler", self._name)
            self.fehlgeschlagen.emit(
                f"Unerwarteter Fehler ({type(fehler).__name__}): {fehler}\n"
                "Einzelheiten stehen in der Protokolldatei.")
        else:
            _log.info("Aufgabe „%s“ beendet", self._name)
            self.fertig.emit(ergebnis)
        finally:
            self.beendet.emit()
            self._thread.quit()
