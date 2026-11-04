"""
eplan_parser.py – Einlesen von EPLAN-Dateien.

Hintergrund
-----------
*.elk (Projekt) und *.zw1 (Projektsicherung) sind proprietäre Formate ohne
öffentliche Spezifikation. Dieses Modul geht deshalb in zwei getrennten Stufen vor:

1. **Container** (:class:`ContainerLeser`): Die Datei wird – unabhängig von der
   Endung – nach ihrem tatsächlichen Aufbau geöffnet: ZIP (auch hinter einem
   Vorspann), GZIP, verschachtelte Archive, Verzeichnisse (``*.edb`` neben einer
   ``*.elk``). Ergebnis ist eine Folge von Teildateien.
2. **Extraktoren** (:class:`DatensatzExtraktor`): Jede Teildatei wird an den
   ersten Extraktor gegeben, der ihren Inhalt erkennt (XML, CSV/TSV, XLSX,
   SQLite). Welche Bezeichnungen (Tags, Attribute, Spaltenüberschriften) zu
   welchem Feld gehören, steht in der konfigurierbaren *Feldzuordnung*.

Neue Formate (andere EPLAN-Versionen, neue Exporte) werden durch einen weiteren
Extraktor ergänzt, ohne bestehenden Code zu ändern (Offen/Geschlossen-Prinzip).

Ein natives, binäres EPLAN-Format, das keiner dieser Stufen entspricht, wird
*nicht geraten*: Der Leser meldet dann ein verständliches Ergebnis samt
Strukturbericht (siehe :mod:`dateianalyse`).

Sicherheit: Dateien gelten als nicht vertrauenswürdig. Es gibt Grenzen für
Anzahl und Größe entpackter Dateien (Schutz vor Zip-Bomben), XML wird ohne
Entitätsauflösung und ohne Netzwerkzugriff gelesen.
"""

from __future__ import annotations

import codecs
import csv
import dataclasses
import gzip
import io
import re
import sqlite3
import threading
import unicodedata
import zipfile
import zlib
from abc import ABC, abstractmethod
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Callable, Iterable, Iterator, Mapping, Sequence

from lxml import etree

from .dateianalyse import DateiAnalysator, beschreibe_kopf, dekodiere_text
from .logging import hole_protokollierer
from .modelle import (
    FELDNAMEN,
    EplanDatensatz,
    EplanDateiFehler,
    EplanSeite,
    Einleseergebnis,
    Einstellungen,
    Fundstelle,
    Quellauszug,
    TeildateiBericht,
    VorgangAbgebrochenFehler,
    leite_seitennummer_ab,
)

_log = hole_protokollierer("parser")

# Grenzen gegen manipulierte oder versehentlich riesige Dateien
MAX_TEILDATEI_BYTES = 1024 * 1024 * 1024        # 1 GiB je entpackter Datei
MAX_GESAMT_BYTES = 4 * 1024 * 1024 * 1024       # 4 GiB insgesamt
MAX_EINTRAEGE = 100_000                         # Dateien je Einlesevorgang
MAX_VERSCHACHTELUNG = 3                         # Archive in Archiven
MAX_SPEICHER_TEILDATEI = 512 * 1024 * 1024      # Dateien, die komplett in den Speicher müssen
KOPF_BYTES = 4096
MAX_KOPFZEILEN_SUCHE = 30
_OFFICE_ENDUNGEN = (".xlsx", ".xlsm", ".docx", ".docm", ".pptx", ".pptm", ".odt", ".ods", ".odp")

FortschrittsMelder = Callable[[str, int, int], None]   # (Meldung, aktuell, gesamt; gesamt 0 = unbekannt)

_LESEFEHLER = (OSError, EOFError, zipfile.BadZipFile, NotImplementedError,
               RuntimeError, zlib.error, gzip.BadGzipFile, etree.XMLSyntaxError,
               sqlite3.DatabaseError, UnicodeError, csv.Error)


def normalisiere_name(text: str) -> str:
    """Vergleichsform einer Bezeichnung: klein, ohne Leer-/Sonderzeichen (``SPS-Adresse`` -> ``spsadresse``)."""
    text = unicodedata.normalize("NFKC", text or "").casefold()
    return "".join(zeichen for zeichen in text if zeichen.isalnum())


# ---------------------------------------------------------------------------
# Feldzuordnung
# ---------------------------------------------------------------------------

class Feldzuordner:
    """Ordnet Bezeichnungen (Spaltenüberschriften, Tags, Attribute) den logischen Feldern zu."""

    def __init__(self, zuordnung: Mapping[str, Iterable[str]]) -> None:
        self._alias_zu_feld: dict[str, str] = {}
        for feld in FELDNAMEN:                      # frühere Felder haben bei Doppelungen Vorrang
            for alias in zuordnung.get(feld, ()):
                schluessel = normalisiere_name(alias)
                if schluessel and schluessel not in self._alias_zu_feld:
                    self._alias_zu_feld[schluessel] = feld

    def feld_fuer(self, bezeichnung: str) -> str | None:
        return self._alias_zu_feld.get(normalisiere_name(bezeichnung))

    def spalten(self, kopfzeile: Sequence[object]) -> dict[str, int]:
        """Index der ersten passenden Spalte je Feld."""
        ergebnis: dict[str, int] = {}
        for index, zelle in enumerate(kopfzeile):
            feld = self.feld_fuer(str(zelle)) if zelle is not None else None
            if feld and feld not in ergebnis:
                ergebnis[feld] = index
        return ergebnis

    @staticmethod
    def ist_brauchbar(spalten: Mapping[str, int]) -> bool:
        """Eine Kopfzeile ist brauchbar, wenn SPS-Adresse oder Symbol und mindestens ein weiteres Feld vorkommt."""
        return len(spalten) >= 2 and ("sps_adresse" in spalten or "symbolische_adresse" in spalten)


# ---------------------------------------------------------------------------
# Container
# ---------------------------------------------------------------------------

class _Mengenbudget:
    """Gemeinsames Limit für alle entpackten Bytes eines Einlesevorgangs."""

    def __init__(self, limit: int) -> None:
        self.verbleibend = limit

    def verbrauche(self, anzahl: int) -> None:
        self.verbleibend -= anzahl
        if self.verbleibend < 0:
            raise EplanDateiFehler(
                "Die entpackten Daten überschreiten die zulässige Gesamtgröße "
                f"({MAX_GESAMT_BYTES // 2**30} GiB). Datei ist beschädigt oder manipuliert.")


