"""Wildcard-Filter mit positiven und negativen Ausdrücken."""

from __future__ import annotations

import pytest

from eplan_pruefer.suchfilter import WildcardFilter


def treffer(ausdruck: str, *texte: str) -> list[str]:
    f = WildcardFilter(ausdruck)
    return [t for t in texte if f.trifft(t)]


def test_leerer_filter_ist_inaktiv_und_laesst_alles_durch():
    f = WildcardFilter("  ; ;")
    assert not f.aktiv and f.trifft("irgendwas") and f.trifft("")


@pytest.mark.parametrize("ausdruck,texte,erwartet", [
    ("Motor", ["Motor_Start", "Der Motor", "Lampe"], ["Motor_Start", "Der Motor"]),             # ohne Platzhalter: enthält
    ("motor", ["Motor_Start"], ["Motor_Start"]),                                                  # Schreibweise egal
    ("Motor*", ["Motor_Start", "Der Motor"], ["Motor_Start"]),                                  # beginnt mit
    ("*Stop", ["Motor_Stop", "Stop_Motor"], ["Motor_Stop"]),                                    # endet mit
    ("*Stop*", ["Motor_Stop", "Stop_Motor", "Lampe"], ["Motor_Stop", "Stop_Motor"]),
    ("E0.?", ["E0.0", "E0.15", "E0."], ["E0.0"]),                                               # genau ein Zeichen
    ("E?.?", ["E0.0", "E10.0"], ["E0.0"]),
    ("A*;E*", ["A1.0", "E1.0", "M1.0"], ["A1.0", "E1.0"]),                                      # ODER
    ("*", ["", "x"], ["", "x"]),
])
def test_positive_muster(ausdruck, texte, erwartet):
    assert treffer(ausdruck, *texte) == erwartet


@pytest.mark.parametrize("ausdruck,texte,erwartet", [
    ("!Stop", ["Motor_Stop", "Motor_Start"], ["Motor_Start"]),
    ("!E0.*", ["E0.0", "E1.0", "A0.0"], ["E1.0", "A0.0"]),
    ("!*Stop;!*Auto*", ["Motor_Stop", "Auto", "Start"], ["Start"]),                             # mehrere negative
    ("Motor*;!*Stop", ["Motor_Start", "Motor_Stop", "Lampe"], ["Motor_Start"]),                 # positiv und negativ
    ("!?*", ["", "x", "yy"], [""]),                                                              # nur leere Zellen
    ("?*", ["", "x"], ["x"]),                                                                    # nur gefüllte Zellen
    ("*;!*Stop", ["Motor_Stop", "Start"], ["Start"]),
    ("!", ["a"], ["a"]),                                                                         # nacktes ! ist wirkungslos
])
def test_negative_muster(ausdruck, texte, erwartet):
    assert treffer(ausdruck, *texte) == erwartet


def test_negatives_muster_gewinnt_gegen_positives():
    assert treffer("Motor*;!Motor_Stop", "Motor_Start", "Motor_Stop") == ["Motor_Start"]


@pytest.mark.parametrize("ausdruck,texte,erwartet", [
    ("\\?", ["Motor_?", "Motor_1"], ["Motor_?"]),                                                # Fragezeichen selbst
    ("*\\?", ["Motor_?", "?Motor"], ["Motor_?"]),
    ("\\*", ["a*b", "ab"], ["a*b"]),
    ("\\!", ["Achtung!", "ok"], ["Achtung!"]),
    ("a\;b", ["a;b", "a", "b"], ["a;b"]),
    ("\\\\", ["C:\\Pfad", "x"], ["C:\\Pfad"]),
    ("????", ["C3E?", "C3E??", "abcd"], ["C3E?", "abcd"]),                                      # Platzhalter-Symbole des Anwenders
    ("*????", ["C3EProzDat1PSU8600????", "Motor_1"], ["C3EProzDat1PSU8600????", "Motor_1"]),
])
def test_maskierung(ausdruck, texte, erwartet):
    assert treffer(ausdruck, *texte) == erwartet


def test_regex_sonderzeichen_werden_wortwoertlich_behandelt():
    assert treffer("a.b", "a.b", "axb") == ["a.b"]
    assert treffer("(x)", "(x)", "x") == ["(x)"]
    assert treffer("[a]*", "[a]b", "ab") == ["[a]b"]
    assert treffer("a+", "a+", "aa") == ["a+"]


def test_umlaute_und_zeilenumbrueche():
    assert treffer("förder*", "Förderband", "Foerderband") == ["Förderband"]
    assert treffer("a*b", "a\nb") == ["a\nb"]


def test_zeilenfilter_ueber_mehrere_zellen():
    f = WildcardFilter("Motor*;!*Stop")
    assert f.trifft_zeile(["x", "Motor_Start"])
    assert not f.trifft_zeile(["Motor_Start", "Förderband Stop"])       # negatives Muster trifft in anderer Zelle
    assert not f.trifft_zeile(["x", "y"])                               # kein positives Muster trifft
    assert WildcardFilter("!Stop").trifft_zeile(["Start", "Lampe"])
    assert not WildcardFilter("!Stop").trifft_zeile(["Start", "Motor Stop"])
    assert WildcardFilter("").trifft_zeile(["a"])


def test_sehr_lange_und_boesartige_muster_bleiben_schnell():
    import time
    muster = "*a" * 200 + "b"                       # mit Regex-Backtracking praktisch endlos
    start = time.perf_counter()
    assert not WildcardFilter(muster).trifft("a" * 2000)
    assert WildcardFilter("*a?" * 100).trifft("xa" * 400 + "x")        # Treffer, trotz vieler Sterne
    assert not WildcardFilter("*a?" * 100).trifft("xa" * 400)
    assert time.perf_counter() - start < 2.0
