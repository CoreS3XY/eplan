"""Tests der SQLite-Referenzdatenbank (echte Dateien, kein Server nötig)."""

from __future__ import annotations

import dataclasses
import os
import sqlite3
import stat
import sys
import threading

import pytest

from eplan_pruefer.datenbank import (
    SqliteReferenzRepository,
    erzeuge_repository,
    lese_spaltennamen,
    lese_tabellen,
    quotiere_bezeichner,
)
from eplan_pruefer.modelle import DatenbankEinstellungen, DatenbankFehler, Fehlertyp, VorgangAbgebrochenFehler

from .conftest import baue_datensatz


@pytest.fixture
def repository(datenbank_einstellungen):
    return SqliteReferenzRepository(datenbank_einstellungen)


def paare_in_datei(einstellungen) -> list[tuple]:
    verbindung = sqlite3.connect(einstellungen.dateipfad)
    try:
        return verbindung.execute(
            f'SELECT "{einstellungen.feld_symbol}", "{einstellungen.feld_funktionstext}" FROM "{einstellungen.tabellenname}"'
        ).fetchall()
    finally:
        verbindung.close()


def test_verbindungstest_erfolgreich(repository):
    ergebnis = repository.teste_verbindung()
    assert ergebnis.erfolgreich, ergebnis.meldungen
    text = "\n".join(ergebnis.meldungen)
    assert "Tabelle „symbole“ gefunden" in text and "3 Datensätze" in text and "SQLite" in text


def test_verbindungstest_meldet_fehlendes_feld_und_nennt_vorhandene(datenbank_einstellungen):
    falsch = dataclasses.replace(datenbank_einstellungen, feld_funktionstext="gibt_es_nicht")
    ergebnis = SqliteReferenzRepository(falsch).teste_verbindung()
    assert not ergebnis.erfolgreich
    assert "gibt_es_nicht" in ergebnis.meldungen[-1] and "funktionstext" in ergebnis.meldungen[-1]


def test_verbindungstest_meldet_fehlende_tabelle_und_nennt_vorhandene(datenbank_einstellungen):
    falsch = dataclasses.replace(datenbank_einstellungen, tabellenname="keine_tabelle")
    ergebnis = SqliteReferenzRepository(falsch).teste_verbindung()
    assert not ergebnis.erfolgreich and "symbole" in ergebnis.meldungen[-1]


def test_fehlende_datei_wird_nicht_angelegt(tmp_path, datenbank_einstellungen):
    ziel = tmp_path / "gibt_es_nicht.db"
    falsch = dataclasses.replace(datenbank_einstellungen, dateipfad=str(ziel))
    for aufruf in (lambda r: r.teste_verbindung().erfolgreich, lambda r: r.lade_referenzdaten(),
                   lambda r: r.uebernehmen([("a", "b")])):
        try:
            assert aufruf(SqliteReferenzRepository(falsch)) is False
        except DatenbankFehler as fehler:
            assert "existiert nicht" in str(fehler)
    assert not ziel.exists()                         # nie eine leere Datenbank erzeugen


def test_keine_sqlite_datei(tmp_path, datenbank_einstellungen):
    kaputt = tmp_path / "text.db"
    kaputt.write_text("das ist keine Datenbank " * 50, encoding="utf-8")
    ergebnis = SqliteReferenzRepository(dataclasses.replace(datenbank_einstellungen, dateipfad=str(kaputt))).teste_verbindung()
    assert not ergebnis.erfolgreich and "keine gültige SQLite-Datenbank" in ergebnis.meldungen[-1]


def test_unvollstaendige_einstellungen():
    ergebnis = SqliteReferenzRepository(DatenbankEinstellungen()).teste_verbindung()
    assert not ergebnis.erfolgreich and "unvollständig" in ergebnis.meldungen[-1] and "Datenbankdatei" in ergebnis.meldungen[-1]
    assert DatenbankEinstellungen().fehlende_angaben() == [
        "Datenbankdatei", "Tabellenname", "Feldname Symbol", "Feldname Funktionstext"]


def test_lade_referenzdaten(repository):
    assert sorted(repository.lade_referenzdaten()) == [
        ("Lampe", "Meldeleuchte"), ("Motor_Start", "Förderband Start"), ("Motor_Stop", "Förderband Stop")]


def test_abbruch_beim_laden(repository):
    abbruch = threading.Event()
    abbruch.set()
    with pytest.raises(VorgangAbgebrochenFehler):
        repository.lade_referenzdaten(abbruch)


