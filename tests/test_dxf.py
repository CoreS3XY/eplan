"""DXF-Seiten in einem Projektbaum: Attribute und Texttabellen, Seite aus dem Dateinamen.

Die Testdateien folgen der öffentlichen ASCII-DXF-Spezifikation (Gruppencode/Wert-Paare). Wie ein echter
EPLAN-Export die Daten ablegt, ist damit NICHT belegt – siehe docs/ANALYSE.md."""

from __future__ import annotations

from pathlib import Path

import pytest

from eplan_pruefer.dxf_parser import bereinige_dxf_text, leite_seite_aus_dateiname
from eplan_pruefer.eplan_parser import EplanDateiLeser, QuellenAuszugLeser
from eplan_pruefer.modelle import Einstellungen, PruefEinstellungen
from eplan_pruefer.pruefer import Pruefmanager, standard_pruefungen


def paare(*gruppen) -> str:
    return "".join(f"{code:>3}\n{wert}\n" for code, wert in gruppen)


def dxf(*entitaeten: str, bloecke: str = "") -> str:
    kopf = paare((0, "SECTION"), (2, "HEADER"), (9, "$ACADVER"), (1, "AC1015"), (0, "ENDSEC"))
    ende = paare((0, "ENDSEC"), (0, "EOF"))
    blockteil = (paare((0, "SECTION"), (2, "BLOCKS")) + bloecke + paare((0, "ENDSEC"))) if bloecke else ""
    return kopf + blockteil + paare((0, "SECTION"), (2, "ENTITIES")) + "".join(entitaeten) + ende


def text(x: float, y: float, inhalt: str, hoehe: float = 2.5) -> str:
    return paare((0, "TEXT"), (8, "TEXT"), (10, x), (20, y), (30, 0), (40, hoehe), (1, inhalt))


def einfuegung(block: str, attribute: dict[str, str]) -> str:
    s = paare((0, "INSERT"), (8, "SPS"), (2, block), (10, 0), (20, 0), (66, 1))
    for marke, wert in attribute.items():
        s += paare((0, "ATTRIB"), (8, "SPS"), (10, 0), (20, 0), (40, 2.5), (1, wert), (2, marke), (70, 0))
    return s + paare((0, "SEQEND"))


# Beispiel des Anwenders (Seite _N2_E2_1.dxf)
ZEILEN = [
    ("1", "=G2+E2-KA42.0", "Spaenefoerderer vor", "N2ASpfVor", "A42.0"),
    ("2", "=G2+E2-KA42.0", "Spaenefoerderer zurueck", "N2ASpfZur", "A42.1"),
    ("9", "=G2+E2-KE44.0", "Spaenefoerderer Ruecklauf", "N2ESpfRcklf", "E45.0"),
    ("10", "=G2+E2-KE44.0", "Spaenefoerderer Automatik", "N2ESpfAuto", "E45.1"),
    ("11", "=G2+E2-KE44.0", "Spaenefoerderer nicht blockiert", "N2ESpfBlckN", "E45.2"),
    ("12", "=G2+E2-KE44.0", "Spaenefoerderer Leistungsschalter nicht ausgeloest", "N2ESpfLsAglN", "E45.3"),
]


def tabelle_als_dxf(zeilen=ZEILEN, y0: float = 200.0) -> str:
    spalten_x = [0, 15, 55, 125, 160]
    kopf = ["Anschl.", "BMK", "Funktionstext", "Symbolische Adresse", "Adresse"]
    s = "".join(text(x, y0, k) for x, k in zip(spalten_x, kopf, strict=True))
    for i, zeile in enumerate(zeilen, start=1):
        s += "".join(text(x, y0 - 8 * i, wert) for x, wert in zip(spalten_x, zeile, strict=True))
    return s


def lese(pfad: Path, einstellungen: Einstellungen | None = None):
    return EplanDateiLeser(einstellungen or Einstellungen()).lese(pfad)


