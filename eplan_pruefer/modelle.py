"""
modelle.py – Datenklassen der EPLAN-Prüfanwendung.

Dieses Modul enthält ausschließlich Datenstrukturen (und kleine, rein
rechnerische Hilfsfunktionen). Es kennt weder die Benutzeroberfläche noch die
Datenbank noch das Dateisystem. Dadurch können Parser, Prüfer, Export und GUI
unabhängig voneinander entwickelt und getestet werden (Trennung von GUI,
Datenmodell und Datenzugriff).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


# ---------------------------------------------------------------------------
# Fehlerklassen (anwendungsweit)
# ---------------------------------------------------------------------------

class EplanPrueferFehler(Exception):
    """Basisklasse aller erwarteten Fehler. Die Meldung ist für Anwender lesbar."""


class EplanDateiFehler(EplanPrueferFehler):
    """Eine EPLAN-Datei konnte nicht gelesen oder interpretiert werden."""


class DatenbankFehler(EplanPrueferFehler):
    """Fehler beim Zugriff auf die Referenzdatenbank."""


class EinstellungsFehler(EplanPrueferFehler):
    """Die Konfiguration ist unvollständig oder ungültig."""


class ExportFehler(EplanPrueferFehler):
    """Die Fehlerliste konnte nicht exportiert werden."""


class VorgangAbgebrochenFehler(EplanPrueferFehler):
    """Der Benutzer hat einen laufenden Vorgang abgebrochen."""


# ---------------------------------------------------------------------------
# Fundstelle und Datensatz
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Fundstelle:
    """Technischer Verweis auf die Stelle der Quelle, aus der ein Datensatz stammt."""

    datei: str                      # Anzeigename der geöffneten Datei (z. B. "Anlage.zw1")
    teildatei: str = ""             # Pfad innerhalb eines Containers (z. B. "Daten/seiten.xml")
    zeile: int | None = None        # Zeilennummer (XML/CSV), Zeile im Tabellenblatt oder Tabelle
    element: str = ""               # Elementname, Tabellenblatt oder Datenbanktabelle

    def als_text(self) -> str:
        """Lesbare Darstellung, z. B. ``Anlage.zw1 › seiten.xml › Zeile 120 (Funktion)``."""
        teile = [self.datei]
        anzeige = self.teildatei
        for trenner in ("!", "/"):              # Containername nicht doppelt anzeigen
            if anzeige.startswith(self.datei + trenner):
                anzeige = anzeige[len(self.datei) + 1:]
        if anzeige and anzeige != self.datei:
            teile.append(anzeige)
        text = " › ".join(teile)
        if self.zeile is not None:
            text += f" › Zeile {self.zeile}"
        if self.element:
            text += f" ({self.element})"
        return text


_SEITENNUMMER_EINFACH = re.compile(r"^\d+(?:[.\-]\w+)?$")


def leite_seitennummer_ab(seitenreferenz: str) -> str:
    """
    Ermittelt die Seitennummer aus einer EPLAN-Seitenbezeichnung.

    Beispiele: ``=ANL+SCH/12`` -> ``12``;  ``12`` -> ``12``;  ``=ANL+SCH`` -> ``""``.
    """
    text = (seitenreferenz or "").strip()
    if not text:
        return ""
    if "/" in text:
        return text.rsplit("/", 1)[1].strip()
    if _SEITENNUMMER_EINFACH.match(text):
        return text
    return ""


@dataclass(frozen=True, slots=True)
class EplanDatensatz:
    """Ein aus dem EPLAN-Projekt gelesener SPS-Anschluss."""

    symbolische_adresse: str = ""
    funktionstext: str = ""
    sps_adresse: str = ""
    seitenreferenz: str = ""        # vollständige Seitenbezeichnung, z. B. "=ANL+SCH/12"
    seitennummer: str = ""          # nur die Nummer, z. B. "12"
    seitenname: str = ""            # Seitenbeschreibung / Seitentitel
    betriebsmittelkennzeichen: str = ""     # „sichtbares" BMK
    vollstaendiges_bmk: str = ""            # vollständiges BMK, sofern die Quelle beide liefert
    fundstelle: Fundstelle = field(default_factory=lambda: Fundstelle(datei=""))
    laufende_nummer: int = 0        # Reihenfolge des Einlesens, beginnt bei 1

    @property
    def seite_anzeige(self) -> str:
        """Kurzform der Seite für Tabellen und Export (Nummer, sonst Referenz)."""
        return self.seitennummer or self.seitenreferenz

    @property
    def seite_ausfuehrlich(self) -> str:
        """Seite mit Name, z. B. ``12 – Antriebe``."""
        seite = self.seite_anzeige or "unbekannt"
        if self.seitenname:
            return f"{seite} – {self.seitenname}"
        return seite


@dataclass(frozen=True, slots=True)
class EplanSeite:
    """Bekannte Seite des Projekts."""

    referenz: str
    nummer: str
    name: str


@dataclass(frozen=True, slots=True)
class Quellauszug:
    """Textausschnitt der Quelle rund um eine Fundstelle (für „Sprung zur Fehlerstelle“)."""

    zeilen: list[tuple[int, str]]       # (Zeilennummer, Text)
    markierte_zeile: int | None
    hinweis: str = ""                   # gesetzt, wenn kein Ausschnitt möglich ist


@dataclass(slots=True)
class TeildateiBericht:
    """Ergebnis der Auswertung einer einzelnen Teildatei eines Containers."""

    name: str
    art: str                        # z. B. "XML", "CSV", "XLSX", "SQLite", "nicht unterstützt"
    datensaetze: int = 0
    hinweis: str = ""


@dataclass(slots=True)
class Einleseergebnis:
    """Gesamtergebnis des Einlesens einer EPLAN-Datei."""

    quelle: str
    datensaetze: list[EplanDatensatz] = field(default_factory=list)
    seiten: list[EplanSeite] = field(default_factory=list)
    teildateien: list[TeildateiBericht] = field(default_factory=list)
    warnungen: list[str] = field(default_factory=list)
    strukturbericht: str = ""       # Ergebnis der Dateistrukturanalyse (Text)

    def datensaetze_der_seite(self, datensatz: EplanDatensatz) -> list[EplanDatensatz]:
        """Alle Datensätze, die auf derselben Seite wie ``datensatz`` stehen."""
        schluessel = (datensatz.seitenreferenz, datensatz.seitennummer)
        return [d for d in self.datensaetze
                if (d.seitenreferenz, d.seitennummer) == schluessel]


# ---------------------------------------------------------------------------
# Prüfergebnisse
# ---------------------------------------------------------------------------

class Fehlertyp(Enum):
    """Alle Fehlerarten. Der Wert ist die Anzeigebezeichnung."""

    DOPPELTES_SYMBOL = "Doppeltes Symbol"
    DOPPELTER_FUNKTIONSTEXT = "Doppelter Funktionstext"
    DOPPELTE_ADRESSE = "Doppelte Adresse"
    PLATZHALTER = "Platzhalter (?)"
    SYMBOL_NICHT_IN_DB = "Symbol nicht in Datenbank vorhanden"
    FUNKTIONSTEXT_NICHT_IN_DB = "Funktionstext nicht in Datenbank vorhanden"
    KOMBINATION_ABWEICHEND = "Symbol/Funktionstext stimmt nicht mit Datenbank überein"

    @property
    def bezeichnung(self) -> str:
        return self.value

    @property
    def uebernehmbar(self) -> bool:
        """Darf der Datensatz über „In Datenbank übernehmen“ gespeichert werden?"""
        return self in (Fehlertyp.SYMBOL_NICHT_IN_DB, Fehlertyp.FUNKTIONSTEXT_NICHT_IN_DB)


@dataclass(frozen=True, slots=True)
class Pruefungsfehler:
    """Ein festgestellter Fehler samt Verweis auf den betroffenen Datensatz."""

    fehlertyp: Fehlertyp
    datensatz: EplanDatensatz
    beschreibung: str
    bezug: EplanDatensatz | None = None     # z. B. erste Verwendung bei Duplikaten

    @property
    def symbol(self) -> str:
        return self.datensatz.symbolische_adresse

    @property
    def funktionstext(self) -> str:
        return self.datensatz.funktionstext

    @property
    def adresse(self) -> str:
        return self.datensatz.sps_adresse

    @property
    def seite(self) -> str:
        return self.datensatz.seite_anzeige

    @property
    def verweis(self) -> str:
        """Fundstelle der ersten Verwendung bei Duplikaten, sonst leer."""
        if self.bezug is None:
            return ""
        return (f"Erste Verwendung auf Seite {self.bezug.seite_ausfuehrlich} – "
                f"{self.bezug.fundstelle.als_text()}")


class DatenbankStatus(Enum):
    """Ergebnis des (optionalen) Datenbankabgleichs für einen Datensatz."""

    VORHANDEN = "In Datenbank vorhanden"
    SYMBOL_FEHLT = "Symbol fehlt in Datenbank"
    FUNKTIONSTEXT_FEHLT = "Funktionstext fehlt in Datenbank"
    KOMBINATION_ABWEICHEND = "Kombination weicht ab"
    NICHT_GEPRUEFT = "–"

    @property
    def bezeichnung(self) -> str:
        return self.value


@dataclass(slots=True)
class Pruefbericht:
    """Gesamtergebnis eines Prüflaufs."""

    fehler: list[Pruefungsfehler] = field(default_factory=list)
    ausgefuehrt: list[str] = field(default_factory=list)            # Bezeichnungen
    uebersprungen: dict[str, str] = field(default_factory=dict)     # Bezeichnung -> Grund
    anzahl_datensaetze: int = 0
    #: Ergebnis des Datenbankabgleichs je Datensatz (Schlüssel = laufende Nummer); leer, wenn nicht geprüft
    datenbankstatus: dict[int, DatenbankStatus] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Datenbankergebnisse
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Vorhandensein:
    """Ergebnis der Vorabprüfung vor dem Speichern in der Referenzdatenbank."""

    symbol: bool
    funktionstext: bool
    kombination: bool


@dataclass(frozen=True, slots=True)
class UebernahmeErgebnis:
    """Ergebnis des Übernehmens eines Datensatzes in die Referenzdatenbank."""

    symbol: str
    funktionstext: str
    uebernommen: bool
    meldung: str
    vorhandensein: Vorhandensein | None = None
    benoetigt_bestaetigung: bool = False    # Symbol oder Text existiert bereits


@dataclass(frozen=True, slots=True)
class Verbindungstest:
    """Ergebnis des Verbindungstests im Einstellungsdialog."""

    erfolgreich: bool
    meldungen: tuple[str, ...]


# ---------------------------------------------------------------------------
# Einstellungen
# ---------------------------------------------------------------------------

#: Logische Felder, die aus EPLAN-Daten gelesen werden (Reihenfolge = Anzeige).
FELDNAMEN: tuple[str, ...] = (
    "symbolische_adresse",
    "funktionstext",
    "sps_adresse",
    "seitenreferenz",
    "seitennummer",
    "seitenname",
    "betriebsmittelkennzeichen",
    "vollstaendiges_bmk",
)

#: Version der Standard-Feldzuordnung. Wird sie erhöht, ergänzt das Laden alter Konfigurationen
#: die neuen Standardbezeichnungen (vorhandene Einträge des Anwenders bleiben erhalten).
FELDZUORDNUNG_VERSION = 3

FELD_ANZEIGENAMEN: dict[str, str] = {
    "symbolische_adresse": "Symbolische Adresse",
    "funktionstext": "Funktionstext (Langtext)",
    "sps_adresse": "SPS-Adresse",
    "seitenreferenz": "Seitenreferenz (Seitenbezeichnung)",
    "seitennummer": "Seitennummer",
    "seitenname": "Seitenname",
    "betriebsmittelkennzeichen": "Betriebsmittelkennzeichen (sichtbar)",
    "vollstaendiges_bmk": "Vollständiges BMK",
}

#: Standard-Bezeichnungen, unter denen die Felder in Exporten/XML/Tabellen vorkommen.
#: Alle Vergleiche sind unabhängig von Groß-/Kleinschreibung, Leer- und Sonderzeichen.
STANDARD_FELDZUORDNUNG: dict[str, tuple[str, ...]] = {
    # Die Bezeichnungen „17_…" und „4_…" sind Attribut-Namen aus EPLAN-DXF-Exporten
    # (<Objekttyp>_<EPLAN-Eigenschaftsnummer>): 17 = Funktion, 4 = Seite.
    "symbolische_adresse": ("Symbolische Adresse", "Symbolic address", "SymbolicAddress",
                            "Symbol", "Symbolik", "17_20402"),
    "funktionstext": ("Funktionstext", "Funktionstext (Langtext)", "Function text",
                      "FunctionText", "Langtext", "17_20031"),
    "sps_adresse": ("SPS-Adresse", "PLC address", "PLCAddress", "Adresse", "Address", "17_20400"),
    "seitenreferenz": ("Seitenreferenz", "Seitenbezeichnung", "Seitenverweis",
                       "Page designation", "Page reference", "4_11009"),
    "seitennummer": ("Seitennummer", "Seite", "Page", "Page number", "Blatt"),
    "seitenname": ("Seitenname", "Seitentitel", "Seitenbeschreibung", "Page name", "4_11011"),
    "betriebsmittelkennzeichen": ("Betriebsmittelkennzeichen", "BMK", "Sichtbares BMK",
                                  "Device tag", "DeviceTag", "Equipment identifier", "Betriebsmittel", "17_20010"),
    "vollstaendiges_bmk": ("Vollständiges BMK", "Vollstaendiges BMK",
                           "Vollständiges Betriebsmittelkennzeichen", "Full device tag", "17_20006"),
}


@dataclass(slots=True)
class DatenbankEinstellungen:
    """Lage und Struktur der SQLite-Referenzdatenbank (nichts davon ist im Code fest)."""

    dateipfad: str = ""                 # SQLite-Datei, z. B. C:\\Daten\\symbole.db
    tabellenname: str = ""
    feld_symbol: str = ""
    feld_funktionstext: str = ""
    primaerschluessel: str = ""
    timeout_s: int = 10                 # Wartezeit, wenn die Datei von einem anderen Programm gesperrt ist

    def fehlende_angaben(self) -> list[str]:
        """Namen der Pflichtangaben, die noch fehlen."""
        pflicht = {
            "Datenbankdatei": self.dateipfad,
            "Tabellenname": self.tabellenname,
            "Feldname Symbol": self.feld_symbol,
            "Feldname Funktionstext": self.feld_funktionstext,
        }
        return [name for name, wert in pflicht.items() if not (wert or "").strip()]

    @property
    def ist_vollstaendig(self) -> bool:
        return not self.fehlende_angaben()


@dataclass(slots=True)
class PruefEinstellungen:
    """Welche Prüfungen laufen und wie Texte verglichen werden."""

    doppelte_symbole: bool = True
    doppelte_funktionstexte: bool = True
    doppelte_adressen: bool = True
    platzhalter: bool = True
    datenbankabgleich: bool = False       # optional
    gross_kleinschreibung_beachten: bool = False
    automatisch_pruefen: bool = True


@dataclass(slots=True)
class AllgemeineEinstellungen:
    protokollstufe: str = "INFO"
    bevorzugte_sprache: str = "de_DE"       # für mehrsprachige Funktionstexte
    letztes_verzeichnis: str = ""


@dataclass(slots=True)
class Einstellungen:
    """Gesamte Konfiguration; wird in einer INI-Datei gespeichert (siehe einstellungen.py)."""

    datenbank: DatenbankEinstellungen = field(default_factory=DatenbankEinstellungen)
    pruefung: PruefEinstellungen = field(default_factory=PruefEinstellungen)
    allgemein: AllgemeineEinstellungen = field(default_factory=AllgemeineEinstellungen)
    feldzuordnung: dict[str, tuple[str, ...]] = field(
        default_factory=lambda: dict(STANDARD_FELDZUORDNUNG))
