# PyInstaller build for the Windows app. Run from the repo root:
#   pyinstaller packaging/ShootClipper.spec --noconfirm
# Output: dist/ShootClipper/ShootClipper.exe plus its _internal folder.
import os
from PyInstaller.utils.hooks import collect_all

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

datas = [
    (os.path.join(ROOT, "web"), "web"),
    (os.path.join(ROOT, "data"), "data"),
    (os.path.join(ROOT, "VERSION"), "."),
]
binaries = []
hiddenimports = ["autopilot", "clipper.selftest"]

# Packages that load DLLs, model assets or plugins at runtime, which PyInstaller's
# import scan can't see on its own.
for pkg in ("faster_whisper", "ctranslate2", "onnxruntime", "tokenizers",
            "webview", "clr_loader", "pythonnet", "keyring"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    [os.path.join(ROOT, "app.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "matplotlib", "torch", "tensorflow", "IPython", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ShootClipper",
    icon=os.path.join(SPECPATH, "icon.ico"),
    console=False,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="ShootClipper", upx=False)
