"""
Rauchtest der gebauten Anwendung (eingefrorene .exe bzw. Binärdatei).

Erzeugt Beispieldaten (CSV, DXF im Projektbaum, XML) und eine SQLite-Referenzdatenbank, startet die
Anwendung ohne Bildschirm (QT_QPA_PLATFORM=offscreen) mit diesen Daten und prüft anhand der
Protokolldatei, dass Einlesen, DXF-Erkennung, Prüfungen und Datenbankabgleich im eingefrorenen
Programm funktionieren (Importe, lxml, sqlite3, Qt-Plugins).

    python scripts/rauchtest.py dist/EplanPruefer.exe

Exit-Code 0 = bestanden, 1 = fehlgeschlagen (mit Ausgabe des Protokolls).
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ERWARTET = (
    "[DXF]: 2 Datensätze",
    "[CSV/Text]: 2 Datensätze",
    "Einlesen beendet: 4 Datensätze",
    "Prüfung „Doppelte Symbole“: 1 Fehler",
    "Prüfung „Platzhalter (Fragezeichen)“: 2 Fehler",
    "Datenbankabgleich: 1 von 4 Datensätzen vollständig in der Datenbank vorhanden",
    "Prüfung beendet:",
)
ZEITLIMIT_S = 90


def _paare(*gruppen: tuple[int, object]) -> str:
    return "".join(f"{code:>3}\n{wert}\n" for code, wert in gruppen)


def _block(symbol: str, adresse: str) -> str:
    attribute = {"17_20402": symbol, "17_20400": adresse, "17_20031": "Spaenefoerderer vor",
                 "17_20006": "=G2+E2-KA42.0"}
    s = _paare((0, "INSERT"), (8, "SPS"), (2, "EPLSYM_Test"), (10, 0), (20, 0), (66, 1))
    for marke, wert in attribute.items():
        s += _paare((0, "ATTRIB"), (8, "SPS"), (1, wert), (2, marke), (70, 0))
    return s + _paare((0, "SEQEND"))


def _rahmen() -> str:
    s = _paare((0, "INSERT"), (8, "0"), (2, "EPLFRAME"), (10, 0), (20, 0), (66, 1))
    for marke, wert in {"4_11009": "=N2+E2/1", "4_11011": "Spaenefoerderer 1"}.items():
        s += _paare((0, "ATTRIB"), (8, "0"), (1, wert), (2, marke), (70, 0))
    return s + _paare((0, "SEQEND"))


def erzeuge_daten(wurzel: Path) -> Path:
    daten = wurzel / "daten"
    (daten / "N2" / "E2").mkdir(parents=True)
    dxf = (_paare((0, "SECTION"), (2, "ENTITIES")) + _block("N2ASpfVor", "A42.0")
           + _block("N2ASpfZur", "A42.1") + _rahmen() + _paare((0, "ENDSEC"), (0, "EOF")))
    (daten / "N2" / "E2" / "_N2_E2_1.dxf").write_text(dxf, encoding="utf-8")
    (daten / "liste.csv").write_text(
        "Symbolische Adresse;Funktionstext;SPS-Adresse;Seite\nMotor_?;Text;E0.0;1\nMotor_?;Text;E0.0;2\n",
        encoding="utf-8")
    (daten / "ProjectInfo.xml").write_text('<ProjectInfo Name="Rauchtest"/>', encoding="utf-8")
    return daten


def erzeuge_datenbank(pfad: Path) -> None:
    verbindung = sqlite3.connect(pfad)
    verbindung.executescript(
        "CREATE TABLE symbole (id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, funktionstext TEXT NOT NULL);"
        "INSERT INTO symbole (symbol, funktionstext) VALUES ('N2ASpfVor', 'Spaenefoerderer vor');")
    verbindung.commit()
    verbindung.close()


def beende(prozess: subprocess.Popen) -> None:
    if prozess.poll() is not None:
        return
    if os.name == "nt":      # --onefile startet einen Kindprozess: ganzen Baum beenden
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(prozess.pid)], capture_output=True, check=False)
    else:
        prozess.kill()
    try:
        prozess.wait(timeout=15)
    except subprocess.TimeoutExpired:
        pass


def _ausgabe_auf_utf8_stellen() -> None:
    """Windows-Konsolen verwenden oft cp1252; Ausgaben dürfen den Rauchtest nie zum Absturz bringen."""
    for strom in (sys.stdout, sys.stderr):
        try:
            strom.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argumente: list[str]) -> int:
    _ausgabe_auf_utf8_stellen()
    if len(argumente) != 1:
        print(__doc__)
        return 1
    programm = Path(argumente[0]).resolve()
    if not programm.is_file():
        print(f"FEHLER: Programm nicht gefunden: {programm}")
        return 1

    arbeit = Path(tempfile.mkdtemp(prefix="eplan_rauchtest_"))
    daten = erzeuge_daten(arbeit)
    datenbank = arbeit / "referenz.db"
    erzeuge_datenbank(datenbank)
    konfiguration = arbeit / "einstellungen.ini"
    konfiguration.write_text(
        f"[datenbank]\ndateipfad = {datenbank}\ntabellenname = symbole\nfeld_symbol = symbol\n"
        "feld_funktionstext = funktionstext\nprimaerschluessel = id\n[pruefung]\ndatenbankabgleich = ja\n",
        encoding="utf-8")
    protokoll = arbeit / "protokoll" / "eplan_pruefer.log"

    umgebung = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    prozess = subprocess.Popen([str(programm), str(daten), "--konfiguration", str(konfiguration)],
                               env=umgebung, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    text, bestanden, ende = "", False, time.monotonic() + ZEITLIMIT_S
    try:
        while time.monotonic() < ende:
            if protokoll.exists():
                text = protokoll.read_text(encoding="utf-8", errors="replace")
                if all(muster in text for muster in ERWARTET):
                    bestanden = True
                    break
            if prozess.poll() is not None:
                text = protokoll.read_text(encoding="utf-8", errors="replace") if protokoll.exists() else ""
                print(f"FEHLER: Das Programm hat sich vorzeitig beendet (Exit-Code {prozess.returncode}).")
                break
            time.sleep(0.5)
    finally:
        beende(prozess)

    if bestanden:
        print("Rauchtest bestanden:")
        for muster in ERWARTET:
            print(f"  [ok] {muster}")
        return 0
    print("Rauchtest FEHLGESCHLAGEN. Nicht gefunden:")
    for muster in ERWARTET:
        if muster not in text:
            print(f"  [FEHLT] {muster}")
    print("--- Protokoll (Ende) ---")
    print("\n".join(text.splitlines()[-40:]) or "(kein Protokoll)")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
