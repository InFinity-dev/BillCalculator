# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules


repo_root = Path(SPECPATH).parent
hidden_imports = (
    collect_submodules("alembic", filter=lambda name: not name.startswith("alembic.testing"))
    + collect_submodules("flask_migrate")
    + [
        "logging.config",
        "sqlalchemy.dialects.sqlite",
        "sqlalchemy.dialects.sqlite.pysqlite",
    ]
)

analysis = Analysis(
    [str(repo_root / "desktop_backend.py")],
    pathex=[str(repo_root)],
    binaries=[],
    datas=[
        (str(repo_root / "templates"), "templates"),
        (str(repo_root / "static"), "static"),
        (str(repo_root / "migrations"), "migrations"),
    ],
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "unittest"],
    noarchive=False,
    optimize=1,
)

pyz = PYZ(analysis.pure)

executable = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="billcalculator-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
