"""
dxf_parser.py – Einlesen von DXF-Dateien (eine Datei entspricht einer Seite des E-Plans).

Das ASCII-DXF-Format ist öffentlich spezifiziert (Paare aus Gruppencode und Wert). *Wo* EPLAN die
SPS-Anschlüsse in der DXF ablegt, hängt jedoch vom Export ab. Der Extraktor unterstützt deshalb
zwei Darstellungen und nutzt die konfigurierbare Feldzuordnung:

A. **Attribute** (so schreibt EPLAN): Ein ``INSERT`` (Symbolblock) trägt ``ATTRIB``-Einträge, deren
   Namen die Form ``<Objekttyp>_<Eigenschaftsnummer>`` haben, z. B. ``17_20400`` (SPS-Adresse),
   ``17_20402`` (symbolische Adresse), ``17_20031`` (Funktionstext), ``17_20006`` (vollständiges BMK).
   Lange Texte stehen in Fortsetzungs-Attributen (``17_20031/1`` …) und werden zusammengefügt.
   Je Block mit SPS-Adresse oder symbolischer Adresse entsteht ein Datensatz. Die Seitendaten
   stehen in einem anderen Block (Rahmen ``EPLFRAME``: ``4_11009`` Seitenreferenz, ``4_11011``
   Seitenname) und gelten für alle Datensätze der Datei.
B. **Texttabelle**: ``TEXT``/``MTEXT``-Objekte bilden eine Tabelle mit Kopfzeile (z. B. „Anschl.",
   „BMK", „Funktionstext", „Symbolische Adresse", „Adresse"). Die Spalten ergeben sich aus den
   X-Positionen der Kopfzeile, die Zeilen aus den Y-Positionen.

Liefert eine DXF-Datei keine Datensätze, wird ihr Aufbau (Entitätsarten, Attribut-Namen mit
Beispielwerten, Texte) in den Strukturbericht geschrieben, damit die Feldzuordnung angepasst oder
der Extraktor erweitert werden kann.

Seitenangaben: aus den Seitenattributen der Datei; fehlen sie, aus dem Dateinamen
(``_N2_E2_1.dxf`` -> ``=N2+E2/1``, Seitennummer ``1``). Der Projektbaum (Unterordner) steht in der Fundstelle.

Nicht unterstützt: Binär-DXF (wird mit klarer Meldung abgelehnt).
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from .eplan_parser import (
    DatensatzExtraktor,
    Feldzuordner,
    LeseKontext,
    Teildatei,
    _dekodiere_text,
    erzeuge_datensatz,
)
from .logging import hole_protokollierer
from .modelle import EplanDatensatz, EplanDateiFehler

_log = hole_protokollierer("dxf")

MAX_DIAGNOSEN = 3                  # so viele DXF-Dateien ohne Treffer werden ausführlich beschrieben
_SEITENNUMMER = re.compile(r"^\d+(?:\.\w+)?$")
#: EPLAN legt lange Texte in Fortsetzungs-Attributen ab: ``17_20031`` + ``17_20031/1`` + ``17_20031/2`` …
_ATTRIBUTNAME = re.compile(r"^(?P<basis>.+?)(?:/(?P<fortsetzung>\d+))?$")
_SEITENFELDER = ("seitenreferenz", "seitennummer", "seitenname")


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def bereinige_dxf_text(text: str) -> str:
    """Entfernt DXF-/MTEXT-Formatierungen und wandelt Escape-Folgen in Zeichen um."""
    text = re.sub(r"\\U\+([0-9A-Fa-f]{4})", lambda m: chr(int(m.group(1), 16)), text)
    text = text.replace("\\P", " ").replace("\\~", " ")
    text = re.sub(r"\\[A-Za-z][^;\\]*;", "", text)         # \fArial|b0;  \C1;  \H2.5x;  \A1;
    text = re.sub(r"\\[LlOoKk]", "", text)                 # Unterstreichen/Durchstreichen ein/aus
    text = text.replace("{", "").replace("}", "")
    text = text.replace("%%d", "°").replace("%%p", "±").replace("%%c", "⌀").replace("%%%", "%")
    text = text.replace("\\\\", "\\")
    return " ".join(text.split())


def leite_seite_aus_dateiname(name: str) -> tuple[str, str]:
    """
    ``_N2_E2_1.dxf`` -> (``=N2+E2/1``, ``1``); ``Seite_5.dxf`` -> (``=Seite/5``, ``5``).
    Liefert leere Texte, wenn der Name keine abschließende Seitennummer enthält.
    """
    stamm = Path(name.replace("!", "/")).stem
    teile = [t for t in stamm.split("_") if t]
    if not teile or not _SEITENNUMMER.match(teile[-1]):
        return "", ""
    nummer = teile[-1]
    vorne = teile[:-1]
    if not vorne:
        return "", nummer
    referenz = "=" + vorne[0] + "".join("+" + t for t in vorne[1:]) + "/" + nummer
    return referenz, nummer


@dataclass(slots=True)
class _Entitaet:
    art: str
    zeile: int
    gruppen: list[tuple[int, str]] = field(default_factory=list)

    def erste(self, code: int, standard: str = "") -> str:
        for c, wert in self.gruppen:
            if c == code:
                return wert
        return standard

    def zahl(self, code: int, standard: float = 0.0) -> float:
        try:
            return float(self.erste(code, "").strip())
        except ValueError:
            return standard


@dataclass(slots=True)
class _Text:
    x: float
    y: float
    hoehe: float
    text: str
    zeile: int


def _attribute_zu_feldern(attribute: list[_Entitaet], zuordner: Feldzuordner,
                          beispiele: dict[str, list[str]]) -> dict[str, str]:
    """Ordnet die Attribute eines Blocks den Feldern zu; Fortsetzungs-Attribute (``…/1``) werden angehängt."""
    teile: dict[str, list[tuple[int, int, str]]] = {}
    for reihenfolge, attribut in enumerate(attribute):
        marke = attribut.erste(2).strip()
        wert = bereinige_dxf_text(attribut.erste(1))
        if marke:
            gesehen = beispiele.setdefault(marke, [])
            if wert and len(gesehen) < 3 and wert not in gesehen:
                gesehen.append(wert)
        if not (marke and wert):
            continue
        treffer = _ATTRIBUTNAME.match(marke)
        basis = treffer.group("basis") if treffer else marke
        feld = zuordner.feld_fuer(basis) or zuordner.feld_fuer(marke)
        if feld:
            fortsetzung = int(treffer.group("fortsetzung") or 0) if treffer else 0
            teile.setdefault(feld, []).append((fortsetzung, reihenfolge, wert))
    return {feld: " ".join(w for _, _, w in sorted(werte)) for feld, werte in teile.items()}


def _entitaeten(text: str) -> Iterator[_Entitaet]:
    """Liest die Entitäten des Abschnitts ENTITIES (Blockdefinitionen werden übersprungen)."""
    zeilen = re.split(r"\r?\n", text)
    abschnitt = ""
    erwarte_namen = False
    aktuell: _Entitaet | None = None
    for index in range(0, len(zeilen) - 1, 2):
        try:
            code = int(zeilen[index].strip())
        except ValueError as fehler:
            raise EplanDateiFehler(
                f"Keine gültige DXF-Struktur (Zeile {index + 1}: „{zeilen[index][:30]}“ ist kein Gruppencode).") from fehler
        wert = zeilen[index + 1]
        if code == 0:
            if aktuell is not None:
                yield aktuell
                aktuell = None
            marke = wert.strip()
            if marke == "SECTION":
                erwarte_namen, abschnitt = True, ""
            elif marke == "ENDSEC":
                abschnitt = ""
            elif marke == "EOF":
                return
            elif abschnitt == "ENTITIES":
                aktuell = _Entitaet(marke, index + 1)
        elif code == 2 and erwarte_namen:
            abschnitt, erwarte_namen = wert.strip(), False
        elif aktuell is not None:
            aktuell.gruppen.append((code, wert))
    if aktuell is not None:
        yield aktuell


def _als_text(entitaet: _Entitaet) -> _Text | None:
    if entitaet.art == "MTEXT":
        inhalt = "".join(w for c, w in entitaet.gruppen if c == 3) + entitaet.erste(1)
    else:
        inhalt = entitaet.erste(1)
    inhalt = bereinige_dxf_text(inhalt)
    if not inhalt:
        return None
    x, y = entitaet.zahl(10), entitaet.zahl(20)
    if entitaet.art == "TEXT" and entitaet.erste(72, "0").strip() not in ("0", "") and entitaet.erste(11):
        x, y = entitaet.zahl(11), entitaet.zahl(21)        # ausgerichteter Text: Ankerpunkt
    return _Text(x, y, entitaet.zahl(40, 0.0), inhalt, entitaet.zeile)


# ---------------------------------------------------------------------------
# Texttabellen
# ---------------------------------------------------------------------------

def _zeilen_bilden(texte: list[_Text]) -> tuple[list[list[_Text]], float]:
    """Gruppiert Texte zu Zeilen (gleiche Y-Lage, von oben nach unten, innen von links nach rechts)."""
    hoehen = sorted(t.hoehe for t in texte if t.hoehe > 0)
    typisch = hoehen[len(hoehen) // 2] if hoehen else 2.5
    toleranz = typisch * 0.6
    zeilen: list[list[_Text]] = []
    for t in sorted(texte, key=lambda t: -t.y):
        if zeilen and abs(zeilen[-1][0].y - t.y) <= toleranz:
            zeilen[-1].append(t)
        else:
            zeilen.append([t])
    for zeile in zeilen:
        zeile.sort(key=lambda t: t.x)
    return zeilen, typisch


def _kopf_felder(zeile: list[_Text], zuordner: Feldzuordner) -> list[tuple[float, str | None]]:
    """Spalten einer möglichen Kopfzeile als (x, Feld oder None); leer, wenn die Zeile keine Kopfzeile ist."""
    spalten = [(t.x, zuordner.feld_fuer(t.text)) for t in zeile]
    felder = {f for _, f in spalten if f}
    if len(felder) >= 2 and ("sps_adresse" in felder or "symbolische_adresse" in felder):
        return spalten
    return []


def _tabellen_datensaetze(texte: list[_Text], zuordner: Feldzuordner, teildatei: str,
                          kontext: LeseKontext, seite: dict[str, str]) -> Iterator[EplanDatensatz]:
    zeilen, typisch = _zeilen_bilden(texte)
    toleranz_x = typisch * 1.5
    maximale_luecke = typisch * 8
    index = 0
    while index < len(zeilen):
        spalten = _kopf_felder(zeilen[index], zuordner)
        if not spalten:
            index += 1
            continue
        letzte_y = zeilen[index][0].y
        index += 1
        while index < len(zeilen):
            zeile = zeilen[index]
            if _kopf_felder(zeile, zuordner) or letzte_y - zeile[0].y > maximale_luecke:
                break                                       # nächste Tabelle bzw. Tabellenende
            letzte_y = zeile[0].y
            zellen: dict[int, list[str]] = {}
            for t in zeile:
                spaltenindex = max((i for i, (x, _) in enumerate(spalten) if x <= t.x + toleranz_x), default=-1)
                if spaltenindex >= 0:
                    zellen.setdefault(spaltenindex, []).append(t.text)
            felder = dict(seite)
            for spaltenindex, teile in zellen.items():
                feld = spalten[spaltenindex][1]
                if feld:
                    felder[feld] = " ".join(teile)
            datensatz = erzeuge_datensatz(felder, kontext.fundstelle(teildatei, zeile[0].zeile, "Texttabelle"))
            if datensatz is not None:
                yield datensatz
            index += 1


# ---------------------------------------------------------------------------
# Extraktor
# ---------------------------------------------------------------------------

class DxfExtraktor(DatensatzExtraktor):
    """Liest SPS-Anschlüsse aus ASCII-DXF-Dateien (Attribute oder Texttabelle, siehe Moduldokumentation)."""

    art = "DXF"

    def unterstuetzt(self, name: str, kopf: bytes) -> bool:
        return name.lower().endswith(".dxf")

    def extrahiere(self, teildatei: Teildatei, kontext: LeseKontext) -> Iterator[EplanDatensatz]:
        roh = teildatei.lese_alles()
        if roh[:22].startswith(b"AutoCAD Binary DXF"):
            raise EplanDateiFehler(
                f"„{teildatei.name}“ ist eine Binär-DXF. Bitte als ASCII-DXF exportieren/speichern.")
        text = _dekodiere_text(roh)

        referenz, nummer = leite_seite_aus_dateiname(teildatei.name)
        seite_namen = {"seitenreferenz": referenz, "seitennummer": nummer}      # Ersatz, falls die Datei nichts enthält
        seite_datei: dict[str, str] = {}                                        # Seitenattribute aus der Datei
        zuordner = kontext.zuordner

        texte: list[_Text] = []
        arten: Counter[str] = Counter()
        attribut_beispiele: dict[str, list[str]] = {}
        bloecke: Counter[str] = Counter()
        kandidaten: list[tuple[dict[str, str], int, str]] = []                  # (Felder, Zeile, Blockname)

        einfuegung: _Entitaet | None = None
        attribute: list[_Entitaet] = []

        def abschliessen() -> None:
            nonlocal einfuegung, attribute
            if einfuegung is not None and attribute:
                felder = _attribute_zu_feldern(attribute, zuordner, attribut_beispiele)
                if felder.get("sps_adresse") or felder.get("symbolische_adresse"):
                    kandidaten.append((felder, einfuegung.zeile, einfuegung.erste(2).strip()))
                else:                                   # z. B. Zeichnungsrahmen mit Seitenattributen
                    for name in _SEITENFELDER:
                        if felder.get(name):
                            seite_datei.setdefault(name, felder[name])
            einfuegung, attribute = None, []

        for zaehler, entitaet in enumerate(_entitaeten(text)):
            if zaehler % 5000 == 0:
                kontext.pruefe_abbruch()
            arten[entitaet.art] += 1
            if entitaet.art == "INSERT":
                abschliessen()
                einfuegung = entitaet
                bloecke[entitaet.erste(2).strip()] += 1
            elif entitaet.art == "ATTRIB" and einfuegung is not None:
                attribute.append(entitaet)
            else:
                abschliessen()
                if entitaet.art in ("TEXT", "MTEXT"):
                    gefundener_text = _als_text(entitaet)
                    if gefundener_text is not None:
                        texte.append(gefundener_text)
        abschliessen()

        # Seite: Attribute der Datei haben Vorrang vor dem Dateinamen
        seite = {**{k: v for k, v in seite_namen.items() if v}, **seite_datei}
        gefunden = 0
        for felder, zeile, blockname in kandidaten:
            datensatz = erzeuge_datensatz(
                {**seite, **felder}, kontext.fundstelle(teildatei.name, zeile, f"Block {blockname or '?'}"))
            if datensatz is not None:
                gefunden += 1
                yield datensatz

        aus_tabelle = 0
        for datensatz in _tabellen_datensaetze(texte, zuordner, teildatei.name, kontext, seite):
            aus_tabelle += 1
            yield datensatz

        if gefunden + aus_tabelle == 0 and len(kontext.diagnose) < MAX_DIAGNOSEN:
            kontext.diagnose.append(self._beschreibe(teildatei.name, arten, bloecke, attribut_beispiele, texte))
        _log.debug("%s: %d Datensätze aus Attributen, %d aus Texttabellen", teildatei.name, gefunden, aus_tabelle)

    @staticmethod
    def _beschreibe(name: str, arten: Counter[str], bloecke: Counter[str],
                    attribute: dict[str, list[str]], texte: list[_Text]) -> str:
        z = [f"DXF ohne erkannte SPS-Datensätze: {name}",
             "  Entitäten: " + (", ".join(f"{a} × {n}" for a, n in arten.most_common(12)) or "keine")]
        if bloecke:
            z.append("  Blöcke (INSERT): " + ", ".join(f"{b or '?'} × {n}" for b, n in bloecke.most_common(10)))
        if attribute:
            z.append("  Attribut-Namen mit Beispielwerten (Tag: Werte):")
            for marke, werte in list(attribute.items())[:20]:
                z.append(f"    {marke}: " + " | ".join(werte) if werte else f"    {marke}: (leer)")
        if texte:
            z.append(f"  Texte: {len(texte)}, erste Texte (x, y: Text):")
            z.extend(f"    {t.x:.1f}, {t.y:.1f}: {t.text[:70]}" for t in texte[:15])
        z.append("  Hinweis: Passende Attribut-Namen bzw. Überschriften unter Einstellungen ▸ Feldzuordnung eintragen.")
        return "\n".join(z)


def entitaets_auszug(text: str, zeile: int, max_entitaeten: int = 80) -> list[tuple[int, str]]:
    """
    Lesbare Zusammenfassung der DXF-Entitäten ab ``zeile`` (Quellauszug für „Sprung zur Fundstelle").

    Ein Attribut-Block belegt in EPLAN-DXF rund 500 Zeilen aus Gruppencodes; angezeigt werden daher je
    Entität nur Art, Name (Gruppe 2) und Wert (Gruppe 1), z. B. ``ATTRIB 17_20400 = A42.0``.
    Die Zusammenfassung endet nach ``SEQEND`` oder nach ``max_entitaeten`` Entitäten.
    """
    zeilen = re.split(r"\r?\n", text)
    ergebnis: list[tuple[int, str]] = []
    art, start, name, wert = "", 0, "", ""

    def abschliessen() -> None:
        if art:
            teile = [art]
            if name:
                teile.append(name)
            if wert:
                teile.append(f"= {bereinige_dxf_text(wert)[:120]}")
            ergebnis.append((start, " ".join(teile)))

    index = max(zeile - 1, 0)
    while index < len(zeilen) - 1 and len(ergebnis) < max_entitaeten:
        try:
            code = int(zeilen[index].strip())
        except ValueError:
            break
        inhalt = zeilen[index + 1]
        if code == 0:
            abschliessen()
            marke = inhalt.strip()
            if marke in ("SEQEND", "ENDSEC", "EOF"):
                if marke == "SEQEND":
                    ergebnis.append((index + 1, "SEQEND"))
                art = ""
                break
            art, start, name, wert = marke, index + 1, "", ""
        elif art and code == 2 and not name:
            name = inhalt.strip()
        elif art and code == 1 and not wert:
            wert = inhalt
        index += 2
    if art:                         # Schleife ohne SEQEND beendet: letzte Entität noch ausgeben
        abschliessen()
    return ergebnis
