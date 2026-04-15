from PyQt5.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QPushButton,
    QFileDialog,
)
from lightparam.gui import ParameterGui
from sashimi.state import State
from pathlib import Path


class SaveWidget(QWidget):
    def __init__(self, state: State, timer):
        super().__init__()
        self.state = state
        self.timer = timer
        self.setLayout(QVBoxLayout())

        self.wid_save_options = ParameterGui(state.save_settings)
        self.save_location_button = QPushButton()

        self.wid_manual_duration = ParameterGui(self.state.trigger_settings)


        self.layout().addWidget(self.wid_save_options)
        self.layout().addWidget(self.save_location_button)
        self.layout().addWidget(self.wid_manual_duration)
        self.wid_manual_duration.setEnabled(True)

        self.set_locationbutton()

        self.save_location_button.clicked.connect(self.set_save_location)
        self.state.trigger_settings.sig_param_changed.connect(
            self.state.send_manual_duration
        )

        self.state.send_manual_duration()

    def set_save_location(self):
        save_dir = QFileDialog.getExistingDirectory()
        self.state.save_settings.save_dir = save_dir
        self.set_locationbutton()

    def set_locationbutton(self):
        pathtext = self.state.save_settings.save_dir
        # check if there is a stack in this location
        if (Path(pathtext) / "original" / "stack_metadata.json").is_file():
            self.save_location_button.setText("Overwrite " + pathtext)
            self.save_location_button.setStyleSheet(
                "background-color:#b5880d; border-color:#fcc203"
            )
            self.state.save_settings.overwrite_save_folder = 1
        else:
            self.save_location_button.setText("Save in " + pathtext)
            self.save_location_button.setStyleSheet("")
            self.state.save_settings.overwrite_save_folder = 0