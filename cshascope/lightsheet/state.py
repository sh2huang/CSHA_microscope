import numpy as np
from multiprocessing import Manager as MultiprocessingManager
from queue import Empty
from typing import Optional
from lightparam.param_qt import ParametrizedQt
from lightparam import Param, ParameterTree

from cshascope.lightsheet.processes.scanning import ScannerProcess
from cshascope.lightsheet.hardware.scanning.scanloops import (
    ScanningState,
    ExperimentPrepareState,
    XYScanning,
    PlanarScanning,
    ZManual,
    ZScanning,
    TriggeringParameters,
    ScanParameters,
)
from cshascope.lightsheet.processes.dispatcher import VolumeDispatcher
from cshascope.lightsheet.processes.logging import ConcurrenceLogger
from multiprocessing import Event
import json
from cshascope.lightsheet.processes.camera import (
    CameraProcess,
    CamParameters,
    CameraMode,
    TriggerMode,
)
from cshascope.lightsheet.processes.streaming_save import StackSaver, SavingParameters, SavingStatus
from cshascope.lightsheet.events import LoggedEvent, LightsheetEvents
from pathlib import Path
from enum import Enum
from cshascope.lightsheet.config import read_config
import time
from cshascope.lightsheet.utilities import clean_json, get_last_parameters

conf = read_config()


class GlobalState(Enum):
    PREVIEW = 1
    VOLUME_PREVIEW = 2
    EXPERIMENT_RUNNING = 3

class LiveCameraState(Enum):
    PAUSED = 0
    RUNNING = 1

class SaveSettings(ParametrizedQt):
    def __init__(self):
        super().__init__()
        self.name = "experiment_settings"
        self.save_dir = Param(conf["default_paths"]["data"], gui=False)
        self.overwrite_save_folder = Param(0, (0, 1), gui=False, loadable=False)


class TriggerSettings(ParametrizedQt):
    def __init__(self):
        super().__init__(self)
        self.name = "trigger_settings"
        self.experiment_duration = Param(5, (1, 50_000), unit="s")


class ScanningSettings(ParametrizedQt):
    def __init__(self):
        super().__init__()
        self.name = "general/scanning_state"
        self.scanning_state = Param(
            "Calibration",
            ["Calibration", "Volume"],
        )


scanning_to_global_state = dict(
    Calibration=GlobalState.PREVIEW,
    Volume=GlobalState.VOLUME_PREVIEW,
)


class PlanarScanningSettings(ParametrizedQt):
    def __init__(self):
        super().__init__()
        self.name = "scanning/planar_scanning"
        self.range = Param((-0.5, 0.5), (-2, 2))
        self.frequency = Param(500.0, (10, 1000), unit="Hz")


class CalibrationZSettings(ParametrizedQt):
    def __init__(self):
        super().__init__()
        self.name = "scanning/z_manual"
        self.piezo = Param(225.0, (0.0, 450.0), unit="um", gui="slider")
        self.galvo = Param(0.0, (-2.0, 2.0), gui="slider")

class ZRecordingSettings(ParametrizedQt):
    def __init__(self):
        super().__init__(self)
        self.name = "scanning/volumetric_recording"
        self.piezo_scan_range = Param((180.0, 220.0), (0.0, 400.0), unit="um")
        self.frequency = Param(3.0, (0.1, 100), unit="volumes/s (Hz)")
        self.n_planes = Param(4, (2, 100))
        self.n_skip_start = Param(0, (0, 20))
        self.n_skip_end = Param(0, (0, 20))


roi_size = [0, 0] + [
    r // conf["camera"]["default_binning"]
    for r in conf["camera"]["max_sensor_resolution"]
]


class CameraSettings(ParametrizedQt):
    def __init__(self):
        super().__init__()
        self.name = "camera/parameters"
        self.exposure_time = Param(
            conf["camera"]["default_exposure"], (1, 1000), unit="ms"
        )
        self.binning = Param(conf["camera"]["default_binning"], [1, 2, 4])
        self.roi = Param(
            roi_size, gui=False
        )  # order of params here is [vpos, hpos, vsize, hsize]


def convert_planar_params(planar: PlanarScanningSettings):
    return PlanarScanning(
        galvo=XYScanning(
            vmin=planar.range[0],
            vmax=planar.range[1],
            frequency=planar.frequency,
        )
    )