@pytest.mark.parametrize("name,erwartet", [
    ("_N2_E2_1.dxf", ("=N2+E2/1", "1")), ("Projekt/N2/E2/_N2_E2_12.dxf", ("=N2+E2/12", "12")),
    ("_A_B_C_3.dxf", ("=A+B+C/3", "3")), ("Seite_5.dxf", ("=Seite/5", "5")), ("7.dxf", ("", "7")),
    ("Übersicht.dxf", ("", "")), ("x.zw1!ordner/_N2_E2_1.dxf", ("=N2+E2/1", "1")),
])
def test_seite_aus_dateiname(name, erwartet):
    assert leite_seite_aus_dateiname(name) == erwartet


def test_textbereinigung():
    assert bereinige_dxf_text(r"{\fArial|b0|i0;Sp\U+00E4nef\U+00F6rderer\Pvor}") == "Spänefördererer vor".replace("rerer", "rer")
    assert bereinige_dxf_text("20%%d C %%p1") == "20° C ±1"
    assert bereinige_dxf_text(r"\C1;rot\L unter\l") == "rot unter"


def test_texttabelle_wie_im_beispiel_des_anwenders(tmp_path):
    ordner = tmp_path / "Projekt" / "N2" / "E2"
    ordner.mkdir(parents=True)
    (ordner / "_N2_E2_1.dxf").write_text(dxf(tabelle_als_dxf()), encoding="cp1252")
    ergebnis = lese(tmp_path / "Projekt")
    d = ergebnis.datensaetze
    assert len(d) == 6 and not ergebnis.warnungen
    assert (d[0].symbolische_adresse, d[0].sps_adresse, d[0].funktionstext, d[0].betriebsmittelkennzeichen) == \
        ("N2ASpfVor", "A42.0", "Spaenefoerderer vor", "=G2+E2-KA42.0")
    assert d[5].funktionstext == "Spaenefoerderer Leistungsschalter nicht ausgeloest" and d[5].sps_adresse == "E45.3"
    assert (d[0].seitenreferenz, d[0].seitennummer) == ("=N2+E2/1", "1")
    assert d[0].fundstelle.als_text().startswith("Projekt › N2/E2/_N2_E2_1.dxf › Zeile ")
    assert ergebnis.teildateien[0].art == "DXF" and ergebnis.teildateien[0].datensaetze == 6


def test_attribute_an_bloecken(tmp_path):
    zeilen = [einfuegung("SPS_ANSCHLUSS", {"SYMBOLISCHE_ADRESSE": s, "FUNKTIONSTEXT": t, "BMK": b, "17_20400": a})
              for _, b, t, s, a in ZEILEN[:2]]
    pfad = tmp_path / "_N2_E2_4.dxf"
    pfad.write_text(dxf(*zeilen, einfuegung("OHNE", {"X": "y"}), text(0, 0, "Seitentitel")), encoding="utf-8")
    d = lese(pfad).datensaetze
    assert [(x.symbolische_adresse, x.sps_adresse, x.funktionstext) for x in d] == [
        ("N2ASpfVor", "A42.0", "Spaenefoerderer vor"), ("N2ASpfZur", "A42.1", "Spaenefoerderer zurueck")]
    assert d[0].seitenreferenz == "=N2+E2/4" and d[0].fundstelle.element == "Block SPS_ANSCHLUSS"


def test_blockdefinitionen_und_leere_attribute_liefern_nichts(tmp_path):
    bloecke = paare((0, "BLOCK"), (2, "SPS_ANSCHLUSS"), (0, "ATTDEF"), (2, "17_20400"), (1, "E0.0"), (0, "ENDBLK"))
    pfad = tmp_path / "_A_1.dxf"
    pfad.write_text(dxf(einfuegung("SPS_ANSCHLUSS", {"17_20400": ""}), bloecke=bloecke), encoding="utf-8")
    ergebnis = lese(pfad)
    assert ergebnis.datensaetze == []


