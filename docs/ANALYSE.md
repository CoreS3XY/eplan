# Technische Analyse

## 1. Dateiformate

### Was belegt ist

| Format | Belegte Aussage | Quelle |
|---|---|---|
| `*.zw1` | Projektsicherung. Entsteht in EPLAN über *Projekt ▸ Sichern ▸ Projekt*; das Projekt `Name.elk` wird als `Name.zw1` gesichert und enthält Projektdaten samt Stammdaten, Symbolen und Artikeln. | [EPLAN-Hilfe „Projekte sichern"](https://www.eplan.help/en-us/infoportal/content/Plattform/2.9/Content/htm/bakbackupdlggui_d_projektesichern.htm) |
| `*.elk` | Das EPLAN-Projekt selbst. Neben der `*.elk` liegt der Projektordner `*.edb`. | dieselbe Quelle; die genaue Aufteilung der Daten ist **nicht** dokumentiert |
| `*.zw0` / `*.elp` | gepackte Projekte (anderer Dateityp als `*.zw1`) | EPLAN-Hilfe |
| SPS-Export | Die EPLAN-API enthält den Dienst `PlcService` mit Exportmethoden, u. a. `ExportAddressOverview` (CSV-Adressübersicht). | [EPLAN Platform API](https://www.eplan.help/en-US/Infoportal/content/api/2027/Eplan.EplApi.HEServicesu~Eplan.EplApi.HEServices.PlcService.html) |

### Was nicht belegt ist

* **Der innere Aufbau von `*.elk` und `*.zw1` ist öffentlich nicht spezifiziert.** Keine der gefundenen Quellen bestätigt, dass `*.zw1` ein ZIP-Container ist; ebenso wenig ist belegt, in welchem Format die Projektdaten im Inneren liegen.
* **Es standen bei der Entwicklung keine echten EPLAN-Dateien zur Verfügung.** Ein „Parser anhand der tatsächlichen Dateistruktur" konnte daher nicht entwickelt werden, ohne die Struktur zu raten. Ein geratener Binärparser würde falsche Ergebnisse als richtig ausgeben – für ein Prüfwerkzeug die schlechteste Eigenschaft.
* **Die EPLAN-Eigenschaftsnummern für SPS-Adresse und symbolische Adresse sind nicht eindeutig belegt.** Die Quellen widersprechen sich (EPLAN-TechTipp: 20400/20404; eine Drittquelle: 40001/40091). Das Programm trägt deshalb **keine** Nummern fest ein; sie sind in der Feldzuordnung konfigurierbar.
* Die Eigenschaftsnummer für den Funktionstext konnte nicht belegt werden.
* Die EPLAN-Hilfeseiten waren aus der Entwicklungsumgebung nicht abrufbar (Netzwerkfreigabe); die Signatur von `ExportAddressOverview` wurde daher nicht gelesen.

## 2. Gewählter Ansatz

Weil das Format unbekannt ist, geht das Programm **in zwei getrennten Stufen** vor, die beide ohne Annahmen über EPLAN-Interna auskommen:

1. **Container** (`ContainerLeser`) – öffnet die Datei nach ihrem *tatsächlichen* Aufbau, nicht nach der Endung: ZIP (auch hinter einem proprietären Vorspann), GZIP, verschachtelte Archive, Verzeichnisse (die `*.edb` neben einer `*.elk` wird automatisch mitgelesen).
2. **Extraktoren** (`DatensatzExtraktor`) – erkennen den Inhalt jeder Teildatei und lesen SPS-Datensätze: **XML**, **CSV/TSV**, **XLSX**, **SQLite**.

Zusätzlich liefert die **Dateistrukturanalyse** (`DateiAnalysator`, Menü *Extras ▸ Dateistruktur analysieren* und automatisch beim Einlesen) alles, was man zur Beurteilung einer unbekannten Datei braucht: magische Bytes, eingebettete Signaturen mit Offset, Textcodierung, Entropie, lesbare Zeichenketten und – bei Archiven – die Dateiliste.

### Ergebnis je Fall

| Fall | Verhalten |
|---|---|
| `*.zw1`/`*.elk` ist ein ZIP (ggf. mit Vorspann) und enthält XML/CSV/XLSX/SQLite mit erkennbaren Feldern | wird gelesen |
| Die Felder heißen anders als erwartet (andere Sprache/Version) | Feldzuordnung im Einstellungsdialog anpassen, **keine** Codeänderung nötig |
| Die Datei ist ein proprietäres Binärformat | **kein** Raten: Das Programm meldet „keine SPS-Datensätze gefunden" und zeigt den Strukturbericht (Signaturen, Entropie, Hinweis auf Kompression/Verschlüsselung) |
| Export aus EPLAN (siehe 3.) | wird gelesen |

> **Was bei Ihnen zu prüfen bleibt:** Ob Ihre `*.zw1`/`*.elk` direkt lesbar sind, ergibt der erste Öffnungsversuch. Falls nicht, schicken Sie den *Strukturbericht* (Menü *Extras ▸ Dateistruktur analysieren*) oder besser eine kleine, nicht vertrauliche Testdatei – dann kann ein dafür passender Extraktor ergänzt werden (siehe `ARCHITEKTUR.md`, Abschnitt Erweiterung).

## 3. Verlässliche Alternative über EPLAN selbst

Wenn die Projektdateien nicht direkt lesbar sind, ist der robuste Weg, EPLAN die Daten ausgeben zu lassen. Das Programm liest solche Ausgaben bereits:

* **Manuell:** In EPLAN die SPS-Adressliste als Bericht/Auswertung ausgeben (CSV, Excel oder XML) und diese Datei öffnen. Die Spaltenüberschriften (z. B. „SPS-Adresse", „Symbolische Adresse", „Funktionstext") werden automatisch erkannt.
* **Automatisiert über die EPLAN-API** (nur mit installiertem EPLAN, C#-Skript oder Add-in): `PlcService.ExportAddressOverview` erzeugt laut API-Dokumentation eine CSV-Adressübersicht. Ein Skript ruft sie je Projekt auf und legt die CSV ab, die dieses Programm anschließend einliest. Dieser Weg wurde hier **nicht umgesetzt oder getestet**, weil dafür eine EPLAN-Installation nötig ist; er ist die empfohlene Ausbaustufe, falls der direkte Dateizugriff nicht funktioniert.

## 4. Bibliotheken

| Aufgabe | Wahl | Begründung |
|---|---|---|
| GUI | **PyQt6** | Moderne Oberfläche, Model/View, Threads; läuft unter Windows/Linux/macOS |
| MySQL | **mysql-connector-python** | Offizieller Oracle-Treiber, reines Python (keine Compiler nötig), unterstützt MariaDB |
| XML | **lxml** | Stream-Parser mit Zeilennummern (für „Sprung zur Fundstelle"), abschaltbare Entitätsauflösung |
| Excel | **openpyxl** | Lesen und Schreiben von `.xlsx` ohne Excel-Installation |
| ZIP/GZIP/SQLite/CSV | Standardbibliothek | keine zusätzliche Abhängigkeit |
| Tests | **pytest** | |

## 5. Risiken

| Risiko | Auswirkung | Gegenmaßnahme |
|---|---|---|
| Proprietäres Format nicht direkt lesbar | keine Datensätze | Strukturanalyse, Export aus EPLAN (Abschnitt 3), neuer Extraktor |
| Abweichende Feldbezeichnungen je EPLAN-Version/Sprache | Felder werden nicht gefunden | Feldzuordnung konfigurierbar, inkl. Eigenschaftsnummern |
| Manipulierte/riesige Archive (Zip-Bombe) | Speicher-/Plattenverbrauch | Limits für Dateianzahl und -größe (1 GiB je Datei, 4 GiB gesamt, 100 000 Dateien, Verschachtelungstiefe 3) |
| XML-Angriffe (XXE) | Dateizugriff/Netzwerk durch Entitäten | Entitätsauflösung und Netzwerkzugriff abgeschaltet (getestet) |
| Formelinjektion in Exporten | Excel führt Fremdtexte als Formel aus | XLSX: alle Zellen als Text; CSV: Hochkomma vor `= + - @` |
| SQL-Injektion | Datenverlust | Werte nur als Parameter; Tabellen-/Feldnamen validiert und quotiert (getestet, inkl. Injektionsversuch) |
| Passwort im Klartext in der INI-Datei | Mitlesen durch andere Benutzer | wie gefordert in der Konfigurationsdatei; Rechte `0600` unter Linux/macOS; nie im Protokoll (getestet). Auf Windows gelten die Rechte des Benutzerprofils |
| TLS-Rückfall des Treibers | Passwort unverschlüsselt im Netz | Bei aktivierter TLS-Option bricht die Verbindung ab, wenn der Server kein TLS bietet (getestet) |
| Kollation der Datenbank beeinflusst Abgleich | `Ä` = `A` o. Ä. | Abgleich in Python mit einheitlicher Normalisierung, nicht per SQL-Kollation |
| Referenztabelle sehr groß | Speicherbedarf | Die Referenz wird einmal komplett geladen (Zeilen à ≈ 2 Texte); bei mehreren Millionen Zeilen wäre ein abschnittsweiser Abgleich nötig |
| Modulname `logging.py` | Schattiert die Standardbibliothek, wenn das Paketverzeichnis im Suchpfad liegt | Start nur über `main.py` / `python -m eplan_pruefer` (steht im Moduldokument und in der README) |