class _BegrenzterStrom:
    """Dünner Lese-Wrapper, der Größenlimits durchsetzt (Schutz vor Zip-Bomben)."""

    def __init__(self, strom: BinaryIO, budget: _Mengenbudget, limit: int) -> None:
        self._strom = strom
        self._budget = budget
        self._rest = limit

    def read(self, groesse: int = -1) -> bytes:
        if groesse is None or groesse < 0 or groesse > self._rest + 1:
            groesse = self._rest + 1
        daten = self._strom.read(groesse)
        if len(daten) > self._rest:
            raise EplanDateiFehler(
                f"Eine Teildatei überschreitet die zulässige Größe von {MAX_TEILDATEI_BYTES // 2**20} MiB.")
        self._rest -= len(daten)
        self._budget.verbrauche(len(daten))
        return daten

    def close(self) -> None:
        self._strom.close()

    def __enter__(self) -> "_BegrenzterStrom":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


@dataclass(slots=True)
class Teildatei:
    """Eine einzelne Datei innerhalb des (ggf. verschachtelten) Containers."""

    name: str
    groesse: int                                # -1 = unbekannt
    _oeffner: Callable[[], BinaryIO] = field(repr=False)
    _budget: _Mengenbudget = field(repr=False)

    def oeffne(self) -> _BegrenzterStrom:
        return _BegrenzterStrom(self._oeffner(), self._budget, MAX_TEILDATEI_BYTES)

    def lese_kopf(self, anzahl: int = KOPF_BYTES) -> bytes:
        with self._oeffner() as strom:
            return strom.read(anzahl)

    def lese_alles(self, limit: int = MAX_SPEICHER_TEILDATEI) -> bytes:
        with self.oeffne() as strom:
            daten = strom.read()
        if len(daten) > limit:
            raise EplanDateiFehler(f"Die Teildatei „{self.name}“ ist für die Verarbeitung zu groß.")
        return daten

    @property
    def endung(self) -> str:
        return Path(self.name.replace("!", "/")).suffix.lower()


class ContainerLeser:
    """Zerlegt Dateien und Verzeichnisse in ihre Teildateien (siehe Moduldokumentation)."""

    def __init__(self, abbruch: threading.Event | None = None) -> None:
        self._abbruch = abbruch
        self._budget = _Mengenbudget(MAX_GESAMT_BYTES)
        self._eintraege = 0
        #: (Name, Grund) für Einträge, die nicht gelesen werden konnten (verschlüsselt, defekt …)
        self.uebersprungen: list[tuple[str, str]] = []
        #: Namen von Dateien, auf die der Zugriff verweigert wurde (z. B. von EPLAN gesperrt)
        self.gesperrt: list[str] = []
        #: Anzahl der übergangenen Sperrdateien (*.lck); sie enthalten keine Projektdaten
        self.sperrdateien = 0

    def teildateien(self, pfad: Path) -> Iterator[Teildatei]:
        if pfad.is_dir():
            for datei in sorted(p for p in pfad.rglob("*") if p.is_file() and not p.is_symlink()):
                if datei.suffix.lower() == ".lck":
                    self.sperrdateien += 1
                    continue
                name = f"{pfad.name}/{datei.relative_to(pfad).as_posix()}"
                yield from self._untersuche(name, _datei_oeffner(datei), datei.stat().st_size, 0, True)
        else:
            yield from self._untersuche(pfad.name, _datei_oeffner(pfad), pfad.stat().st_size, 0, True)

    # -- intern -----------------------------------------------------------------

    def _pruefe(self) -> None:
        if self._abbruch is not None and self._abbruch.is_set():
            raise VorgangAbgebrochenFehler("Der Vorgang wurde abgebrochen.")
        self._eintraege += 1
        if self._eintraege > MAX_EINTRAEGE:
            raise EplanDateiFehler(f"Der Container enthält mehr als {MAX_EINTRAEGE} Dateien.")

    def _untersuche(self, name: str, oeffner: Callable[[], BinaryIO], groesse: int,
                    tiefe: int, ist_datei_auf_platte: bool) -> Iterator[Teildatei]:
        self._pruefe()
        try:
            with oeffner() as strom:
                kopf = strom.read(KOPF_BYTES)
        except PermissionError:
            self.gesperrt.append(name)
            return
        except _LESEFEHLER as fehler:
            self.uebersprungen.append((name, f"nicht lesbar: {fehler}"))
            return

        # GZIP: entpacken und Inhalt erneut untersuchen
        if kopf.startswith(b"\x1f\x8b") and tiefe < MAX_VERSCHACHTELUNG:
            innen = name[:-3] if name.lower().endswith(".gz") else name + "~entpackt"
            yield from self._untersuche(
                innen, lambda o=oeffner: gzip.GzipFile(fileobj=o()), -1, tiefe + 1, False)
            return

        # Office-Dokumente sind ZIPs, werden aber nicht zerlegt: Arbeitsmappen wertet der
        # XLSX-Extraktor als Ganzes aus, Word-/PowerPoint-Dateien enthalten keine SPS-Daten.
        if name.lower().endswith(_OFFICE_ENDUNGEN) and kopf.startswith(b"PK"):
            yield Teildatei(name, groesse, oeffner, self._budget)
            return

        # ZIP: bei Dateien auf der Platte auch mit fremdem Vorspann (Endverzeichnis am Dateiende)
        if (kopf.startswith(b"PK") or ist_datei_auf_platte) and tiefe < MAX_VERSCHACHTELUNG:
            archiv = self._oeffne_zip(oeffner, groesse)
            if archiv is not None:
                yield from self._durchlaufe_zip(name, archiv, tiefe)
                return

        yield Teildatei(name, groesse, oeffner, self._budget)

    def _oeffne_zip(self, oeffner: Callable[[], BinaryIO], groesse: int) -> zipfile.ZipFile | None:
        try:
            strom = oeffner()
        except _LESEFEHLER:
            return None
        try:
            if not strom.seekable():
                daten = strom.read(MAX_SPEICHER_TEILDATEI + 1)
                strom.close()
                if len(daten) > MAX_SPEICHER_TEILDATEI:
                    raise EplanDateiFehler("Ein verschachteltes Archiv ist zu groß.")
                self._budget.verbrauche(len(daten))
                strom = io.BytesIO(daten)
            return zipfile.ZipFile(strom)
        except (zipfile.BadZipFile, NotImplementedError, OSError, ValueError):
            strom.close()
            return None

    def _durchlaufe_zip(self, name: str, archiv: zipfile.ZipFile, tiefe: int) -> Iterator[Teildatei]:
        with archiv:
            eintraege = [e for e in archiv.infolist() if not e.is_dir()]
            for eintrag in eintraege:
                if eintrag.flag_bits & 0x1:
                    self.uebersprungen.append((f"{name}!{eintrag.filename}", "verschlüsselt"))
                    continue
                if eintrag.file_size > MAX_TEILDATEI_BYTES:
                    self.uebersprungen.append((f"{name}!{eintrag.filename}", "zu groß"))
                    continue
                yield from self._untersuche(
                    f"{name}!{eintrag.filename}",
                    lambda a=archiv, e=eintrag: a.open(e), eintrag.file_size, tiefe + 1, False)


