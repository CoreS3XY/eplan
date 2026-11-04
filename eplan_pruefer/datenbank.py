"""
datenbank.py – Zugriff auf die SQLite-Referenzdatenbank (*.db).

Die Struktur der Referenztabelle (Datei, Tabelle, Feldnamen, Primärschlüssel) steht nicht im Code,
sondern kommt vollständig aus :class:`DatenbankEinstellungen`.

Schichtung (Dependency Inversion):
* :class:`ReferenzRepository` – abstrakte Schnittstelle, von Prüfer und GUI verwendet
* :class:`SqliteReferenzRepository` – Implementierung für SQLite-Dateien

Weitere Datenbanken (MySQL, SQL Server, Oracle …) werden durch eine weitere Repository-Klasse
ergänzt; Prüfer und GUI bleiben unverändert.

Sicherheit und Datenschutz der Datei:
* Werte werden ausschließlich als Parameter übergeben (keine SQL-Injection).
* Tabellen- und Feldnamen (nicht parametrisierbar) werden validiert und in Anführungszeichen gesetzt.
* Lesen geschieht immer schreibgeschützt (``mode=ro``); nur „In Datenbank übernehmen" öffnet die
  Datei zum Schreiben, und nur eine bereits vorhandene Datei (es wird nie eine neue angelegt).
* Jede Übernahme läuft in einer eigenen Transaktion (``BEGIN IMMEDIATE`` … ``COMMIT``).

Vergleich: SQLite vergleicht Text standardmäßig exakt. Damit „Symbol existiert bereits" dasselbe
bedeutet wie beim Datenbankabgleich der Prüfung, vergleicht das Repository in Python mit derselben
Normalisierung (Leerzeichen, Groß-/Kleinschreibung laut Einstellung).
"""

from __future__ import annotations

import os
import sqlite3
import threading
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from .logging import hole_protokollierer
from .modelle import (
    DatenbankEinstellungen,
    DatenbankFehler,
    UebernahmeErgebnis,
    Verbindungstest,
    Vorhandensein,
    VorgangAbgebrochenFehler,
)

_log = hole_protokollierer("datenbank")

_BLOCKGROESSE = 5000
_MAX_BEZEICHNERLAENGE = 128
Normalisierer = Callable[[str], str]


def standard_normalisierer(text: str) -> str:
    """Leerzeichen vereinheitlichen und Groß-/Kleinschreibung ignorieren."""
    return " ".join((text or "").split()).casefold()


class ReferenzRepository(ABC):
    """Schnittstelle zur Referenzdatenbank mit Symbol/Funktionstext-Paaren."""

    @abstractmethod
    def lade_referenzdaten(self, abbruch: threading.Event | None = None) -> list[tuple[str, str]]:
        """Liefert alle (Symbol, Funktionstext)-Paare der Referenz."""

    @abstractmethod
    def pruefe_vorhandensein(self, symbol: str, funktionstext: str) -> Vorhandensein:
        """Gibt an, ob Symbol, Funktionstext und deren Kombination bereits existieren."""

    @abstractmethod
    def uebernehmen(self, paare: Sequence[tuple[str, str]],
                    konflikt_erlauben: bool = False) -> list[UebernahmeErgebnis]:
        """Speichert neue Paare. Bereits vorhandene Kombinationen werden nie doppelt angelegt."""

    @abstractmethod
    def teste_verbindung(self) -> Verbindungstest:
        """Prüft Datei, Tabelle und Felder gemäß Konfiguration."""


def quotiere_bezeichner(name: str, art: str = "Bezeichner") -> str:
    """
    Prüft einen Tabellen-/Feldnamen aus der Konfiguration und setzt ihn für SQLite in Anführungszeichen.

    :raises DatenbankFehler: bei leerem, zu langem oder unzulässigem Namen.
    """
    bereinigt = (name or "").strip()
    if not bereinigt:
        raise DatenbankFehler(f"{art} ist nicht konfiguriert.")
    if len(bereinigt) > _MAX_BEZEICHNERLAENGE:
        raise DatenbankFehler(f"{art} „{bereinigt[:20]}…“ ist länger als {_MAX_BEZEICHNERLAENGE} Zeichen.")
    if any(ord(zeichen) < 32 or zeichen == "\x7f" for zeichen in bereinigt):
        raise DatenbankFehler(f"{art} „{bereinigt!r}“ enthält Steuerzeichen.")
    return '"' + bereinigt.replace('"', '""') + '"'