def test_lesen_veraendert_die_datei_nicht(repository, datenbank_einstellungen):
    vorher = os.stat(datenbank_einstellungen.dateipfad)
    repository.lade_referenzdaten()
    repository.teste_verbindung()
    nachher = os.stat(datenbank_einstellungen.dateipfad)
    assert (vorher.st_size, vorher.st_mtime_ns) == (nachher.st_size, nachher.st_mtime_ns)


def test_lesen_ist_schreibgeschuetzt(datenbank_einstellungen):
    from eplan_pruefer.datenbank import _oeffne
    with _oeffne(datenbank_einstellungen.dateipfad, False, 1) as verbindung:
        with pytest.raises(sqlite3.OperationalError):
            verbindung.execute("DELETE FROM symbole")


def test_vorhandensein_mit_schreibweise_toleranz(repository):
    v = repository.pruefe_vorhandensein("Motor_Start", "Förderband Start")
    assert (v.symbol, v.funktionstext, v.kombination) == (True, True, True)
    v = repository.pruefe_vorhandensein("  MOTOR_START ", "förderband   start")
    assert v.kombination is True
    v = repository.pruefe_vorhandensein("Motor_Start", "Anderer Text")
    assert (v.symbol, v.funktionstext, v.kombination) == (True, False, False)
    v = repository.pruefe_vorhandensein("Neu", "Förderband Stop")
    assert (v.symbol, v.funktionstext, v.kombination) == (False, True, False)
    v = repository.pruefe_vorhandensein("Neu", "Neuer Text")
    assert (v.symbol, v.funktionstext, v.kombination) == (False, False, False)


def test_vorhandensein_gross_klein_beachten(datenbank_einstellungen):
    streng = SqliteReferenzRepository(datenbank_einstellungen, normalisierer=lambda t: " ".join(t.split()))
    assert streng.pruefe_vorhandensein("motor_start", "Förderband Start").symbol is False
    assert streng.pruefe_vorhandensein("Motor_Start", "Förderband Start").kombination is True


def test_uebernahme_neuer_datensatz_und_wiederholung(repository, datenbank_einstellungen):
    erg = repository.uebernehmen([("Ventil_1", "Ventil öffnen")])[0]
    assert erg.uebernommen and not erg.benoetigt_bestaetigung
    assert ("Ventil_1", "Ventil öffnen") in paare_in_datei(datenbank_einstellungen)
    erg = repository.uebernehmen([("Ventil_1", "Ventil öffnen")])[0]
    assert not erg.uebernommen and "bereits vorhanden" in erg.meldung and erg.vorhandensein.kombination
    erg = repository.uebernehmen([("VENTIL_1", "ventil  öffnen")])[0]          # andere Schreibweise = vorhanden
    assert not erg.uebernommen
    assert paare_in_datei(datenbank_einstellungen).count(("Ventil_1", "Ventil öffnen")) == 1


def test_uebernahme_vergibt_fortlaufenden_schluessel(repository, datenbank_einstellungen):
    repository.uebernehmen([("A", "Text A"), ("B", "Text B")])
    verbindung = sqlite3.connect(datenbank_einstellungen.dateipfad)
    ids = [z[0] for z in verbindung.execute("SELECT id FROM symbole ORDER BY id")]
    verbindung.close()
    assert ids == [1, 2, 3, 4, 5]


def test_uebernahme_konflikt_braucht_bestaetigung(repository, datenbank_einstellungen):
    erg = repository.uebernehmen([("Motor_Start", "Neuer Text")])[0]
    assert not erg.uebernommen and erg.benoetigt_bestaetigung and "Symbol existiert bereits" in erg.meldung
    erg = repository.uebernehmen([("Neu_Symbol", "Förderband Stop")])[0]
    assert not erg.uebernommen and erg.benoetigt_bestaetigung and "Funktionstext existiert bereits" in erg.meldung
    assert len(paare_in_datei(datenbank_einstellungen)) == 3
    erg = repository.uebernehmen([("Motor_Start", "Neuer Text")], konflikt_erlauben=True)[0]
    assert erg.uebernommen and len(paare_in_datei(datenbank_einstellungen)) == 4


def test_uebernahme_leere_werte_werden_abgelehnt(repository, datenbank_einstellungen):
    ergebnisse = repository.uebernehmen([("", "Text"), ("Symbol", "  ")])
    assert not any(e.uebernommen for e in ergebnisse)
    assert len(paare_in_datei(datenbank_einstellungen)) == 3


def test_uebernahme_mehrere_mit_gemischtem_ergebnis_und_doppelten_im_stapel(repository):
    ergebnisse = repository.uebernehmen([("A", "Text A"), ("Lampe", "Meldeleuchte"), ("B", "Text B"), ("A", "Text A")])
    assert [e.uebernommen for e in ergebnisse] == [True, False, True, False]     # zweites A: im selben Stapel erkannt


