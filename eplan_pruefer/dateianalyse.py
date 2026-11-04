"""
dateianalyse.py – Untersuchung des tatsächlichen Aufbaus einer Datei.

*.elk und *.zw1 sind proprietäre EPLAN-Formate ohne öffentliche Spezifikation.
Dieses Modul stellt fest, womit man es tatsächlich zu tun hat – unabhängig von
der Dateiendung –, und liefert die Entscheidungsgrundlage für den Parser:

* Magische Bytes / eingebettete Signaturen (ZIP auch hinter einem Vorspann,
  GZIP, OLE2, SQLite, 7z, XML, PDF …)
* Textcodierung (UTF-8/UTF-16/Windows-1252) bei Textdateien
* Entropie (Hinweis auf Kompression oder Verschlüsselung)
* lesbare Zeichenketten (ASCII und UTF-16LE) als Stichprobe
* bei Archiven die Liste der enthaltenen Dateien

Die Analyse verändert nie eine Datei und liest höchstens ``MAX_SUCHBEREICH`` Bytes.
"""

from __future__ import annotations

import math
import re
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .logging import hole_protokollierer
from .modelle import EplanDateiFehler

_log = hole_protokollierer("dateianalyse")

MAX_SUCHBEREICH = 16 * 1024 * 1024        # Bereich, in dem nach Signaturen gesucht wird
ENTROPIE_BEREICH = 1024 * 1024
ZEICHENKETTEN_BEREICH = 256 * 1024
MAX_SIGNATUREN_JE_ART = 20
MAX_ZEICHENKETTEN = 40
MAX_ARCHIVEINTRAEGE = 200

