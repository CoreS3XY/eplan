"""Tests von Konfiguration, Protokollierung und Strukturanalyse."""

from __future__ import annotations

import logging
import os
import sys
import zipfile

import pytest

from eplan_pruefer.dateianalyse import DateiAnalysator, berechne_entropie, erkenne_codierung
from eplan_pruefer.einstellungen import EinstellungsVerwalter
from eplan_pruefer.logging import (
    hole_protokollierer,
    registriere_geheimnis,
    richte_protokollierung_ein,
)
from eplan_pruefer.modelle import EinstellungsFehler, EplanDateiFehler, Einstellungen


def test_standardwerte_wenn_datei_fehlt(tmp_path):
    e = EinstellungsVerwalter(tmp_path / "x.ini").laden()
    assert e == Einstellungen()


def test_speichern_und_laden_roundtrip(tmp_path):
    verwalter = EinstellungsVerwalter(tmp_path / "unter" / "einstellungen.ini")
    e = Einstellungen()
    d = e.datenbank
    d.dateipfad = "C:\\Daten\\Symbole ä;#1.db"
    d.tabellenname, d.feld_symbol, d.feld_funktionstext, d.primaerschluessel = "Meine Tabelle", "sym", "txt", "id"
    d.timeout_s = 30
    e.pruefung.doppelte_adressen = False
    e.pruefung.gross_kleinschreibung_beachten = True
    e.allgemein.bevorzugte_sprache = "en_US"
    e.feldzuordnung["symbolische_adresse"] = ("Kurzname", "P20404")
    verwalter.speichern(e)
    assert verwalter.laden() == e
    if sys.platform != "win32":
        assert oct(os.stat(verwalter.pfad).st_mode & 0o777) == "0o600"


def test_alte_mysql_konfiguration_wird_ohne_fehler_gelesen(tmp_path):
    pfad = tmp_path / "e.ini"
    pfad.write_text("[datenbank]\nserver = db\nport = 3306\nbenutzer = u\npasswort = geheim\ndatenbankname = d\n"
                    "tabellenname = symbole\nfeld_symbol = symbol\nfeld_funktionstext = text\nprimaerschluessel = id\n",
                    encoding="utf-8")
    d = EinstellungsVerwalter(pfad).laden().datenbank
    assert (d.tabellenname, d.feld_symbol, d.feld_funktionstext, d.primaerschluessel) == ("symbole", "symbol", "text", "id")
    assert d.dateipfad == "" and not d.ist_vollstaendig            # Datei muss neu gewählt werden
    EinstellungsVerwalter(pfad).speichern(EinstellungsVerwalter(pfad).laden())
    assert "geheim" not in pfad.read_text(encoding="utf-8")          # Passwort verschwindet beim nächsten Speichern


def test_ungueltige_werte_fallen_auf_standard_zurueck(tmp_path):
    pfad = tmp_path / "e.ini"
    pfad.write_text("[datenbank]\ntimeout_s = abc\ndateipfad = x.db\n"
                    "[pruefung]\ndoppelte_symbole = nein\ndatenbankabgleich = vielleicht\n", encoding="utf-8")
    e = EinstellungsVerwalter(pfad).laden()
    assert e.datenbank.timeout_s == 10 and e.datenbank.dateipfad == "x.db"
    assert e.pruefung.datenbankabgleich is False            # Standard: optional, aus
    assert e.pruefung.doppelte_symbole is False


def test_defekte_datei_wird_gesichert(tmp_path):
    pfad = tmp_path / "e.ini"
    pfad.write_bytes(b"\xff\xfe\x00 kein ini [[[")
    assert EinstellungsVerwalter(pfad).laden() == Einstellungen()
    assert (tmp_path / "e.ini.defekt").exists() and not pfad.exists()


