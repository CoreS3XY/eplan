"""Tests des Einlesens (Container, XML, CSV, XLSX, SQLite, Fehlerfälle)."""

from __future__ import annotations

import gzip
import io
import os
import sqlite3
import threading
import zipfile
from pathlib import Path

import pytest

from eplan_pruefer import eplan_parser
from eplan_pruefer.eplan_parser import (
    ContainerLeser,
    EplanDateiLeser,
    Feldzuordner,
    QuellenAuszugLeser,
    normalisiere_name,
)
from eplan_pruefer.modelle import (
    STANDARD_FELDZUORDNUNG,
    EplanDateiFehler,
    Einstellungen,
    VorgangAbgebrochenFehler,
    leite_seitennummer_ab,
)

from .conftest import CSV_BEISPIEL, XML_BEISPIEL


def lese(pfad: Path, einstellungen: Einstellungen | None = None):
    return EplanDateiLeser(einstellungen or Einstellungen()).lese(pfad)


def test_normalisiere_name():
    assert normalisiere_name("SPS-Adresse") == "spsadresse"
    assert normalisiere_name(" Symbolische_Adresse ") == "symbolischeadresse"


@pytest.mark.parametrize("referenz,erwartet", [
    ("=ANL+SCH/12", "12"), ("12", "12"), ("=ANL+SCH", ""), ("", ""), ("5.1", "5.1"), ("=A/B/7", "7")])
def test_seitennummer_ableiten(referenz, erwartet):
    assert leite_seitennummer_ab(referenz) == erwartet


def test_xml_felder_seiten_und_sprache(xml_datei):
    ergebnis = lese(xml_datei)
    assert len(ergebnis.datensaetze) == 3          # Gerät ohne SPS-Bezug wird nicht übernommen
    start, stop, sensor = ergebnis.datensaetze

    assert (start.symbolische_adresse, start.funktionstext, start.sps_adresse) == \
        ("Motor_Start", "Förderband Start", "E0.0")
    assert start.betriebsmittelkennzeichen == "-K1"
    assert (start.seitennummer, start.seitenname, start.seitenreferenz) == ("5", "Antriebe", "=ANL+ST1/5")
    assert stop.seitennummer == "5"

    # Seite aus Kindelementen, Name/Wert-Eigenschaft, mehrsprachiger Funktionstext (de_DE bevorzugt)
    assert sensor.symbolische_adresse == "Sensor_?"
    assert sensor.funktionstext == "Förderband Sensor"
    assert sensor.sps_adresse == "E0.0"
    assert sensor.seitennummer == "12" and sensor.seitenname == "Sensorik"
    assert sensor.betriebsmittelkennzeichen == "-B7"
    assert [d.laufende_nummer for d in ergebnis.datensaetze] == [1, 2, 3]
    assert sensor.fundstelle.zeile is not None and sensor.fundstelle.teildatei == "projekt.xml"
    assert {s.nummer for s in ergebnis.seiten} == {"5", "12"}


def test_xml_sprache_einstellbar(xml_datei):
    e = Einstellungen()
    e.allgemein.bevorzugte_sprache = "en_US"
    assert lese(xml_datei, e).datensaetze[2].funktionstext == "Conveyor sensor"


def test_xml_numerische_eigenschafts_ids_per_konfiguration(tmp_path):
    xml = ('<Export><Seite Nummer="3"><Objekt><P20404>Ventil_1</P20404><P20400>A4.2</P20400>'
           '<P20450>Ventil öffnen</P20450></Objekt></Seite></Export>')
    pfad = tmp_path / "ids.xml"
    pfad.write_text(xml, encoding="utf-8")
    e = Einstellungen()
    assert lese(pfad, e).datensaetze == []         # ohne Zuordnung unbekannt
    e.feldzuordnung = dict(STANDARD_FELDZUORDNUNG)
    e.feldzuordnung["symbolische_adresse"] += ("P20404",)
    e.feldzuordnung["sps_adresse"] += ("P20400",)
    e.feldzuordnung["funktionstext"] += ("P20450",)
    d = lese(pfad, e).datensaetze
    assert len(d) == 1 and (d[0].symbolische_adresse, d[0].sps_adresse, d[0].funktionstext, d[0].seitennummer) \
        == ("Ventil_1", "A4.2", "Ventil öffnen", "3")


def test_xml_externe_entitaeten_werden_nicht_aufgeloest(tmp_path):
    geheim = tmp_path / "geheim.txt"
    geheim.write_text("GEHEIMER_INHALT", encoding="utf-8")
    xml = (f'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file://{geheim}">]>'
           '<R><Funktion Symbol="&e;" Adresse="E1.0"/></R>')
    pfad = tmp_path / "xxe.xml"
    pfad.write_text(xml, encoding="utf-8")
    ergebnis = lese(pfad)
    assert all("GEHEIMER_INHALT" not in d.symbolische_adresse for d in ergebnis.datensaetze)


