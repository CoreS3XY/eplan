"""Tests des CSV- und Excel-Exports."""

from __future__ import annotations

import csv

import pytest
from openpyxl import load_workbook

from eplan_pruefer.export import SPALTEN, CsvExporter, exportiere_fehlerliste, waehle_exporter
from eplan_pruefer.modelle import ExportFehler, Fehlertyp, Pruefungsfehler

from .conftest import baue_datensatz


@pytest.fixture
def fehler():
    return [
        Pruefungsfehler(Fehlertyp.DOPPELTE_ADRESSE, baue_datensatz("Motor_ä", "Förderband; Start", "E0.0", seite="12"),
                        "Adresse bereits auf Seite 5 verwendet."),
        Pruefungsfehler(Fehlertyp.PLATZHALTER, baue_datensatz("=HYPERLINK(\"http://x\")", "-ok", "A1.0", seite="3"),
                        "Symbolische Adresse enthält einen Platzhalter (?)."),
    ]


def test_csv_inhalt_und_format(tmp_path, fehler):
    ziel = exportiere_fehlerliste(fehler, tmp_path / "fehler.csv")
    rohbytes = ziel.read_bytes()
    assert rohbytes.startswith(b"\xef\xbb\xbf") and b"\r\n" in rohbytes          # BOM für Excel
    with open(ziel, encoding="utf-8-sig", newline="") as datei:
        zeilen = list(csv.reader(datei, delimiter=";"))
    assert tuple(zeilen[0]) == SPALTEN == ("Fehlertyp", "Symbol", "Funktionstext", "Adresse", "Seite", "Beschreibung")
    assert zeilen[1] == ["Doppelte Adresse", "Motor_ä", "Förderband; Start", "E0.0", "12",
                         "Adresse bereits auf Seite 5 verwendet."]


def test_csv_formelschutz(tmp_path, fehler):
    ziel = exportiere_fehlerliste(fehler, tmp_path / "f.csv")
    with open(ziel, encoding="utf-8-sig", newline="") as datei:
        zeile = list(csv.reader(datei, delimiter=";"))[2]
    assert zeile[1].startswith("'=HYPERLINK") and zeile[2] == "'-ok"
    ohne = tmp_path / "ohne.csv"
    CsvExporter(formelschutz=False).exportiere(fehler, ohne)
    assert "\"=HYPERLINK" in ohne.read_text(encoding="utf-8-sig")


def test_excel_inhalt_und_texttyp(tmp_path, fehler):
    ziel = exportiere_fehlerliste(fehler, tmp_path / "fehler.xlsx")
    blatt = load_workbook(ziel).active
    assert [z.value for z in blatt[1]] == list(SPALTEN)
    assert [z.value for z in blatt[2]][:5] == ["Doppelte Adresse", "Motor_ä", "Förderband; Start", "E0.0", "12"]
    zelle = blatt["B3"]
    assert zelle.value == '=HYPERLINK("http://x")' and zelle.data_type == "s"       # Text, keine Formel
    assert blatt.freeze_panes == "A2" and blatt.auto_filter.ref == "A1:F3"


def test_leere_liste(tmp_path):
    for endung in (".csv", ".xlsx"):
        assert exportiere_fehlerliste([], tmp_path / f"leer{endung}").exists()


def test_endung_wird_ergaenzt_und_unbekanntes_format_abgelehnt(tmp_path, fehler):
    assert CsvExporter().exportiere(fehler, tmp_path / "x.txt").suffix == ".csv"
    with pytest.raises(ExportFehler):
        waehle_exporter(tmp_path / "x.pdf")


def test_zielverzeichnis_nicht_beschreibbar(tmp_path, fehler):
    gesperrt = tmp_path / "datei"
    gesperrt.write_text("x")
    with pytest.raises(ExportFehler):
        exportiere_fehlerliste(fehler, gesperrt / "unter" / "f.csv")


def test_bestehende_datei_wird_ersetzt_und_keine_temporaeren_reste(tmp_path, fehler):
    ziel = tmp_path / "f.csv"
    ziel.write_text("alt")
    exportiere_fehlerliste(fehler, ziel)
    assert ziel.read_text(encoding="utf-8-sig").startswith("Fehlertyp;Symbol;")
    assert [p.name for p in tmp_path.iterdir()] == ["f.csv"]