def _datei_oeffner(pfad: Path) -> Callable[[], BinaryIO]:
    return lambda: open(pfad, "rb")


# ---------------------------------------------------------------------------
# Lesekontext und Extraktor-Schnittstelle
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class LeseKontext:
    """Gemeinsame Angaben für alle Extraktoren eines Einlesevorgangs."""

    zuordner: Feldzuordner
    bevorzugte_sprache: str
    hauptdatei: str
    abbruch: threading.Event | None = None
    warnungen: list[str] = field(default_factory=list)
    #: Strukturbeschreibungen von Dateien, die erkannt, aber nicht ausgewertet werden konnten
    diagnose: list[str] = field(default_factory=list)

    def pruefe_abbruch(self) -> None:
        if self.abbruch is not None and self.abbruch.is_set():
            raise VorgangAbgebrochenFehler("Der Vorgang wurde abgebrochen.")

    def fundstelle(self, teildatei: str, zeile: int | None, element: str = "") -> Fundstelle:
        return Fundstelle(datei=self.hauptdatei, teildatei=teildatei, zeile=zeile, element=element)


def _bereinige(wert: object) -> str:
    if wert is None:
        return ""
    if isinstance(wert, float) and wert.is_integer():
        wert = int(wert)
    return str(wert).replace("\x00", "").strip()


#: Ein „sichtbares BMK" der Form ``=U4+E2/2.2`` ist in EPLAN eine Fundstelle (Seite 2, Spalte 2).
_SEITENVERWEIS = re.compile(r"^(?P<referenz>[=+][^/]*/(?P<nummer>\d+[A-Za-z]?))(?:\.\d+)?$")


def erzeuge_datensatz(felder: Mapping[str, str], fundstelle: Fundstelle) -> EplanDatensatz | None:
    """
    Baut einen Datensatz aus den gelesenen Feldern. Liefert ``None``, wenn weder
    SPS-Adresse noch symbolische Adresse vorhanden sind (kein SPS-Anschluss).
    """
    werte = {name: _bereinige(felder.get(name, "")) for name in FELDNAMEN}
    if not werte["sps_adresse"] and not werte["symbolische_adresse"]:
        return None
    if not werte["seitenreferenz"] and not werte["seitennummer"]:
        verweis = _SEITENVERWEIS.match(werte["betriebsmittelkennzeichen"])
        if verweis:                        # Quelle ohne Seitenspalte: Seite aus der Fundstelle im BMK
            werte["seitenreferenz"] = verweis.group("referenz")
            werte["seitennummer"] = verweis.group("nummer")
    if not werte["seitennummer"]:
        werte["seitennummer"] = leite_seitennummer_ab(werte["seitenreferenz"])
    return EplanDatensatz(fundstelle=fundstelle, **werte)


class DatensatzExtraktor(ABC):
    """Strategie: erkennt ein Datenformat und liest daraus Datensätze."""

    art: str = "unbekannt"

    @abstractmethod
    def unterstuetzt(self, name: str, kopf: bytes) -> bool:
        """Erkennt das Format anhand von Name und den ersten Bytes."""

    @abstractmethod
    def extrahiere(self, teildatei: Teildatei, kontext: LeseKontext) -> Iterator[EplanDatensatz]:
        """Liefert alle Datensätze der Teildatei. Fehler: :class:`EplanDateiFehler`."""


# ---------------------------------------------------------------------------
# Tabellen (gemeinsame Logik für CSV, XLSX, SQLite)
# ---------------------------------------------------------------------------

def _tabellenzeilen_zu_datensaetzen(
        zeilen: Iterable[tuple[int, Sequence[object]]], teildatei: str, element: str,
        kontext: LeseKontext) -> Iterator[EplanDatensatz]:
    """
    Sucht in den ersten Zeilen die Kopfzeile und wandelt alle folgenden Zeilen in Datensätze um.
    ``zeilen`` liefert (Zeilennummer, Zellen).
    """
    iterator = iter(zeilen)
    gesehen: list[tuple[int, Sequence[object]]] = []
    spalten: dict[str, int] | None = None
    for eintrag in iterator:
        gesehen.append(eintrag)
        kandidat = kontext.zuordner.spalten(eintrag[1])
        if kontext.zuordner.ist_brauchbar(kandidat):
            spalten = kandidat
            break
        if len(gesehen) >= MAX_KOPFZEILEN_SUCHE:
            break
    if spalten is None:
        return

    for zaehler, (zeilennummer, zellen) in enumerate(iterator):
        if zaehler % 2000 == 0:
            kontext.pruefe_abbruch()
        felder = {feld: _bereinige(zellen[index]) if index < len(zellen) else ""
                  for feld, index in spalten.items()}
        datensatz = erzeuge_datensatz(felder, kontext.fundstelle(teildatei, zeilennummer, element))
        if datensatz is not None:
            yield datensatz


_TEXT_ENDUNGEN = {".csv", ".tsv", ".txt", ".lst", ".dat", ".prn"}


_dekodiere_text = dekodiere_text


def _erkenne_trennzeichen(text: str) -> str:
    zeilen = [z for z in text.splitlines()[:20] if z.strip()]
    beste, beste_summe = ";", 0
    for kandidat in (";", "\t", ",", "|"):
        anzahlen = [z.count(kandidat) for z in zeilen]
        if anzahlen and min(anzahlen) > 0:
            summe = sum(anzahlen)
            if summe > beste_summe:
                beste, beste_summe = kandidat, summe
    return beste


def _ohne_anfuehrung(zelle: str) -> str:
    zelle = zelle.strip()
    if len(zelle) >= 2 and zelle[0] == zelle[-1] == '"':
        return zelle[1:-1].replace('""', '"')
    return zelle


