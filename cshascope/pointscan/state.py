from multiprocessing import Event, Queue
from lightparam import Param
from lightparam.param_qt import ParametrizedQt
from cshascope.pointscan.scanning import (
    Scanner,
    ScanningParameters,
    ScanningState,
    ImageReconstructor,
    NI_USB_6363_MAX_AO_SAMPLE_RATE_3_CHANNELS,
    frame_rate,
    limit_sample_rate_out,
)
from pathlib import Path
from cshascope.pointscan.streaming_save import StackSaver, SavingParameters, SavingStatus
from arrayqueues.shared_arrays import ArrayQueue
from queue import Empty
from PyQt5.QtCore import QObject, pyqtSignal
from typing import Optional
from time import sleep
import numpy as np

PIEZO_MAX_UM = 450.0
PIEZO_UM_PER_VOLT = 45.0
PIEZO_MAX_VOLTAGE = PIEZO_MAX_UM / PIEZO_UM_PER_VOLT

class ExperimentSettings(ParametrizedQt):
    def __init__(self):
        super().__init__()
        self.name = "recording"
        self.n_planes = Param(1, (1, 500))
        self.n_frames = Param(100, (1, 100000))
        self.dz = Param(1.0, (-50, 50.0), unit="um")
        self.save_dir = Param(str(Path.home() / "Desktop"), gui=False)


class ScanningSettings(ParametrizedQt):
    def __init__(self):
        super().__init__()
        self.name = "scanning"
        self.n_pixel_x = Param(200, (1, 4096))
        self.n_pixel_y = Param(200, (1, 4096))
        self.galvo_voltage = Param(3.0, (0.2, 5.0), unit="V")
        self.output_rate_khz = Param(
            100.0, (1.0, NI_USB_6363_MAX_AO_SAMPLE_RATE_3_CHANNELS / 1000), unit="kHz"
        )
        self.binning = Param(5, (1, 20))
        self.n_turn = Param(10, (0, 100))
        self.n_extra_point = Param(100, (0, 100000))
        self.signal_delay = Param(80.0, (-10000.0, 10000.0), unit="us")


def convert_params(st: ScanningSettings, piezo_z_um=0.0) -> ScanningParameters:
    """
    Converts the GUI scanning settings in parameters appropriate for the
    laser scanning

    """
    n_bin = int(st.binning)
    requested_sample_rate = float(st.output_rate_khz) * 1000
    sample_rate = limit_sample_rate_out(requested_sample_rate, n_bin)
    n_x = int(st.n_pixel_x)
    n_y = int(st.n_pixel_y)

    voltage_max = float(st.galvo_voltage)
    if n_y >= n_x:
        voltage_y = voltage_max
        voltage_x = voltage_y * n_x / n_y
    else:
        voltage_x = voltage_max
        voltage_y = voltage_x * n_y / n_x
    voltage_z = float(np.clip(piezo_z_um / PIEZO_UM_PER_VOLT, 0.0, PIEZO_MAX_VOLTAGE))

    sp = ScanningParameters(
        voltage_x=float(voltage_x),
        voltage_y=float(voltage_y),
        voltage_z=voltage_z,
        n_x=int(n_x),
        n_y=int(n_y),
        n_turn=int(st.n_turn),
        n_extra=int(st.n_extra_point),
        n_bin=n_bin,
        sample_rate_out=float(sample_rate),
        signal_delay=float(st.signal_delay),
    )
    sp.framerate = frame_rate(sp)
    return sp


