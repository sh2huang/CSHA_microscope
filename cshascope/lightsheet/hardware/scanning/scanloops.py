from copy import deepcopy
from dataclasses import dataclass, asdict
from enum import Enum
from multiprocessing.queues import Queue
from time import sleep
from typing import Tuple, Union
from arrayqueues.shared_arrays import ArrayQueue

import numpy as np

from cshascope.lightsheet.config import read_config
from cshascope.lightsheet.processes.logging import ConcurrenceLogger
from cshascope.lightsheet.utilities import lcm, get_last_parameters
from cshascope.lightsheet.waveforms import (
    TriangleWaveform,
    SawtoothWaveform,
    set_impulses,
    camera_trigger_pulse_samples,
)
from cshascope.lightsheet.hardware.scanning import ScanningError
from cshascope.lightsheet.hardware.scanning.__init__ import AbstractScanInterface

conf = read_config()


class ScanningState(Enum):
    PAUSED = 1
    PLANAR = 2
    VOLUMETRIC = 3


class ExperimentPrepareState(Enum):
    PREVIEW = 1
    NO_TRIGGER = 2
    EXPERIMENT_STARTED = 3
    ABORT = 4


@dataclass
class XYScanning:
    vmin: float = 1
    vmax: float = 0
    frequency: float = 800


@dataclass
class PlanarScanning:
    galvo: XYScanning = XYScanning()


@dataclass
class ZManual:
    piezo: float = 0
    galvo: float = 0


@dataclass
class ZScanning:
    piezo_min: float = 0
    piezo_max: float = 0
    frequency: float = 1
    galvo_sync: Tuple[float, float] = (0.0, 0.0)


@dataclass
class TriggeringParameters:
    n_planes: int = 0
    n_skip_start: int = 0
    n_skip_end: int = 0
    frequency: Union[None, float] = None


@dataclass
class ScanParameters:
    state: ScanningState = ScanningState.PAUSED
    experiment_state: ExperimentPrepareState = ExperimentPrepareState.PREVIEW
    z: Union[ZScanning, ZManual] = ZManual()
    xy: PlanarScanning = PlanarScanning()
    triggering: TriggeringParameters = TriggeringParameters()


@dataclass
class PreparedVolumeWaveforms:
    ao_waveforms: np.ndarray
    measured_piezo: np.ndarray
    z_period_samples: int
    repeat_samples: int


class ScanLoop:
    """General class for the control of the event loop of the scanning, taking
    care of the synchronization between the galvo and piezo scanning and the camera triggering.
    In this class we handle only the lateral scanning, which is common to calibration,
    planar, and volumetric acquisitions. Each child class defines its own loop because
    the NI task lifecycle is different for calibration, waveform preparation, and
    volumetric playback.

    The class does not implement a Process by itself; instead, the suitable child of this class (depending on
    the scanning mode) is "mounted" by the ScannerProcess process, and the child loop method is executed.

    """

    def __init__(
        self,
        board: AbstractScanInterface,
        stop_event,
        restart_event,
        initial_parameters: ScanParameters,
        parameter_queue: Queue,
        n_samples,
        sample_rate,
        waveform_queue: ArrayQueue,
        wait_signal,
        logger: ConcurrenceLogger,
        trigger_exp_from_scanner,
    ):
        self.sample_rate = sample_rate
        self.n_samples = n_samples

        self.board = board

        self.stop_event = stop_event
        self.restart_event = restart_event
        self.logger = logger

        self.parameter_queue = parameter_queue
        self.waveform_queue = waveform_queue

        self.parameters = initial_parameters
        self.old_parameters = initial_parameters

        self.trigger_exp_from_scanner = trigger_exp_from_scanner

        self.started = False
        self.n_acquired = 0
        self.first_update = True
        self.i_sample = 0
        self.n_samples_read = 0

        self.xy_waveform = TriangleWaveform(**asdict(self.parameters.xy.galvo))

        self.time = np.arange(self.n_samples) / self.sample_rate
        self.shifted_time = self.time.copy()

        self.wait_signal = wait_signal

    def initialize(self):
        self.n_acquired = 0
        self.first_update = True
        self.i_sample = 0
        self.n_samples_read = 0

    def n_samples_period(self):
        return int(round(self.sample_rate / self.xy_waveform.frequency))

    def update_settings(self):
        """Update parameters and return True only if got new parameters."""
        new_params = get_last_parameters(self.parameter_queue)
        if new_params is None:
            return False

        self.parameters = new_params
        self.xy_waveform = TriangleWaveform(**asdict(self.parameters.xy.galvo))
        self.first_update = False  # To avoid multiple updates
        return True

    def loop_condition(self):
        """Returns False if main event loop has to be interrupted. this happens both when we want
        to restart the scanning loop (if restart_event is set), or we want to interrupt scanning
        (stop_event is set).

        """
        if self.restart_event.is_set():
            self.restart_event.clear()
            return False
        return not self.stop_event.is_set()

    def check_start(self):
        if not self.started:
            self.board.start()
            self.started = True

    def fill_xy_waveform(self):
        self.shifted_time[:] = self.time + self.i_sample / self.sample_rate
        self.board.xy_galvo = self.xy_waveform.values(self.shifted_time)

    def write(self):
        self.board.write()
        self.logger.log_message("write")

    def read(self):
        self.board.read()
        self.logger.log_message("read")
        self.n_samples_read += self.board.n_samples

    def loop(self, first_run=False):
        raise NotImplementedError("ScanLoop subclasses must define their own loop.")


