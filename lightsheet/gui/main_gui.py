from PyQt5.QtCore import QTimer, Qt
from PyQt5.QtWidgets import QWidget, QMainWindow, QDockWidget, QTabWidget
from lightsheet.gui.calibration_gui import CalibrationWidget
from lightsheet.gui.scanning_gui import (
    PlanarScanningWidget,
    VolumeScanningWidget,
)
from lightsheet.gui.camera_gui import ViewingWidget, CameraSettingsWidget
from lightsheet.gui.save_gui import SaveWidget
from lightsheet.gui.status_bar import StatusBarWidget
from lightsheet.gui.top_bar import TopWidget
from lightsheet.state import State, LiveCameraState


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


class MainWindow(QMainWindow):
    def __init__(self, st: State, style: str):
        super().__init__()
        self.st = st
        self.timer = QTimer()
        self.showMaximized()

        self.wid_status = StatusWidget(st, self.timer)
        self.wid_display = ViewingWidget(st, self.timer, style)
        self.wid_save_options = SaveWidget(st, self.timer)
        self.wid_scan = PlanarScanningWidget(st)
        self.wid_camera = CameraSettingsWidget(st, self.wid_display, self.timer)
        self.wid_status_bar = StatusBarWidget(st, self.timer)
        self.toolbar = TopWidget(st, self.timer)

        self.addToolBar(Qt.TopToolBarArea, self.toolbar)

        self.setCentralWidget(self.wid_display)

        self.addDockWidget(
            Qt.LeftDockWidgetArea,
            DockedWidget(widget=self.wid_status, title="Mode"),
        )

        self.addDockWidget(
            Qt.RightDockWidgetArea,
            DockedWidget(widget=self.wid_scan, title="Scanning settings"),
        )


        self.addDockWidget(
            Qt.RightDockWidgetArea,
            DockedWidget(widget=self.wid_camera, title="Camera settings"),
        )

        self.addDockWidget(
            Qt.RightDockWidgetArea,
            DockedWidget(widget=self.wid_save_options, title="Saving"),
        )

        self.setStatusBar(self.wid_status_bar)

        self.st.camera_settings.sig_param_changed.connect(
            self.st.reset_noise_subtraction
        )
        # TODO also change the check box of the button without triggering

        self.timer.start()
        self.timer.timeout.connect(self.check_end_experiment)

        self.refresh_param_values()


    def closeEvent(self, a0) -> None:
        self.st.wrap_up()
        a0.accept()

    def refresh_param_values(self, omit_wid_camera=False):
        # TODO should be possible with lightparam, when it's implemented there remove here
        self.wid_scan.wid_planar.refresh_widgets()
        self.wid_status.wid_volume.wid_volume.refresh_widgets()
        self.wid_status.wid_calibration.refresh_widgets()
        if not omit_wid_camera:
            self.wid_camera.wid_camera_settings.refresh_widgets()
            self.wid_camera.set_roi()
        self.wid_save_options.wid_save_options.refresh_widgets()
        self.wid_save_options.set_locationbutton()

    # TODO: Avoid hierarchy in GUI by emitting a PyQt5.QtCore.pyqtSignal() when experiment ends/aborts
    def check_end_experiment(self):
        if self.st.saver_stopped_signal.is_set():
            self.st.end_experiment()
            self.refresh_param_values(omit_wid_camera=True)
            self.toolbar.experiment_progress.hide()
            self.toolbar.lbl_experiment_progress.hide()
            self.st.saver_stopped_signal.clear()
            self.toolbar.experiment_toggle_btn.flip_icon(False)

        # check if experiment started or ended and update gui enabling
        if self.st.is_exp_started():
            self.set_enabled_gui(enable=False)

        elif self.st.is_exp_ended():
            self.set_enabled_gui(enable=True)

    def set_enabled_gui(self, enable):
        """
        Disable all the gui elements during the experiment
        and re-enables them after
        """
        self.menuBar().setEnabled(enable)
        self.wid_status.setEnabled(enable)
        self.wid_scan.setEnabled(enable)
        self.wid_camera.setEnabled(enable)
        self.wid_save_options.setEnabled(enable)
        self.wid_display.auto_contrast_chk.setEnabled(enable)


class StatusWidget(QTabWidget):
    def __init__(self, st: State, timer):
        super().__init__()

        self.state = st
        self.timer = timer
        self.scan_settings = self.state.status
        self.option_dict = {
            0: "Calibration",
            1: "Volume",
        }

        self.wid_calibration = CalibrationWidget(st, st.calibration, self.timer)
        self.wid_volume = VolumeScanningWidget(st, self.timer)

        self.addTab(self.wid_calibration, self.option_dict[0])
        self.addTab(self.wid_volume, self.option_dict[1])

        self._volume_index = 1
        self._fallback_index = 0
        self._internal_tab_change = False

        self.currentChanged.connect(self.update_status)
        self.currentChanged.connect(self.wid_volume.wid_wave.update_pulses)

        self.timer.timeout.connect(self.refresh_volume_tab_enabled_state)
        self.refresh_volume_tab_enabled_state()

    def is_camera_running(self):
        return self.state.live_camera_state == LiveCameraState.RUNNING

    def has_valid_calibration(self):
        return self.state.has_valid_calibration()

    def refresh_volume_tab_enabled_state(self):
        camera_running = self.is_camera_running()
        calibration_ready = self.has_valid_calibration()
        self.setTabEnabled(self._volume_index, camera_running and calibration_ready)
        if not calibration_ready and self.currentIndex() == self._volume_index:
            self.setCurrentIndex(self._fallback_index)

    def update_status(self):
        self.state.status.scanning_state = self.option_dict[self.currentIndex()]
