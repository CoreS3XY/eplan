"""
pruefer.py – Alle Prüfalgorithmen.

Jede Prüfung ist eine eigene Klasse (Strategie-Muster, Einzelverantwortung). Der
:class:`Pruefmanager` führt die aktivierten Prüfungen nacheinander aus; schlägt
eine Prüfung aus technischen Gründen fehl (z. B. Datenbank nicht erreichbar),
laufen die übrigen trotzdem weiter und der Grund wird im Bericht ausgewiesen.

Prüfungen
---------
1. :class:`DoppelteSymbolePruefung`
2. :class:`DoppelteFunktionstextePruefung`
3. :class:`DoppelteAdressenPruefung`
4. :class:`PlatzhalterPruefung`
5. :class:`DatenbankAbgleichPruefung`

Bei Duplikaten wird die erste Verwendung als in Ordnung betrachtet; jede weitere
Verwendung ist ein Fehler, dessen Beschreibung auf die erste verweist
(„Adresse bereits auf Seite 5 verwendet.“).
"""

from __future__ import annotations

import threading
import unicodedata
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Sequence

from .datenbank import ReferenzRepository
from .logging import hole_protokollierer
from .modelle import (
    EplanDatensatz,
    EplanPrueferFehler,
    Fehlertyp,
    PruefEinstellungen,
    Pruefbericht,
    Pruefungsfehler,
    VorgangAbgebrochenFehler,
)

_log = hole_protokollierer("pruefer")

PLATZHALTERZEICHEN = ("?", "？")   # ASCII- und Vollbreiten-Fragezeichen


class PruefungUebersprungenFehler(EplanPrueferFehler):
    """Eine Prüfung kann derzeit nicht ausgeführt werden (Grund steht in der Meldung)."""


class Textnormalisierer:
    """
    Einheitliche Vergleichsform für Texte.

    * Unicode-Normalisierung (NFC), mehrfache Leerzeichen/Zeilenumbrüche -> ein Leerzeichen
    * optional Groß-/Kleinschreibung ignorieren (Standard)
    * Adressen: alle Leerzeichen entfernt und immer ohne Beachtung der Schreibweise
      (``E 0.0`` und ``e0.0`` sind dieselbe Adresse)
    """

    def __init__(self, gross_klein_beachten: bool = False) -> None:
        self._beachten = gross_klein_beachten

    def text(self, wert: str) -> str:
        bereinigt = " ".join(unicodedata.normalize("NFC", wert or "").split())
        return bereinigt if self._beachten else bereinigt.casefold()

    @staticmethod
    def adresse(wert: str) -> str:
        return "".join(unicodedata.normalize("NFC", wert or "").split()).casefold()


@dataclass(slots=True)
class Pruefkontext:
    """Gemeinsame Hilfsmittel für alle Prüfungen eines Laufs."""

    normalisierer: Textnormalisierer
    referenz: ReferenzRepository | None = None
    abbruch: threading.Event | None = None

    def pruefe_abbruch(self) -> None:
        if self.abbruch is not None and self.abbruch.is_set():
            raise VorgangAbgebrochenFehler("Der Vorgang wurde abgebrochen.")


class Pruefung(ABC):
    """Basisklasse einer einzelnen Prüfung."""

    bezeichnung: str = ""

    @abstractmethod
    def pruefe(self, daten: Sequence[EplanDatensatz], kontext: Pruefkontext) -> list[Pruefungsfehler]:
        """Liefert alle Fehler dieser Prüfung."""


def _seite_text(datensatz: EplanDatensatz) -> str:
    return datensatz.seite_anzeige or "unbekannt"


class _DoppeltePruefung(Pruefung):
    """Gemeinsame Logik der drei Duplikatprüfungen (Template-Methode)."""

    fehlertyp: Fehlertyp

    @abstractmethod
    def _schluessel(self, datensatz: EplanDatensatz, kontext: Pruefkontext) -> str:
        """Vergleichsschlüssel; leer = Datensatz wird nicht betrachtet."""

    @abstractmethod
    def _beschreibung(self, datensatz: EplanDatensatz, erster: EplanDatensatz) -> str:
        """Fehlertext für ein Duplikat."""

    def pruefe(self, daten: Sequence[EplanDatensatz], kontext: Pruefkontext) -> list[Pruefungsfehler]:
        erste: dict[str, EplanDatensatz] = {}
        fehler: list[Pruefungsfehler] = []
        for zaehler, datensatz in enumerate(daten):
            if zaehler % 5000 == 0:
                kontext.pruefe_abbruch()
            schluessel = self._schluessel(datensatz, kontext)
            if not schluessel:
                continue
            erster = erste.setdefault(schluessel, datensatz)
            if erster is not datensatz:
                fehler.append(Pruefungsfehler(
                    self.fehlertyp, datensatz, self._beschreibung(datensatz, erster), erster))
        return fehler


