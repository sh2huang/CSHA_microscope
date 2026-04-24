from PyQt5.QtCore import QTimer, Qt
from PyQt5.QtWidgets import (
    QWidget,
    QMainWindow,
    QDockWidget,
    QVBoxLayout,
    QGridLayout,
    QPushButton,
    QLabel,
    QProgressBar,
    QFileDialog,
    QCheckBox,
)
from brunoise.state import ExperimentState
from brunoise.scanning import (
    ScanningParameters,
    dwell_time_s,
    n_output_samples,
    sample_rate_in,
)
from brunoise.piezo_z_control import PiezoZControl

import pyqtgraph as pg
from pathlib import Path

from lightparam.gui import ParameterGui


class CalculatedParameterDisplay(QWidget):
    def __init__(self):
        super().__init__()
        self.setLayout(QVBoxLayout())
        self.lbl_frameinfo = QLabel()
        self.layout().addWidget(self.lbl_frameinfo)
        self.lbl_frameinfo.setMinimumHeight(120)

    def display_scanning_parameters(self, sp: ScanningParameters):
        output_samples = n_output_samples(sp)
        self.lbl_frameinfo.setText(
            "Total samples/frame: {}\n".format(output_samples)
            + "Frame rate: {:.3f} Hz\n".format(sp.framerate)
            + "Output rate: {:.1f} kHz\n".format(sp.sample_rate_out / 1000)
            + "Input rate: {:.1f} kHz\n".format(sample_rate_in(sp) / 1000)
            + "Dwell time: {:.3f} us".format(dwell_time_s(sp) * 1e6)
        )


class ExperimentControl(QWidget):
    def __init__(self, state: ExperimentState):
        super().__init__()
        self.state = state
        self.experiment_settings_gui = ParameterGui(state.experiment_settings)
        self.save_location_button = QPushButton()
        self.set_locationbutton()
        self.save_location_button.clicked.connect(self.set_save_location)
        self.startstop_button = QPushButton()
        self.update_start_button()
        self.chk_pause = QCheckBox("Pause after experiment")
        self.stack_progress = QProgressBar()
        self.plane_progress = QProgressBar()
        self.plane_progress.setFormat("Frame %v of %m")
        self.stack_progress.setFormat("Plane %v of %m")
        self.startstop_button.clicked.connect(self.toggle_start)

        self.setLayout(QVBoxLayout())
        self.layout().addWidget(self.experiment_settings_gui)
        self.layout().addWidget(self.save_location_button)
        self.layout().addWidget(self.startstop_button)
        self.layout().addWidget(self.chk_pause)
        self.layout().addWidget(self.plane_progress)
        self.layout().addWidget(self.stack_progress)

    def set_saving(self):
        self.startstop_button.setText("Start recording")
        self.startstop_button.setStyleSheet(
            "background-color:#1d824f; border-color:#1c9e66"
        )

    def set_notsaving(self):
        self.startstop_button.setText("Stop recording")
        self.startstop_button.setStyleSheet(
            "background-color:#82271d; border-color:#9e391c"
        )

    def set_finishing(self):
        self.startstop_button.setText("Finishing save...")
        self.startstop_button.setStyleSheet("")

    def update_start_button(self):
        if self.state.saving:
            self.startstop_button.setEnabled(True)
            self.set_notsaving()
        elif self.state.save_in_progress:
            self.startstop_button.setEnabled(False)
            self.set_finishing()
        else:
            self.startstop_button.setEnabled(True)
            self.set_saving()

    def toggle_start(self):
        if self.state.saving:
            self.state.end_experiment(force=True)
        else:
            self.state.pause_after = self.chk_pause.isChecked()
            if self.state.start_experiment():
                self.set_notsaving()
        self.update_start_button()

    def set_locationbutton(self):
        pathtext = self.state.experiment_settings.save_dir
        # check if there is a stack in this location
        if (Path(pathtext) / "original" / "stack_metadata.json").is_file():
            self.save_location_button.setText("Overwrite " + pathtext)
            self.save_location_button.setStyleSheet(
                "background-color:#b5880d; border-color:#fcc203"
            )
        else:
            self.save_location_button.setText("Save in " + pathtext)
            self.save_location_button.setStyleSheet("")

    def set_save_location(self):
        save_dir = QFileDialog.getExistingDirectory()
        self.state.experiment_settings.save_dir = save_dir
        self.set_locationbutton()

    def update(self):
        sstatus = self.state.get_save_status()
        if sstatus is not None:
            self.plane_progress.setMaximum(sstatus.target_params.n_t)
            self.plane_progress.setValue(sstatus.i_t)
            self.stack_progress.setMaximum(sstatus.target_params.n_z)
            self.stack_progress.setValue(sstatus.i_z)
        self.update_start_button()


