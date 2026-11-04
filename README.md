# EPLAN-Prüfer

Desktop-Anwendung (Python, PyQt6) zur Analyse von SPS-Anschlüssen aus EPLAN-Projekten.
Sie liest Symbolische Adresse, Funktionstext, SPS-Adresse, Seitenreferenz und Betriebsmittelkennzeichen ein
und prüft sie auf

1. doppelte Symbole,
2. doppelte Funktionstexte,
3. doppelte SPS-Adressen,
4. Platzhalter (Fragezeichen),
5. **optional:** Abgleich mit einer SQLite-Referenzdatenbank (`*.db`) – zeigt je Datensatz, ob das Symbol samt
   Funktionstext dort vorhanden ist (Symbol, Funktionstext, Kombination).

Fehler lassen sich mit Seitennummer, Seitenname und Quellauszug anzeigen, als CSV/Excel exportieren und – soweit
„nicht in Datenbank vorhanden" – direkt in die Referenzdatenbank übernehmen.

## Wichtig: Stand der Unterstützung für `*.elk` und `*.zw1`

Die Formate `*.elk` (Projekt) und `*.zw1` (Projektsicherung) sind von EPLAN **nicht öffentlich dokumentiert**, und bei
der Entwicklung lagen **keine echten Projektdateien** vor. Das Programm rät deshalb keinen Binärparser, sondern:

* öffnet die Datei nach ihrem **tatsächlichen Aufbau** (ZIP – auch hinter einem Vorspann –, GZIP, verschachtelte Archive;
  ein `*.edb`-Ordner neben einer `*.elk` wird mitgelesen) und liest darin **XML, CSV/TSV, XLSX und SQLite**;
* zeigt bei jeder Datei einen **Strukturbericht** (Signaturen, Codierung, Entropie, Archivinhalt);
* meldet bei einem nicht lesbaren Binärformat ehrlich „keine SPS-Datensätze gefunden", statt falsche Daten zu liefern;
* liest **Exporte aus EPLAN** (SPS-Adressliste als CSV/Excel/XML) zuverlässig – das ist der sichere Weg.

Die Feldbezeichnungen (auch EPLAN-Eigenschaftsnummern wie `P20400`) sind im Einstellungsdialog konfigurierbar.
Ob Ihre Projektdateien direkt lesbar sind, zeigt der erste Öffnungsversuch. Details und Alternativen: [`docs/ANALYSE.md`](docs/ANALYSE.md).

## Unterstützte Eingabeformate

| Format | Inhalt | Stand |
|---|---|---|
| `*.txt` / `*.csv` / `*.tsv` | Listen, z. B. `Symbolische Adresse ⇥ Adresse ⇥ Funktionstext ⇥ Sichtbares BMK ⇥ Vollständiges BMK`; Trennzeichen wird erkannt (Tabulator, `;`, `,`), auch bei mehreren Tabulatoren zur Ausrichtung | getestet mit dem Beispiel des Anwenders |
| `*.dxf` | eine Seite des E-Plans je Datei; Datensätze aus den **Block-Attributen** des EPLAN-Exports (`17_20400` Adresse, `17_20402` Symbol, `17_20031` Funktionstext, `17_20006` BMK; Seite und Seitenname aus dem Rahmenblock `4_11009`/`4_11011`), ersatzweise aus einer Texttabelle; Seite sonst aus dem Dateinamen | an einer **echten EPLAN-DXF (R2018)** verifiziert: liefert die 6 erwarteten Zeilen. Andere EPLAN-Versionen: Feldzuordnung anpassen |
| `*.xlsx`, `*.xml`, SQLite | siehe oben | getestet |
| `*.elk`, `*.zw1` | siehe oben | Format proprietär, nicht direkt lesbar, siehe `docs/ANALYSE.md` |

DXF-Projektbäume öffnen Sie über **Datei ▸ Ordner öffnen** (oder Ordner in das Fenster ziehen): alle Dateien in allen
Unterordnern werden eingelesen, der Pfad steht in der Fundstelle.

**Seite bei Listen ohne Seitenspalte:** Hat die Quelle keine Seite, aber ein sichtbares BMK der Form `=U4+E2/2.2`,
wird dies als Fundstelle gelesen (Seitenreferenz `=U4+E2/2`, Seitennummer `2`; die Spalte `.2` bleibt im BMK).
Das ist eine Annahme über das EPLAN-Format – bitte prüfen.

## Installation

Voraussetzung: **Python 3.11 oder neuer** (entwickelt und getestet mit Python 3.13).

### Windows

1. Python von <https://www.python.org/downloads/> installieren (Haken bei „Add python.exe to PATH").
2. Eingabeaufforderung im Projektordner öffnen und eine virtuelle Umgebung anlegen:
   ```bash
   py -m venv .venv
   .venv\Scripts\activate
   ```
