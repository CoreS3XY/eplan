"""
logging.py – Protokollierung aller Vorgänge.

ACHTUNG: Dieses Modul heißt bewusst wie die Standardbibliothek. Es funktioniert,
solange das Paket-Verzeichnis ``eplan_pruefer`` selbst nicht im Suchpfad
(``sys.path``) steht, d. h. die Anwendung muss über ``python main.py`` oder
``python -m eplan_pruefer`` gestartet werden – nicht über
``python eplan_pruefer/gui.py``. Innerhalb dieses Moduls ist ``import logging``
ein absoluter Import und lädt daher die Standardbibliothek.

Funktionen:
* Rotierende Protokolldatei (UTF-8) und Konsolenausgabe
* Speicherpuffer für die Protokollanzeige in der GUI
* Maskierung von Geheimnissen (Passwörter erscheinen nie im Protokoll)
* Protokollierung nicht abgefangener Ausnahmen
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
import threading
from collections import deque
from pathlib import Path
from types import TracebackType

NAMENSRAUM = "eplan_pruefer"
PROTOKOLLDATEI = "eplan_pruefer.log"
_FORMAT = "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s"
_ZEITFORMAT = "%Y-%m-%d %H:%M:%S"

_sperre = threading.Lock()
_geheimnisse: set[str] = set()
_eigene_handler: list[logging.Handler] = []
_speicher_handler: "SpeicherHandler | None" = None


def hole_protokollierer(name: str) -> logging.Logger:
    """Liefert einen Logger unterhalb des Anwendungs-Namensraums."""
    return logging.getLogger(f"{NAMENSRAUM}.{name}")


class GeheimnisFilter(logging.Filter):
    """Ersetzt registrierte Geheimnisse (z. B. Passwörter) in Protokollmeldungen durch ***."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not _geheimnisse:
            return True
        meldung = record.getMessage()
        veraendert = False
        for geheimnis in _geheimnisse:
            if geheimnis in meldung:
                meldung = meldung.replace(geheimnis, "***")
                veraendert = True
        if veraendert:
            record.msg = meldung
            record.args = None
        if record.exc_info and _geheimnisse:
            # Ausnahmetexte ebenfalls bereinigen: Traceback vorab formatieren und maskieren.
            text = logging.Formatter().formatException(record.exc_info)
            for geheimnis in _geheimnisse:
                text = text.replace(geheimnis, "***")
            record.exc_text = text
        return True


def registriere_geheimnis(geheimnis: str) -> None:
    """Meldet einen Text an, der nie im Protokoll erscheinen darf (z. B. ein Passwort)."""
    if geheimnis and len(geheimnis) >= 3:
        with _sperre:
            _geheimnisse.add(geheimnis)


class SpeicherHandler(logging.Handler):
    """Hält die letzten Protokollzeilen im Speicher (für die Protokollanzeige der GUI)."""

    def __init__(self, maximale_zeilen: int = 5000) -> None:
        super().__init__()
        self._zeilen: deque[str] = deque(maxlen=maximale_zeilen)
        self.setFormatter(logging.Formatter(_FORMAT, _ZEITFORMAT))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._zeilen.append(self.format(record))
        except Exception:  # noqa: BLE001 – Protokollierung darf die Anwendung nie stören
            self.handleError(record)

    def zeilen(self) -> list[str]:
        with self.lock:  # type: ignore[union-attr]
            return list(self._zeilen)


def hole_speicher_handler() -> "SpeicherHandler | None":
    return _speicher_handler


def _entferne_eigene_handler(wurzel: logging.Logger) -> None:
    for handler in _eigene_handler:
        wurzel.removeHandler(handler)
        try:
            handler.close()
        except Exception:  # noqa: BLE001
            pass
    _eigene_handler.clear()


def setze_stufe(stufe: str) -> None:
    """Ändert die Protokollstufe zur Laufzeit (DEBUG, INFO, WARNING, ERROR)."""
    zahl = logging.getLevelName(str(stufe).upper())
    if not isinstance(zahl, int):
        zahl = logging.INFO
    logging.getLogger(NAMENSRAUM).setLevel(zahl)


def richte_protokollierung_ein(verzeichnis: Path | None, stufe: str = "INFO",
                               konsole: bool = True) -> Path | None:
    """
    Richtet die Protokollierung ein (wiederholbarer Aufruf ersetzt die alte Einrichtung).

    :param verzeichnis: Ordner für die Protokolldatei; ``None`` = nur Konsole/Speicher.
    :param stufe: DEBUG, INFO, WARNING oder ERROR.
    :param konsole: zusätzlich auf stderr ausgeben.
    :return: Pfad der Protokolldatei oder ``None``, falls keine angelegt werden konnte.
    """
    global _speicher_handler
    wurzel = logging.getLogger(NAMENSRAUM)
    with _sperre:
        _entferne_eigene_handler(wurzel)
        wurzel.propagate = False
        formatierer = logging.Formatter(_FORMAT, _ZEITFORMAT)
        filter_ = GeheimnisFilter()

        _speicher_handler = SpeicherHandler()
        handlerliste: list[logging.Handler] = [_speicher_handler]

        dateipfad: Path | None = None
        datei_fehler: str | None = None
        if verzeichnis is not None:
            try:
                verzeichnis.mkdir(parents=True, exist_ok=True)
                dateipfad = verzeichnis / PROTOKOLLDATEI
                datei_handler = logging.handlers.RotatingFileHandler(
                    dateipfad, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
                datei_handler.setFormatter(formatierer)
                handlerliste.append(datei_handler)
            except OSError as fehler:
                dateipfad = None
                datei_fehler = str(fehler)
        if konsole or dateipfad is None:
            konsolen_handler = logging.StreamHandler(sys.stderr)
            konsolen_handler.setFormatter(formatierer)
            handlerliste.append(konsolen_handler)

        for handler in handlerliste:
            handler.addFilter(filter_)
            wurzel.addHandler(handler)
            _eigene_handler.append(handler)

    setze_stufe(stufe)
    protokollierer = hole_protokollierer("protokoll")
    if datei_fehler:
        protokollierer.warning("Protokolldatei nicht möglich (%s) – nur Konsole/Speicher.",
                               datei_fehler)
    protokollierer.info("Protokollierung gestartet (Stufe %s, Datei: %s)",
                        stufe, dateipfad or "keine")
    return dateipfad


def installiere_ausnahmeprotokoll() -> None:
    """Schreibt nicht abgefangene Ausnahmen (auch aus Threads) ins Protokoll."""
    protokollierer = hole_protokollierer("unbehandelt")
    original = sys.excepthook

    def haken(art: type[BaseException], wert: BaseException,
              spur: TracebackType | None) -> None:
        if issubclass(art, KeyboardInterrupt):
            original(art, wert, spur)
            return
        protokollierer.critical("Nicht behandelte Ausnahme", exc_info=(art, wert, spur))
        original(art, wert, spur)

    def thread_haken(argumente: threading.ExceptHookArgs) -> None:
        if argumente.exc_type is SystemExit:
            return
        protokollierer.critical(
            "Nicht behandelte Ausnahme im Thread %s",
            getattr(argumente.thread, "name", "?"),
            exc_info=(argumente.exc_type, argumente.exc_value, argumente.exc_traceback))

    sys.excepthook = haken
    threading.excepthook = thread_haken