def _tabulatorzeilen(text: str, zuordner: Feldzuordner) -> list[tuple[int, list[str]]]:
    """
    Zerlegt tabulatorgetrennten Text (z. B. EPLAN-Exportlisten) in Zellen.

    Solche Listen verwenden oft mehrere Tabulatoren zur optischen Ausrichtung. Zuerst wird
    exakt an jedem Tabulator getrennt (Leerspalten bleiben erhalten). Passen die Datenzeilen
    dazu nicht – Werte stehen in Spalten, die in der Kopfzeile leer sind –, werden aufeinander-
    folgende Tabulatoren als ein Trenner behandelt.
    """
    zeilen = [(nummer, zeile) for nummer, zeile in enumerate(re.split(r"\r?\n", text), start=1)
              if zeile.strip()]
    exakt = [(n, [_ohne_anfuehrung(z) for z in zeile.split("\t")]) for n, zeile in zeilen]
    if _exakte_trennung_passt(exakt, zuordner):
        return exakt
    return [(n, [_ohne_anfuehrung(z) for z in re.split(r"\t+", zeile.rstrip("\t"))])
            for n, zeile in zeilen]


def _exakte_trennung_passt(zeilen: list[tuple[int, list[str]]], zuordner: Feldzuordner) -> bool:
    for index, (_, zellen) in enumerate(zeilen[:MAX_KOPFZEILEN_SUCHE]):
        if not zuordner.ist_brauchbar(zuordner.spalten(zellen)):
            continue
        leere = {i for i, zelle in enumerate(zellen) if not zelle.strip()}
        if not leere:
            return True
        probe = [z for _, z in zeilen[index + 1:index + 301]]
        verletzt = sum(1 for z in probe if any(i < len(z) and z[i].strip() for i in leere))
        return verletzt <= 0.1 * len(probe)
    return False


class CsvExtraktor(DatensatzExtraktor):
    """Textlisten (CSV/TSV), wie sie EPLAN-Berichte und -Exporte erzeugen."""

    art = "CSV/Text"

    def unterstuetzt(self, name: str, kopf: bytes) -> bool:
        endung = Path(name.replace("!", "/")).suffix.lower()
        if endung not in _TEXT_ENDUNGEN:
            return False
        if kopf.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
            return True
        return b"\x00" not in kopf

    def extrahiere(self, teildatei: Teildatei, kontext: LeseKontext) -> Iterator[EplanDatensatz]:
        text = _dekodiere_text(teildatei.lese_alles())
        trennzeichen = _erkenne_trennzeichen(text)
        if trennzeichen == "\t":
            yield from _tabellenzeilen_zu_datensaetzen(
                _tabulatorzeilen(text, kontext.zuordner), teildatei.name, "", kontext)
            return
        leser = csv.reader(io.StringIO(text, newline=""), delimiter=trennzeichen, quotechar='"')

        def zeilen() -> Iterator[tuple[int, Sequence[object]]]:
            letzte = 0
            for zellen in leser:
                start = letzte + 1
                letzte = leser.line_num
                if any(z.strip() for z in zellen):
                    yield start, zellen

        yield from _tabellenzeilen_zu_datensaetzen(zeilen(), teildatei.name, "", kontext)


class XlsxExtraktor(DatensatzExtraktor):
    """Excel-Arbeitsmappen (.xlsx) mit Listen der SPS-Anschlüsse."""

    art = "XLSX"

    def unterstuetzt(self, name: str, kopf: bytes) -> bool:
        return kopf.startswith(b"PK") and Path(name.replace("!", "/")).suffix.lower() in (".xlsx", ".xlsm")

    def extrahiere(self, teildatei: Teildatei, kontext: LeseKontext) -> Iterator[EplanDatensatz]:
        from openpyxl import load_workbook
        from openpyxl.utils.exceptions import InvalidFileException

        try:
            mappe = load_workbook(io.BytesIO(teildatei.lese_alles()), read_only=True, data_only=True)
        except (InvalidFileException, KeyError, zipfile.BadZipFile, ValueError) as fehler:
            raise EplanDateiFehler(f"Arbeitsmappe nicht lesbar: {fehler}") from fehler
        try:
            for blatt in mappe.worksheets:
                zeilen = ((nummer, zellen) for nummer, zellen in
                          enumerate(blatt.iter_rows(values_only=True), start=1)
                          if any(z not in (None, "") for z in zellen))
                yield from _tabellenzeilen_zu_datensaetzen(
                    zeilen, teildatei.name, f"Blatt {blatt.title}", kontext)
        finally:
            mappe.close()


def _sql_name(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


class SqliteExtraktor(DatensatzExtraktor):
    """SQLite-Datenbanken (Tabellen mit passenden Spalten werden automatisch erkannt)."""

    art = "SQLite"

    def unterstuetzt(self, name: str, kopf: bytes) -> bool:
        return kopf.startswith(b"SQLite format 3\x00")

    def extrahiere(self, teildatei: Teildatei, kontext: LeseKontext) -> Iterator[EplanDatensatz]:
        daten = teildatei.lese_alles()
        verbindung = sqlite3.connect(":memory:")
        try:
            verbindung.deserialize(daten)
            tabellen = [r[0] for r in verbindung.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view') "
                "AND name NOT LIKE 'sqlite_%'")]
            for tabelle in tabellen:
                kontext.pruefe_abbruch()
                spaltennamen = [r[1] for r in verbindung.execute(f"PRAGMA table_info({_sql_name(tabelle)})")]
                if not kontext.zuordner.ist_brauchbar(kontext.zuordner.spalten(spaltennamen)):
                    continue
                abfrage = f"SELECT {', '.join(_sql_name(s) for s in spaltennamen)} FROM {_sql_name(tabelle)}"
                zeilen = ((nummer, zeile) for nummer, zeile in enumerate(verbindung.execute(abfrage), start=1))
                kopfzeile = ((0, spaltennamen),)
                yield from _tabellenzeilen_zu_datensaetzen(
                    _verkette(kopfzeile, zeilen), teildatei.name, f"Tabelle {tabelle}", kontext)
        except sqlite3.DatabaseError as fehler:
            raise EplanDateiFehler(f"SQLite-Datenbank nicht lesbar: {fehler}") from fehler
        finally:
            verbindung.close()


def _verkette(*teile: Iterable[tuple[int, Sequence[object]]]) -> Iterator[tuple[int, Sequence[object]]]:
    for teil in teile:
        yield from teil