def test_mtext_und_umlaute_cp1252(tmp_path):
    mtext = paare((0, "MTEXT"), (8, "T"), (10, 55), (20, 192), (40, 2.5), (1, r"{\fArial|b0;Spänefördererer\Pvor}"))
    kopf = "".join(text(x, 200, k) for x, k in zip([0, 55, 125, 160],
                                                  ["Anschl.", "Funktionstext", "Symbolische Adresse", "Adresse"], strict=True))
    zelle = text(0, 192, "1") + mtext + text(125, 192, "N2ASpfVor") + text(160, 192, "A42.0")
    pfad = tmp_path / "_N2_E2_2.dxf"
    pfad.write_bytes(dxf(kopf + zelle).encode("cp1252"))
    d = lese(pfad).datensaetze
    assert len(d) == 1 and d[0].funktionstext == "Spänefördererer vor"


def test_mehrere_seiten_pruefungen_nennen_die_seite(tmp_path):
    for name, adresse in (("_N2_E2_1.dxf", "A42.0"), ("_N2_E2_2.dxf", "A42.0")):
        (tmp_path / name).write_text(dxf(tabelle_als_dxf([("1", "=G2+E2-K1", "Text " + name, "Sym" + name[-5], adresse)])),
                                     encoding="utf-8")
    ergebnis = lese(tmp_path)
    assert [d.seitennummer for d in ergebnis.datensaetze] == ["1", "2"]
    bericht = Pruefmanager(standard_pruefungen(PruefEinstellungen(datenbankabgleich=False))).pruefe(ergebnis.datensaetze)
    adressfehler = [f for f in bericht.fehler if f.fehlertyp.name == "DOPPELTE_ADRESSE"]
    assert len(adressfehler) == 1 and adressfehler[0].beschreibung == "Adresse bereits auf Seite 1 verwendet."
    assert adressfehler[0].seite == "2"


def test_zwei_tabellen_auf_einer_seite_und_fremde_texte(tmp_path):
    erste = tabelle_als_dxf(ZEILEN[:2], y0=200)
    zweite = tabelle_als_dxf(ZEILEN[2:4], y0=80)
    pfad = tmp_path / "_A_B_3.dxf"
    pfad.write_text(dxf(text(0, 400, "Zeichnungskopf"), erste, text(0, 150, "Bemerkung"), zweite), encoding="utf-8")
    assert [x.sps_adresse for x in lese(pfad).datensaetze] == ["A42.0", "A42.1", "E45.0", "E45.1"]


def test_dxf_ohne_treffer_liefert_diagnose(tmp_path):
    pfad = tmp_path / "_N2_E2_9.dxf"
    pfad.write_text(dxf(einfuegung("SYMBOL", {"EPLAN999": "wert", "EPLAN998": "x"}), text(10, 10, "Titel")),
                    encoding="utf-8")
    ergebnis = lese(pfad)
    assert ergebnis.datensaetze == []
    assert "EPLAN999: wert" in ergebnis.strukturbericht and "Blöcke (INSERT): SYMBOL × 1" in ergebnis.strukturbericht
    assert "10.0, 10.0: Titel" in ergebnis.strukturbericht
    assert any("Feldzuordnung" in w for w in ergebnis.warnungen)


def test_feldzuordnung_fuer_eigene_attributnamen(tmp_path):
    pfad = tmp_path / "_A_1.dxf"
    pfad.write_text(dxf(einfuegung("B", {"EPLAN100": "Sym1", "EPLAN200": "E1.0", "EPLAN300": "Text"})), encoding="utf-8")
    assert lese(pfad).datensaetze == []
    e = Einstellungen()
    e.feldzuordnung = {**e.feldzuordnung, "symbolische_adresse": ("EPLAN100",), "sps_adresse": ("EPLAN200",),
                       "funktionstext": ("EPLAN300",)}
    d = lese(pfad, e).datensaetze
    assert [(x.symbolische_adresse, x.sps_adresse, x.funktionstext) for x in d] == [("Sym1", "E1.0", "Text")]


