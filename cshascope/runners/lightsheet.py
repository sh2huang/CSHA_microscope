import warnings

from cshascope.qt import create_qt_app, execute_qt_app, get_app_icon


def configure_hardware(scopeless):
    from cshascope.lightsheet.config import cli_edit_config

    cli_edit_config("scopeless", scopeless)
    cli_edit_config("scanning", "mock" if scopeless else "ni")


def run(scopeless=False):
    configure_hardware(scopeless)

    warnings.filterwarnings(
        "ignore",
        category=FutureWarning,
        message=r".*Public access to Window\.qt_viewer is deprecated.*",
    )

    from cshascope.lightsheet.gui.main_gui import MainWindow
    from cshascope.lightsheet.state import State

    app, style = create_qt_app("CSHAScope Lightsheet")
    state = State()
    main_window = MainWindow(state, style)

    icon = get_app_icon()
    app.setWindowIcon(icon)
    main_window.setWindowIcon(icon)

    main_window.show()
    return execute_qt_app(app)
