"""Integrationstests der Datenbankschicht gegen einen echten MariaDB/MySQL-Server."""

from __future__ import annotations

import dataclasses
import logging

import pytest

from eplan_pruefer.datenbank import MySqlReferenzRepository, quotiere_bezeichner
from eplan_pruefer.modelle import DatenbankFehler

from .conftest import baue_datensatz

pytestmark = pytest.mark.mysql

TABELLE_AUTO = """
DROP TABLE IF EXISTS symbole;
CREATE TABLE symbole (
  id INT AUTO_INCREMENT PRIMARY KEY,
  symbol VARCHAR(100) NOT NULL,
  funktionstext VARCHAR(200) NOT NULL
) CHARACTER SET utf8mb4;
INSERT INTO symbole (symbol, funktionstext) VALUES
 ('Motor_Start', 'Förderband Start'), ('Motor_Stop', 'Förderband Stop'), ('Lampe', 'Meldeleuchte');
"""


@pytest.fixture
def repository(datenbank_einstellungen, sql):
    sql(TABELLE_AUTO)
    return MySqlReferenzRepository(datenbank_einstellungen)



def test_verbindungstest_erfolgreich(repository):
    ergebnis = repository.teste_verbindung()
    assert ergebnis.erfolgreich, ergebnis.meldungen
    text = "\n".join(ergebnis.meldungen)
    assert "Tabelle „symbole“ gefunden" in text and "3 Datensätze" in text


def test_verbindungstest_meldet_fehlendes_feld(datenbank_einstellungen, sql):
    sql(TABELLE_AUTO)
    falsch = dataclasses.replace(datenbank_einstellungen, feld_funktionstext="gibt_es_nicht")
    ergebnis = MySqlReferenzRepository(falsch).teste_verbindung()
    assert not ergebnis.erfolgreich
    assert any("gibt_es_nicht" in m and "NICHT" in m for m in ergebnis.meldungen)


def test_verbindungstest_meldet_fehlende_tabelle(datenbank_einstellungen):
    falsch = dataclasses.replace(datenbank_einstellungen, tabellenname="keine_tabelle")
    ergebnis = MySqlReferenzRepository(falsch).teste_verbindung()
    assert not ergebnis.erfolgreich and any("existiert nicht" in m for m in ergebnis.meldungen)


def test_falsches_passwort_wird_verstaendlich_gemeldet_und_nicht_protokolliert(
        datenbank_einstellungen, caplog):
    falsch = dataclasses.replace(datenbank_einstellungen, passwort="FALSCHES-Passwort-123")
    with caplog.at_level(logging.DEBUG):
        ergebnis = MySqlReferenzRepository(falsch).teste_verbindung()
    assert not ergebnis.erfolgreich
    assert "FALSCHES-Passwort-123" not in "\n".join(ergebnis.meldungen)
    assert "FALSCHES-Passwort-123" not in caplog.text


def test_unvollstaendige_einstellungen():
    from eplan_pruefer.modelle import DatenbankEinstellungen
    ergebnis = MySqlReferenzRepository(DatenbankEinstellungen()).teste_verbindung()
    assert not ergebnis.erfolgreich and "unvollständig" in ergebnis.meldungen[-1]


def test_lade_referenzdaten(repository):
    assert sorted(repository.lade_referenzdaten()) == [
        ("Lampe", "Meldeleuchte"), ("Motor_Start", "Förderband Start"), ("Motor_Stop", "Förderband Stop")]


def test_vorhandensein(repository):
    v = repository.pruefe_vorhandensein("Motor_Start", "Förderband Start")
    assert (v.symbol, v.funktionstext, v.kombination) == (True, True, True)
    v = repository.pruefe_vorhandensein("Motor_Start", "Anderer Text")
    assert (v.symbol, v.funktionstext, v.kombination) == (True, False, False)
    v = repository.pruefe_vorhandensein("Neu", "Förderband Stop")
    assert (v.symbol, v.funktionstext, v.kombination) == (False, True, False)
    v = repository.pruefe_vorhandensein("Neu", "Neuer Text")
    assert (v.symbol, v.funktionstext, v.kombination) == (False, False, False)