class ExperimentState(QObject):
    sig_scanning_changed = pyqtSignal()
    sig_display_changed = pyqtSignal()

    def __init__(self):
        super().__init__()

        self.experiment_start_event = Event()
        self.scanning_settings = ScanningSettings()
        self.experiment_settings = ExperimentSettings()
        self.pause_after = False

        self.end_event = Event()
        self.scanner = Scanner(self.experiment_start_event)
        self.scanning_parameters = None
        self.reconstructor = ImageReconstructor(
            self.scanner.data_queue, self.scanner.stop_event
        )
        self.save_queue = ArrayQueue(max_mbytes=800)
        self.timestamp_queue = Queue()

        self.saver = StackSaver(
            self.scanner.stop_event, self.save_queue, self.timestamp_queue
        )
        self.save_status: Optional[SavingStatus] = None

        self.piezo_z_um = 225.0
        self.recording_start_z_um = 0.0
        self.scanning_settings.sig_param_changed.connect(self.send_scan_params)
        self.scanning_settings.sig_param_changed.connect(self.send_save_params)
        self.scanner.start()
        self.reconstructor.start()
        self.saver.start()
        self.open_setup()

        self.paused = False
        self.recording = False
        self.current_plane = 0
        self.frames_in_plane = 0
        self.plane_end_requested = False
        self.recording_n_frames = None
        self.recording_n_planes = None
        self.recording_plane_z_um = None
        self.inverted = True

    @property
    def saving(self):
        return self.recording

    @property
    def save_in_progress(self):
        return self.saver.busy_signal.is_set()

    def open_setup(self):
        self.send_scan_params()

    def start_experiment(self, first_plane=True):
        if first_plane and self.save_in_progress:
            return False

        if first_plane:
            self.current_plane = 0
            self.recording_n_frames = self.experiment_settings.n_frames
            self.recording_n_planes = self.experiment_settings.n_planes
            self.recording_start_z_um = self.piezo_z_um
            self.recording_plane_z_um = self.compute_plane_z_positions(
                self.recording_start_z_um,
                self.recording_n_planes,
                self.experiment_settings.dz,
            )

        params_to_send = convert_params(self.scanning_settings, self.piezo_z_um)
        params_to_send.scanning_state = ScanningState.EXPERIMENT_RUNNING
        params_to_send.n_frames = self.recording_n_frames
        self.scanner.parameter_queue.put(params_to_send)

        self.recording = True
        self.frames_in_plane = 0
        self.plane_end_requested = False
        if first_plane:
            self.send_save_params()
            self.saver.saving_signal.set()
        self.experiment_start_event.set()
        return True

    def end_experiment(self, force=False):
        self.plane_end_requested = True
        self.experiment_start_event.clear()

        if not force and self.current_plane + 1 < self.recording_n_planes:
            self.advance_plane()
        else:
            self.recording = False
            sleep(0.2)
            self.saver.saving_signal.clear()
            if self.pause_after:
                self.pause_scanning()
            else:
                self.restart_scanning()

    def restart_scanning(self):
        params_to_send = convert_params(self.scanning_settings, self.piezo_z_um)
        params_to_send.scanning_state = ScanningState.PREVIEW
        self.scanner.parameter_queue.put(params_to_send)
        self.paused = False

    def pause_scanning(self):
        params_to_send = convert_params(self.scanning_settings, self.piezo_z_um)
        params_to_send.scanning_state = ScanningState.PAUSED
        self.scanner.parameter_queue.put(params_to_send)
        self.paused = True

    def advance_plane(self):
        self.current_plane += 1
        self.piezo_z_um = self.recording_plane_z_um[self.current_plane]
        sleep(0.2)
        self.start_experiment(first_plane=False)

    def close_setup(self):
        """ Cleanup on programe close:
        end all parallel processes, close all communication channels

        """
        self.scanner.stop_event.set()
        self.end_event.set()
        self.scanner.join()
        self.reconstructor.join()
        self.saver.join()

    def get_image(self):
        try:
            images = self.reconstructor.output_queue.get(timeout=0.001)
            if self.inverted:
                images = -images
            try:
                t = self.scanner.time_queue.get(timeout=0.001)
            except Empty:
                t = 0
                print("scanner time queue is empty")
            if self.recording:
                self.save_queue.put(images)
                self.timestamp_queue.put(t)
                self.frames_in_plane += 1
                if (
                    not self.plane_end_requested
                    and self.frames_in_plane >= self.recording_n_frames
                ):
                    self.end_experiment()
            return images
        except Empty:
            return None

    def set_inverted(self, inverted):
        self.inverted = inverted
        self.sig_display_changed.emit()

    def set_piezo_z_um(self, z_um):
        self.piezo_z_um = float(np.clip(z_um, 0.0, PIEZO_MAX_UM))
        if not self.saving:
            self.send_scan_params()

    def compute_plane_z_positions(self, start_z_um, n_planes, dz_um):
        return tuple(
            float(np.clip(start_z_um + (i * dz_um), 0.0, PIEZO_MAX_UM))
            for i in range(n_planes)
        )

    def send_scan_params(self):
        self.scanning_parameters = convert_params(self.scanning_settings, self.piezo_z_um)
        self.scanner.parameter_queue.put(self.scanning_parameters)
        self.reconstructor.parameter_queue.put(self.scanning_parameters)
        self.sig_scanning_changed.emit()

    def send_save_params(self):
        if self.recording or self.experiment_start_event.is_set():
            n_t = self.recording_n_frames
            n_z = self.recording_n_planes
            plane_z_um = self.recording_plane_z_um
        else:
            n_t = self.experiment_settings.n_frames
            n_z = self.experiment_settings.n_planes
            plane_z_um = self.compute_plane_z_positions(
                self.piezo_z_um,
                n_z,
                self.experiment_settings.dz,
            )

        self.saver.saving_parameter_queue.put(
            SavingParameters(
                output_dir=Path(self.experiment_settings.save_dir),
                plane_size=(self.scanning_parameters.n_y, self.scanning_parameters.n_x),
                n_t=n_t,
                n_z=n_z,
                plane_z_um=plane_z_um,
            )
        )

    def get_save_status(self) -> Optional[SavingStatus]:
        try:
            self.save_status = self.saver.saved_status_queue.get(timeout=0.001)
            return self.save_status
        except Empty:
            pass
        return None
