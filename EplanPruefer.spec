# -*- mode: python ; coding: utf-8 -*-
# PyInstaller-Beschreibung für die EPLAN-Prüfer-Anwendung (eine einzelne Datei, ohne Konsolenfenster).
#
#   pyinstaller --noconfirm EplanPruefer.spec
#
# Ergebnis: dist/EplanPruefer.exe (Windows) bzw. dist/EplanPruefer (Linux/macOS).

from PyInstaller.utils.hooks import collect_submodules

# Alle Module des Pakets ausdrücklich einbinden. dxf_parser wird erst zur Laufzeit importiert
# (zirkuläre Abhängigkeit vermeiden) und wäre sonst leicht zu übersehen.
versteckte_importe = collect_submodules("eplan_pruefer")

analyse = Analysis(
    ["main.py"],
    pathex=["."],
    binaries=[],
    datas=[],
    hiddenimports=versteckte_importe,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "pytest", "_pytest"],
    noarchive=False,
)
archiv = PYZ(analyse.pure)

anwendung = EXE(
    archiv,
    analyse.scripts,
    analyse.binaries,
    analyse.datas,
    [],
    name="EplanPruefer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,               # UPX-gepackte Programme lösen häufiger Virenwarnungen aus
    runtime_tmpdir=None,
    console=False,           # Oberflächenanwendung ohne Konsolenfenster
    disable_windowed_traceback=False,
)
