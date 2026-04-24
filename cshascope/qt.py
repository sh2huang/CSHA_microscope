from PyQt5.QtWidgets import QApplication
import qdarkstyle


def create_qt_app(app_name):
    app = QApplication.instance() or QApplication([])
    style = qdarkstyle.load_stylesheet_pyqt5()
    app.setStyleSheet(style)
    app.setApplicationName(app_name)
    return app, style


def execute_qt_app(app):
    if hasattr(app, "exec"):
        return app.exec()
    return app.exec_()

