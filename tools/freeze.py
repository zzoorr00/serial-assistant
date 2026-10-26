"""Build entry: repair Qt 5 wheel metadata when installed in Unicode paths."""
from pathlib import Path

import PyInstaller.__main__
from PyInstaller.utils.hooks.qt import pyqt5_library_info


info = pyqt5_library_info
old_prefix = info.location["PrefixPath"]
real_prefix = info.package_location / "Qt5"
if real_prefix.is_dir() and not Path(info.location["PluginsPath"]).is_dir():
    info.location = {
        name: str(real_prefix) + value[len(old_prefix):] if value.startswith(old_prefix) else value
        for name, value in info.location.items()
    }
    info.qt_inside_package = True
    info.qt_lib_dir = real_prefix / "bin"

project = Path(__file__).resolve().parents[1]
PyInstaller.__main__.run([
    "--noconfirm", "--onefile", "--windowed", "--name", "串口助手",
    "--hidden-import", "serial.urlhandler.protocol_loop", str(project / "main.py"),
    "--distpath", str(project / "dist"), "--workpath", str(project / "build"),
    "--specpath", str(project),
])
