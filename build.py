"""Build the desktop app for the current OS.

  Windows: dist/Data Revenue De-Identifier/  (folder; installer/windows.iss wraps it)
  macOS:   dist/Data Revenue De-Identifier.app

python build.py
"""
import os
import subprocess
import sys

from deid import __version__

NAME = "Data Revenue De-Identifier"
HERE = os.path.dirname(os.path.abspath(__file__))
SEP = ";" if sys.platform == "win32" else ":"


def main():
    args = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
        "--name", NAME,
        "--add-data", f"deid/ui{SEP}deid/ui",
        "--collect-all", "rapidocr_onnxruntime",
        "--collect-all", "pydicom",
        "--collect-all", "libjpeg", "--collect-all", "openjpeg",
        "--collect-all", "webview",
        "--copy-metadata", "pylibjpeg", "--copy-metadata", "pylibjpeg-libjpeg",
        "--copy-metadata", "pylibjpeg-openjpeg",
        "--hidden-import", "pylibjpeg", "--hidden-import", "libjpeg",
        "--hidden-import", "openjpeg",
        "--exclude-module", "matplotlib", "--exclude-module", "pandas",
        "--exclude-module", "scipy", "--exclude-module", "pytest",
        "--exclude-module", "IPython", "--exclude-module", "tkinter",
    ]
    if sys.platform == "win32":
        args += ["--icon", "assets/icon.ico", "--onedir"]
    elif sys.platform == "darwin":
        args += ["--icon", "assets/icon.png", "--onedir",
                 "--osx-bundle-identifier", "io.datarevenue.deidentifier"]
    args.append("app.py")
    subprocess.check_call(args, cwd=HERE)
    if sys.platform == "darwin":
        plist = os.path.join(HERE, "dist", f"{NAME}.app", "Contents", "Info.plist")
        for key, val in [("CFBundleShortVersionString", __version__),
                         ("CFBundleVersion", __version__),
                         ("LSMinimumSystemVersion", "11.0"),
                         ("NSHighResolutionCapable", "true")]:
            typ = "-bool" if val == "true" else "-string"
            subprocess.call(["plutil", "-replace", key, typ, val, plist])
    print(f"built {NAME} {__version__}")


if __name__ == "__main__":
    main()