def test_binaer_dxf_wird_abgelehnt(tmp_path):
    pfad = tmp_path / "_A_1.dxf"
    pfad.write_bytes(b"AutoCAD Binary DXF\r\n\x1a\x00" + b"\x00" * 50)
    ergebnis = lese(pfad)
    assert ergebnis.datensaetze == [] and any("Binär-DXF" in w for w in ergebnis.warnungen)


def test_kaputte_dxf_liefert_warnung_statt_absturz(tmp_path):
    pfad = tmp_path / "_A_1.dxf"
    pfad.write_text("kein dxf\nirgendwas\n", encoding="utf-8")
    ergebnis = lese(pfad)
    assert ergebnis.datensaetze == [] and any("DXF" in w for w in ergebnis.warnungen)


def test_quellauszug_zeigt_dxf_zeilen(tmp_path):
    ordner = tmp_path / "P" / "E2"
    ordner.mkdir(parents=True)
    (ordner / "_N2_E2_1.dxf").write_text(dxf(tabelle_als_dxf()), encoding="utf-8")
    ergebnis = lese(tmp_path / "P")
    d = ergebnis.datensaetze[2]
    auszug = QuellenAuszugLeser().lese(tmp_path / "P", d.fundstelle, umgebung=3)
    assert auszug.markierte_zeile == d.fundstelle.zeile
    assert auszug.zeilen[0][0] == d.fundstelle.zeile and auszug.zeilen[0][1].startswith("TEXT")
    assert any("= N2ESpfRcklf" in zeile for _, zeile in auszug.zeilen)      # ganze Tabellenzeile sichtbar


def test_ordner_in_der_oberflaeche(tmp_path, monkeypatch):
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication
    from eplan_pruefer import gui
    from eplan_pruefer.einstellungen import EinstellungsVerwalter
    from .test_gui import warte

    app = QApplication.instance() or QApplication([])
    (tmp_path / "P" / "E2").mkdir(parents=True)
    (tmp_path / "P" / "E2" / "_N2_E2_1.dxf").write_text(dxf(tabelle_als_dxf()), encoding="utf-8")
    monkeypatch.setattr(gui, "zeige_information", lambda *a: None)
    monkeypatch.setattr(gui.QFileDialog, "getExistingDirectory", lambda *a, **k: str(tmp_path / "P"))
    e = Einstellungen()
    e.pruefung.datenbankabgleich = False
    fenster = gui.Hauptfenster(EinstellungsVerwalter(tmp_path / "k.ini"), e, None)
    try:
        fenster.ordner_oeffnen()
        warte(fenster)
        assert app is not None and fenster._datensatz_modell.rowCount() == 6
        assert fenster._datensatz_modell.index(0, 4).data() == "1"
    finally:
        fenster.close()


# ---------------------------------------------------------------------------
# Aufbau einer echten EPLAN-DXF (R2018): Attribute „<Objekttyp>_<Eigenschaftsnummer>", Seitendaten im
# Rahmen-Block EPLFRAME, lange Texte in Fortsetzungs-Attributen. Werte stammen aus dem Beispiel des Anwenders.
# ---------------------------------------------------------------------------

def eplan_sps_block(anschl: str, bmk: str, text: str, symbol: str, adresse: str, text2: str = "") -> str:
    attribute = {"17_20028[1]": anschl, "17_20301": "=G2/16.1", "17_20402": symbol, "17_20006": bmk,
                 "17_20031": text, "17_20400": adresse, "17_20406": "", "17_20428": ""}
    if text2:
        attribute["17_20031/1"] = text2
    return einfuegung("EPLSYM_LibraryIEC_symbol_Symbol358_Variant0_Placement1", attribute)


def eplan_rahmen() -> str:
    return einfuegung("EPLFRAME", {"14_10013": "80079501", "4_11009": "=N2+E2/1", "4_11011": "Spaenefoerderer 1",
                                   "4_11011/1": "links", "4_11000": "1", "4_1120": "N2", "4_1220": "E2"})