class DoppelteSymbolePruefung(_DoppeltePruefung):
    """Prüfung 1: mehrfach vergebene symbolische Adressen."""

    bezeichnung = "Doppelte Symbole"
    fehlertyp = Fehlertyp.DOPPELTES_SYMBOL

    def _schluessel(self, datensatz: EplanDatensatz, kontext: Pruefkontext) -> str:
        return kontext.normalisierer.text(datensatz.symbolische_adresse)

    def _beschreibung(self, datensatz: EplanDatensatz, erster: EplanDatensatz) -> str:
        return (f"Symbolische Adresse „{datensatz.symbolische_adresse}“ bereits auf Seite "
                f"{_seite_text(erster)} verwendet.")


class DoppelteFunktionstextePruefung(_DoppeltePruefung):
    """Prüfung 2: gleiche Funktionstexte (Langtexte)."""

    bezeichnung = "Doppelte Funktionstexte"
    fehlertyp = Fehlertyp.DOPPELTER_FUNKTIONSTEXT

    def _schluessel(self, datensatz: EplanDatensatz, kontext: Pruefkontext) -> str:
        return kontext.normalisierer.text(datensatz.funktionstext)

    def _beschreibung(self, datensatz: EplanDatensatz, erster: EplanDatensatz) -> str:
        return (f"Funktionstext „{datensatz.funktionstext}“ bereits auf Seite "
                f"{_seite_text(erster)} verwendet.")


class DoppelteAdressenPruefung(_DoppeltePruefung):
    """Prüfung 3: doppelt vergebene SPS-Adressen."""

    bezeichnung = "Doppelte SPS-Adressen"
    fehlertyp = Fehlertyp.DOPPELTE_ADRESSE

    def _schluessel(self, datensatz: EplanDatensatz, kontext: Pruefkontext) -> str:
        return kontext.normalisierer.adresse(datensatz.sps_adresse)

    def _beschreibung(self, datensatz: EplanDatensatz, erster: EplanDatensatz) -> str:
        return f"Adresse bereits auf Seite {_seite_text(erster)} verwendet."


class PlatzhalterPruefung(Pruefung):
    """Prüfung 4: Fragezeichen in Symbol oder Funktionstext."""

    bezeichnung = "Platzhalter (Fragezeichen)"

    def pruefe(self, daten: Sequence[EplanDatensatz], kontext: Pruefkontext) -> list[Pruefungsfehler]:
        fehler: list[Pruefungsfehler] = []
        for zaehler, datensatz in enumerate(daten):
            if zaehler % 5000 == 0:
                kontext.pruefe_abbruch()
            im_symbol = any(z in datensatz.symbolische_adresse for z in PLATZHALTERZEICHEN)
            im_text = any(z in datensatz.funktionstext for z in PLATZHALTERZEICHEN)
            if not (im_symbol or im_text):
                continue
            if im_symbol and im_text:
                beschreibung = "Symbolische Adresse und Funktionstext enthalten einen Platzhalter (?)."
            elif im_symbol:
                beschreibung = "Symbolische Adresse enthält einen Platzhalter (?)."
            else:
                beschreibung = "Funktionstext enthält einen Platzhalter (?)."
            fehler.append(Pruefungsfehler(Fehlertyp.PLATZHALTER, datensatz, beschreibung))
        return fehler