def convert_calibration_params(
    planar: PlanarScanningSettings, zsettings: CalibrationZSettings
):
    sp = ScanParameters(
        state=ScanningState.PLANAR,
        xy=convert_planar_params(planar),
        z=ZManual(**zsettings.params.values),
    )
    return sp


class Calibration(ParametrizedQt):
    def __init__(self):
        super().__init__()
        self.name = "general/calibration"
        self.z_settings = CalibrationZSettings()
        self.calibrations_points = []
        self.calibration = Param([(0, 0.01)], gui=False)

    def add_calibration_point(self):
        self.calibrations_points.append(
            (
                self.z_settings.piezo,
                self.z_settings.galvo,
            )
        )
        self.calculate_calibration()

    def remove_calibration_point(self):
        if len(self.calibrations_points) > 0:
            self.calibrations_points.pop()
            self.calculate_calibration()

    def calculate_calibration(self):
        if len(self.calibrations_points) < 2:
            self.calibration = None
            return False

        calibration_data = np.array(self.calibrations_points)
        piezo_val = np.pad(
            calibration_data[:, 0:1],
            ((0, 0), (1, 0)),
            constant_values=1.0,
            mode="constant",
        )
        galvo_val = calibration_data[:, 1]

        # solve least squares according to standard formula b = (XtX)^-1 * Xt * y
        piezo_cor = np.linalg.pinv(piezo_val.T @ piezo_val)

        self.calibration = [
            tuple(piezo_cor @ piezo_val.T @ galvo_val)
        ]

        return True


def get_voxel_size(
    scanning_settings: ZRecordingSettings,
    camera_settings: CameraSettings,
):
    binning = int(camera_settings.binning)

    scan_length = (
        scanning_settings.piezo_scan_range[1]
        - scanning_settings.piezo_scan_range[0]
    )
    inter_plane = scan_length / scanning_settings.n_planes

    return (
        inter_plane,
        conf["voxel_size"]["y"] * binning,
        conf["voxel_size"]["x"] * binning,
    )


def convert_save_params(
    save_settings: SaveSettings,
    scanning_settings: ZRecordingSettings,
    camera_settings: CameraSettings,
    trigger_settings: TriggerSettings,
):
    n_planes = scanning_settings.n_planes - (
        scanning_settings.n_skip_start + scanning_settings.n_skip_end
    )

    return SavingParameters(
        output_dir=Path(save_settings.save_dir),
        n_planes=n_planes,
        volumerate=scanning_settings.frequency,
        voxel_size=get_voxel_size(scanning_settings, camera_settings),
        crop=[
            int(item) for item in camera_settings.roi
        ],  # int conversion makes it json serializable
    )

def convert_volume_params(
    planar: PlanarScanningSettings,
    z_setting: ZRecordingSettings,
    calibration: Calibration,
):
    return ScanParameters(
        state=ScanningState.VOLUMETRIC,
        xy=convert_planar_params(planar),
        z=ZScanning(
            piezo_min=z_setting.piezo_scan_range[0],
            piezo_max=z_setting.piezo_scan_range[1],
            frequency=z_setting.frequency,
            galvo_sync=tuple(calibration.calibration[0]),
        ),
        triggering=TriggeringParameters(
            n_planes=z_setting.n_planes,
            n_skip_start=z_setting.n_skip_start,
            n_skip_end=z_setting.n_skip_end,
            frequency=None,
        ),
    )