def _zu_text(wert: Any) -> str:
    if wert is None:
        return ""
    if isinstance(wert, (bytes, bytearray)):
        return bytes(wert).decode("utf-8", errors="replace")
    return str(wert)


@dataclass(frozen=True, slots=True)
class Spalte:
    """Eine Spalte der Referenztabelle (Ergebnis von ``PRAGMA table_info``)."""

    name: str
    typ: str
    pflicht: bool
    standardwert: str | None
    primaer: int            # 0 = kein Primärschlüssel, sonst Position im Schlüssel


def _beschreibe(fehler: sqlite3.Error, pfad: Path) -> str:
    text = str(fehler)
    kleinbuchstaben = text.lower()
    if "locked" in kleinbuchstaben or "busy" in kleinbuchstaben:
        return (f"Die Datenbankdatei ist von einem anderen Programm gesperrt: {pfad}. "
                "Bitte das andere Programm schließen oder später erneut versuchen.")
    if "not a database" in kleinbuchstaben or "malformed" in kleinbuchstaben:
        return f"Die Datei ist keine gültige SQLite-Datenbank (oder beschädigt): {pfad}"
    if "readonly" in kleinbuchstaben or "read-only" in kleinbuchstaben:
        return f"Die Datenbankdatei ist schreibgeschützt: {pfad}"
    if "unable to open" in kleinbuchstaben:
        return f"Die Datenbankdatei kann nicht geöffnet werden: {pfad}"
    return f"Datenbankfehler: {text}"


@contextmanager
def _oeffne(dateipfad: str, schreibend: bool, timeout_s: int) -> Iterator[sqlite3.Connection]:
    """Öffnet eine vorhandene SQLite-Datei (lesend oder schreibend) und schließt sie sicher wieder."""
    text = (dateipfad or "").strip()
    if not text:
        raise DatenbankFehler("Es ist keine Datenbankdatei konfiguriert.")
    pfad = Path(text).expanduser()
    if not pfad.is_file():
        raise DatenbankFehler(f"Die Datenbankdatei existiert nicht: {pfad}")
    modus = "rw" if schreibend else "ro"
    verbindung = None
    try:
        verbindung = sqlite3.connect(f"{pfad.resolve().as_uri()}?mode={modus}", uri=True,
                                     timeout=max(1, timeout_s), isolation_level=None)
        yield verbindung
    except DatenbankFehler:
        raise
    except sqlite3.Error as fehler:
        _log.error("SQLite-Fehler (%s): %s", pfad, fehler)
        raise DatenbankFehler(_beschreibe(fehler, pfad)) from fehler
    finally:
        if verbindung is not None:
            try:
                verbindung.close()
            except sqlite3.Error:
                pass


def _lese_spalten(verbindung: sqlite3.Connection, tabelle: str) -> list[Spalte]:
    zeilen = verbindung.execute(f"PRAGMA table_info({quotiere_bezeichner(tabelle, 'Tabellenname')})").fetchall()
    return [Spalte(z[1], z[2] or "", bool(z[3]), z[4], int(z[5])) for z in zeilen]


def lese_tabellen(dateipfad: str, timeout_s: int = 5) -> list[str]:
    """Namen aller Tabellen und Sichten einer SQLite-Datei (für die Auswahl im Einstellungsdialog)."""
    with _oeffne(dateipfad, False, timeout_s) as verbindung:
        return [z[0] for z in verbindung.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view') "
            "AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\' ORDER BY name")]


def lese_spaltennamen(dateipfad: str, tabelle: str, timeout_s: int = 5) -> list[str]:
    """Spaltennamen einer Tabelle (für die Auswahl im Einstellungsdialog)."""
    with _oeffne(dateipfad, False, timeout_s) as verbindung:
        return [s.name for s in _lese_spalten(verbindung, tabelle)]


