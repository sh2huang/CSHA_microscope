from pathlib import Path

from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import QApplication
import qdarkstyle

import cshascope


def create_qt_app(app_name):
    app = QApplication.instance() or QApplication([])
    style = qdarkstyle.load_stylesheet_pyqt5()
    app.setStyleSheet(style)
    app.setApplicationName(app_name)
    return app, style


def get_app_icon():
    icon_path = Path(cshascope.__file__).resolve().parent / "icons" / "main_icon.png"
    return QIcon(str(icon_path))


def execute_qt_app(app):
    if hasattr(app, "exec"):
        return app.exec()
    return app.exec_()