class State:
    def __init__(self):
        self.conf = read_config()
        self.sample_rate = conf["sample_rate"]

        self.logger = ConcurrenceLogger("main")

        self.calibration_ref = None
        self.waveform = None
        self.current_plane = 0
        self.volume_waveforms_dirty = False
        self.stop_event = LoggedEvent(self.logger, LightsheetEvents.CLOSE_ALL)
        self.restart_event = LoggedEvent(self.logger, LightsheetEvents.RESTART_SCANNING)
        self.prepare_event = LoggedEvent(self.logger, LightsheetEvents.PREPARE_SCANNING)
        self.noise_subtraction_active = LoggedEvent(
            self.logger, LightsheetEvents.NOISE_SUBTRACTION_ACTIVE, Event()
        )
        self.is_saving_event = LoggedEvent(self.logger, LightsheetEvents.IS_SAVING)
        self.live_camera_state = LiveCameraState.PAUSED

        # The even active during scanning preparation (before first real camera trigger)
        self.is_waiting_event = LoggedEvent(
            self.logger, LightsheetEvents.WAITING_FOR_TRIGGER
        )

        self.experiment_state = ExperimentPrepareState.PREVIEW
        self.status = ScanningSettings()

        self.scanner = ScannerProcess(
            stop_event=self.stop_event,
            restart_event=self.restart_event,
            prepare_event=self.prepare_event,
            waiting_event=self.is_waiting_event,
            sample_rate=self.sample_rate,
        )
        self.camera_settings = CameraSettings()
        self.trigger_settings = TriggerSettings()

        self.settings_tree = ParameterTree()

        self.camera = CameraProcess(
            stop_event=self.stop_event,
        )

        self.multiprocessing_manager = MultiprocessingManager()

        self.experiment_duration_queue = self.multiprocessing_manager.Queue()


        self.saver = StackSaver(
            stop_event=self.stop_event,
            is_saving_event=self.is_saving_event,
            duration_queue=self.experiment_duration_queue,
        )
        self.saver_stopped_signal = self.saver.saver_stopped_signal.new_reference(
            self.logger
        )

        self.dispatcher = VolumeDispatcher(
            stop_event=self.stop_event,
            saving_signal=self.is_saving_event,
            wait_signal=self.is_waiting_event,
            noise_subtraction_on=self.noise_subtraction_active,
            camera_queue=self.camera.image_queue,
            saver_queue=self.saver.save_queue,
        )

        self.save_settings = SaveSettings()

        self.settings_tree = ParameterTree()

        self.global_state = scanning_to_global_state[self.status.scanning_state]
        self.current_exp_state = self.global_state
        self.prev_exp_state = self.current_exp_state

        self.planar_setting = PlanarScanningSettings()

        self.save_status: Optional[SavingStatus] = None

        self.volume_setting = ZRecordingSettings()
        self.calibration = Calibration()

        for setting in [
            self.planar_setting,
            self.volume_setting,
            self.calibration,
            self.calibration.z_settings,
            self.camera_settings,
            self.save_settings,
        ]:
            self.settings_tree.add(setting)

        self.status.sig_param_changed.connect(self.change_global_state)

        self.planar_setting.sig_param_changed.connect(self.handle_planar_settings_change)
        self.calibration.z_settings.sig_param_changed.connect(
            self.handle_calibration_settings_change
        )
        self.volume_setting.sig_param_changed.connect(self.handle_volume_settings_change)

        self.save_settings.sig_param_changed.connect(self.handle_save_settings_change)

        self.camera.start()
        self.scanner.start()
        self.saver.start()
        self.dispatcher.start()

        self.current_binning = conf["camera"]["default_binning"]
        self.voxel_size = None
        self.send_preview_scansave_settings()
        self.logger.log_message("initialized")

    def run_camera_live(self):
        self.live_camera_state = LiveCameraState.RUNNING
        self.send_camera_settings()

    def pause_camera_live(self):
        self.live_camera_state = LiveCameraState.PAUSED

        self.send_camera_settings()
        if self.global_state == GlobalState.PREVIEW:
            self.send_preview_scansave_settings()

    def restore_tree(self, restore_file):
        with open(restore_file, "r") as f:
            self.settings_tree.deserialize(json.load(f))

    def save_tree(self, save_file):
        with open(save_file, "w") as f:
            json.dump(clean_json(self.settings_tree.serialize()), f)

    def change_global_state(self):
        previous_global_state = self.global_state
        self.global_state = scanning_to_global_state[self.status.scanning_state]

        if self.current_exp_state != GlobalState.EXPERIMENT_RUNNING:
            self.current_exp_state = self.global_state
            self.prev_exp_state = self.current_exp_state

        self.send_camera_settings()
        if self.global_state == GlobalState.VOLUME_PREVIEW:
            self.refresh_volume_waveforms()
        else:
            if previous_global_state == GlobalState.VOLUME_PREVIEW:
                self.restart_event.set()
            self.send_preview_scansave_settings()

    def send_camera_settings(self):
        self.camera.image_queue.clear()
        self.camera.parameter_queue.put(self.camera_params)

    def handle_planar_settings_change(self, param_changed=None):
        del param_changed
        if self.global_state == GlobalState.PREVIEW:
            self.send_preview_scansave_settings()
        elif self.global_state == GlobalState.VOLUME_PREVIEW:
            self.mark_volume_waveforms_dirty()

    def handle_calibration_settings_change(self, param_changed=None):
        del param_changed
        if self.global_state == GlobalState.PREVIEW:
            self.send_preview_scansave_settings()

    def handle_volume_settings_change(self, param_changed=None):
        del param_changed
        self.voxel_size = get_voxel_size(self.volume_setting, self.camera_settings)
        if self.global_state == GlobalState.VOLUME_PREVIEW:
            self.mark_volume_waveforms_dirty()

    def handle_calibration_points_change(self):
        self.mark_volume_waveforms_dirty()

    def mark_volume_waveforms_dirty(self):
        self.volume_waveforms_dirty = True

    def has_valid_calibration(self):
        return (
            self.calibration.calibration is not None
            and len(self.calibration.calibrations_points) >= 2
        )

    def handle_save_settings_change(self, param_changed=None):
        del param_changed
        self.voxel_size = get_voxel_size(self.volume_setting, self.camera_settings)
        self.saver.saving_parameter_queue.put(self.save_params)

    def send_preview_scansave_settings(self):
        self.current_plane = 0
        self.scanner.parameter_queue.put(self.scan_params)
        self.voxel_size = get_voxel_size(self.volume_setting, self.camera_settings)
        self.saver.saving_parameter_queue.put(self.save_params)
        self.dispatcher.n_planes_queue.put(1)

    def restart_volume_playback(self):
        if self.global_state != GlobalState.VOLUME_PREVIEW:
            return

        self.current_plane = min(self.current_plane, self.n_planes - 1)
        self.is_waiting_event.set()
        self.scanner.parameter_queue.put(self.scan_params)
        self.voxel_size = get_voxel_size(self.volume_setting, self.camera_settings)
        self.saver.saving_parameter_queue.put(self.save_params)
        self.dispatcher.n_planes_queue.put(self.n_planes)
        self.restart_event.set()

    def refresh_volume_waveforms(self):
        if self.global_state != GlobalState.VOLUME_PREVIEW:
            return

        self.volume_waveforms_dirty = False
        self.waveform = None
        self.scanner.waveform_queue.clear()
        self.prepare_event.set()
        self.restart_volume_playback()

    @property
    def n_planes(self):
        if self.global_state == GlobalState.VOLUME_PREVIEW:
            return (
                self.volume_setting.n_planes
                - self.volume_setting.n_skip_start
                - self.volume_setting.n_skip_end
            )
        else:
            return 1

    @property
    def save_params(self):
        save_p = convert_save_params(
            self.save_settings,
            self.volume_setting,
            self.camera_settings,
            self.trigger_settings,
        )
        return save_p

    @property
    def scan_params(self):
        """Return parameters for the scanning, depending on the state."""
        if self.global_state == GlobalState.PREVIEW:
            params = convert_calibration_params(
                self.planar_setting, self.calibration.z_settings
            )

        elif self.global_state == GlobalState.VOLUME_PREVIEW:
            params = convert_volume_params(
                self.planar_setting, self.volume_setting, self.calibration
            )

        else:
            raise RuntimeError(f"Unexpected global_state: {self.global_state}")

        params.experiment_state = self.experiment_state
        return params

    @property
    def camera_params(self):
        camera_params = CamParameters(
            exposure_time=self.camera_settings.exposure_time,
            binning=int(self.camera_settings.binning),
            roi=tuple(self.camera_settings.roi),
        )

        camera_params.trigger_mode = (
            TriggerMode.FREE
            if self.global_state == GlobalState.PREVIEW
            else TriggerMode.EXTERNAL_TRIGGER
        )

        camera_params.camera_mode = (
            CameraMode.PREVIEW
            if self.live_camera_state == LiveCameraState.RUNNING
            else CameraMode.PAUSED
        )

        return camera_params

    @property
    def all_settings(self):
        all_settings = dict(scanning=self.scan_params, camera=self.camera_params)

        if self.waveform is not None:
            pulses = self.calculate_pulse_times() * self.sample_rate
            try:
                pulse_log = self.waveform[pulses.astype(int)]
                all_settings["piezo_log"] = dict(trigger=pulse_log.tolist())
            except IndexError:
                pass

        return all_settings

    def start_experiment(self) -> None:
        """
        Sets all the signals and cleans the queue
        to trigger the start of the experiment
        """
        if self.live_camera_state != LiveCameraState.RUNNING:
            self.logger.log_message("experiment start rejected: camera not running")
            return

        self.current_exp_state = GlobalState.EXPERIMENT_RUNNING
        self.logger.log_message("started experiment")
        if self.global_state == GlobalState.VOLUME_PREVIEW:
            if self.volume_waveforms_dirty:
                self.refresh_volume_waveforms()
            else:
                self.restart_volume_playback()
        else:
            self.send_preview_scansave_settings()
        self.send_manual_duration()
        self.saver.save_queue.clear()
        self.camera.image_queue.clear()
        time.sleep(0.01)
        self.is_saving_event.set()

    def end_experiment(self) -> None:
        """
        Sets all the signals and cleans the queue
        to trigger the end of the experiment
        """
        self.logger.log_message("experiment ended")
        self.is_saving_event.clear()
        self.saver.save_queue.clear()
        if self.global_state == GlobalState.VOLUME_PREVIEW:
            self.restart_volume_playback()
        else:
            self.send_preview_scansave_settings()
        self.current_exp_state = self.global_state

    def is_exp_started(self) -> bool:
        """
        check if the experiment has started:
        looks for tha change in the value hold by current_exp_state

        Returns:
            bool
        """
        if (
            self.current_exp_state == GlobalState.EXPERIMENT_RUNNING
            and self.prev_exp_state != GlobalState.EXPERIMENT_RUNNING
        ):
            self.prev_exp_state = GlobalState.EXPERIMENT_RUNNING
            return True
        else:
            return False

    def is_exp_ended(self) -> bool:
        """
        check if the experiment has ended:
        looks for tha change in the value hold by current_exp_state

        Returns:
            bool
        """
        if (
            self.prev_exp_state == GlobalState.EXPERIMENT_RUNNING
            and self.current_exp_state != GlobalState.EXPERIMENT_RUNNING
        ):
            self.prev_exp_state = self.current_exp_state
            return True
        else:
            return False

    def obtain_noise_average(self, n_images=50):
        """Obtains average noise of n_images to subtract to acquired,
        both for display and saving.

        Parameters
        ----------
        n_images : int
            Number of frames to average.

        """
        self.noise_subtraction_active.clear()

        n_image = 0
        while n_image < n_images:
            current_volume = self.get_volume()
            if current_volume is not None:
                current_image = current_volume[0, :, :]
                if n_image == 0:
                    calibration_set = np.empty(
                        shape=(n_images, *current_image.shape),
                        dtype=current_volume.dtype,
                    )
                calibration_set[n_image, :, :] = current_image
                n_image += 1

        self.calibration_ref = np.mean(calibration_set, axis=0).astype(
            dtype=current_volume.dtype
        )

        self.noise_subtraction_active.set()

        self.dispatcher.calibration_ref_queue.put(self.calibration_ref)

    def reset_noise_subtraction(self):
        self.calibration_ref = None
        self.noise_subtraction_active.clear()

    def get_volume(self):
        # TODO consider get_last_parameters method
        try:
            return self.dispatcher.viewer_queue.get(timeout=0.001)
        except Empty:
            return None

    def get_save_status(self) -> Optional[SavingStatus]:
        return get_last_parameters(self.saver.saved_status_queue)

    def get_triggered_frame_rate(self):
        return get_last_parameters(self.camera.triggered_frame_rate_queue)

    def get_waveform(self):
        waveform = get_last_parameters(self.scanner.waveform_queue)
        if waveform is not None:
            self.waveform = waveform
        return self.waveform

    def calculate_pulse_times(self):
        return np.arange(
            self.volume_setting.n_skip_start,
            self.volume_setting.n_planes - self.volume_setting.n_skip_end,
        ) / (self.volume_setting.frequency * self.volume_setting.n_planes)

    def send_manual_duration(self):
        self.experiment_duration_queue.put(self.trigger_settings.experiment_duration)

    def wrap_up(self):
        self.stop_event.set()

        self.scanner.join(timeout=10)
        self.saver.join(timeout=10)
        self.camera.join(timeout=10)
        self.dispatcher.join(timeout=10)
        self.logger.close()