@pytest.mark.parametrize("aenderung,meldung", [
    (lambda e: setattr(e.datenbank, "timeout_s", 0), "Timeout"),
    (lambda e: setattr(e.datenbank, "tabellenname", "a\x00b"), "Steuerzeichen"),
    (lambda e: setattr(e.allgemein, "protokollstufe", "LAUT"), "Protokollstufe"),
    (lambda e: e.feldzuordnung.__setitem__("sps_adresse", ()), "sps_adresse"),
])
def test_ungueltige_einstellungen_werden_abgelehnt(tmp_path, aenderung, meldung):
    e = Einstellungen()
    aenderung(e)
    with pytest.raises(EinstellungsFehler, match=meldung):
        EinstellungsVerwalter(tmp_path / "e.ini").speichern(e)
    assert not (tmp_path / "e.ini").exists()


def test_passwort_erscheint_nicht_im_protokoll(tmp_path):
    datei = richte_protokollierung_ein(tmp_path, "DEBUG", konsole=False)
    registriere_geheimnis("Sup3rGeheim!")
    log = hole_protokollierer("test")
    log.info("Verbindung mit Passwort Sup3rGeheim! aufgebaut")
    try:
        raise RuntimeError("Fehler mit Sup3rGeheim! im Text")
    except RuntimeError:
        log.exception("Fehlgeschlagen")
    for handler in logging.getLogger("eplan_pruefer").handlers:
        handler.flush()
    inhalt = datei.read_text(encoding="utf-8")
    assert "Sup3rGeheim!" not in inhalt and "***" in inhalt and "Fehlgeschlagen" in inhalt


def test_protokollierung_ohne_verzeichnis_faellt_auf_konsole_zurueck(tmp_path):
    blockiert = tmp_path / "datei"
    blockiert.write_text("x")
    assert richte_protokollierung_ein(blockiert / "log", "INFO", konsole=False) is None


# -- Dateianalyse ---------------------------------------------------------------

def test_entropie_und_codierung():
    assert berechne_entropie(b"aaaa") == 0.0
    assert berechne_entropie(bytes(range(256)) * 4) == pytest.approx(8.0)
    assert erkenne_codierung("Ünïcode".encode("utf-8")) == "UTF-8"
    assert erkenne_codierung("Ü".encode("cp1252") + b"abc") == "Windows-1252 (ANSI)"
    assert erkenne_codierung("<a/>".encode("utf-16")) == "UTF-16 mit BOM"
    assert erkenne_codierung(os.urandom(500) + b"\x00") == ""


def test_analyse_zip_mit_vorspann_und_zufallsdaten(tmp_path):
    archiv = tmp_path / "a.zip"
    with zipfile.ZipFile(archiv, "w") as z:
        z.writestr("projekt/daten.xml", "<a/>")
    pfad = tmp_path / "p.zw1"
    pfad.write_bytes(b"EPLAN-PROPRIETAER" + os.urandom(100) + archiv.read_bytes())
    bericht = DateiAnalysator().analysiere(pfad)
    assert bericht.archiv_anzahl == 1 and "projekt/daten.xml" in bericht.archiveintraege[0]
    assert any("HINTER" in h for h in bericht.hinweise)
    assert any("ZIP" in name for _, name in bericht.signaturen)

    rauschen = tmp_path / "r.elk"
    rauschen.write_bytes(os.urandom(200_000))
    b2 = DateiAnalysator().analysiere(rauschen)
    assert b2.erkannte_art.startswith("Binärdaten") and b2.entropie > 7.5
    assert any("komprimiert oder verschlüsselt" in h for h in b2.hinweise)


def test_analyse_verzeichnis_und_fehler(tmp_path):
    (tmp_path / "x.edb").mkdir()
    (tmp_path / "x.edb" / "a.xml").write_text("<a/>")
    bericht = DateiAnalysator().analysiere(tmp_path / "x.edb")
    assert bericht.ist_verzeichnis and "a.xml" in bericht.verzeichnisinhalt[0]
    with pytest.raises(EplanDateiFehler):
        DateiAnalysator().analysiere(tmp_path / "gibt_es_nicht")