class DatenbankAbgleichPruefung(Pruefung):
    """
    Prüfung 5: Abgleich mit der Referenzdatenbank.

    Je Datensatz genau eines von vier Ergebnissen:

    * Symbol und Funktionstext als Paar vorhanden        -> in Ordnung
    * Symbol fehlt in der Datenbank                       -> Fehler
    * Symbol vorhanden, Funktionstext fehlt               -> Fehler
    * beide einzeln vorhanden, aber nicht als Kombination -> Fehler

    Die Referenzdaten werden einmal komplett geladen und in Python mit derselben
    Normalisierung wie die übrigen Prüfungen verglichen. So hängt das Ergebnis
    nicht von der Kollation der Datenbank ab.
    """

    bezeichnung = "Datenbankabgleich"

    def pruefe(self, daten: Sequence[EplanDatensatz], kontext: Pruefkontext) -> list[Pruefungsfehler]:
        if kontext.referenz is None:
            raise PruefungUebersprungenFehler("Die Referenzdatenbank ist nicht konfiguriert.")
        norm = kontext.normalisierer
        referenz = kontext.referenz.lade_referenzdaten(kontext.abbruch)

        texte_je_symbol: dict[str, set[str]] = {}
        alle_texte: set[str] = set()
        for symbol, text in referenz:
            n_symbol, n_text = norm.text(symbol), norm.text(text)
            texte_je_symbol.setdefault(n_symbol, set()).add(n_text)
            alle_texte.add(n_text)

        fehler: list[Pruefungsfehler] = []
        for zaehler, datensatz in enumerate(daten):
            if zaehler % 5000 == 0:
                kontext.pruefe_abbruch()
            symbol = datensatz.symbolische_adresse
            text = datensatz.funktionstext
            n_symbol, n_text = norm.text(symbol), norm.text(text)

            if not n_symbol or n_symbol not in texte_je_symbol:
                zusatz = "" if (n_text and n_text in alle_texte) else (
                    " Auch der Funktionstext ist in der Datenbank nicht vorhanden." if n_text else "")
                beschreibung = (
                    "Symbolische Adresse ist leer; ein Abgleich mit der Datenbank ist nicht möglich."
                    if not n_symbol else
                    f"Symbol „{symbol}“ fehlt in der Referenzdatenbank." + zusatz)
                fehler.append(Pruefungsfehler(Fehlertyp.SYMBOL_NICHT_IN_DB, datensatz, beschreibung))
            elif not n_text or n_text not in alle_texte:
                beschreibung = (
                    "Funktionstext ist leer; ein Abgleich mit der Datenbank ist nicht möglich."
                    if not n_text else
                    f"Funktionstext „{text}“ fehlt in der Referenzdatenbank.")
                fehler.append(Pruefungsfehler(Fehlertyp.FUNKTIONSTEXT_NICHT_IN_DB, datensatz, beschreibung))
            elif n_text not in texte_je_symbol[n_symbol]:
                fehler.append(Pruefungsfehler(
                    Fehlertyp.KOMBINATION_ABWEICHEND, datensatz,
                    f"Symbol „{symbol}“ und Funktionstext „{text}“ sind einzeln in der Datenbank "
                    "vorhanden, gehören dort aber nicht zusammen."))
        _log.info("Datenbankabgleich: %d Referenzzeilen, %d Abweichungen", len(referenz), len(fehler))
        return fehler


# ---------------------------------------------------------------------------
# Ablaufsteuerung
# ---------------------------------------------------------------------------

def standard_pruefungen(einstellungen: PruefEinstellungen) -> list[Pruefung]:
    """Erzeugt die laut Einstellung aktiven Prüfungen in fester Reihenfolge."""
    kandidaten = (
        (einstellungen.doppelte_symbole, DoppelteSymbolePruefung),
        (einstellungen.doppelte_funktionstexte, DoppelteFunktionstextePruefung),
        (einstellungen.doppelte_adressen, DoppelteAdressenPruefung),
        (einstellungen.platzhalter, PlatzhalterPruefung),
        (einstellungen.datenbankabgleich, DatenbankAbgleichPruefung),
    )
    return [klasse() for aktiv, klasse in kandidaten if aktiv]


class Pruefmanager:
    """Führt eine Folge von Prüfungen aus und sammelt Ergebnis und Hinweise."""

    def __init__(self, pruefungen: Sequence[Pruefung], gross_klein_beachten: bool = False) -> None:
        self._pruefungen = list(pruefungen)
        self._normalisierer = Textnormalisierer(gross_klein_beachten)

    def pruefe(self, daten: Sequence[EplanDatensatz], referenz: ReferenzRepository | None = None,
               fortschritt: Callable[[str, int, int], None] | None = None,
               abbruch: threading.Event | None = None) -> Pruefbericht:
        kontext = Pruefkontext(self._normalisierer, referenz, abbruch)
        bericht = Pruefbericht(anzahl_datensaetze=len(daten))
        _log.info("Prüfung gestartet: %d Datensätze, %d Prüfungen", len(daten), len(self._pruefungen))

        for index, pruefung in enumerate(self._pruefungen, start=1):
            kontext.pruefe_abbruch()
            if fortschritt:
                fortschritt(f"Prüfe: {pruefung.bezeichnung}", index - 1, len(self._pruefungen))
            try:
                gefunden = pruefung.pruefe(daten, kontext)
            except VorgangAbgebrochenFehler:
                raise
            except PruefungUebersprungenFehler as grund:
                bericht.uebersprungen[pruefung.bezeichnung] = str(grund)
                _log.warning("Prüfung „%s“ übersprungen: %s", pruefung.bezeichnung, grund)
                continue
            except EplanPrueferFehler as fehler:
                bericht.uebersprungen[pruefung.bezeichnung] = str(fehler)
                _log.error("Prüfung „%s“ fehlgeschlagen: %s", pruefung.bezeichnung, fehler)
                continue
            bericht.ausgefuehrt.append(pruefung.bezeichnung)
            bericht.fehler.extend(gefunden)
            _log.info("Prüfung „%s“: %d Fehler", pruefung.bezeichnung, len(gefunden))

        bericht.fehler.sort(key=lambda f: (f.datensatz.laufende_nummer, list(Fehlertyp).index(f.fehlertyp)))
        _log.info("Prüfung beendet: %d Fehler insgesamt, %d Prüfungen übersprungen",
                  len(bericht.fehler), len(bericht.uebersprungen))
        return bericht