3. Abhängigkeiten installieren:
   ```bash
   pip install -r requirements.txt
   ```
4. Programm starten:
   ```bash
   py main.py
   ```

### Linux / macOS

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

Unter Linux benötigt Qt einige Systembibliotheken, z. B. auf Debian/Ubuntu:

```bash
sudo apt-get install libegl1 libgl1 libxkbcommon0 libfontconfig1 libdbus-1-3
```

> **Start nur über `main.py` oder `python -m eplan_pruefer`.** Das Modul `eplan_pruefer/logging.py` trägt – wie
> in der Aufgabenstellung vorgegeben – den Namen der Standardbibliothek; es darf nicht direkt aus dem Paketordner
> gestartet werden (z. B. `python eplan_pruefer/gui.py`).

Kommandozeile: `python main.py [Datei] [--konfiguration PFAD] [--protokollstufe DEBUG|INFO|WARNING|ERROR]`

## Erste Schritte

1. **Datenbankabgleich (optional):** *Extras ▸ Einstellungen ▸ Datenbank*: SQLite-Datei (`*.db`) über **Durchsuchen …**
   wählen – Tabelle und Felder (Symbol, Funktionstext, optional Primärschlüssel) lassen sich danach aus der Datei
   auswählen –, **Verbindung testen**, **Speichern**. Eingeschaltet wird der Abgleich über *Prüfung ▸ Datenbankabgleich
   durchführen (optional)* (oder den Reiter „Prüfungen"); er ist standardmäßig **aus**. Ist er an, zeigt die Spalte
   **Datenbank** der Datensatzliste je Datensatz „In Datenbank vorhanden", „Symbol fehlt in Datenbank",
   „Funktionstext fehlt in Datenbank" oder „Kombination weicht ab"; abweichende Datensätze erscheinen zusätzlich in der
   Fehlerliste. Ohne Datenbankangaben laufen die Prüfungen 1–4; der Abgleich wird mit Hinweis übersprungen.
   Die Datei wird zum Abgleich nur **gelesen**; geschrieben wird ausschließlich über „In Datenbank übernehmen" und nie
   in eine neu angelegte Datei. Vor dem ersten Übernehmen empfiehlt sich eine Sicherungskopie der `*.db`.
2. **Datei öffnen** (oder in das Fenster ziehen): `*.elk`, `*.zw1` oder ein Export (`*.csv`, `*.xlsx`, `*.xml`).
   Einlesen und Prüfung laufen im Hintergrund, Esc bricht ab.
3. **Fehlerliste:** Filter (siehe unten), Sortierung (Seiten numerisch), Spalte **Verweis** mit der Fundstelle der
   ersten Verwendung bei Duplikaten. Auswahl eines Fehlers zeigt unten
   Beschreibung, Seitennummer, Seitenname, Seitenreferenz, Fundstelle und – bei Duplikaten – die erste Verwendung.
   **Seite anzeigen** (Doppelklick, Strg+G) öffnet Seite, alle Einträge der Seite und den Quellauszug.
4. **Export:** *Datei ▸ Fehlerliste als CSV/Excel exportieren*. Spalten: Fehlertyp, Symbol, Funktionstext, Adresse,
   Seite, Beschreibung. Ist ein Filter aktiv, wird gefragt, ob nur die sichtbaren Fehler exportiert werden.
5. **Datenbankpflege:** Fehler „Symbol/Funktionstext nicht in Datenbank vorhanden" markieren (mehrere möglich),
   **In Datenbank übernehmen** (Strg+D). Das Programm prüft Symbol, Funktionstext und Kombination, fragt bei
   Konflikten nach und prüft danach automatisch erneut.

## Filter

Jede Spalte der Fehlerliste **und** der Datensatzliste hat unter der Überschrift ein Filterfeld; zusätzlich gibt es eine
**Gesamtsuche** über alle Spalten und (in der Fehlerliste) die Auswahl nach Fehlertyp. **Filter zurücksetzen** löscht alle.
Der Export berücksichtigt auf Nachfrage nur die sichtbaren Fehler.

| Eingabe | Bedeutung |
|---|---|
| `Motor` | enthält „Motor" (ohne Platzhalter immer „enthält") |
| `Motor*` / `*Stop` / `*Stop*` | beginnt mit / endet mit / enthält |
| `E0.?` | `?` = genau ein Zeichen (`E0.0`, aber nicht `E0.15`) |
| `A*;E*` | mehrere Muster mit `;` = ODER |
| `!E0.*` | **negativ**: Treffer ausschließen |
| `Motor*;!*Stop` | positiv und negativ kombiniert: beginnt mit „Motor", endet aber nicht auf „Stop" |
| `!?*` / `?*` | nur leere / nur gefüllte Zellen |
| `\?` | das Zeichen `?` selbst (Platzhalter-Symbole wie `Motor_?`); ebenso `\*`, `\!`, `\;` |