# ---------------------------------------------------------------------------
# XML
# ---------------------------------------------------------------------------

_SEITEN_TAGS = frozenset({"seite", "page", "blatt", "sheet"})
_SEITEN_NUMMER_NAMEN = frozenset({"nummer", "nr", "number", "no", "id"})
_SEITEN_NAME_NAMEN = frozenset({"name", "titel", "title", "bezeichnung", "beschreibung"})
_KENNUNG_ATTRIBUTE = frozenset({"name", "id", "key", "bezeichnung", "property", "eigenschaft"})
_WERT_ATTRIBUTE = ("wert", "value", "val", "text")
_SPRACH_ATTRIBUTE = frozenset({"sprache", "lang", "language", "l", "lcid"})


class _Knoten:
    """Zustand eines geöffneten XML-Elements während des Stream-Parsens."""

    __slots__ = ("tag", "felder", "hat_kinder", "kindtexte")

    def __init__(self, tag: str, felder: dict[str, str]) -> None:
        self.tag = tag                                  # normalisierter lokaler Tagname
        self.felder = felder                            # bisher bekannte Feldwerte
        self.hat_kinder = False
        self.kindtexte: list[tuple[str, str]] = []      # (Sprache, Text) mehrsprachiger Werte


def _lokaler_name(name: str) -> str:
    return name.rsplit("}", 1)[-1] if "}" in name else name


def _dekodiere_kopf(kopf: bytes) -> str:
    if kopf.startswith(codecs.BOM_UTF8):
        return kopf[3:].decode("utf-8", errors="replace").lstrip()
    if kopf.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return kopf.decode("utf-16", errors="replace").lstrip()
    return kopf.decode("utf-8", errors="replace").lstrip()


class XmlExtraktor(DatensatzExtraktor):
    """
    Allgemeiner XML-Extraktor (Stream-Parser, geringer Speicherbedarf).

    Ein Element gilt als **Datensatz**, wenn es (über Attribute oder direkte
    Kindelemente) eine SPS-Adresse oder eine symbolische Adresse liefert.
    Felder werden erkannt über

    * den Tagnamen eines Kindelements (``<Funktionstext>…</Funktionstext>``),
    * Attribute (``<Funktion Symbol="…" Adresse="…"/>``),
    * Name/Wert-Paare (``<Eigenschaft Name="SPS-Adresse" Wert="E0.0"/>``),
    * mehrsprachige Werte (``<Funktionstext><Text sprache="de_DE">…</Text></Funktionstext>``).

    Seiteninformationen werden aus umschließenden Elementen übernommen
    (z. B. ``<Seite Nummer="12" Name="Antriebe">``).
    """

    art = "XML"

    def unterstuetzt(self, name: str, kopf: bytes) -> bool:
        text = _dekodiere_kopf(kopf)
        return len(text) > 1 and text[0] == "<" and (text[1].isalpha() or text[1] in "?!_")

    def extrahiere(self, teildatei: Teildatei, kontext: LeseKontext) -> Iterator[EplanDatensatz]:
        zuordner = kontext.zuordner
        sprache = normalisiere_name(kontext.bevorzugte_sprache)
        stapel: list[_Knoten] = []

        try:
            with teildatei.oeffne() as strom:
                ereignisse = etree.iterparse(
                    strom, events=("start", "end"), resolve_entities=False, no_network=True,
                    load_dtd=False, huge_tree=False, remove_comments=True,
                    remove_pis=True)
                for zaehler, (ereignis, element) in enumerate(ereignisse):
                    if zaehler % 5000 == 0:
                        kontext.pruefe_abbruch()
                    if ereignis == "start":
                        self._beginne(element, stapel, zuordner)
                    else:
                        datensatz = self._beende(element, stapel, zuordner, sprache,
                                                 teildatei.name, kontext)
                        if datensatz is not None:
                            yield datensatz
        except etree.XMLSyntaxError as fehler:
            raise EplanDateiFehler(f"XML-Fehler in „{teildatei.name}“: {fehler}") from fehler

    # -- Start-Ereignis -----------------------------------------------------------

    @staticmethod
    def _beginne(element: "etree._Element", stapel: list[_Knoten], zuordner: Feldzuordner) -> None:
        tag = normalisiere_name(_lokaler_name(element.tag))
        felder: dict[str, str] = {}
        if stapel:
            stapel[-1].hat_kinder = True
        for attribut, wert in element.attrib.items():
            name = _lokaler_name(attribut)
            feld = zuordner.feld_fuer(name)
            if feld is None and tag in _SEITEN_TAGS:
                kurz = normalisiere_name(name)
                if kurz in _SEITEN_NUMMER_NAMEN:
                    feld = "seitennummer"
                elif kurz in _SEITEN_NAME_NAMEN:
                    feld = "seitenname"
            if feld is not None and wert.strip() and feld not in felder:
                felder[feld] = wert.strip()
        stapel.append(_Knoten(tag, felder))

    # -- Ende-Ereignis ---------------------------------------------------------------

    def _beende(self, element: "etree._Element", stapel: list[_Knoten], zuordner: Feldzuordner,
                sprache: str, teildatei: str, kontext: LeseKontext) -> EplanDatensatz | None:
        knoten = stapel.pop()
        eltern = stapel[-1] if stapel else None
        text = (element.text or "").strip()
        zeile = element.sourceline
        lokaler_tag = _lokaler_name(element.tag)
        datensatz: EplanDatensatz | None = None

        # 1. Ist das Element selbst ein Feldwert des Elternelements?
        feld, wert = self._als_feld(element, knoten, zuordner, text, sprache)
        if feld is not None and eltern is not None:
            if wert and feld not in eltern.felder:
                eltern.felder[feld] = wert
        else:
            # 2. Reiner Textknoten (Sprachvariante / Seiten-Unterelement)?
            if eltern is not None and not knoten.hat_kinder and text:
                sprachwert = next((v.strip() for k, v in element.attrib.items()
                                   if normalisiere_name(_lokaler_name(k)) in _SPRACH_ATTRIBUTE), "")
                if sprachwert:
                    eltern.kindtexte.append((normalisiere_name(sprachwert), text))
                elif eltern.tag in _SEITEN_TAGS:
                    kurz = knoten.tag
                    if kurz in _SEITEN_NUMMER_NAMEN:
                        eltern.felder.setdefault("seitennummer", text)
                    elif kurz in _SEITEN_NAME_NAMEN:
                        eltern.felder.setdefault("seitenname", text)
            # 3. Container: Datensatz?
            if knoten.felder.get("sps_adresse") or knoten.felder.get("symbolische_adresse"):
                datensatz = self._baue_datensatz(knoten, stapel, teildatei, zeile, lokaler_tag, kontext)

        # Speicher freigeben: Das Element wurde vollständig verarbeitet.
        element.clear()
        vater = element.getparent()
        if vater is not None:
            while element.getprevious() is not None:
                del vater[0]
        return datensatz

    @staticmethod
    def _als_feld(element: "etree._Element", knoten: _Knoten, zuordner: Feldzuordner,
                  text: str, sprache: str) -> tuple[str | None, str]:
        """Prüft, ob ``element`` ein Feldwert ist, und liefert (Feld, Wert)."""
        feld = zuordner.feld_fuer(_lokaler_name(element.tag))
        wert_attribut = ""
        if feld is None:
            for attribut, inhalt in element.attrib.items():
                if normalisiere_name(_lokaler_name(attribut)) in _KENNUNG_ATTRIBUTE:
                    kandidat = zuordner.feld_fuer(inhalt)
                    if kandidat is not None:
                        feld = kandidat
                        break
        if feld is None:
            return None, ""

        # Name/Wert-Paar: Wert steht in einem Attribut
        for attribut, inhalt in element.attrib.items():
            if normalisiere_name(_lokaler_name(attribut)) in _WERT_ATTRIBUTE and inhalt.strip():
                wert_attribut = inhalt.strip()
                break

        if not knoten.hat_kinder:
            wert = text or wert_attribut
            if wert:
                return feld, wert
            # Leeres Element mit Feldattributen (z. B. <Seite Nummer="3"/>) ist ein Container.
            return (None, "") if knoten.felder else (feld, "")
        if knoten.kindtexte:
            # mehrsprachiger Wert: bevorzugte Sprache, sonst erster Eintrag
            for kennung, inhalt in knoten.kindtexte:
                if sprache and (kennung == sprache or kennung.startswith(sprache[:2])
                                and sprache[:2] == kennung[:2]):
                    return feld, inhalt
            return feld, knoten.kindtexte[0][1]
        return None, ""

    @staticmethod
    def _baue_datensatz(knoten: _Knoten, stapel: list[_Knoten], teildatei: str, zeile: int | None,
                        element_name: str, kontext: LeseKontext) -> EplanDatensatz | None:
        # Seitenangaben der umschließenden Elemente (innere überschreiben äußere)
        felder: dict[str, str] = {}
        for vorfahre in stapel:
            for name in ("seitenreferenz", "seitennummer", "seitenname"):
                if vorfahre.felder.get(name):
                    felder[name] = vorfahre.felder[name]
        felder.update(knoten.felder)
        return erzeuge_datensatz(felder, kontext.fundstelle(teildatei, zeile, element_name))


