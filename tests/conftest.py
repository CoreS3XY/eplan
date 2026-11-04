"""Gemeinsame Hilfen für alle Tests."""

from __future__ import annotations

import os
import zipfile
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from eplan_pruefer.modelle import EplanDatensatz, Einstellungen, Fundstelle  # noqa: E402

XML_BEISPIEL = """<?xml version="1.0" encoding="UTF-8"?>
<Projekt>
  <Seite Nummer="5" Name="Antriebe" Seitenreferenz="=ANL+ST1/5">
    <Funktion Symbol="Motor_Start" Funktionstext="Förderband Start" Adresse="E0.0" BMK="-K1"/>
    <Funktion Symbol="Motor_Stop" Funktionstext="Förderband Stop" Adresse="E0.1"/>
    <Funktion Funktionstext="Nur Gerät ohne SPS-Bezug"/>
  </Seite>
  <Seite>
    <Nummer>12</Nummer>
    <Name>Sensorik</Name>
    <Funktion>
      <Symbolische_Adresse>Sensor_?</Symbolische_Adresse>
      <Funktionstext>
        <Text sprache="en_US">Conveyor sensor</Text>
        <Text sprache="de_DE">Förderband Sensor</Text>
      </Funktionstext>
      <Eigenschaft Name="SPS-Adresse" Wert="E0.0"/>
      <Betriebsmittelkennzeichen>-B7</Betriebsmittelkennzeichen>
    </Funktion>
  </Seite>
</Projekt>
"""

CSV_BEISPIEL = (
    "Symbolische Adresse;Funktionstext;SPS-Adresse;Seite;Seitenname;BMK\r\n"
    "Motor_Start;Förderband Start;E0.0;5;Antriebe;-K1\r\n"
    "Motor_Stop;\"Förderband; Stop\";E0.1;5;Antriebe;\r\n"
    ";;;;;\r\n"
    "Lampe;Meldeleuchte;A0.0;12;Meldungen;-H1\r\n"
)


def baue_datensatz(symbol: str = "", text: str = "", adresse: str = "", seite: str = "1",
                   nummer: int = 1, seitenname: str = "", bmk: str = "") -> EplanDatensatz:
    return EplanDatensatz(
        symbolische_adresse=symbol, funktionstext=text, sps_adresse=adresse,
        seitenreferenz=seite, seitennummer=seite, seitenname=seitenname,
        betriebsmittelkennzeichen=bmk, fundstelle=Fundstelle("test.xml", "test.xml", nummer),
        laufende_nummer=nummer)


@pytest.fixture
def einstellungen() -> Einstellungen:
    return Einstellungen()


@pytest.fixture
def xml_datei(tmp_path: Path) -> Path:
    pfad = tmp_path / "projekt.xml"
    pfad.write_text(XML_BEISPIEL, encoding="utf-8")
    return pfad


@pytest.fixture
def csv_datei(tmp_path: Path) -> Path:
    pfad = tmp_path / "export.csv"
    pfad.write_bytes(b"\xef\xbb\xbf" + CSV_BEISPIEL.encode("utf-8"))
    return pfad


@pytest.fixture
def zw1_zip(tmp_path: Path) -> Path:
    pfad = tmp_path / "anlage.zw1"
    with zipfile.ZipFile(pfad, "w", zipfile.ZIP_DEFLATED) as archiv:
        archiv.writestr("daten/seiten.xml", XML_BEISPIEL)
        archiv.writestr("bilder/logo.png", os.urandom(2048))
    return pfad


# ---------------------------------------------------------------------------
# SQLite-Referenzdatenbank für die Datenbanktests
# ---------------------------------------------------------------------------

import sqlite3  # noqa: E402

from eplan_pruefer.modelle import DatenbankEinstellungen  # noqa: E402

REFERENZ_TABELLE = """
CREATE TABLE symbole (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol TEXT NOT NULL,
  funktionstext TEXT NOT NULL
);
INSERT INTO symbole (symbol, funktionstext) VALUES
 ('Motor_Start', 'Förderband Start'), ('Motor_Stop', 'Förderband Stop'), ('Lampe', 'Meldeleuchte');
"""


@pytest.fixture
def datenbank_einstellungen(tmp_path) -> DatenbankEinstellungen:
    """Eine echte SQLite-Datei mit der Referenztabelle ``symbole``."""
    pfad = tmp_path / "referenz.db"
    verbindung = sqlite3.connect(pfad)
    verbindung.executescript(REFERENZ_TABELLE)
    verbindung.commit()
    verbindung.close()
    return DatenbankEinstellungen(dateipfad=str(pfad), tabellenname="symbole", feld_symbol="symbol",
                                  feld_funktionstext="funktionstext", primaerschluessel="id", timeout_s=2)


@pytest.fixture
def sql(datenbank_einstellungen):
    """Führt SQL direkt auf der Referenzdatei aus (zum Vorbereiten von Sonderfällen)."""
    def ausfuehren(skript: str) -> None:
        verbindung = sqlite3.connect(datenbank_einstellungen.dateipfad)
        verbindung.executescript(skript)
        verbindung.commit()
        verbindung.close()
    return ausfuehren
