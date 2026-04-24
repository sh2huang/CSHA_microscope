from PyQt5.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QPushButton,
)
from lightparam.gui import ParameterGui
from lightparam.gui.collapsible_widget import CollapsibleWidget
from lightsheet.gui.waveform_gui import WaveformWidget


class PlanarScanningWidget(QWidget):
    def __init__(self, state):
        super().__init__()
        self.state = state
        self.setLayout(QVBoxLayout())
        self.wid_planar = ParameterGui(state.planar_setting)
        self.layout().addWidget(self.wid_planar)

class VolumeScanningWidget(QWidget):
    def __init__(self, state, timer):
        super().__init__()
        self.state = state
        self.timer = timer
        self.setLayout(QVBoxLayout())
        self.wid_volume = ParameterGui(state.volume_setting)
        self.btn_update_scanning = QPushButton("Update Scanning Setting")
        self.btn_update_scanning.clicked.connect(self.state.refresh_volume_waveforms)

        self.wid_wave = WaveformWidget(timer=self.timer, state=self.state)
        self.wid_collapsible_wave = CollapsibleWidget(
            child=self.wid_wave, name="Piezo impulse-response waveform"
        )
        self.wid_collapsible_wave.toggle_collapse()

        self.layout().addWidget(self.wid_volume)
        self.layout().addWidget(self.btn_update_scanning)
        self.layout().addWidget(self.wid_collapsible_wave)