def test_xml_defekt_liefert_teilergebnis_und_warnung(tmp_path):
    pfad = tmp_path / "kaputt.xml"
    pfad.write_text('<R><Funktion Symbol="A" Adresse="E1.0"/><Funktion Symbol="B" Adresse="E1.1"/><x>', encoding="utf-8")
    ergebnis = lese(pfad)
    assert len(ergebnis.datensaetze) == 2
    assert any("kaputt.xml" in w for w in ergebnis.warnungen)


def test_csv_mit_semikolon_anfuehrungszeichen_und_leerzeilen(csv_datei):
    ergebnis = lese(csv_datei)
    assert [d.symbolische_adresse for d in ergebnis.datensaetze] == ["Motor_Start", "Motor_Stop", "Lampe"]
    assert ergebnis.datensaetze[1].funktionstext == "Förderband; Stop"
    assert ergebnis.datensaetze[0].betriebsmittelkennzeichen == "-K1"
    assert ergebnis.datensaetze[2].seitenname == "Meldungen"


def test_csv_windows1252_und_tabulator(tmp_path):
    text = "Symbol\tFunktionstext\tAdresse\nMotor_Ü\tFörderband\tE0.0\n"
    pfad = tmp_path / "liste.txt"
    pfad.write_bytes(text.encode("cp1252"))
    d = lese(pfad).datensaetze
    assert len(d) == 1 and d[0].symbolische_adresse == "Motor_Ü" and d[0].funktionstext == "Förderband"


def test_csv_ohne_passende_spalten(tmp_path):
    pfad = tmp_path / "x.csv"
    pfad.write_text("Name;Wert\na;b\n", encoding="utf-8")
    ergebnis = lese(pfad)
    assert ergebnis.datensaetze == [] and ergebnis.warnungen


def test_xlsx(tmp_path):
    from openpyxl import Workbook
    mappe = Workbook()
    blatt = mappe.active
    blatt.title = "SPS"
    blatt.append(["Projekt XY"])
    blatt.append([])
    blatt.append(["Symbolische Adresse", "Funktionstext", "SPS-Adresse", "Seite"])
    blatt.append(["Motor_Start", "Förderband Start", "E0.0", 5])
    blatt.append(["Motor_Stop", "Förderband Stop", "E0.1", 5.0])
    pfad = tmp_path / "export.xlsx"
    mappe.save(pfad)
    d = lese(pfad).datensaetze
    assert len(d) == 2 and d[0].seitennummer == "5" and d[1].sps_adresse == "E0.1"
    assert d[0].fundstelle.element == "Blatt SPS" and d[0].fundstelle.zeile == 4


def test_sqlite_tabelle_wird_erkannt(tmp_path):
    pfad = tmp_path / "projekt.db"
    verbindung = sqlite3.connect(pfad)
    verbindung.execute('CREATE TABLE "Anschluesse" ("Symbolische Adresse" TEXT, Funktionstext TEXT, "SPS-Adresse" TEXT)')
    verbindung.execute('CREATE TABLE andere (a TEXT, b TEXT)')
    verbindung.executemany('INSERT INTO "Anschluesse" VALUES (?,?,?)', [("A", "Text A", "E0.0"), ("B", "Text B", "E0.1")])
    verbindung.commit()
    verbindung.close()
    d = lese(pfad).datensaetze
    assert [x.symbolische_adresse for x in d] == ["A", "B"]


def test_zw1_als_zip_mit_nutzdaten_und_binaerdateien(zw1_zip):
    ergebnis = lese(zw1_zip)
    assert len(ergebnis.datensaetze) == 3
    fundstelle = ergebnis.datensaetze[0].fundstelle
    assert fundstelle.teildatei == "anlage.zw1!daten/seiten.xml"
    assert fundstelle.als_text() == "anlage.zw1 › daten/seiten.xml › Zeile 4 (Funktion)"
    assert any(t.art == "nicht unterstützt" for t in ergebnis.teildateien)
    assert "ZIP" in ergebnis.strukturbericht


def test_zip_hinter_proprietaerem_vorspann(tmp_path, zw1_zip):
    pfad = tmp_path / "mit_vorspann.zw1"
    pfad.write_bytes(os.urandom(777) + zw1_zip.read_bytes())
    ergebnis = lese(pfad)
    assert len(ergebnis.datensaetze) == 3
    assert "HINTER" in ergebnis.strukturbericht


def test_verschachteltes_archiv_und_gzip(tmp_path):
    innen = io.BytesIO()
    with zipfile.ZipFile(innen, "w") as z:
        z.writestr("a.csv", CSV_BEISPIEL)
    pfad = tmp_path / "aussen.zw1"
    with zipfile.ZipFile(pfad, "w") as z:
        z.writestr("innen.zip", innen.getvalue())
        z.writestr("zusatz.xml.gz", gzip.compress(XML_BEISPIEL.encode("utf-8")))
    ergebnis = lese(pfad)
    assert len(ergebnis.datensaetze) == 3 + 3      # CSV (3) + XML (3), keine Überschneidung