Groß-/Kleinschreibung spielt keine Rolle. Ein negatives Muster gewinnt immer gegen ein positives. In der Gesamtsuche
schließt ein negatives Muster die Zeile aus, sobald es in irgendeiner Spalte trifft.

## Konfiguration und Protokoll

* Konfiguration (INI): Windows `%APPDATA%\EplanPruefer\einstellungen.ini`, sonst `~/.config/eplan_pruefer/einstellungen.ini`
  (oder `--konfiguration`). Sie wird vom Programm geschrieben und ist unter Linux/macOS nur für den Benutzer lesbar.
  Eine alte Konfiguration aus der MySQL-Zeit wird weiter gelesen (Tabelle und Feldnamen bleiben); Server, Benutzer und
  Passwort werden ignoriert und beim nächsten Speichern entfernt – die Datenbankdatei muss neu gewählt werden.
* Protokoll: Ordner `protokoll` neben der Konfiguration, rotierend (5 × 1 MB). Es protokolliert alle Vorgänge (Öffnen,
  Einlesen je Teildatei, Prüfungen, Datenbankzugriffe, Übernahmen, Export, Fehler mit Stacktrace); geheime Werte werden
  maskiert. Anzeige: *Extras ▸ Protokoll anzeigen*.

Eine passende Beispieltabelle steht in [`docs/ARCHITEKTUR.md`](docs/ARCHITEKTUR.md#datenbankmodell); Tabellen- und
Feldnamen sind frei wählbar.

## Projektstruktur

```
eplan/
├── main.py                    Startskript
├── requirements.txt           Laufzeit-Abhängigkeiten
├── requirements-dev.txt       + pytest
├── pytest.ini
├── README.md
├── docs/
│   ├── ANALYSE.md             Formatanalyse, Bibliotheken, Risiken
│   └── ARCHITEKTUR.md         Klassenmodell, Datenfluss, Datenbankmodell, Erweiterung
├── eplan_pruefer/
│   ├── modelle.py             Datenklassen (Datensatz, Fehler, Einstellungen, Fehlerklassen)
│   ├── logging.py             Protokollierung (Datei, Speicher, Geheimnis-Maskierung)
│   ├── einstellungen.py       Laden/Speichern der INI-Konfiguration
│   ├── dateianalyse.py        Strukturanalyse beliebiger Dateien
│   ├── eplan_parser.py        Container (ZIP/GZIP/Ordner) + Extraktoren (XML/CSV/XLSX/SQLite)
│   ├── datenbank.py           SQLite-Zugriff (Repository-Schnittstelle + SQLite-Implementierung)
│   ├── suchfilter.py          Wildcard-Filter (positiv/negativ)
│   ├── dxf_parser.py          DXF-Seiten (Block-Attribute, Texttabellen)
│   ├── pruefer.py             Die fünf Prüfungen und der Prüfmanager
│   ├── export.py              CSV- und Excel-Export
│   ├── gui.py                 Hauptfenster und Programmstart
│   ├── gui_dialoge.py         Einstellungen, Seitenansicht, Berichte
│   ├── gui_modelle.py         Qt-Tabellenmodelle, Spaltenfilter
│   ├── gui_filter.py          Tabellenkopf mit Filterzeile
│   └── gui_arbeiter.py        Hintergrund-Threads
└── tests/                     222 automatische Tests
```

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

Die Datenbanktests laufen gegen **echte SQLite-Dateien** (kein Server nötig), die GUI-Tests ohne Bildschirm
(`QT_QPA_PLATFORM=offscreen`). Ein Test (schreibgeschützte Datei) wird als Superuser/unter Windows übersprungen.

**Nicht getestet** wurden: Ausführung unter Windows und macOS und – mangels Beispieldateien – echte `*.elk`/`*.zw1`-Projekte.

## Erweiterungsmöglichkeiten

* **Weitere EPLAN-Versionen / Formate:** neuer `DatensatzExtraktor`; bei anderen Feldnamen genügt die Feldzuordnung.
  Container, Strukturanalyse, Seiten- und Quellenanzeige werden wiederverwendet.
* **EPLAN-API-Anbindung:** automatischer SPS-Export über `PlcService` (siehe `docs/ANALYSE.md`), Ergebnis wird wie
  jede CSV eingelesen.
* **MySQL / SQL Server / Oracle:** weitere Klasse neben `SqliteReferenzRepository` (Schnittstelle `ReferenzRepository`),
  Auswahl über ein Konfigurationsfeld „Datenbanktyp". Eine frühere MySQL-Umsetzung steht in der Git-Historie.
* **PDF-Export:** weitere Klasse von `FehlerExporter` und Eintrag in `EXPORTER`.
* **Auslieferung als `.exe`:** z. B. mit PyInstaller (hier nicht erprobt).