@dataclass(slots=True)
class _Index:
    """Normalisierte Sicht auf die Referenz für schnelle Existenzprüfungen."""

    symbole: set[str]
    texte: set[str]
    paare: set[tuple[str, str]]

    @classmethod
    def aus(cls, paare: Sequence[tuple[str, str]], normal: Normalisierer) -> "_Index":
        index = cls(set(), set(), set())
        for symbol, text in paare:
            index.merke(symbol, text, normal)
        return index

    def merke(self, symbol: str, text: str, normal: Normalisierer) -> None:
        s, t = normal(symbol), normal(text)
        self.symbole.add(s)
        self.texte.add(t)
        self.paare.add((s, t))

    def vorhanden(self, symbol: str, text: str, normal: Normalisierer) -> Vorhandensein:
        s, t = normal(symbol), normal(text)
        return Vorhandensein(s in self.symbole, t in self.texte, (s, t) in self.paare)


class SqliteReferenzRepository(ReferenzRepository):
    """Referenz-Repository für eine SQLite-Datei."""

    def __init__(self, einstellungen: DatenbankEinstellungen, normalisierer: Normalisierer | None = None) -> None:
        self._e = einstellungen
        self._normal = normalisierer or standard_normalisierer

    # -- Konfiguration und Struktur ---------------------------------------------------

    def _namen(self) -> tuple[str, str, str]:
        fehlend = self._e.fehlende_angaben()
        if fehlend:
            raise DatenbankFehler("Die Datenbankeinstellungen sind unvollständig: " + ", ".join(fehlend) + ".")
        return (quotiere_bezeichner(self._e.tabellenname, "Tabellenname"),
                quotiere_bezeichner(self._e.feld_symbol, "Feldname Symbol"),
                quotiere_bezeichner(self._e.feld_funktionstext, "Feldname Funktionstext"))

    def _pruefe_struktur(self, verbindung: sqlite3.Connection) -> dict[str, Spalte]:
        """Stellt sicher, dass Tabelle und beide Felder existieren; liefert die Spalten (kleingeschrieben)."""
        spalten = {s.name.casefold(): s for s in _lese_spalten(verbindung, self._e.tabellenname)}
        if not spalten:
            vorhanden = [z[0] for z in verbindung.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view') ORDER BY name")]
            raise DatenbankFehler(
                f"Die Tabelle „{self._e.tabellenname}“ existiert in der Datenbank nicht. "
                f"Vorhandene Tabellen: {', '.join(vorhanden) or '(keine)'}.")
        for bezeichnung, feld in (("Feld Symbol", self._e.feld_symbol),
                                  ("Feld Funktionstext", self._e.feld_funktionstext)):
            if feld.strip().casefold() not in spalten:
                raise DatenbankFehler(
                    f"{bezeichnung} „{feld}“ existiert in der Tabelle „{self._e.tabellenname}“ nicht. "
                    f"Vorhandene Felder: {', '.join(s.name for s in spalten.values())}.")
        return spalten

    def _lese_paare(self, verbindung: sqlite3.Connection, abbruch: threading.Event | None = None) -> list[tuple[str, str]]:
        tabelle, symbol, text = self._namen()
        self._pruefe_struktur(verbindung)
        paare: list[tuple[str, str]] = []
        cursor = verbindung.execute(f"SELECT {symbol}, {text} FROM {tabelle}")
        while True:
            if abbruch is not None and abbruch.is_set():
                raise VorgangAbgebrochenFehler("Der Vorgang wurde abgebrochen.")
            block = cursor.fetchmany(_BLOCKGROESSE)
            if not block:
                return paare
            paare.extend((_zu_text(s), _zu_text(t)) for s, t in block)

    # -- Lesen -------------------------------------------------------------------------

    def lade_referenzdaten(self, abbruch: threading.Event | None = None) -> list[tuple[str, str]]:
        with _oeffne(self._e.dateipfad, False, self._e.timeout_s) as verbindung:
            paare = self._lese_paare(verbindung, abbruch)
        _log.info("Referenzdaten geladen: %d Zeilen aus %s, Tabelle %s", len(paare),
                  Path(self._e.dateipfad).name, self._e.tabellenname)
        return paare

    def pruefe_vorhandensein(self, symbol: str, funktionstext: str) -> Vorhandensein:
        return _Index.aus(self.lade_referenzdaten(), self._normal).vorhanden(symbol, funktionstext, self._normal)

    # -- Schreiben ------------------------------------------------------------------------

    def uebernehmen(self, paare: Sequence[tuple[str, str]],
                    konflikt_erlauben: bool = False) -> list[UebernahmeErgebnis]:
        tabelle, feld_symbol, feld_text = self._namen()
        ergebnisse: list[UebernahmeErgebnis] = []
        with _oeffne(self._e.dateipfad, True, self._e.timeout_s) as verbindung:
            spalten = self._pruefe_struktur(verbindung)
            index = _Index.aus(self._lese_paare(verbindung), self._normal)
            for symbol, funktionstext in paare:
                ergebnis = self._uebernimm_ein_paar(verbindung, spalten, index, tabelle, feld_symbol, feld_text,
                                                    symbol.strip(), funktionstext.strip(), konflikt_erlauben)
                ergebnisse.append(ergebnis)
                _log.info("Übernahme in Datenbank: Symbol=%r Funktionstext=%r -> %s (%s)", symbol, funktionstext,
                          "gespeichert" if ergebnis.uebernommen else "nicht gespeichert", ergebnis.meldung)
        return ergebnisse

    def _uebernimm_ein_paar(self, verbindung: sqlite3.Connection, spalten: dict[str, Spalte], index: _Index,
                            tabelle: str, feld_symbol: str, feld_text: str, symbol: str, funktionstext: str,
                            konflikt_erlauben: bool) -> UebernahmeErgebnis:
        if not symbol or not funktionstext:
            return UebernahmeErgebnis(symbol, funktionstext, False,
                                      "Symbol und Funktionstext müssen beide ausgefüllt sein.")
        vorhanden = index.vorhanden(symbol, funktionstext, self._normal)
        if vorhanden.kombination:
            return UebernahmeErgebnis(symbol, funktionstext, False, "Die Kombination ist bereits vorhanden.",
                                      vorhanden)
        if (vorhanden.symbol or vorhanden.funktionstext) and not konflikt_erlauben:
            gruende = []
            if vorhanden.symbol:
                gruende.append("Das Symbol existiert bereits (mit anderem Funktionstext)")
            if vorhanden.funktionstext:
                gruende.append("Der Funktionstext existiert bereits (mit anderem Symbol)")
            return UebernahmeErgebnis(symbol, funktionstext, False, "; ".join(gruende) + ".", vorhanden,
                                      benoetigt_bestaetigung=True)
        try:
            verbindung.execute("BEGIN IMMEDIATE")
            spaltennamen, werte = [feld_symbol, feld_text], [symbol, funktionstext]
            schluessel = self._naechster_schluessel(verbindung, spalten, tabelle)
            if schluessel is not None:
                spaltennamen.append(schluessel[0])
                werte.append(schluessel[1])
            platzhalter = ", ".join("?" * len(werte))
            verbindung.execute(f"INSERT INTO {tabelle} ({', '.join(spaltennamen)}) VALUES ({platzhalter})", werte)
            verbindung.execute("COMMIT")
        except (DatenbankFehler, sqlite3.Error) as fehler:
            if verbindung.in_transaction:
                verbindung.execute("ROLLBACK")
            if isinstance(fehler, sqlite3.Error):
                _log.error("Speichern fehlgeschlagen: %s", fehler)
                meldung = _beschreibe(fehler, Path(self._e.dateipfad))
            else:
                meldung = str(fehler)
            return UebernahmeErgebnis(symbol, funktionstext, False, meldung, vorhanden)
        index.merke(symbol, funktionstext, self._normal)
        return UebernahmeErgebnis(symbol, funktionstext, True, "In die Datenbank übernommen.", vorhanden)

    def _naechster_schluessel(self, verbindung: sqlite3.Connection, spalten: dict[str, Spalte],
                              tabelle: str) -> tuple[str, int] | None:
        """
        Ermittelt bei Bedarf einen Primärschlüsselwert.

        * kein Schlüssel konfiguriert oder Schlüssel = Symbol-/Textfeld -> kein Zusatzwert
        * Spalte mit Standardwert -> die Datenbank vergibt ihn
        * Ganzzahlschlüssel -> MAX()+1 (innerhalb der Schreibtransaktion)
        * anderer Typ -> Fehler: kann nicht automatisch vergeben werden
        """
        name = self._e.primaerschluessel.strip()
        if not name or name.casefold() in (self._e.feld_symbol.strip().casefold(),
                                           self._e.feld_funktionstext.strip().casefold()):
            return None
        spalte = spalten.get(name.casefold())
        if spalte is None:
            raise DatenbankFehler(f"Der Primärschlüssel „{name}“ existiert in der Tabelle nicht.")
        if spalte.standardwert is not None:
            return None
        if "INT" not in spalte.typ.upper():
            raise DatenbankFehler(
                f"Der Primärschlüssel „{name}“ ({spalte.typ or 'ohne Typ'}) ist keine Ganzzahl und hat keinen "
                "Standardwert; ein Wert kann nicht automatisch vergeben werden.")
        quotiert = quotiere_bezeichner(name, "Primärschlüssel")
        wert = verbindung.execute(f"SELECT COALESCE(MAX({quotiert}), 0) + 1 FROM {tabelle}").fetchone()[0]
        return quotiert, int(wert)

    # -- Verbindungstest -------------------------------------------------------------------------

    def teste_verbindung(self) -> Verbindungstest:
        meldungen: list[str] = []
        try:
            tabelle, _, _ = self._namen()
            pfad = Path(self._e.dateipfad.strip()).expanduser()
            with _oeffne(self._e.dateipfad, False, self._e.timeout_s) as verbindung:
                version = verbindung.execute("SELECT sqlite_version()").fetchone()[0]
                meldungen.append(f"Datenbankdatei gefunden ({pfad.stat().st_size:,} Bytes, SQLite {version}).".replace(",", "."))
                spalten = self._pruefe_struktur(verbindung)
                meldungen.append(f"Tabelle „{self._e.tabellenname}“ gefunden.")
                meldungen.append(f"Feld Symbol „{self._e.feld_symbol}“ und Feld Funktionstext "
                                 f"„{self._e.feld_funktionstext}“ vorhanden.")
                anzahl = verbindung.execute(f"SELECT COUNT(*) FROM {tabelle}").fetchone()[0]
                meldungen.append(f"Lesezugriff möglich ({anzahl} Datensätze in der Referenz).")
                name = self._e.primaerschluessel.strip()
                if name:
                    if name.casefold() not in spalten:
                        return Verbindungstest(False, (*meldungen, f"Primärschlüssel „{name}“ existiert NICHT in der Tabelle."))
                    spalte = spalten[name.casefold()]
                    if (name.casefold() not in (self._e.feld_symbol.strip().casefold(),
                                                self._e.feld_funktionstext.strip().casefold())
                            and spalte.standardwert is None and "INT" not in spalte.typ.upper()):
                        meldungen.append("WARNUNG: Der Primärschlüssel ist keine Ganzzahl und hat keinen Standardwert – "
                                         "„In Datenbank übernehmen“ wird nicht möglich sein.")
                if not os.access(pfad, os.W_OK):
                    meldungen.append("HINWEIS: Die Datei ist schreibgeschützt – „In Datenbank übernehmen“ ist nicht möglich.")
            return Verbindungstest(True, tuple(meldungen))
        except DatenbankFehler as fehler:
            meldungen.append(str(fehler))
            return Verbindungstest(False, tuple(meldungen))


def erzeuge_repository(einstellungen: DatenbankEinstellungen,
                       normalisierer: Normalisierer | None = None) -> ReferenzRepository:
    """Fabrikfunktion; hier wird bei weiteren Datenbanktypen nach Einstellung verzweigt."""
    return SqliteReferenzRepository(einstellungen, normalisierer)