def test_elk_mit_edb_verzeichnis(tmp_path):
    elk = tmp_path / "Anlage.elk"
    elk.write_bytes(os.urandom(512))               # proprietärer Inhalt, nicht lesbar
    edb = tmp_path / "Anlage.edb" / "Daten"
    edb.mkdir(parents=True)
    (edb / "liste.csv").write_text(CSV_BEISPIEL, encoding="utf-8")
    ergebnis = lese(elk)
    assert len(ergebnis.datensaetze) == 3
    assert ergebnis.datensaetze[0].fundstelle.datei == "Anlage.elk"


def test_identische_datensaetze_aus_verschiedenen_teildateien_werden_zusammengefuehrt(tmp_path):
    pfad = tmp_path / "doppelt.zw1"
    with zipfile.ZipFile(pfad, "w") as z:
        z.writestr("a.xml", XML_BEISPIEL)
        z.writestr("kopie/a.xml", XML_BEISPIEL)
    assert len(lese(pfad).datensaetze) == 3


def test_gleiche_datensaetze_innerhalb_einer_datei_bleiben_erhalten(tmp_path):
    pfad = tmp_path / "d.csv"
    pfad.write_text("Symbol;Adresse\nA;E0.0\nA;E0.0\n", encoding="utf-8")
    assert len(lese(pfad).datensaetze) == 2


def test_unbekanntes_binaerformat_liefert_verstaendlichen_befund(tmp_path):
    pfad = tmp_path / "unbekannt.zw1"
    pfad.write_bytes(os.urandom(50_000))
    ergebnis = lese(pfad)
    assert ergebnis.datensaetze == []
    assert any("keine SPS-Datensätze" in w for w in ergebnis.warnungen)
    assert "Entropie" in ergebnis.strukturbericht


def test_nicht_vorhandene_datei():
    with pytest.raises(EplanDateiFehler):
        lese(Path("/gibt/es/nicht.elk"))


def test_zu_grosse_zip_eintraege_werden_uebersprungen(tmp_path, monkeypatch):
    pfad = tmp_path / "gross.zw1"
    with zipfile.ZipFile(pfad, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("riesig.xml", "<a>" + "x" * 5000 + "</a>")
    monkeypatch.setattr(eplan_parser, "MAX_TEILDATEI_BYTES", 1000)
    container = ContainerLeser()
    assert list(container.teildateien(pfad)) == []
    assert container.uebersprungen and "zu groß" in container.uebersprungen[0][1]


def test_gzip_bombe_wird_durch_stromlimit_gestoppt(tmp_path, monkeypatch):
    pfad = tmp_path / "bombe.xml.gz"
    pfad.write_bytes(gzip.compress(b"<a>" + b"x" * 200_000 + b"</a>"))
    monkeypatch.setattr(eplan_parser, "MAX_TEILDATEI_BYTES", 10_000)
    ergebnis = lese(pfad)
    assert ergebnis.datensaetze == []
    assert any("zulässige Größe" in w for w in ergebnis.warnungen)


def test_abbruch(xml_datei):
    abbruch = threading.Event()
    abbruch.set()
    with pytest.raises(VorgangAbgebrochenFehler):
        EplanDateiLeser(Einstellungen()).lese(xml_datei, abbruch=abbruch)


def test_feldzuordner_eigene_bezeichnung():
    z = Feldzuordner({"symbolische_adresse": ("Kurzbezeichnung",), "sps_adresse": ("Klemme",)})
    assert z.spalten(["x", "Kurzbezeichnung", "KLEMME"]) == {"symbolische_adresse": 1, "sps_adresse": 2}


def test_quellauszug_xml(xml_datei):
    ergebnis = lese(xml_datei)
    sensor = ergebnis.datensaetze[2]
    auszug = QuellenAuszugLeser().lese(xml_datei, sensor.fundstelle, umgebung=2)
    assert auszug.markierte_zeile == sensor.fundstelle.zeile
    assert any(nummer == auszug.markierte_zeile for nummer, _ in auszug.zeilen)
    assert any("Sensor" in text for _, text in auszug.zeilen)


def test_quellauszug_aus_zip(zw1_zip):
    ergebnis = lese(zw1_zip)
    d = ergebnis.datensaetze[0]
    auszug = QuellenAuszugLeser().lese(zw1_zip, d.fundstelle)
    assert any("Motor_Start" in text for _, text in auszug.zeilen)


def test_quellauszug_datei_geloescht(xml_datei):
    d = lese(xml_datei).datensaetze[0]
    xml_datei.unlink()
    assert QuellenAuszugLeser().lese(xml_datei, d.fundstelle).hinweis
