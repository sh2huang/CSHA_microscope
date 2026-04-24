from cshascope.qt import create_qt_app, execute_qt_app, get_app_icon


def run():
    from pointscan.gui import TwopViewer

    app, _style = create_qt_app("CSHAScope Pointscan")
    icon = get_app_icon()
    app.setWindowIcon(icon)

    viewer = TwopViewer()
    viewer.setWindowIcon(icon)
    viewer.show()
    return execute_qt_app(app)
