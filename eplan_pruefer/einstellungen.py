"""
einstellungen.py – Speichern und Laden der Konfiguration.

Die Konfiguration liegt in einer INI-Datei (``configparser``):

* Windows: ``%APPDATA%\\EplanPruefer\\einstellungen.ini``
* sonst:   ``$XDG_CONFIG_HOME/eplan_pruefer/einstellungen.ini`` bzw. ``~/.config/...``

Die Datenbankstruktur (Tabelle, Felder, Primärschlüssel) steht ausschließlich
hier – nie im Quellcode. Das Passwort wird – wie gefordert – in der Datei
gespeichert; unter Linux/macOS erhält die Datei deshalb die Rechte 0600. Unter
Windows gelten die Rechte des Benutzerprofils.
"""

from __future__ import annotations

import configparser
import os
import sys
import tempfile
from dataclasses import fields
from pathlib import Path

from .logging import hole_protokollierer, registriere_geheimnis
from .modelle import (
    FELDNAMEN,
    STANDARD_FELDZUORDNUNG,
    AllgemeineEinstellungen,
    DatenbankEinstellungen,
    EinstellungsFehler,
    Einstellungen,
    PruefEinstellungen,
)

_log = hole_protokollierer("einstellungen")

DATEINAME = "einstellungen.ini"
_ABSCHNITT_DB = "datenbank"
_ABSCHNITT_PRUEFUNG = "pruefung"
_ABSCHNITT_ALLGEMEIN = "allgemein"
_ABSCHNITT_FELDER = "feldzuordnung"
_TRENNER_ALIAS = "|"


def konfigurationsverzeichnis() -> Path:
    """Standardordner für Konfiguration und Protokolle."""
    if sys.platform == "win32":
        basis = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        return basis / "EplanPruefer"
    basis = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return basis / "eplan_pruefer"


def _lese_bool(text: str, standard: bool) -> bool:
    wert = text.strip().lower()
    if wert in ("1", "true", "ja", "yes", "on", "an", "wahr"):
        return True
    if wert in ("0", "false", "nein", "no", "off", "aus", "falsch"):
        return False
    return standard