class PlanarScanLoop(ScanLoop):
    """Class for controlling the planar scanning mode, where we image only one plane and
    do not control the piezo and vertical galvo."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.prepared_waveforms = None

    def loop_condition(self):
        return (
            super().loop_condition() and self.parameters.state == ScanningState.PLANAR
        )

    def n_samples_period(self):
        if (
            self.parameters.triggering.frequency is None
            or self.parameters.triggering.frequency == 0
        ):
            return super().n_samples_period()
        else:
            n_samples_trigger = int(
                round(self.sample_rate / self.parameters.triggering.frequency)
            )
            return lcm(n_samples_trigger, super().n_samples_period())

    def prepare_waveforms(self):
        if not isinstance(self.parameters.z, ZManual):
            raise ScanningError("Planar scanning requires manual z parameters.")

        repeat_samples = self.n_samples_period()
        repeat_time = np.arange(repeat_samples, dtype=np.float64) / self.sample_rate

        ao_waveforms = np.zeros((4, repeat_samples), dtype=np.float64)
        ao_waveforms[0, :] = self.xy_waveform.values(repeat_time)
        ao_waveforms[1, :] = self.parameters.z.galvo
        ao_waveforms[2, :] = (
            self.parameters.z.piezo * self.board.conf["piezo"]["scale"]
        )
        ao_waveforms[3, :] = 0

        return ao_waveforms

    def restart_output(self):
        if self.started:
            self.board.stop()
            self.started = False

        self.wait_signal.set()
        self.prepared_waveforms = self.prepare_waveforms()
        self.board.configure_playback(self.prepared_waveforms)
        self.logger.log_message("configure_playback")
        self.board.start_playback()
        self.logger.log_message("start_playback")
        self.started = True
        self.wait_signal.clear()
        self.n_acquired += 1

    def loop(self, first_run=False):
        """Write planar calibration output only when parameters change.

        Calibration does not need piezo feedback. The AO task can regenerate the
        prepared waveform until a new galvo or piezo setting arrives.
        """
        try:
            while True:
                previous_parameters = deepcopy(self.parameters)
                was_first_update = self.first_update
                updated = self.update_settings()
                needs_output_update = (
                    was_first_update
                    or (updated and self.parameters != previous_parameters)
                )
                self.old_parameters = deepcopy(self.parameters)
                if not self.loop_condition():
                    break
                if needs_output_update:
                    self.restart_output()
                    self.first_update = False
                if first_run:
                    break
                sleep(0.05)
        finally:
            if self.started:
                self.board.stop()
                self.started = False


class VolumetricScanLoop(ScanLoop):
    def __init__(self, *args, prepared_waveforms=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.z_waveform = SawtoothWaveform()
        self.prepared_waveforms = prepared_waveforms
        self.wait_signal.set()

    def initialize(self):
        super().initialize()
        self.wait_signal.set()

    def loop_condition(self):
        return (
            super().loop_condition()
            and self.parameters.state == ScanningState.VOLUMETRIC
        )

    def z_period_samples(self):
        return int(round(self.sample_rate / self.parameters.z.frequency))

    def repeat_samples(self):
        return lcm(self.z_period_samples(), super().n_samples_period())

    def _prepare_block_arrays(self):
        self.board.z_galvo = 0
        self.board.camera_trigger = 0
        self.fill_xy_waveform()
        self.board.piezo = self.z_waveform.values(self.shifted_time)

    def prepare_waveforms(self, n_cycles=10, keep_last=5):
        self.wait_signal.set()
        self.initialize()

        self.z_waveform = SawtoothWaveform(
            frequency=self.parameters.z.frequency,
            vmin=self.parameters.z.piezo_min,
            vmax=self.parameters.z.piezo_max,
        )

        z_period_samples = self.z_period_samples()
        total_samples = n_cycles * z_period_samples
        measured_piezo = np.empty(total_samples, dtype=np.float64)
        write_pos = 0

        while write_pos < total_samples and self.loop_condition():
            self._prepare_block_arrays()
            self.write()
            self.check_start()
            self.read()

            n_copy = min(self.n_samples, total_samples - write_pos)
            measured_piezo[write_pos: write_pos + n_copy] = self.board.piezo[:n_copy]
            write_pos += n_copy
            self.i_sample += self.n_samples

        self.board.stop()
        self.started = False

        if write_pos < total_samples:
            raise ScanningError(
                "Volume waveform preparation interrupted before completion."
            )

        measured_cycles = measured_piezo.reshape(n_cycles, z_period_samples)
        averaged_piezo = np.mean(measured_cycles[-keep_last:], axis=0)
        self.waveform_queue.put(averaged_piezo.copy())

        repeat_samples = self.repeat_samples()
        repeat_time = np.arange(repeat_samples, dtype=np.float64) / self.sample_rate
        xy_repeat = self.xy_waveform.values(repeat_time)
        piezo_repeat = self.z_waveform.values(repeat_time)

        averaged_piezo_repeat = np.tile(
            averaged_piezo,
            repeat_samples // z_period_samples,
        )
        z_galvo_repeat = calc_sync(
            averaged_piezo_repeat, self.parameters.z.galvo_sync
        )
        if np.any(np.abs(z_galvo_repeat) >= 2):
            raise ScanningError(
                "Prepared z galvo waveform exceeds the configured safe range."
            )

        camera_cycle = np.zeros(z_period_samples, dtype=np.float64)
        trigger_width_samples = camera_trigger_pulse_samples(self.sample_rate)
        set_impulses(
            camera_cycle,
            self.parameters.triggering.n_planes,
            n_skip_start=self.parameters.triggering.n_skip_start,
            n_skip_end=self.parameters.triggering.n_skip_end,
            width_samples=trigger_width_samples,
        )
        camera_repeat = np.tile(
            camera_cycle,
            repeat_samples // z_period_samples,
        )

        ao_waveforms = np.zeros((4, repeat_samples), dtype=np.float64)
        ao_waveforms[0, :] = xy_repeat
        ao_waveforms[1, :] = z_galvo_repeat
        ao_waveforms[2, :] = piezo_repeat * self.board.conf["piezo"]["scale"]
        ao_waveforms[3, :] = camera_repeat

        return PreparedVolumeWaveforms(
            ao_waveforms=ao_waveforms,
            measured_piezo=averaged_piezo,
            z_period_samples=z_period_samples,
            repeat_samples=repeat_samples,
        )

    def start_playback(self):
        if self.prepared_waveforms is None:
            raise ScanningError("Volume playback requested without prepared waveforms.")

        self.wait_signal.set()
        self.board.configure_playback(self.prepared_waveforms.ao_waveforms)
        self.logger.log_message("configure_playback")
        self.board.start_playback()
        self.logger.log_message("start_playback")
        self.started = True
        self.wait_signal.clear()

    def loop(self, first_run=False):
        del first_run
        self.start_playback()
        try:
            while self.loop_condition():
                sleep(0.05)
        finally:
            self.board.stop()
            self.started = False
            self.wait_signal.set()


def calc_sync(z, sync_coef):
    return sync_coef[0] + sync_coef[1] * z
