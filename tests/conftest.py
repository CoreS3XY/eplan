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
# Echter MariaDB/MySQL-Server für die Datenbank-Integrationstests
# ---------------------------------------------------------------------------

import shutil  # noqa: E402
import socket  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402

from eplan_pruefer.modelle import DatenbankEinstellungen  # noqa: E402

_SERVER_PROGRAMM = shutil.which("mariadbd") or shutil.which("mysqld")
_INSTALLER = shutil.which("mariadb-install-db") or shutil.which("mysql_install_db")
_CLIENT = shutil.which("mariadb") or shutil.which("mysql")


def _freier_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def mysql_server(tmp_path_factory):
    """Startet für die Testsitzung einen eigenen MariaDB-Server; überspringt, falls keiner installiert ist."""
    if not (_SERVER_PROGRAMM and _INSTALLER and _CLIENT):
        pytest.skip("Kein lokaler MariaDB/MySQL-Server installiert")
    basis = tmp_path_factory.mktemp("mysql")
    daten, socket_pfad, port = basis / "daten", basis / "s.sock", _freier_port()
    subprocess.run([_INSTALLER, "--no-defaults", "--user=root", f"--datadir={daten}",
                    "--auth-root-authentication-method=normal"],
                   check=True, capture_output=True)
    prozess = subprocess.Popen(
        [_SERVER_PROGRAMM, "--no-defaults", "--user=root", f"--datadir={daten}",
         f"--socket={socket_pfad}", f"--port={port}", "--bind-address=127.0.0.1"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            if prozess.poll() is not None:
                pytest.skip("MariaDB-Server ließ sich nicht starten")
            fertig = subprocess.run([_CLIENT, "--no-defaults", "-uroot", f"--socket={socket_pfad}",
                                     "-e", "SELECT 1"], capture_output=True)
            if fertig.returncode == 0:
                break
            time.sleep(0.2)
        else:
            pytest.skip("MariaDB-Server wurde nicht rechtzeitig bereit")
        subprocess.run([_CLIENT, "--no-defaults", "-uroot", f"--socket={socket_pfad}", "-e",
                        "CREATE DATABASE referenz CHARACTER SET utf8mb4; "
                        "DROP USER IF EXISTS ''@'localhost'; DROP USER IF EXISTS ''@'%'; "
                        "CREATE USER 'pruefer'@'%' IDENTIFIED BY 'geheim-Pw_1'; "
                        "CREATE USER 'pruefer'@'localhost' IDENTIFIED BY 'geheim-Pw_1'; "
                        "GRANT ALL ON referenz.* TO 'pruefer'@'%'; "
                        "GRANT ALL ON referenz.* TO 'pruefer'@'localhost'; FLUSH PRIVILEGES;"],
                       check=True, capture_output=True)
        yield {"port": port, "socket": str(socket_pfad)}
    finally:
        prozess.terminate()
        try:
            prozess.wait(timeout=15)
        except subprocess.TimeoutExpired:
            prozess.kill()


@pytest.fixture
def datenbank_einstellungen(mysql_server) -> DatenbankEinstellungen:
    return DatenbankEinstellungen(
        server="127.0.0.1", port=mysql_server["port"], datenbankname="referenz",
        benutzer="pruefer", passwort="geheim-Pw_1", tabellenname="symbole",
        feld_symbol="symbol", feld_funktionstext="funktionstext", primaerschluessel="id",
        tls_verwenden=False, verbindungs_timeout_s=5)


@pytest.fixture
def sql(mysql_server):
    """Führt SQL als Administrator aus (zum Vorbereiten der Referenztabelle)."""
    def ausfuehren(anweisung: str) -> None:
        subprocess.run([_CLIENT, "--no-defaults", "-uroot", f"--socket={mysql_server['socket']}",
                        "referenz", "--default-character-set=utf8mb4", "-e", anweisung],
                       check=True, capture_output=True)
    return ausfuehren
