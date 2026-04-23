from PyQt5.QtWidgets import QApplication
import qdarkstyle
import warnings

warnings.filterwarnings(
    "ignore",
    category=FutureWarning,
    message=r".*Public access to Window\.qt_viewer is deprecated.*",
)

from sashimi.gui.main_gui import MainWindow
from PyQt5.QtGui import QIcon
import click
from sashimi.config import cli_edit_config
from sashimi.state import State
from pathlib import Path

@click.command()
@click.option("--scopeless", is_flag=True, help="Scopeless mode for simulated hardware")
def main(scopeless):
    cli_edit_config("scopeless", scopeless)
    cli_edit_config("scanning", "mock" if scopeless else "ni")

    app = QApplication([])
    style = qdarkstyle.load_stylesheet_pyqt5()
    app.setStyleSheet(style)
    app.setApplicationName("Sashimi")
    st = State()
    main_window = MainWindow(st, style)
    icon_dir = (Path(__file__).parents[0]).resolve() / "icons/main_icon.png"
    app.setWindowIcon(QIcon(str(icon_dir)))  # PyQt does not accept Path
    main_window.show()
    app.exec()
