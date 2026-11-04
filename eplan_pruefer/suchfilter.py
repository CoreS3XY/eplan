"""
suchfilter.py – Wildcard-Filter mit positiven und negativen Ausdrücken.

Syntax eines Filterausdrucks (Groß-/Kleinschreibung wird ignoriert):

* ``*``  beliebig viele Zeichen (auch keines),  ``?``  genau ein Zeichen
* Mehrere Muster werden mit ``;`` getrennt und sind ODER-verknüpft (positive Muster).
* Ein vorangestelltes ``!`` macht ein Muster **negativ**: Treffer werden ausgeschlossen.
* Ein Muster **ohne** ``*``/``?`` bedeutet „enthält"; ein Muster **mit** Platzhaltern muss den
  ganzen Text beschreiben (``Motor*`` = beginnt mit „Motor", ``*Stop`` = endet mit „Stop",
  ``*Stop*`` = enthält „Stop").
* ``\\*``, ``\\?``, ``\\!``, ``\\;`` und ``\\\\`` stehen für das Zeichen selbst
  (z. B. ``\\?`` findet ein Fragezeichen).

Ein Text wird angezeigt, wenn **kein** negatives Muster trifft **und** (es keine positiven Muster gibt
**oder** mindestens eines trifft). Beispiele:

* ``Motor*;!*Stop``   Texte, die mit „Motor" beginnen, aber nicht auf „Stop" enden
* ``!E0.*``           alles außer Adressen, die mit „E0." beginnen
* ``!?*``             nur leere Zellen
* ``\\?``              Texte mit einem Fragezeichen
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

HILFETEXT = (
    "Platzhalter: * = beliebig viele Zeichen, ? = genau ein Zeichen.\n"
    "Mehrere Muster mit ; trennen (ODER). Ein ! davor schließt Treffer aus.\n"
    "Text ohne Platzhalter = „enthält“; mit Platzhaltern muss das Muster den ganzen Text beschreiben.\n"
    "Beispiele:  Motor*;!*Stop   ·   !E0.*   ·   *Prozessdaten*   ·   !?* (nur leere)   ·   \\? (Fragezeichen)\n"
    "Mit \\* \\? \\! \\; sucht man das Zeichen selbst."
)


_ANY = object()          # „?"  genau ein Zeichen
_STAR = object()         # „*"  beliebig viele Zeichen


def _passt(muster: tuple, text: str) -> bool:
    """
    Vollständiger Wildcard-Vergleich ohne Regex.

    Iterativer Zwei-Zeiger-Algorithmus mit Rücksprung nur zum letzten ``*``: Der Aufwand ist
    höchstens ``len(text) * len(muster)``. Ein Regex-Übersetzen von ``*`` nach ``.*`` hätte bei vielen
    Sternen exponentielles Backtracking (ReDoS) und könnte die Oberfläche einfrieren.
    """
    t = p = 0
    stern = -1
    marke = 0
    while t < len(text):
        if p < len(muster) and muster[p] is _STAR:
            stern, marke = p, t
            p += 1
        elif p < len(muster) and (muster[p] is _ANY or muster[p] == text[t]):
            t += 1
            p += 1
        elif stern != -1:
            p = stern + 1
            marke += 1
            t = marke
        else:
            return False
    while p < len(muster) and muster[p] is _STAR:
        p += 1
    return p == len(muster)


@dataclass(frozen=True, slots=True)
class _Muster:
    teile: tuple                    # Zeichen sowie _ANY/_STAR
    ganz: bool                      # True: vollständiger Treffer, False: „enthält"
    wortlaut: str                   # nur für „enthält"

    def trifft(self, text: str) -> bool:
        gefaltet = text.casefold()
        return _passt(self.teile, gefaltet) if self.ganz else self.wortlaut in gefaltet


def _zerlege(ausdruck: str) -> list[str]:
    """Trennt an unmaskierten ``;``; Maskierungen bleiben für das Übersetzen erhalten."""
    teile, aktuell, i = [], [], 0
    while i < len(ausdruck):
        zeichen = ausdruck[i]
        if zeichen == "\\" and i + 1 < len(ausdruck):
            aktuell.append(ausdruck[i:i + 2])
            i += 2
            continue
        if zeichen == ";":
            teile.append("".join(aktuell))
            aktuell = []
        else:
            aktuell.append(zeichen)
        i += 1
    teile.append("".join(aktuell))
    return teile


def _uebersetze(muster: str) -> _Muster:
    teile: list = []
    mit_platzhalter = False
    i = 0
    while i < len(muster):
        zeichen = muster[i]
        if zeichen == "\\" and i + 1 < len(muster):
            teile.extend(muster[i + 1].casefold())
            i += 2
            continue
        if zeichen == "*":
            if not teile or teile[-1] is not _STAR:          # aufeinanderfolgende Sterne zusammenfassen
                teile.append(_STAR)
            mit_platzhalter = True
        elif zeichen == "?":
            teile.append(_ANY)
            mit_platzhalter = True
        else:
            teile.extend(zeichen.casefold())
        i += 1
    wortlaut = "".join(t for t in teile if isinstance(t, str))
    return _Muster(tuple(teile), mit_platzhalter, wortlaut)


class WildcardFilter:
    """Ein ausgewerteter Filterausdruck (siehe Moduldokumentation)."""

    def __init__(self, ausdruck: str = "") -> None:
        self.ausdruck = ausdruck or ""
        self._positiv: list[_Muster] = []
        self._negativ: list[_Muster] = []
        for teil in _zerlege(self.ausdruck):
            teil = teil.strip()
            if not teil:
                continue
            if teil.startswith("!"):
                rest = teil[1:].strip()
                if rest:
                    self._negativ.append(_uebersetze(rest))
            else:
                self._positiv.append(_uebersetze(teil))

    @property
    def aktiv(self) -> bool:
        return bool(self._positiv or self._negativ)

    def trifft(self, text: str) -> bool:
        """Soll ein Text mit diesem Filter angezeigt werden?"""
        text = text or ""
        if any(m.trifft(text) for m in self._negativ):
            return False
        return not self._positiv or any(m.trifft(text) for m in self._positiv)

    def trifft_zeile(self, texte: Iterable[str]) -> bool:
        """
        Filter über mehrere Zellen einer Zeile (Gesamtsuche): ein negatives Muster, das in
        irgendeiner Zelle trifft, schließt die Zeile aus; ein positives Muster muss in
        irgendeiner Zelle treffen.
        """
        zellen = [t or "" for t in texte]
        if any(m.trifft(z) for m in self._negativ for z in zellen):
            return False
        return not self._positiv or any(m.trifft(z) for m in self._positiv for z in zellen)