# ---------------------------------------------------------------------------
# Lesen vollständiger Dateien
# ---------------------------------------------------------------------------

def standard_extraktoren() -> list[DatensatzExtraktor]:
    """Reihenfolge = Priorität bei der Formaterkennung."""
    from .dxf_parser import DxfExtraktor        # hier importiert: dxf_parser nutzt Klassen dieses Moduls
    return [DxfExtraktor(), SqliteExtraktor(), XlsxExtraktor(), XmlExtraktor(), CsvExtraktor()]


def _finde_edb_verzeichnis(elk: Path) -> Path | None:
    """EPLAN legt die Projektdaten einer ``*.elk`` in einem gleichnamigen ``*.edb``-Ordner ab."""
    if elk.suffix.lower() != ".elk":
        return None
    try:
        for eintrag in elk.parent.iterdir():
            if (eintrag.is_dir() and eintrag.suffix.lower() == ".edb"
                    and eintrag.stem.casefold() == elk.stem.casefold()):
                return eintrag
    except OSError:
        return None
    return None


def _mit_punkten(zahl: int) -> str:
    """Zahl mit deutschem Tausenderpunkt (1234567 -> 1.234.567)."""
    return f"{zahl:,}".replace(",", ".")


class _UnbekannteDateien:
    """Sammelt je Dateiendung, was kein Extraktor erkannt hat, samt Strukturbeispielen."""

    MAX_BEISPIELE = 3
    MAX_ENDUNGEN = 25

    def __init__(self) -> None:
        self.anzahl: Counter[str] = Counter()
        self.bytes: Counter[str] = Counter()
        self._beispiele: dict[str, list[tuple[int, str, bytes]]] = {}

    def merke(self, teildatei: Teildatei, kopf: bytes) -> None:
        endung = teildatei.endung or "(ohne Endung)"
        self.anzahl[endung] += 1
        self.bytes[endung] += max(teildatei.groesse, 0)
        beispiele = self._beispiele.setdefault(endung, [])
        beispiele.append((teildatei.groesse, teildatei.name, kopf))
        beispiele.sort(key=lambda eintrag: -eintrag[0])
        del beispiele[self.MAX_BEISPIELE:]

    def als_text(self) -> str:
        zeilen = ["Nicht ausgewertete Dateien (kein passender Extraktor), je Endung:"]
        for endung, _ in self.bytes.most_common(self.MAX_ENDUNGEN):
            zeilen.append(f"  {endung}: {self.anzahl[endung]} Datei(en), {_mit_punkten(self.bytes[endung])} Bytes")
            for groesse, name, kopf in self._beispiele[endung]:
                zeilen.append(f"    Beispiel: {name} ({_mit_punkten(max(groesse, 0))} Bytes)")
                zeilen.extend(f"      {zeile}" for zeile in beschreibe_kopf(kopf))
        if len(self.bytes) > self.MAX_ENDUNGEN:
            zeilen.append(f"  … und {len(self.bytes) - self.MAX_ENDUNGEN} weitere Endungen")
        return "\n".join(zeilen)


def erzeuge_diagnosebericht(ergebnis: Einleseergebnis) -> str:
    """Textbericht über ein Einleseergebnis – zum Speichern und Weitergeben bei Formatproblemen."""
    zeilen = ["Diagnosebericht EPLAN-Prüfer", f"Quelle: {ergebnis.quelle}",
              f"Datensätze: {len(ergebnis.datensaetze)}, Seiten: {len(ergebnis.seiten)}", "",
              "Teildateien:"]
    zeilen.extend(f"  [{t.art}] {t.name} – {t.datensaetze} Datensätze"
                  + (f" – {t.hinweis}" if t.hinweis else "") for t in ergebnis.teildateien)
    zeilen += ["", "Warnungen:"]
    zeilen.extend(f"  - {w}" for w in ergebnis.warnungen or ["keine"])
    zeilen += ["", "Strukturbericht:", ergebnis.strukturbericht or "(keiner)"]
    return "\n".join(zeilen) + "\n"