def test_sql_injektion_wird_als_text_gespeichert(repository, datenbank_einstellungen):
    boese = "x'); DROP TABLE symbole; --"
    assert repository.uebernehmen([(boese, "Text")])[0].uebernommen
    assert (boese, "Text") in paare_in_datei(datenbank_einstellungen)
    assert repository.teste_verbindung().erfolgreich


def test_primaerschluessel_ganzzahl_ohne_autoinkrement(datenbank_einstellungen, sql):
    sql("DROP TABLE symbole; CREATE TABLE symbole (id INT PRIMARY KEY, symbol TEXT NOT NULL, funktionstext TEXT NOT NULL);"
        "INSERT INTO symbole VALUES (7, 'A', 'Text A');")
    repo = SqliteReferenzRepository(datenbank_einstellungen)
    assert all(e.uebernommen for e in repo.uebernehmen([("B", "Text B"), ("C", "Text C")]))
    verbindung = sqlite3.connect(datenbank_einstellungen.dateipfad)
    assert [z[0] for z in verbindung.execute("SELECT id FROM symbole ORDER BY id")] == [7, 8, 9]
    verbindung.close()


def test_primaerschluessel_text_ohne_standardwert_wird_verweigert(datenbank_einstellungen, sql):
    sql("DROP TABLE symbole; CREATE TABLE symbole (id TEXT PRIMARY KEY, symbol TEXT NOT NULL, funktionstext TEXT NOT NULL);")
    repo = SqliteReferenzRepository(datenbank_einstellungen)
    erg = repo.uebernehmen([("B", "Text B")])[0]
    assert not erg.uebernommen and "nicht automatisch vergeben" in erg.meldung
    assert any("WARNUNG" in m for m in repo.teste_verbindung().meldungen)
    assert paare_in_datei(datenbank_einstellungen) == []


def test_primaerschluessel_mit_standardwert_wird_von_der_datenbank_vergeben(datenbank_einstellungen, sql):
    sql("DROP TABLE symbole; CREATE TABLE symbole (id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(4)))), "
        "symbol TEXT NOT NULL, funktionstext TEXT NOT NULL);")
    assert SqliteReferenzRepository(datenbank_einstellungen).uebernehmen([("B", "Text B")])[0].uebernommen


def test_ohne_konfigurierten_primaerschluessel_und_symbol_als_schluessel(datenbank_einstellungen, sql):
    sql("DROP TABLE symbole; CREATE TABLE symbole (symbol TEXT PRIMARY KEY, funktionstext TEXT NOT NULL);")
    for schluessel in ("", "symbol"):
        e = dataclasses.replace(datenbank_einstellungen, primaerschluessel=schluessel)
        assert SqliteReferenzRepository(e).uebernehmen([("B" + schluessel, "Text B" + schluessel)])[0].uebernommen


def test_nicht_null_spalte_ohne_wert_ergibt_verstaendliche_meldung(datenbank_einstellungen, sql):
    sql("DROP TABLE symbole; CREATE TABLE symbole (id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, "
        "funktionstext TEXT NOT NULL, projekt TEXT NOT NULL);")
    erg = SqliteReferenzRepository(datenbank_einstellungen).uebernehmen([("B", "Text B")])[0]
    assert not erg.uebernommen and "NOT NULL" in erg.meldung and "projekt" in erg.meldung
    assert paare_in_datei(datenbank_einstellungen) == []        # Transaktion zurückgerollt


def test_andere_tabellen_und_feldnamen_mit_sonderzeichen(datenbank_einstellungen, sql):
    sql('CREATE TABLE "Meine ""Tabelle""" (pk INTEGER PRIMARY KEY, "Sym bol" TEXT, langtext TEXT);'
        'INSERT INTO "Meine ""Tabelle""" ("Sym bol", langtext) VALUES (\'X\', \'Text X\');')
    e = dataclasses.replace(datenbank_einstellungen, tabellenname='Meine "Tabelle"', feld_symbol="Sym bol",
                            feld_funktionstext="langtext", primaerschluessel="pk")
    repo = SqliteReferenzRepository(e)
    assert repo.lade_referenzdaten() == [("X", "Text X")]
    assert repo.uebernehmen([("Y", "Text Y")])[0].uebernommen


def test_namen_ohne_beachtung_der_schreibweise(datenbank_einstellungen):
    e = dataclasses.replace(datenbank_einstellungen, tabellenname="SYMBOLE", feld_symbol="Symbol")
    assert len(SqliteReferenzRepository(e).lade_referenzdaten()) == 3