def test_echter_eplan_aufbau_wie_im_beispiel_des_anwenders(tmp_path):
    ordner = tmp_path / "Projekt" / "N2" / "E2"
    ordner.mkdir(parents=True)
    zeilen = [
        eplan_sps_block("1", "=G2+E2-KA42.0", "Spaenefoerderer vor", "N2ASpfVor", "A42.0"),
        eplan_sps_block("12", "=G2+E2-KE44.0", "Spaenefoerderer Leistungsschalter", "N2ESpfLsAglN", "E45.3",
                        "nicht ausgeloest"),
        einfuegung("EPLSYM_LibraryIEC_symbol_Symbol31_Variant0_Placement1",           # Betriebsmittel ohne SPS-Bezug
                   {"17_20010": "-Q1", "17_20027": "24V", "17_20006": "=G2+E2-Q1"}),
    ]
    # Der Rahmen steht NACH den Funktionen: die Seitendaten müssen trotzdem gelten.
    (ordner / "_N2_E2_1.dxf").write_text(dxf(*zeilen, eplan_rahmen()), encoding="utf-8")
    ergebnis = lese(tmp_path / "Projekt")
    d = ergebnis.datensaetze
    assert len(d) == 2 and not ergebnis.warnungen
    assert (d[0].symbolische_adresse, d[0].sps_adresse, d[0].funktionstext) == \
        ("N2ASpfVor", "A42.0", "Spaenefoerderer vor")
    assert d[0].vollstaendiges_bmk == "=G2+E2-KA42.0" and d[0].betriebsmittelkennzeichen == ""
    assert d[1].funktionstext == "Spaenefoerderer Leistungsschalter nicht ausgeloest"      # Fortsetzung angehängt
    assert (d[0].seitenreferenz, d[0].seitennummer, d[0].seitenname) == ("=N2+E2/1", "1", "Spaenefoerderer 1 links")
    assert d[0].seite_ausfuehrlich == "1 – Spaenefoerderer 1 links"


def test_seitenattribute_haben_vorrang_vor_dem_dateinamen(tmp_path):
    pfad = tmp_path / "_X_Y_7.dxf"
    pfad.write_text(dxf(eplan_sps_block("1", "=A", "Text", "Sym", "E1.0"), eplan_rahmen()), encoding="utf-8")
    assert lese(pfad).datensaetze[0].seitenreferenz == "=N2+E2/1"
    ohne_rahmen = tmp_path / "_X_Y_8.dxf"
    ohne_rahmen.write_text(dxf(eplan_sps_block("1", "=A", "Text", "Sym", "E1.0")), encoding="utf-8")
    assert lese(ohne_rahmen).datensaetze[0].seitenreferenz == "=X+Y/8"


def test_quellauszug_zeigt_attribute_lesbar(tmp_path):
    pfad = tmp_path / "_N2_E2_1.dxf"
    pfad.write_text(dxf(eplan_sps_block("1", "=G2+E2-KA42.0", "Spaenefoerderer vor", "N2ASpfVor", "A42.0"),
                        eplan_sps_block("2", "=G2+E2-KA42.0", "Zweiter", "N2ASpfZur", "A42.1")), encoding="utf-8")
    d = lese(pfad).datensaetze[0]
    auszug = QuellenAuszugLeser().lese(pfad, d.fundstelle)
    zeilen = [z for _, z in auszug.zeilen]
    assert zeilen[0] == "INSERT EPLSYM_LibraryIEC_symbol_Symbol358_Variant0_Placement1"
    assert "ATTRIB 17_20400 = A42.0" in zeilen and "ATTRIB 17_20402 = N2ASpfVor" in zeilen
    assert zeilen[-1] == "SEQEND"                       # endet am Blockende, zeigt nicht den nächsten Block
    assert not any("A42.1" in z for z in zeilen)