#: Bekannte Signaturen: (Name, Byte-Muster)
SIGNATUREN: tuple[tuple[str, bytes], ...] = (
    ("ZIP (lokaler Dateikopf)", b"PK\x03\x04"),
    ("ZIP (Ende des zentralen Verzeichnisses)", b"PK\x05\x06"),
    ("GZIP", b"\x1f\x8b\x08"),
    ("OLE2 / Compound File", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"),
    ("SQLite 3", b"SQLite format 3\x00"),
    ("7-Zip", b"7z\xbc\xaf\x27\x1c"),
    ("RAR", b"Rar!\x1a\x07"),
    ("BZIP2", b"BZh"),
    ("Zstandard", b"\x28\xb5\x2f\xfd"),
    ("XML-Deklaration", b"<?xml"),
    ("PDF", b"%PDF-"),
)

_ASCII_ZEICHENKETTE = re.compile(rb"[\x20-\x7e\xc4\xd6\xdc\xe4\xf6\xfc\xdf]{6,}")
_UTF16_ZEICHENKETTE = re.compile(rb"(?:[\x20-\x7e]\x00){6,}")


@dataclass(slots=True)
class Strukturbericht:
    """Ergebnis der Strukturanalyse."""

    pfad: str
    ist_verzeichnis: bool = False
    groesse: int = 0
    kopf_hex: str = ""
    kopf_text: str = ""
    erkannte_art: str = "unbekannt"
    codierung: str = ""
    entropie: float = 0.0
    signaturen: list[tuple[int, str]] = field(default_factory=list)
    archiveintraege: list[str] = field(default_factory=list)
    archiv_anzahl: int = 0
    zeichenketten: list[str] = field(default_factory=list)
    verzeichnisinhalt: list[str] = field(default_factory=list)
    hinweise: list[str] = field(default_factory=list)

    def als_text(self) -> str:
        """Menschenlesbare, protokollfähige Darstellung."""
        z = [f"Datei:        {self.pfad}"]
        if self.ist_verzeichnis:
            z.append("Art:          Verzeichnis")
            z.append(f"Einträge:     {len(self.verzeichnisinhalt)} (Auszug folgt)")
            z.extend(f"  {e}" for e in self.verzeichnisinhalt[:60])
        else:
            z.append(f"Größe:        {self.groesse:,} Bytes".replace(",", "."))
            z.append(f"Erkannte Art: {self.erkannte_art}")
            if self.codierung:
                z.append(f"Codierung:    {self.codierung}")
            z.append(f"Entropie:     {self.entropie:.2f} Bit/Byte (max. 8,00)".replace(".", ",", 1))
            z.append(f"Dateikopf:    {self.kopf_hex}")
            z.append(f"              {self.kopf_text}")
            if self.signaturen:
                z.append("Gefundene Signaturen (Offset: Art):")
                z.extend(f"  {offset}: {name}" for offset, name in self.signaturen)
            if self.archiv_anzahl:
                z.append(f"Archivinhalt: {self.archiv_anzahl} Einträge")
                z.extend(f"  {e}" for e in self.archiveintraege)
                if self.archiv_anzahl > len(self.archiveintraege):
                    z.append(f"  … und {self.archiv_anzahl - len(self.archiveintraege)} weitere")
            if self.zeichenketten:
                z.append("Lesbare Zeichenketten (Stichprobe):")
                z.extend(f"  {s}" for s in self.zeichenketten)
        if self.hinweise:
            z.append("Bewertung:")
            z.extend(f"  - {h}" for h in self.hinweise)
        return "\n".join(z)


def berechne_entropie(daten: bytes) -> float:
    """Shannon-Entropie in Bit pro Byte (0 … 8)."""
    if not daten:
        return 0.0
    gesamt = len(daten)
    return -sum((n / gesamt) * math.log2(n / gesamt) for n in Counter(daten).values())


def erkenne_codierung(daten: bytes) -> str:
    """Bestimmt die Codierung eines Textes; leer, wenn die Daten binär wirken."""
    if daten.startswith(b"\xef\xbb\xbf"):
        return "UTF-8 mit BOM"
    if daten.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "UTF-16 mit BOM"
    if not daten:
        return ""
    if b"\x00" in daten:
        # UTF-16 ohne BOM: jedes zweite Byte ist (fast) immer 0
        gerade, ungerade = daten[0::2], daten[1::2]
        if ungerade.count(0) > len(ungerade) * 0.8 or gerade.count(0) > len(gerade) * 0.8:
            return "UTF-16 ohne BOM"
        return ""
    steuer = sum(1 for b in daten if b < 0x20 and b not in (9, 10, 13))
    if steuer > len(daten) * 0.02:
        return ""
    try:
        daten.decode("utf-8")
        return "UTF-8"
    except UnicodeDecodeError:
        return "Windows-1252 (ANSI)"


def _erkenne_art(kopf: bytes, codierung: str) -> str:
    if kopf.startswith(b"PK\x03\x04") or kopf.startswith(b"PK\x05\x06"):
        return "ZIP-Archiv"
    if kopf.startswith(b"\x1f\x8b"):
        return "GZIP-Datenstrom"
    if kopf.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return "OLE2 / Compound File"
    if kopf.startswith(b"SQLite format 3\x00"):
        return "SQLite-Datenbank"
    if kopf.startswith(b"7z\xbc\xaf\x27\x1c"):
        return "7-Zip-Archiv"
    if kopf.startswith(b"%PDF-"):
        return "PDF"
    if codierung:
        text = kopf.lstrip(b"\xef\xbb\xbf\xff\xfe\xfe\xff\x00 \t\r\n")
        if text.startswith(b"<"):
            return "XML/Markup-Text"
        return "Text"
    return "Binärdaten (unbekanntes Format)"


class DateiAnalysator:
    """Analysiert Dateien und Verzeichnisse (siehe Moduldokumentation)."""

    def analysiere(self, pfad: str | Path) -> Strukturbericht:
        pfad = Path(pfad)
        if not pfad.exists():
            raise EplanDateiFehler(f"Die Datei oder das Verzeichnis existiert nicht: {pfad}")
        if pfad.is_dir():
            bericht = self._analysiere_verzeichnis(pfad)
        else:
            bericht = self._analysiere_datei(pfad)
        _log.info("Strukturanalyse:\n%s", bericht.als_text())
        return bericht

    # -- Verzeichnis ----------------------------------------------------------

    @staticmethod
    def _analysiere_verzeichnis(pfad: Path) -> Strukturbericht:
        bericht = Strukturbericht(pfad=str(pfad), ist_verzeichnis=True, erkannte_art="Verzeichnis")
        endungen: Counter[str] = Counter()
        try:
            dateien = sorted(p for p in pfad.rglob("*") if p.is_file() and not p.is_symlink())
        except OSError as fehler:
            raise EplanDateiFehler(f"Verzeichnis nicht lesbar: {pfad}\n{fehler}") from fehler
        for datei in dateien:
            endungen[datei.suffix.lower() or "(ohne Endung)"] += 1
        bericht.verzeichnisinhalt = [str(p.relative_to(pfad)) for p in dateien[:200]]
        if endungen:
            bericht.hinweise.append("Dateitypen: " + ", ".join(
                f"{endung} × {anzahl}" for endung, anzahl in endungen.most_common()))
        else:
            bericht.hinweise.append("Das Verzeichnis enthält keine Dateien.")
        return bericht

    # -- Datei ----------------------------------------------------------------

    def _analysiere_datei(self, pfad: Path) -> Strukturbericht:
        try:
            groesse = pfad.stat().st_size
            with open(pfad, "rb") as datei:
                bereich = datei.read(MAX_SUCHBEREICH)
        except OSError as fehler:
            raise EplanDateiFehler(f"Datei nicht lesbar: {pfad}\n{fehler}") from fehler

        kopf = bereich[:4096]
        bericht = Strukturbericht(pfad=str(pfad), groesse=groesse)
        bericht.kopf_hex = bereich[:32].hex(" ").upper()
        bericht.kopf_text = "".join(chr(b) if 32 <= b < 127 else "." for b in bereich[:32])
        bericht.codierung = erkenne_codierung(kopf)
        bericht.erkannte_art = _erkenne_art(kopf, bericht.codierung)
        bericht.entropie = berechne_entropie(bereich[:ENTROPIE_BEREICH])
        bericht.signaturen = self._suche_signaturen(bereich)
        self._lese_archiv(pfad, bericht)
        bericht.zeichenketten = self._sammle_zeichenketten(bereich[:ZEICHENKETTEN_BEREICH])
        self._bewerte(pfad, bericht)
        return bericht

    @staticmethod
    def _suche_signaturen(bereich: bytes) -> list[tuple[int, str]]:
        treffer: list[tuple[int, str]] = []
        for name, muster in SIGNATUREN:
            position = bereich.find(muster)
            anzahl = 0
            while position != -1 and anzahl < MAX_SIGNATUREN_JE_ART:
                # <?xml im Textkopf ist uninteressant, wenn er am Dateianfang steht
                treffer.append((position, name))
                anzahl += 1
                position = bereich.find(muster, position + 1)
        treffer.sort()
        return treffer[:MAX_SIGNATUREN_JE_ART * 3]

    @staticmethod
    def _lese_archiv(pfad: Path, bericht: Strukturbericht) -> None:
        """Listet ZIP-Inhalte auf – auch wenn dem Archiv ein fremder Vorspann vorangestellt ist."""
        try:
            with zipfile.ZipFile(pfad) as archiv:
                eintraege = archiv.infolist()
                bericht.archiv_anzahl = len(eintraege)
                bericht.archiveintraege = [
                    f"{e.filename}  ({e.file_size:,} Bytes)".replace(",", ".")
                    for e in eintraege[:MAX_ARCHIVEINTRAEGE]]
        except (zipfile.BadZipFile, OSError, NotImplementedError, RuntimeError):
            return

    @staticmethod
    def _sammle_zeichenketten(bereich: bytes) -> list[str]:
        gefunden: list[str] = []
        for treffer in _ASCII_ZEICHENKETTE.finditer(bereich):
            gefunden.append(treffer.group().decode("cp1252", errors="replace"))
            if len(gefunden) >= MAX_ZEICHENKETTEN:
                return gefunden
        for treffer in _UTF16_ZEICHENKETTE.finditer(bereich):
            gefunden.append(treffer.group().decode("utf-16-le", errors="replace"))
            if len(gefunden) >= MAX_ZEICHENKETTEN:
                break
        return gefunden

    @staticmethod
    def _bewerte(pfad: Path, bericht: Strukturbericht) -> None:
        h = bericht.hinweise
        art = bericht.erkannte_art
        ist_zip_am_anfang = art == "ZIP-Archiv"
        if bericht.archiv_anzahl and not ist_zip_am_anfang:
            h.append("Ein ZIP-Archiv liegt HINTER einem proprietären Vorspann – "
                     "der Inhalt kann trotzdem gelesen werden.")
        elif ist_zip_am_anfang:
            h.append("Standard-ZIP-Container: Inhalt kann direkt gelesen werden.")
        elif any(name.startswith("ZIP") for _, name in bericht.signaturen):
            h.append("ZIP-Signatur im Inneren gefunden, aber kein gültiges Archivende – "
                     "Archiv beschädigt, mehrteilig oder verschlüsselt.")
        if art == "SQLite-Datenbank":
            h.append("SQLite-Datenbank: Tabellen können direkt gelesen werden.")
        if art == "OLE2 / Compound File":
            h.append("OLE2-Container: wird von diesem Programm nicht ausgewertet.")
        if art in ("XML/Markup-Text", "Text"):
            h.append("Textdatei: kann direkt gelesen werden.")
        if art.startswith("Binärdaten"):
            if bericht.entropie > 7.5 and not bericht.archiv_anzahl:
                h.append("Sehr hohe Entropie ohne erkennbare Signatur: Die Daten sind vermutlich "
                         "proprietär komprimiert oder verschlüsselt und nicht lesbar.")
            else:
                h.append("Proprietäres Binärformat ohne bekannte Signatur.")
            h.append("Alternative: Daten in EPLAN exportieren (Bericht/SPS-Adressübersicht als "
                     "CSV/XLSX/XML oder per EPLAN-API) und diese Datei öffnen – siehe README.")
        if pfad.suffix.lower() in (".elk", ".zw1") and art == "ZIP-Archiv":
            h.append(f"Die Endung {pfad.suffix.lower()} enthält hier ein Standard-ZIP.")
