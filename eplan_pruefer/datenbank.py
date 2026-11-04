"""
datenbank.py – Zugriff auf die MySQL-Referenzdatenbank.

Die Struktur der Referenztabelle (Datenbank, Tabelle, Feldnamen, Primärschlüssel)
steht nicht im Code, sondern kommt vollständig aus :class:`DatenbankEinstellungen`.

Schichtung (Dependency Inversion):
* :class:`ReferenzRepository` – abstrakte Schnittstelle, von Prüfer und GUI verwendet
* :class:`MySqlReferenzRepository` – Implementierung für MySQL/MariaDB

Weitere Datenbanken (SQL Server, Oracle …) werden durch eine weitere
Repository-Klasse ergänzt; Prüfer und GUI bleiben unverändert.

Sicherheit:
* Werte werden ausschließlich als Parameter übergeben (keine SQL-Injection).
* Tabellen- und Feldnamen (nicht parametrisierbar) werden validiert und mit
  Backticks quotiert.
* Passwörter werden nie protokolliert.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Sequence

from .logging import hole_protokollierer, registriere_geheimnis
from .modelle import (
    DatenbankEinstellungen,
    DatenbankFehler,
    UebernahmeErgebnis,
    Verbindungstest,
    Vorhandensein,
    VorgangAbgebrochenFehler,
)

try:  # Der Treiber ist Pflicht für den Betrieb, soll aber den Import des Moduls nicht verhindern.
    import mysql.connector as _mysql
    _TreiberFehler: type[BaseException] = _mysql.Error
except ImportError:  # pragma: no cover - nur ohne installierten Treiber
    _mysql = None  # type: ignore[assignment]
    _TreiberFehler = DatenbankFehler

_log = hole_protokollierer("datenbank")

_BLOCKGROESSE = 5000
_GANZZAHLTYPEN = {"tinyint", "smallint", "mediumint", "int", "integer", "bigint"}


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
        """Prüft Verbindung, Tabelle und Felder gemäß Konfiguration."""


def quotiere_bezeichner(name: str, art: str = "Bezeichner") -> str:
    """
    Prüft einen Tabellen-/Feldnamen aus der Konfiguration und quotiert ihn für MySQL.

    :raises DatenbankFehler: bei leerem, zu langem oder unzulässigem Namen.
    """
    bereinigt = (name or "").strip()
    if not bereinigt:
        raise DatenbankFehler(f"{art} ist nicht konfiguriert.")
    if len(bereinigt) > 64:
        raise DatenbankFehler(f"{art} „{bereinigt[:20]}…“ ist länger als 64 Zeichen.")
    if any(ord(zeichen) < 32 or zeichen in "`\x7f" for zeichen in bereinigt):
        raise DatenbankFehler(f"{art} „{bereinigt!r}“ enthält unzulässige Zeichen.")
    return "`" + bereinigt + "`"


def _zu_text(wert: Any) -> str:
    if wert is None:
        return ""
    if isinstance(wert, (bytes, bytearray)):
        return bytes(wert).decode("utf-8", errors="replace")
    return str(wert)


def _standard_verbinder(einstellungen: DatenbankEinstellungen) -> Any:
    if _mysql is None:
        raise DatenbankFehler(
            "Der MySQL-Treiber fehlt. Bitte „pip install mysql-connector-python“ ausführen.")
    verbindung = _mysql.connect(
        host=einstellungen.server.strip(),
        port=einstellungen.port,
        user=einstellungen.benutzer.strip(),
        password=einstellungen.passwort,
        database=einstellungen.datenbankname.strip(),
        connection_timeout=einstellungen.verbindungs_timeout_s,
        charset="utf8mb4",
        use_pure=True,
        ssl_disabled=not einstellungen.tls_verwenden,
        autocommit=False,
    )
    # Der Treiber fällt bei fehlendem Server-TLS stillschweigend auf eine unverschlüsselte
    # Verbindung zurück. Wurde Verschlüsselung verlangt, darf das nicht passieren.
    if einstellungen.tls_verwenden and not verbindung.is_secure:
        verbindung.close()
        raise DatenbankFehler(
            "Verschlüsselte Verbindung (TLS) ist aktiviert, der Datenbankserver bietet sie aber nicht an. "
            "Entweder TLS auf dem Server einrichten oder „TLS-Verschlüsselung verwenden“ in den "
            "Datenbank-Einstellungen abschalten (nur in vertrauenswürdigen Netzen).")
    return verbindung


class MySqlReferenzRepository(ReferenzRepository):
    """Referenz-Repository für MySQL und MariaDB."""

    def __init__(self, einstellungen: DatenbankEinstellungen,
                 verbinder: Callable[[DatenbankEinstellungen], Any] | None = None) -> None:
        self._e = einstellungen
        self._verbinder = verbinder or _standard_verbinder
        registriere_geheimnis(einstellungen.passwort)

    # -- Verbindung -----------------------------------------------------------

    def _pruefe_konfiguration(self) -> tuple[str, str, str]:
        fehlend = self._e.fehlende_angaben()
        if fehlend:
            raise DatenbankFehler(
                "Die Datenbankeinstellungen sind unvollständig: " + ", ".join(fehlend) + ".")
        return (quotiere_bezeichner(self._e.tabellenname, "Tabellenname"),
                quotiere_bezeichner(self._e.feld_symbol, "Feldname Symbol"),
                quotiere_bezeichner(self._e.feld_funktionstext, "Feldname Funktionstext"))

    @contextmanager
    def _verbindung(self) -> Iterator[Any]:
        verbindung = None
        try:
            _log.debug("Verbinde mit %s:%s, Datenbank %s, Benutzer %s",
                       self._e.server, self._e.port, self._e.datenbankname, self._e.benutzer)
            verbindung = self._verbinder(self._e)
            yield verbindung
        except DatenbankFehler:
            raise
        except _TreiberFehler as fehler:
            _log.error("Datenbankfehler: %s", fehler)
            raise DatenbankFehler(self._beschreibe(fehler)) from fehler
        finally:
            if verbindung is not None:
                try:
                    verbindung.close()
                except Exception:  # noqa: BLE001 – Schließen darf nie zusätzliche Fehler auslösen
                    pass

    def _beschreibe(self, fehler: BaseException) -> str:
        nummer = getattr(fehler, "errno", None)
        text = getattr(fehler, "msg", None) or str(fehler)
        ziel = f"{self._e.server}:{self._e.port}"
        return f"Datenbankfehler ({ziel}{', Nr. ' + str(nummer) if nummer else ''}): {text}"

    # -- Lesen ------------------------------------------------------------------

    def lade_referenzdaten(self, abbruch: threading.Event | None = None) -> list[tuple[str, str]]:
        tabelle, symbol, text = self._pruefe_konfiguration()
        paare: list[tuple[str, str]] = []
        with self._verbindung() as verbindung:
            cursor = verbindung.cursor()
            try:
                cursor.execute(f"SELECT {symbol}, {text} FROM {tabelle}")
                while True:
                    if abbruch is not None and abbruch.is_set():
                        raise VorgangAbgebrochenFehler("Der Vorgang wurde abgebrochen.")
                    block = cursor.fetchmany(_BLOCKGROESSE)
                    if not block:
                        break
                    paare.extend((_zu_text(s), _zu_text(t)) for s, t in block)
            finally:
                cursor.close()
        _log.info("Referenzdaten geladen: %d Zeilen aus %s.%s", len(paare),
                  self._e.datenbankname, self._e.tabellenname)
        return paare

    def pruefe_vorhandensein(self, symbol: str, funktionstext: str) -> Vorhandensein:
        tabelle, feld_symbol, feld_text = self._pruefe_konfiguration()
        with self._verbindung() as verbindung:
            cursor = verbindung.cursor()
            try:
                return self._vorhandensein(cursor, tabelle, feld_symbol, feld_text,
                                           symbol, funktionstext)
            finally:
                cursor.close()

    @staticmethod
    def _vorhandensein(cursor: Any, tabelle: str, feld_symbol: str, feld_text: str,
                       symbol: str, funktionstext: str) -> Vorhandensein:
        cursor.execute(f"SELECT COUNT(*) FROM {tabelle} WHERE {feld_symbol} = %s", (symbol,))
        hat_symbol = cursor.fetchone()[0] > 0
        cursor.execute(f"SELECT COUNT(*) FROM {tabelle} WHERE {feld_text} = %s", (funktionstext,))
        hat_text = cursor.fetchone()[0] > 0
        cursor.execute(
            f"SELECT COUNT(*) FROM {tabelle} WHERE {feld_symbol} = %s AND {feld_text} = %s",
            (symbol, funktionstext))
        hat_kombination = cursor.fetchone()[0] > 0
        return Vorhandensein(hat_symbol, hat_text, hat_kombination)

    # -- Schreiben ----------------------------------------------------------------

    def uebernehmen(self, paare: Sequence[tuple[str, str]],
                    konflikt_erlauben: bool = False) -> list[UebernahmeErgebnis]:
        tabelle, feld_symbol, feld_text = self._pruefe_konfiguration()
        ergebnisse: list[UebernahmeErgebnis] = []
        with self._verbindung() as verbindung:
            cursor = verbindung.cursor()
            try:
                for symbol, funktionstext in paare:
                    ergebnis = self._uebernimm_ein_paar(
                        verbindung, cursor, tabelle, feld_symbol, feld_text,
                        symbol.strip(), funktionstext.strip(), konflikt_erlauben)
                    ergebnisse.append(ergebnis)
                    _log.info("Übernahme in Datenbank: Symbol=%r Funktionstext=%r -> %s (%s)",
                              symbol, funktionstext,
                              "gespeichert" if ergebnis.uebernommen else "nicht gespeichert",
                              ergebnis.meldung)
            finally:
                cursor.close()
        return ergebnisse

    def _uebernimm_ein_paar(self, verbindung: Any, cursor: Any, tabelle: str, feld_symbol: str,
                            feld_text: str, symbol: str, funktionstext: str,
                            konflikt_erlauben: bool) -> UebernahmeErgebnis:
        if not symbol or not funktionstext:
            return UebernahmeErgebnis(
                symbol, funktionstext, False,
                "Symbol und Funktionstext müssen beide ausgefüllt sein.")
        try:
            vorhanden = self._vorhandensein(cursor, tabelle, feld_symbol, feld_text,
                                            symbol, funktionstext)
            if vorhanden.kombination:
                verbindung.rollback()
                return UebernahmeErgebnis(symbol, funktionstext, False,
                                          "Die Kombination ist bereits vorhanden.", vorhanden)
            if (vorhanden.symbol or vorhanden.funktionstext) and not konflikt_erlauben:
                verbindung.rollback()
                gruende = []
                if vorhanden.symbol:
                    gruende.append("Das Symbol existiert bereits (mit anderem Funktionstext)")
                if vorhanden.funktionstext:
                    gruende.append("Der Funktionstext existiert bereits (mit anderem Symbol)")
                return UebernahmeErgebnis(symbol, funktionstext, False,
                                          "; ".join(gruende) + ".", vorhanden,
                                          benoetigt_bestaetigung=True)

            spalten = [feld_symbol, feld_text]
            werte: list[Any] = [symbol, funktionstext]
            schluesselwert = self._naechster_schluessel(cursor, tabelle)
            if schluesselwert is not None:
                spalten.append(schluesselwert[0])
                werte.append(schluesselwert[1])
            platzhalter = ", ".join(["%s"] * len(werte))
            cursor.execute(
                f"INSERT INTO {tabelle} ({', '.join(spalten)}) VALUES ({platzhalter})", werte)
            verbindung.commit()
            return UebernahmeErgebnis(symbol, funktionstext, True,
                                      "In die Datenbank übernommen.", vorhanden)
        except DatenbankFehler as fehler:
            verbindung.rollback()
            return UebernahmeErgebnis(symbol, funktionstext, False, str(fehler))
        except _TreiberFehler as fehler:
            try:
                verbindung.rollback()
            except Exception:  # noqa: BLE001
                pass
            _log.error("Speichern fehlgeschlagen: %s", fehler)
            return UebernahmeErgebnis(symbol, funktionstext, False, self._beschreibe(fehler))

    def _naechster_schluessel(self, cursor: Any, tabelle: str) -> tuple[str, int] | None:
        """
        Ermittelt bei Bedarf einen Primärschlüsselwert.

        * kein/gleich Symbol- oder Textfeld oder AUTO_INCREMENT -> kein eigener Wert nötig
        * Ganzzahlschlüssel ohne AUTO_INCREMENT -> MAX()+1 (gesperrt bis zum Commit)
        * sonstiger Schlüsseltyp -> Fehler (kann nicht automatisch vergeben werden)
        """
        name = self._e.primaerschluessel.strip()
        if not name or name.casefold() in (self._e.feld_symbol.strip().casefold(),
                                           self._e.feld_funktionstext.strip().casefold()):
            return None
        quotiert = quotiere_bezeichner(name, "Primärschlüssel")
        cursor.execute(
            "SELECT EXTRA, DATA_TYPE FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s",
            (self._e.tabellenname.strip(), name))
        zeile = cursor.fetchone()
        if zeile is None:
            raise DatenbankFehler(f"Der Primärschlüssel „{name}“ existiert in der Tabelle nicht.")
        extra, datentyp = _zu_text(zeile[0]).lower(), _zu_text(zeile[1]).lower()
        if "auto_increment" in extra or "generated" in extra:
            return None
        if datentyp not in _GANZZAHLTYPEN:
            raise DatenbankFehler(
                f"Der Primärschlüssel „{name}“ ({datentyp}) hat kein AUTO_INCREMENT; "
                "ein Wert kann nicht automatisch vergeben werden.")
        cursor.execute(f"SELECT COALESCE(MAX({quotiert}), 0) + 1 FROM {tabelle} FOR UPDATE")
        return quotiert, int(cursor.fetchone()[0])

    # -- Verbindungstest ------------------------------------------------------------

    def teste_verbindung(self) -> Verbindungstest:
        meldungen: list[str] = []
        try:
            tabelle, feld_symbol, feld_text = self._pruefe_konfiguration()
            with self._verbindung() as verbindung:
                cursor = verbindung.cursor()
                try:
                    cursor.execute("SELECT VERSION()")
                    meldungen.append(f"Verbindung hergestellt (Server {_zu_text(cursor.fetchone()[0])}).")
                    cursor.execute(
                        "SELECT COLUMN_NAME, DATA_TYPE, COLUMN_KEY, EXTRA FROM information_schema.COLUMNS "
                        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
                        (self._e.tabellenname.strip(),))
                    spalten = {_zu_text(z[0]).casefold(): z for z in cursor.fetchall()}
                    if not spalten:
                        return Verbindungstest(False, (*meldungen,
                            f"Die Tabelle „{self._e.tabellenname}“ existiert nicht oder ist nicht sichtbar.",))
                    meldungen.append(f"Tabelle „{self._e.tabellenname}“ gefunden.")
                    ok = True
                    angaben = [("Feld Symbol", self._e.feld_symbol),
                               ("Feld Funktionstext", self._e.feld_funktionstext)]
                    if self._e.primaerschluessel.strip():
                        angaben.append(("Primärschlüssel", self._e.primaerschluessel))
                    for bezeichnung, feldname in angaben:
                        if feldname.strip().casefold() in spalten:
                            meldungen.append(f"{bezeichnung} „{feldname}“ vorhanden.")
                        else:
                            ok = False
                            meldungen.append(f"{bezeichnung} „{feldname}“ existiert NICHT in der Tabelle.")
                    if not ok:
                        return Verbindungstest(False, tuple(meldungen))
                    cursor.execute(f"SELECT COUNT(*) FROM {tabelle}")
                    meldungen.append(f"Lesezugriff möglich ({cursor.fetchone()[0]} Datensätze in der Referenz).")
                    pk = spalten.get(self._e.primaerschluessel.strip().casefold())
                    if pk is not None and self._e.primaerschluessel.strip().casefold() not in (
                            feld_symbol.strip("`").casefold(), feld_text.strip("`").casefold()):
                        if "auto_increment" not in _zu_text(pk[3]).lower() \
                                and _zu_text(pk[1]).lower() not in _GANZZAHLTYPEN:
                            meldungen.append(
                                "WARNUNG: Der Primärschlüssel hat weder AUTO_INCREMENT noch einen "
                                "Ganzzahltyp – „In Datenbank übernehmen“ wird nicht möglich sein.")
                    return Verbindungstest(True, tuple(meldungen))
                finally:
                    cursor.close()
        except DatenbankFehler as fehler:
            meldungen.append(str(fehler))
            return Verbindungstest(False, tuple(meldungen))


def erzeuge_repository(einstellungen: DatenbankEinstellungen) -> ReferenzRepository:
    """Fabrikfunktion; hier wird bei weiteren Datenbanktypen nach Einstellung verzweigt."""
    return MySqlReferenzRepository(einstellungen)
