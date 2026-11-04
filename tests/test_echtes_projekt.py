"""Nachbildung eines echten EPLAN-Projekts, wie es ein Anwender gemeldet hat:
UTF-16-``*.elk`` als Verweis, ``*.edb``-Ordner mit Sperrdateien (*.lck), ProjectInfo.xml,
Office-Dokumenten und proprietären Datendateien."""

from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path

import pytest

from eplan_pruefer import eplan_parser
from eplan_pruefer.eplan_parser import EplanDateiLeser, erzeuge_diagnosebericht
from eplan_pruefer.modelle import Einstellungen

LOCK_NAMEN = ["Article", "Page", "Function", "Plc", "PlcIoEntries", "Project", "User"]


def docx_bytes() -> bytes:
    puffer = io.BytesIO()
    with zipfile.ZipFile(puffer, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", "<w:document xmlns:w='x'><w:p/></w:document>")
    return puffer.getvalue()


@pytest.fixture
def projekt(tmp_path: Path) -> Path:
    elk = tmp_path / "80079501.elk"
    elk.write_bytes("80079501.edb\n17.0.0.0\n".encode("utf-16"))
    edb = tmp_path / "80079501.edb"
    (edb / "DOC").mkdir(parents=True)
    (edb / "CCL1ESS.sdb").mkdir()
    for name in LOCK_NAMEN:
        (edb / f"{name}.lck").write_bytes(b"")
        (edb / "CCL1ESS.sdb" / f"{name}.lck").write_bytes(b"")
    (edb / "ProjectInfo.xml").write_text('<?xml version="1.0"?><ProjectInfo Name="Test"/>', encoding="utf-8")
    (edb / "DOC" / "Schlauchtyp.docx").write_bytes(docx_bytes())
    (edb / "Page.dat").write_bytes(os.urandom(5000))
    (edb / "Function.dat").write_bytes(b"EDBF\x01\x00" + os.urandom(3000))
    return elk


def lese(pfad: Path):
    return EplanDateiLeser(Einstellungen()).lese(pfad)


def test_lck_dateien_werden_still_gezaehlt_statt_hunderte_warnungen(projekt, caplog):
    with caplog.at_level("WARNING"):
        ergebnis = lese(projekt)
    assert not [r for r in caplog.records if ".lck" in r.getMessage()]
    sperren = [t for t in ergebnis.teildateien if t.art == "ignoriert"]
    assert len(sperren) == 1 and sperren[0].name.startswith("14 Sperrdatei")


def test_elk_verweis_inhalt_ist_im_bericht_sichtbar(projekt):
    ergebnis = lese(projekt)
    assert "Inhalt (Textdatei):" in ergebnis.strukturbericht
    assert "| 80079501.edb" in ergebnis.strukturbericht and "17.0.0.0" in ergebnis.strukturbericht


def test_office_dokumente_werden_nicht_zerlegt(projekt):
    ergebnis = lese(projekt)
    assert not any("[Content_Types].xml" in t.name or "word/" in t.name for t in ergebnis.teildateien)
    assert any(t.art == "nicht unterstützt" and ".docx" in t.name for t in ergebnis.teildateien)


def test_unbekannte_datendateien_werden_je_endung_diagnostiziert(projekt):
    bericht = lese(projekt).strukturbericht
    assert "Nicht ausgewertete Dateien" in bericht
    assert "  .dat: 2 Datei(en), 8.0" in bericht or "  .dat: 2 Datei(en), 8." in bericht
    assert "Beispiel: 80079501.edb/Page.dat" in bericht
    assert "Entropie (Kopf)" in bericht and "Kopf: " in bericht


def test_warnung_erklaert_edb_datenbankformat(projekt):
    ergebnis = lese(projekt)
    assert ergebnis.datensaetze == []
    assert len(ergebnis.warnungen) == 1
    assert "80079501.edb" in ergebnis.warnungen[0] and "Datenbankformat" in ergebnis.warnungen[0]


def test_wirklich_gesperrte_datei_wird_zusammengefasst(projekt, monkeypatch):
    original = eplan_parser._datei_oeffner

    def oeffner(pfad: Path):
        if pfad.name.startswith("Page"):
            def gesperrt():
                raise PermissionError(13, "Permission denied")
            return gesperrt
        return original(pfad)

    monkeypatch.setattr(eplan_parser, "_datei_oeffner", oeffner)
    ergebnis = lese(projekt)
    assert any("gesperrt" in w and "in EPLAN geöffnet" in w for w in ergebnis.warnungen)
    assert any(t.art == "gesperrt" for t in ergebnis.teildateien)
    assert "Beispiel: 80079501.edb/Page.dat" not in ergebnis.strukturbericht   # gesperrt, nicht analysiert


def test_diagnosebericht_enthaelt_alles_wesentliche(projekt):
    text = erzeuge_diagnosebericht(lese(projekt))
    for erwartet in ("Quelle:", "Teildateien:", "Warnungen:", "Strukturbericht:", "80079501.edb",
                     "Nicht ausgewertete Dateien", "[ignoriert]"):
        assert erwartet in text


def test_diagnosebericht_in_der_oberflaeche(projekt, tmp_path, monkeypatch):
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication
    from eplan_pruefer import gui
    from eplan_pruefer.einstellungen import EinstellungsVerwalter
    from .test_gui import warte

    app = QApplication.instance() or QApplication([])
    meldungen = []
    monkeypatch.setattr(gui, "zeige_information", lambda *a: meldungen.append(a[-1]))
    monkeypatch.setattr(gui, "zeige_warnung", lambda *a: meldungen.append(a[-1]))
    monkeypatch.setattr(gui.EinleseberichtDialog, "exec", lambda self: 0)
    fenster = gui.Hauptfenster(EinstellungsVerwalter(tmp_path / "k.ini"), Einstellungen(), None)
    try:
        assert not fenster.a_diagnose.isEnabled()
        fenster.datei_laden(projekt)
        warte(fenster)
        assert fenster.a_diagnose.isEnabled()
        ziel = tmp_path / "diagnose.txt"
        assert fenster.diagnose_speichern_nach(ziel)
        assert "Datenbankformat" in ziel.read_text(encoding="utf-8")
        assert any("Dateipfade" in m for m in meldungen)
        assert app is not None
    finally:
        fenster.close()