class ViewingWidget(QWidget):
    def __init__(self, state):
        super().__init__()
        self.state = state
        self.image_viewer = pg.ImageView()
        self.image_viewer.ui.roiBtn.hide()
        self.image_viewer.ui.menuBtn.hide()

        self.layout = QGridLayout()
        self.layout.addWidget(self.image_viewer, 0, 0)
        self.setLayout(self.layout)

        self.first_image = True
        self.levelMode_in_use = "mono"
        self.state.sig_display_changed.connect(self.reset_display_levels)

    def reset_display_levels(self):
        self.first_image = True

    def update(self) -> None:
        current_images = self.state.get_image()

        if current_images is None:
            return

        current_image = current_images[0, :, :]

        self.image_viewer.setImage(
            current_image,
            autoLevels=self.first_image,
            autoRange=self.first_image,
            autoHistogramRange=self.first_image,
            levelMode=self.levelMode_in_use
        )
        self.first_image = False

class DockedWidget(QDockWidget):
    def __init__(self, widget=None, layout=None, title=""):
        super().__init__()
        if widget is not None:
            self.setWidget(widget)
        else:
            self.setWidget(QWidget())
            self.widget().setLayout(layout)
        if title != "":
            self.setWindowTitle(title)


class ScanningWidget(QWidget):
    def __init__(self, state: ExperimentState):
        self.state = state
        super().__init__()
        self.scanning_layout = QVBoxLayout()

        self.scanning_settings_gui = ParameterGui(self.state.scanning_settings)
        self.scanning_calc = CalculatedParameterDisplay()
        self.chk_inverted = QCheckBox("Inverted")
        self.chk_inverted.setChecked(self.state.inverted)
        self.chk_inverted.toggled.connect(self.state.set_inverted)
        self.pause_button = QPushButton()
        self.pause_button.clicked.connect(self.toggle_pause)

        self.scanning_layout.addWidget(self.scanning_settings_gui)
        self.scanning_layout.addWidget(self.scanning_calc)
        self.scanning_layout.addWidget(self.chk_inverted)
        self.scanning_layout.addWidget(self.pause_button)
        self.setLayout(self.scanning_layout)

        self.state.sig_scanning_changed.connect(self.update_display)
        self.update_display()  # We cannot catch the first signal, so we trigger it manually.
        self.update_button()

    def update_display(self):
        self.scanning_calc.display_scanning_parameters(self.state.scanning_parameters)

    def set_controls_enabled(self, enabled: bool):
        self.scanning_settings_gui.setEnabled(enabled)
        self.chk_inverted.setEnabled(enabled)
        self.pause_button.setEnabled(enabled)

    def update_button(self):
        if self.state.paused:
            self.pause_button.setText("Resume")
        else:
            self.pause_button.setText("Pause")

    def toggle_pause(self):
        if self.state.paused:
            self.state.restart_scanning()
        else:
            self.state.pause_scanning()
        self.update_button()

class TwopViewer(QMainWindow):
    def __init__(self):
        super().__init__()

        # State variables
        self.state = ExperimentState()

        self.image_display = ViewingWidget(self.state)
        self.setCentralWidget(self.image_display)

        self.scanning_widget = ScanningWidget(self.state)
        self.experiment_widget = ExperimentControl(self.state)
        self.piezo_z_control = PiezoZControl(self.state)

        self.addDockWidget(
            Qt.LeftDockWidgetArea,
            DockedWidget(widget=self.scanning_widget, title="Scanning settings"),
        )
        self.addDockWidget(
            Qt.RightDockWidgetArea,
            DockedWidget(widget=self.piezo_z_control, title="Piezo z control"),
        )
        self.addDockWidget(
            Qt.RightDockWidgetArea,
            DockedWidget(widget=self.experiment_widget, title="Experiment running"),
        )

        self.timer = QTimer()
        self.timer.timeout.connect(self.update)
        self.timer.start()

    def update(self):
        self.image_display.update()
        self.piezo_z_control.sync_state()
        self.experiment_widget.update()

        scanning_controls_enabled = not (
                self.state.saving or self.state.save_in_progress
        )
        self.scanning_widget.set_controls_enabled(scanning_controls_enabled)

    def closeEvent(self, event) -> None:
        self.state.close_setup()
        event.accept()