class EplanDateiLeser:
    """
    Fassade: liest eine EPLAN-Datei (oder einen Export davon) vollständig ein.

    Wird mit einer Liste von Extraktoren und der Feldzuordnung konfiguriert
    (Dependency Injection), damit sich das Verhalten ohne Codeänderung erweitern lässt.
    """

    def __init__(self, einstellungen: Einstellungen,
                 extraktoren: Sequence[DatensatzExtraktor] | None = None,
                 analysator: DateiAnalysator | None = None) -> None:
        self._einstellungen = einstellungen
        self._extraktoren = list(extraktoren) if extraktoren is not None else standard_extraktoren()
        self._analysator = analysator or DateiAnalysator()

    def lese(self, pfad: str | Path, fortschritt: FortschrittsMelder | None = None,
             abbruch: threading.Event | None = None) -> Einleseergebnis:
        pfad = Path(pfad)
        if not pfad.exists():
            raise EplanDateiFehler(f"Die Datei existiert nicht:\n{pfad}")
        _log.info("Einlesen gestartet: %s", pfad)

        ergebnis = Einleseergebnis(quelle=str(pfad))
        try:
            ergebnis.strukturbericht = self._analysator.analysiere(pfad).als_text()
        except EplanDateiFehler as fehler:
            ergebnis.warnungen.append(str(fehler))

        kontext = LeseKontext(
            zuordner=Feldzuordner(self._einstellungen.feldzuordnung),
            bevorzugte_sprache=self._einstellungen.allgemein.bevorzugte_sprache,
            hauptdatei=pfad.name, abbruch=abbruch, warnungen=ergebnis.warnungen)

        quellen = [pfad]
        edb = _finde_edb_verzeichnis(pfad)
        if edb is not None:
            _log.info("Zugehöriges Projektverzeichnis gefunden: %s", edb)
            quellen.append(edb)

        gesehen: dict[tuple[str, ...], str] = {}
        unbekannte = _UnbekannteDateien()
        verdopplungen = 0
        rohdaten: list[EplanDatensatz] = []

        for quelle in quellen:
            container = ContainerLeser(abbruch)
            try:
                for teildatei in container.teildateien(quelle):
                    if fortschritt:
                        fortschritt(f"Lese {teildatei.name}", len(ergebnis.teildateien), 0)
                    verdopplungen += self._verarbeite(
                        teildatei, kontext, ergebnis, rohdaten, gesehen, unbekannte)
            except _LESEFEHLER as fehler:
                raise EplanDateiFehler(f"Die Datei „{quelle.name}“ ist nicht lesbar: {fehler}") from fehler
            for name, grund in container.uebersprungen:
                ergebnis.teildateien.append(TeildateiBericht(name, "übersprungen", 0, grund))
                _log.warning("Übersprungen: %s (%s)", name, grund)
            self._melde_sperren(container, quelle, ergebnis)

        for endung, anzahl in sorted(unbekannte.anzahl.items()):
            ergebnis.teildateien.append(TeildateiBericht(
                f"{anzahl} Datei(en) {endung}", "nicht unterstützt", 0,
                "Format wird nicht ausgewertet"))
        if unbekannte.anzahl:
            diagnose = unbekannte.als_text()
            ergebnis.strukturbericht += "\n\n" + diagnose
            _log.info("Strukturanalyse der nicht ausgewerteten Dateien:\n%s", diagnose)
        if kontext.diagnose:
            text = "\n\n".join(kontext.diagnose)
            ergebnis.strukturbericht += "\n\n" + text
            _log.info("Strukturanalyse erkannter, aber leerer Dateien:\n%s", text)

        if verdopplungen:
            _log.info("%d identische Datensätze aus mehreren Teildateien zusammengeführt.", verdopplungen)
        ergebnis.datensaetze = [dataclasses.replace(d, laufende_nummer=n)
                                for n, d in enumerate(rohdaten, start=1)]
        ergebnis.seiten = self._sammle_seiten(ergebnis.datensaetze)

        if not ergebnis.datensaetze and kontext.diagnose:
            ergebnis.warnungen.append(
                "Es wurden keine SPS-Datensätze gefunden. Die gelesenen Dateien (z. B. DXF) enthalten keine "
                "Felder, die der Feldzuordnung entsprechen. Ihr Aufbau steht im Einleseprotokoll; passende "
                "Bezeichnungen unter Extras ▸ Einstellungen ▸ Feldzuordnung eintragen.")
        elif not ergebnis.datensaetze:
            if edb is not None:
                ergebnis.warnungen.append(
                    "Es wurden keine SPS-Datensätze gefunden. Die Projektdaten liegen im Ordner "
                    f"„{edb.name}“ in EPLANs eigenem Datenbankformat, das dieses Programm nicht direkt "
                    "lesen kann (die *.elk enthält nur einen Verweis darauf). Bitte die SPS-Daten in EPLAN "
                    "exportieren (CSV/XLSX/XML) und diese Datei öffnen. Welche Dateien der Ordner enthält, "
                    "steht im Einleseprotokoll (Extras ▸ Diagnosebericht speichern).")
            else:
                ergebnis.warnungen.append(
                    "Es wurden keine SPS-Datensätze gefunden. Die Datei hat vermutlich ein proprietäres, "
                    "nicht lesbares Binärformat oder enthält keine passenden Felder. "
                    "Bitte Strukturbericht prüfen und ggf. einen Export aus EPLAN (CSV/XLSX/XML) öffnen.")
        _log.info("Einlesen beendet: %d Datensätze, %d Seiten, %d Warnungen (%s)",
                  len(ergebnis.datensaetze), len(ergebnis.seiten), len(ergebnis.warnungen), pfad.name)
        return ergebnis

    # -- intern ------------------------------------------------------------------

    def _verarbeite(self, teildatei: Teildatei, kontext: LeseKontext, ergebnis: Einleseergebnis,
                    rohdaten: list[EplanDatensatz], gesehen: dict[tuple[str, ...], str],
                    unbekannte: _UnbekannteDateien) -> int:
        """Liest eine Teildatei; liefert die Zahl zusammengeführter Dubletten."""
        try:
            kopf = teildatei.lese_kopf()
        except _LESEFEHLER as fehler:
            ergebnis.teildateien.append(TeildateiBericht(teildatei.name, "unlesbar", 0, str(fehler)))
            return 0

        extraktor = next((e for e in self._extraktoren if e.unterstuetzt(teildatei.name, kopf)), None)
        if extraktor is None:
            unbekannte.merke(teildatei, kopf)
            _log.debug("Kein Extraktor für %s", teildatei.name)
            return 0

        gefunden = 0
        verdoppelt = 0
        hinweis = ""
        try:
            for datensatz in extraktor.extrahiere(teildatei, kontext):
                schluessel = (datensatz.symbolische_adresse, datensatz.funktionstext,
                              datensatz.sps_adresse, datensatz.seitenreferenz,
                              datensatz.seitennummer, datensatz.betriebsmittelkennzeichen)
                erste_teildatei = gesehen.setdefault(schluessel, teildatei.name)
                if erste_teildatei != teildatei.name:
                    verdoppelt += 1            # identischer Datensatz stammt schon aus anderer Teildatei
                    continue
                rohdaten.append(datensatz)
                gefunden += 1
        except VorgangAbgebrochenFehler:
            raise
        except (EplanDateiFehler, *_LESEFEHLER) as fehler:
            hinweis = f"Teilweise gelesen: {fehler}"
            kontext.warnungen.append(f"{teildatei.name}: {fehler}")
            _log.warning("Fehler beim Lesen von %s: %s", teildatei.name, fehler)

        ergebnis.teildateien.append(TeildateiBericht(teildatei.name, extraktor.art, gefunden, hinweis))
        _log.info("%s [%s]: %d Datensätze", teildatei.name, extraktor.art, gefunden)
        return verdoppelt

    @staticmethod
    def _melde_sperren(container: ContainerLeser, quelle: Path, ergebnis: Einleseergebnis) -> None:
        """Fasst gesperrte Dateien und Sperrdateien (*.lck) in je einer Meldung zusammen."""
        if container.sperrdateien:
            _log.info("%d Sperrdateien (*.lck) in %s übergangen.", container.sperrdateien, quelle.name)
            ergebnis.teildateien.append(TeildateiBericht(
                f"{container.sperrdateien} Sperrdatei(en) *.lck", "ignoriert", 0,
                "enthalten keine Projektdaten"))
        if container.gesperrt:
            beispiele = ", ".join(container.gesperrt[:3])
            meldung = (f"{len(container.gesperrt)} Datei(en) in „{quelle.name}“ sind gesperrt "
                       f"(Zugriff verweigert, z. B. {beispiele}). Das Projekt ist vermutlich in EPLAN "
                       "geöffnet. Bitte das Projekt in EPLAN schließen oder eine Kopie verwenden.")
            ergebnis.warnungen.append(meldung)
            ergebnis.teildateien.append(TeildateiBericht(
                f"{len(container.gesperrt)} gesperrte Datei(en)", "gesperrt", 0, "Zugriff verweigert"))
            _log.warning(meldung)

    @staticmethod
    def _sammle_seiten(datensaetze: Sequence[EplanDatensatz]) -> list[EplanSeite]:
        seiten: dict[tuple[str, str], EplanSeite] = {}
        for d in datensaetze:
            if not (d.seitenreferenz or d.seitennummer or d.seitenname):
                continue
            schluessel = (d.seitenreferenz, d.seitennummer)
            if schluessel not in seiten or (d.seitenname and not seiten[schluessel].name):
                seiten[schluessel] = EplanSeite(d.seitenreferenz, d.seitennummer, d.seitenname)
        return list(seiten.values())