def test_uebernahme_neuer_datensatz_und_wiederholung(repository):
    erg = repository.uebernehmen([("Ventil_1", "Ventil öffnen")])[0]
    assert erg.uebernommen and not erg.benoetigt_bestaetigung
    assert ("Ventil_1", "Ventil öffnen") in repository.lade_referenzdaten()
    # zweiter Versuch: Kombination existiert bereits -> nichts wird doppelt angelegt
    erg = repository.uebernehmen([("Ventil_1", "Ventil öffnen")])[0]
    assert not erg.uebernommen and "bereits vorhanden" in erg.meldung and erg.vorhandensein.kombination
    assert repository.lade_referenzdaten().count(("Ventil_1", "Ventil öffnen")) == 1


def test_uebernahme_konflikt_braucht_bestaetigung(repository):
    erg = repository.uebernehmen([("Motor_Start", "Neuer Text")])[0]
    assert not erg.uebernommen and erg.benoetigt_bestaetigung and "Symbol existiert bereits" in erg.meldung
    erg = repository.uebernehmen([("Neu_Symbol", "Förderband Stop")])[0]
    assert not erg.uebernommen and erg.benoetigt_bestaetigung and "Funktionstext existiert bereits" in erg.meldung
    assert len(repository.lade_referenzdaten()) == 3
    erg = repository.uebernehmen([("Motor_Start", "Neuer Text")], konflikt_erlauben=True)[0]
    assert erg.uebernommen and len(repository.lade_referenzdaten()) == 4


def test_uebernahme_leere_werte_werden_abgelehnt(repository):
    ergebnisse = repository.uebernehmen([("", "Text"), ("Symbol", "  ")])
    assert not any(e.uebernommen for e in ergebnisse)
    assert len(repository.lade_referenzdaten()) == 3


def test_uebernahme_mehrere_mit_gemischtem_ergebnis(repository):
    ergebnisse = repository.uebernehmen([("A", "Text A"), ("Lampe", "Meldeleuchte"), ("B", "Text B")])
    assert [e.uebernommen for e in ergebnisse] == [True, False, True]


def test_sql_injektion_wird_als_text_gespeichert(repository):
    boese = "x'); DROP TABLE symbole; --"
    assert repository.uebernehmen([(boese, "Text")])[0].uebernommen
    assert (boese, "Text") in repository.lade_referenzdaten()
    assert repository.teste_verbindung().erfolgreich       # Tabelle existiert noch


def test_primaerschluessel_ganzzahl_ohne_auto_increment(datenbank_einstellungen, sql):
    sql("DROP TABLE IF EXISTS symbole; CREATE TABLE symbole (id INT PRIMARY KEY, symbol VARCHAR(50) NOT NULL, "
        "funktionstext VARCHAR(50) NOT NULL); INSERT INTO symbole VALUES (7, 'A', 'Text A');")
    repo = MySqlReferenzRepository(datenbank_einstellungen)
    assert repo.uebernehmen([("B", "Text B"), ("C", "Text C")])[1].uebernommen
    assert len(repo.lade_referenzdaten()) == 3


def test_primaerschluessel_text_ohne_auto_wert_wird_verweigert(datenbank_einstellungen, sql):
    sql("DROP TABLE IF EXISTS symbole; CREATE TABLE symbole (id VARCHAR(20) PRIMARY KEY, symbol VARCHAR(50) NOT NULL, "
        "funktionstext VARCHAR(50) NOT NULL);")
    repo = MySqlReferenzRepository(datenbank_einstellungen)
    erg = repo.uebernehmen([("B", "Text B")])[0]
    assert not erg.uebernommen and "kein AUTO_INCREMENT" in erg.meldung
    assert any("WARNUNG" in m for m in repo.teste_verbindung().meldungen)