def test_null_und_zahlenwerte_werden_zu_texten(datenbank_einstellungen, sql):
    sql("DROP TABLE symbole; CREATE TABLE symbole (id INTEGER PRIMARY KEY, symbol, funktionstext);"
        "INSERT INTO symbole (symbol, funktionstext) VALUES ('A', NULL), (42, 3.5);")
    assert SqliteReferenzRepository(datenbank_einstellungen).lade_referenzdaten() == [("A", ""), ("42", "3.5")]


@pytest.mark.parametrize("name", ["", "   ", "a\x00b", "x" * 129, "a\nb"])
def test_unzulaessige_bezeichner(name):
    with pytest.raises(DatenbankFehler):
        quotiere_bezeichner(name)


def test_bezeichner_wird_quotiert():
    assert quotiere_bezeichner(" Meine Tabelle ") == '"Meine Tabelle"'
    assert quotiere_bezeichner('a"b') == '"a""b"'


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="Dateirechte greifen für root/Windows nicht")
def test_schreibgeschuetzte_datei(datenbank_einstellungen):
    os.chmod(datenbank_einstellungen.dateipfad, stat.S_IRUSR)
    repo = SqliteReferenzRepository(datenbank_einstellungen)
    assert any("schreibgeschützt" in m for m in repo.teste_verbindung().meldungen)
    erg = repo.uebernehmen([("A", "B")])
    assert not erg[0].uebernommen


def test_gesperrte_datei_meldet_verstaendlich(datenbank_einstellungen):
    sperre = sqlite3.connect(datenbank_einstellungen.dateipfad, isolation_level=None)
    sperre.execute("BEGIN EXCLUSIVE")                   # anderes Programm hält die Datei exklusiv
    try:
        repo = SqliteReferenzRepository(dataclasses.replace(datenbank_einstellungen, timeout_s=1))
        with pytest.raises(DatenbankFehler, match="gesperrt"):
            repo.lade_referenzdaten()
        with pytest.raises(DatenbankFehler, match="gesperrt"):
            repo.uebernehmen([("A", "Text")])
        ergebnis = repo.teste_verbindung()
        assert not ergebnis.erfolgreich and "gesperrt" in ergebnis.meldungen[-1]
    finally:
        sperre.execute("ROLLBACK")
        sperre.close()
    assert len(SqliteReferenzRepository(datenbank_einstellungen).lade_referenzdaten()) == 3      # danach wieder lesbar


def test_tabellen_und_spalten_fuer_die_auswahl(datenbank_einstellungen):
    assert lese_tabellen(datenbank_einstellungen.dateipfad) == ["symbole"]
    assert lese_spaltennamen(datenbank_einstellungen.dateipfad, "symbole") == ["id", "symbol", "funktionstext"]
    with pytest.raises(DatenbankFehler):
        lese_tabellen(datenbank_einstellungen.dateipfad + ".fehlt")


def test_fabrik_liefert_sqlite(datenbank_einstellungen):
    assert isinstance(erzeuge_repository(datenbank_einstellungen), SqliteReferenzRepository)


def test_datenbankabgleich_pruefung_gegen_echte_datei_mit_status_je_datensatz(repository):
    from eplan_pruefer.modelle import DatenbankStatus
    from eplan_pruefer.pruefer import DatenbankAbgleichPruefung, Pruefkontext, Textnormalisierer
    daten = [
        baue_datensatz("Motor_Start", "Förderband Start", "E0.0", nummer=1),       # ok
        baue_datensatz("Unbekannt", "Irgendwas", "E0.1", nummer=2),                 # Symbol fehlt
        baue_datensatz("Lampe", "Fehlender Text", "E0.2", nummer=3),                # Text fehlt
        baue_datensatz("Lampe", "Förderband Stop", "E0.3", nummer=4),               # Kombination
        baue_datensatz("MOTOR_START", "förderband  start", "E0.4", nummer=5),       # ok (Schreibweise egal)
    ]
    kontext = Pruefkontext(Textnormalisierer(), repository)
    fehler = DatenbankAbgleichPruefung().pruefe(daten, kontext)
    assert [(f.datensatz.laufende_nummer, f.fehlertyp) for f in fehler] == [
        (2, Fehlertyp.SYMBOL_NICHT_IN_DB), (3, Fehlertyp.FUNKTIONSTEXT_NICHT_IN_DB),
        (4, Fehlertyp.KOMBINATION_ABWEICHEND)]
    assert kontext.datenbankstatus == {
        1: DatenbankStatus.VORHANDEN, 2: DatenbankStatus.SYMBOL_FEHLT, 3: DatenbankStatus.FUNKTIONSTEXT_FEHLT,
        4: DatenbankStatus.KOMBINATION_ABWEICHEND, 5: DatenbankStatus.VORHANDEN}
