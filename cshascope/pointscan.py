from pathlib import Path

from PyQt5.QtGui import QIcon

from cshascope.qt import create_qt_app, execute_qt_app


def run():
    from brunoise.gui import TwopViewer
    import sashimi

    app, _style = create_qt_app("CSHAScope Pointscan")
    icon_path = Path(sashimi.__file__).resolve().parent / "icons" / "main_icon.png"
    icon = QIcon(str(icon_path))
    app.setWindowIcon(icon)

    viewer = TwopViewer()
    viewer.setWindowIcon(icon)
    viewer.show()
    return execute_qt_app(app)