def test_symbol_ist_primaerschluessel(datenbank_einstellungen, sql):
    sql("DROP TABLE IF EXISTS symbole; CREATE TABLE symbole (symbol VARCHAR(50) PRIMARY KEY, "
        "funktionstext VARCHAR(50) NOT NULL);")
    einstellungen = dataclasses.replace(datenbank_einstellungen, primaerschluessel="symbol")
    repo = MySqlReferenzRepository(einstellungen)
    assert repo.uebernehmen([("B", "Text B")])[0].uebernommen


def test_andere_tabellen_und_feldnamen_aus_konfiguration(datenbank_einstellungen, sql):
    sql("DROP TABLE IF EXISTS `Meine Tabelle`; CREATE TABLE `Meine Tabelle` (pk INT AUTO_INCREMENT PRIMARY KEY, "
        "`Sym bol` VARCHAR(50), langtext TEXT); INSERT INTO `Meine Tabelle` (`Sym bol`, langtext) VALUES ('X', 'Text X');")
    e = dataclasses.replace(datenbank_einstellungen, tabellenname="Meine Tabelle", feld_symbol="Sym bol",
                            feld_funktionstext="langtext", primaerschluessel="pk")
    repo = MySqlReferenzRepository(e)
    assert repo.lade_referenzdaten() == [("X", "Text X")]
    assert repo.uebernehmen([("Y", "Text Y")])[0].uebernommen


def test_null_werte_werden_zu_leeren_texten(datenbank_einstellungen, sql):
    sql("DROP TABLE IF EXISTS symbole; CREATE TABLE symbole (id INT AUTO_INCREMENT PRIMARY KEY, symbol VARCHAR(50), "
        "funktionstext VARCHAR(50)); INSERT INTO symbole (symbol, funktionstext) VALUES ('A', NULL);")
    assert MySqlReferenzRepository(datenbank_einstellungen).lade_referenzdaten() == [("A", "")]


@pytest.mark.parametrize("name", ["", "   ", "a`b", "a\x00b", "x" * 65, "a\nb"])
def test_unzulaessige_bezeichner(name):
    with pytest.raises(DatenbankFehler):
        quotiere_bezeichner(name)


def test_bezeichner_wird_quotiert():
    assert quotiere_bezeichner(" Meine Tabelle ") == "`Meine Tabelle`"


def test_datenbankabgleich_pruefung_gegen_echte_datenbank(repository):
    from eplan_pruefer.pruefer import DatenbankAbgleichPruefung, Pruefkontext, Textnormalisierer
    from eplan_pruefer.modelle import Fehlertyp
    daten = [
        baue_datensatz("Motor_Start", "Förderband Start", "E0.0", nummer=1),       # ok
        baue_datensatz("Unbekannt", "Irgendwas", "E0.1", nummer=2),                 # Symbol fehlt
        baue_datensatz("Lampe", "Fehlender Text", "E0.2", nummer=3),                # Text fehlt
        baue_datensatz("Lampe", "Förderband Stop", "E0.3", nummer=4),               # Kombination
        baue_datensatz("MOTOR_START", "förderband  start", "E0.4", nummer=5),       # ok (Schreibweise egal)
    ]
    fehler = DatenbankAbgleichPruefung().pruefe(daten, Pruefkontext(Textnormalisierer(), repository))
    assert [(f.datensatz.laufende_nummer, f.fehlertyp) for f in fehler] == [
        (2, Fehlertyp.SYMBOL_NICHT_IN_DB), (3, Fehlertyp.FUNKTIONSTEXT_NICHT_IN_DB),
        (4, Fehlertyp.KOMBINATION_ABWEICHEND)]


def test_tls_wird_erzwungen_und_nicht_stillschweigend_abgeschaltet(datenbank_einstellungen, sql):
    sql(TABELLE_AUTO)
    verlangt = dataclasses.replace(datenbank_einstellungen, tls_verwenden=True)
    ergebnis = MySqlReferenzRepository(verlangt).teste_verbindung()
    # Der Testserver hat kein TLS: die Verbindung muss mit klarer Meldung scheitern.
    assert not ergebnis.erfolgreich and "TLS" in ergebnis.meldungen[-1]
    assert MySqlReferenzRepository(datenbank_einstellungen).teste_verbindung().erfolgreich