# ---------------------------------------------------------------------------
# Quellauszug („Sprung zur Fehlerstelle“)
# ---------------------------------------------------------------------------

class QuellenAuszugLeser:
    """Liefert den Textausschnitt um eine Fundstelle, damit der Anwender die Quelle sehen kann."""

    MAX_ZEILENLAENGE = 300

    def lese(self, hauptdatei: str | Path, fundstelle: Fundstelle, umgebung: int = 6) -> Quellauszug:
        pfad = Path(hauptdatei)
        if not pfad.exists():
            return Quellauszug([], None, f"Die Quelldatei existiert nicht mehr: {pfad}")
        if fundstelle.zeile is None:
            return Quellauszug([], None, "Für diesen Datensatz ist keine Zeilenangabe bekannt.")

        quellen = [pfad]
        edb = _finde_edb_verzeichnis(pfad)
        if edb is not None:
            quellen.append(edb)
        for quelle in quellen:
            container = ContainerLeser()
            try:
                for teildatei in container.teildateien(quelle):
                    if teildatei.name != fundstelle.teildatei:
                        continue
                    return self._auszug(teildatei, fundstelle, umgebung)
            except _LESEFEHLER as fehler:
                return Quellauszug([], None, f"Quelle nicht lesbar: {fehler}")
        return Quellauszug([], None, f"Die Teildatei „{fundstelle.teildatei}“ wurde nicht gefunden.")

    def _auszug(self, teildatei: Teildatei, fundstelle: Fundstelle, umgebung: int) -> Quellauszug:
        endung = teildatei.endung
        if endung in (".xlsx", ".xlsm") or teildatei.lese_kopf(16).startswith(b"SQLite format 3\x00"):
            return Quellauszug([], None, f"Binäre Quelle – {fundstelle.element}, Zeile {fundstelle.zeile}.")
        text = _dekodiere_text(teildatei.lese_alles())
        zeilen = text.splitlines()
        ziel = fundstelle.zeile or 1
        start = max(1, ziel - umgebung)
        ende = min(len(zeilen), ziel + umgebung)
        if endung == ".dxf":
            # Eine DXF-Entität belegt viele Zeilen (Gruppencode/Wert); die Fundstelle zeigt auf ihren
            # Beginn, der Inhalt steht darunter.
            start = max(1, ziel - 2)
            ende = min(len(zeilen), ziel + max(umgebung, 80))
        ausschnitt: list[tuple[int, str]] = []
        for nummer in range(start, ende + 1):
            inhalt = zeilen[nummer - 1]
            if len(inhalt) > self.MAX_ZEILENLAENGE:
                inhalt = inhalt[: self.MAX_ZEILENLAENGE] + " …"
            ausschnitt.append((nummer, inhalt))
        return Quellauszug(ausschnitt, ziel, "")