class EinstellungsVerwalter:
    """Lädt und speichert :class:`Einstellungen`."""

    def __init__(self, pfad: Path | None = None) -> None:
        self._pfad = Path(pfad) if pfad else konfigurationsverzeichnis() / DATEINAME

    @property
    def pfad(self) -> Path:
        return self._pfad

    # -- Laden --------------------------------------------------------------

    def laden(self) -> Einstellungen:
        """
        Liest die Konfiguration. Fehlt die Datei, werden Standardwerte geliefert.
        Ist die Datei unlesbar, wird sie als ``.defekt`` gesichert und es werden
        Standardwerte verwendet; einzelne ungültige Werte fallen auf ihren
        Standard zurück und werden protokolliert.
        """
        einstellungen = Einstellungen()
        if not self._pfad.exists():
            _log.info("Keine Konfigurationsdatei vorhanden (%s) – Standardwerte.", self._pfad)
            return einstellungen

        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str  # Schlüssel unverändert lassen
        try:
            with open(self._pfad, "r", encoding="utf-8") as datei:
                parser.read_file(datei)
        except (OSError, UnicodeDecodeError, configparser.Error) as fehler:
            sicherung = self._pfad.with_suffix(self._pfad.suffix + ".defekt")
            _log.error("Konfigurationsdatei unlesbar (%s): %s – Sicherung nach %s",
                       self._pfad, fehler, sicherung)
            try:
                os.replace(self._pfad, sicherung)
            except OSError as sicherungsfehler:
                _log.error("Sicherung der defekten Konfiguration fehlgeschlagen: %s",
                           sicherungsfehler)
            return einstellungen

        self._lese_dataclass(parser, _ABSCHNITT_DB, einstellungen.datenbank)
        self._lese_dataclass(parser, _ABSCHNITT_PRUEFUNG, einstellungen.pruefung)
        self._lese_dataclass(parser, _ABSCHNITT_ALLGEMEIN, einstellungen.allgemein)
        einstellungen.feldzuordnung = self._lese_feldzuordnung(parser)

        registriere_geheimnis(einstellungen.datenbank.passwort)
        _log.info("Konfiguration geladen: %s", self._pfad)
        return einstellungen

    @staticmethod
    def _lese_dataclass(parser: configparser.ConfigParser, abschnitt: str, ziel: object) -> None:
        if not parser.has_section(abschnitt):
            return
        for feld in fields(ziel):  # type: ignore[arg-type]
            if not parser.has_option(abschnitt, feld.name):
                continue
            roh = parser.get(abschnitt, feld.name)
            standard = getattr(ziel, feld.name)
            try:
                if isinstance(standard, bool):
                    wert: object = _lese_bool(roh, standard)
                elif isinstance(standard, int):
                    wert = int(roh.strip())
                else:
                    wert = roh.strip() if feld.name != "passwort" else roh
                setattr(ziel, feld.name, wert)
            except ValueError:
                _log.warning("Ungültiger Wert für [%s] %s = %r – Standard %r bleibt.",
                             abschnitt, feld.name, roh, standard)

    @staticmethod
    def _lese_feldzuordnung(parser: configparser.ConfigParser) -> dict[str, tuple[str, ...]]:
        ergebnis = dict(STANDARD_FELDZUORDNUNG)
        if not parser.has_section(_ABSCHNITT_FELDER):
            return ergebnis
        for feld in FELDNAMEN:
            if parser.has_option(_ABSCHNITT_FELDER, feld):
                aliase = tuple(a.strip() for a in
                               parser.get(_ABSCHNITT_FELDER, feld).split(_TRENNER_ALIAS)
                               if a.strip())
                if aliase:
                    ergebnis[feld] = aliase
        return ergebnis

    # -- Speichern ----------------------------------------------------------

    def speichern(self, einstellungen: Einstellungen) -> None:
        """Schreibt die Konfiguration atomar (temporäre Datei, dann Ersetzen)."""
        self.pruefe(einstellungen)
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str
        for abschnitt, objekt in (
            (_ABSCHNITT_DB, einstellungen.datenbank),
            (_ABSCHNITT_PRUEFUNG, einstellungen.pruefung),
            (_ABSCHNITT_ALLGEMEIN, einstellungen.allgemein),
        ):
            parser[abschnitt] = {}
            for feld in fields(objekt):
                wert = getattr(objekt, feld.name)
                parser[abschnitt][feld.name] = ("ja" if wert else "nein") \
                    if isinstance(wert, bool) else str(wert)
        parser[_ABSCHNITT_FELDER] = {
            feld: _TRENNER_ALIAS.join(einstellungen.feldzuordnung.get(feld, ()))
            for feld in FELDNAMEN
        }

        verzeichnis = self._pfad.parent
        try:
            verzeichnis.mkdir(parents=True, exist_ok=True)
            deskriptor, temporaer = tempfile.mkstemp(prefix=".einstellungen-", dir=verzeichnis)
            try:
                with os.fdopen(deskriptor, "w", encoding="utf-8") as datei:
                    datei.write("; EPLAN-Prüfer – Konfiguration (wird vom Programm verwaltet)\n")
                    parser.write(datei)
                if sys.platform != "win32":
                    os.chmod(temporaer, 0o600)
                os.replace(temporaer, self._pfad)
            except BaseException:
                try:
                    os.unlink(temporaer)
                except OSError:
                    pass
                raise
        except OSError as fehler:
            _log.error("Konfiguration konnte nicht gespeichert werden: %s", fehler)
            raise EinstellungsFehler(
                f"Die Konfiguration konnte nicht gespeichert werden:\n{self._pfad}\n{fehler}"
            ) from fehler
        registriere_geheimnis(einstellungen.datenbank.passwort)
        _log.info("Konfiguration gespeichert: %s", self._pfad)

    # -- Plausibilität -------------------------------------------------------

    @staticmethod
    def pruefe(einstellungen: Einstellungen) -> None:
        """Wirft :class:`EinstellungsFehler`, wenn Werte offensichtlich unzulässig sind."""
        db: DatenbankEinstellungen = einstellungen.datenbank
        if not 1 <= db.port <= 65535:
            raise EinstellungsFehler("Der Datenbank-Port muss zwischen 1 und 65535 liegen.")
        if not 1 <= db.verbindungs_timeout_s <= 300:
            raise EinstellungsFehler("Der Verbindungs-Timeout muss zwischen 1 und 300 s liegen.")
        if einstellungen.allgemein.protokollstufe.upper() not in (
                "DEBUG", "INFO", "WARNING", "ERROR"):
            raise EinstellungsFehler("Die Protokollstufe muss DEBUG, INFO, WARNING oder ERROR sein.")
        for feld in FELDNAMEN:
            if not einstellungen.feldzuordnung.get(feld):
                raise EinstellungsFehler(
                    f"Für das Feld „{feld}“ ist keine Bezeichnung hinterlegt.")
        if any(zeichen in db.passwort for zeichen in ("\n", "\r", "\x00")):
            raise EinstellungsFehler("Das Passwort darf keine Zeilenumbrüche enthalten "
                                     "(es wird in einer Textdatei gespeichert).")
        for name in (db.tabellenname, db.feld_symbol, db.feld_funktionstext, db.primaerschluessel):
            if name and any(zeichen in name for zeichen in ("`", "\x00", "\n", "\r")):
                raise EinstellungsFehler(
                    "Tabellen- und Feldnamen dürfen keine Backticks oder Steuerzeichen enthalten.")
            if len(name) > 64:
                raise EinstellungsFehler("Tabellen- und Feldnamen sind auf 64 Zeichen begrenzt.")
