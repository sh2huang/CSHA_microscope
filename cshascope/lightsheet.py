from pathlib import Path
import warnings

from PyQt5.QtGui import QIcon

from cshascope.qt import create_qt_app, execute_qt_app


def configure_hardware(scopeless):
    from sashimi.config import cli_edit_config

    cli_edit_config("scopeless", scopeless)
    cli_edit_config("scanning", "mock" if scopeless else "ni")


def run(scopeless=False):
    configure_hardware(scopeless)

    warnings.filterwarnings(
        "ignore",
        category=FutureWarning,
        message=r".*Public access to Window\.qt_viewer is deprecated.*",
    )

    from sashimi.gui.main_gui import MainWindow
    from sashimi.state import State
    import sashimi

    app, style = create_qt_app("CSHAScope Lightsheet")
    state = State()
    main_window = MainWindow(state, style)

    icon_path = Path(sashimi.__file__).resolve().parent / "icons" / "main_icon.png"
    app.setWindowIcon(QIcon(str(icon_path)))

    main_window.show()
    return execute_qt_app(app)

