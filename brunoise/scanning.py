from multiprocessing import Event, Process, Queue
import numpy as np
try:
    import nidaqmx
    from nidaqmx import Task
    from nidaqmx.stream_readers import AnalogMultiChannelReader
    from nidaqmx.stream_writers import AnalogMultiChannelWriter
    from nidaqmx.constants import Edge, AcquisitionType, RegenerationMode
    from nidaqmx.errors import DaqError
except ImportError:
    from theknights.task import Task
    from theknights.stream_readers import AnalogMultiChannelReader
    from theknights.stream_writers import AnalogMultiChannelWriter
    from theknights.constants import Edge, AcquisitionType, RegenerationMode
    from theknights.errors import DaqError


from arrayqueues.shared_arrays import ArrayQueue
from queue import Empty

import scanning_patterns
from copy import copy
from dataclasses import dataclass
from enum import Enum
from time import sleep, perf_counter


NI_USB_6363_MAX_AI_SAMPLE_RATE = 2000000.0 * 0.8
NI_USB_6363_MAX_AO_SAMPLE_RATE_3_CHANNELS = 1540000.0 * 0.8


class ScanningState(Enum):
    PREVIEW = 1
    EXPERIMENT_RUNNING = 2
    PAUSED = 3


@dataclass
class ScanningParameters:
    n_x: int = 200
    n_y: int = 200
    voltage_x: float = 3
    voltage_y: float = 3
    voltage_z: float = 0
    n_bin: int = 5
    n_turn: int = 10
    n_extra: int = 100
    signal_delay_us: float = 80.0
    sample_rate_out: float = 100000.0
    scanning_state: ScanningState = ScanningState.PREVIEW
    n_frames: int = 100
    framerate: float = 0.5935422602089269


def n_output_samples(sp: ScanningParameters):
    return scanning_patterns.n_total(sp.n_x, sp.n_y, sp.n_turn, sp.n_extra)


def sample_rate_in(sp: ScanningParameters):
    return sp.sample_rate_out * sp.n_bin


def max_sample_rate_out_for_binning(n_bin):
    return min(
        NI_USB_6363_MAX_AO_SAMPLE_RATE_3_CHANNELS,
        NI_USB_6363_MAX_AI_SAMPLE_RATE / max(1, n_bin),
    )


def limit_sample_rate_out(sample_rate_out, n_bin):
    return min(float(sample_rate_out), max_sample_rate_out_for_binning(n_bin))


def frame_duration(sp: ScanningParameters):
    return n_output_samples(sp) / sp.sample_rate_out


def frame_rate(sp: ScanningParameters):
    return 1.0 / frame_duration(sp)


def dwell_time_s(sp: ScanningParameters):
    return 1.0 / sp.sample_rate_out


def signal_delay_samples(sp: ScanningParameters):
    return -int(round(sp.signal_delay_us * sample_rate_in(sp) / 1000000.0))



def compute_waveform(sp: ScanningParameters):
    return scanning_patterns.simple_scanning_pattern(
        sp.n_x, sp.n_y, sp.n_turn, sp.n_extra
    )

class Scanner(Process):
    def __init__(self, experiment_start_event, max_queuesize=200):
        super().__init__()
        self.data_queue = ArrayQueue(max_mbytes=max_queuesize)
        self.time_queue = Queue()
        self.parameter_queue = Queue()
        self.stop_event = Event()
        self.experiment_start_event = experiment_start_event
        self.scanning_parameters = ScanningParameters()
        self.new_parameters = copy(self.scanning_parameters)

    def run(self):
        self.compute_scan_parameters()
        self.run_scanning()

    def compute_scan_parameters(self):
        self.scanning_parameters.sample_rate_out = limit_sample_rate_out(
            self.scanning_parameters.sample_rate_out,
            self.scanning_parameters.n_bin,
        )
        self.scanning_parameters.framerate = frame_rate(self.scanning_parameters)

        self.extent_x = (
            -self.scanning_parameters.voltage_x,
            self.scanning_parameters.voltage_x,
        )
        self.extent_y = (
            -self.scanning_parameters.voltage_y,
            self.scanning_parameters.voltage_y,
        )

        self.n_x = self.scanning_parameters.n_x
        self.n_y = self.scanning_parameters.n_y
        self.raw_x, self.raw_y = compute_waveform(self.scanning_parameters)
        self.pos_x = (
            self.raw_x * ((self.extent_x[1] - self.extent_x[0]) / self.n_x)
            + self.extent_x[0]
        )
        self.pos_y = (
            self.raw_y * ((self.extent_y[1] - self.extent_y[0]) / self.n_y)
            + self.extent_y[0]
        )

        self.n_bin = self.scanning_parameters.n_bin

        self.n_samples_out = len(self.raw_x)
        self.n_samples_in = self.n_samples_out * self.n_bin

        self.sample_rate_out = self.scanning_parameters.sample_rate_out
        self.plane_duration = self.n_samples_out / self.sample_rate_out

        self.sample_rate_in = sample_rate_in(self.scanning_parameters)

        self.write_signals = np.stack(
            [
                self.pos_x,
                self.pos_y,
                np.full(self.n_samples_out, self.scanning_parameters.voltage_z),
            ],
            0,
        )
        self.read_buffer = np.zeros((1, self.n_samples_in))

    def setup_tasks(self, read_task, write_task):
        # Configure the acquisition and galvo output lines.
        read_task.ai_channels.add_ai_voltage_chan(
            "Dev1/ai1", min_val=-1, max_val=1
        )
        write_task.ao_channels.add_ao_voltage_chan("Dev1/ao0", min_val=-5, max_val=5)
        write_task.ao_channels.add_ao_voltage_chan("Dev1/ao1", min_val=-5, max_val=5)
        write_task.ao_channels.add_ao_voltage_chan("Dev1/ao2", min_val=0, max_val=10)

        write_task.out_stream.regen_mode = RegenerationMode.ALLOW_REGENERATION

        # Set the timing of both to the onboard clock so that they are synchronised
        read_task.timing.cfg_samp_clk_timing(
            rate=self.sample_rate_in,
            source="OnboardClock",
            active_edge=Edge.RISING,
            sample_mode=AcquisitionType.CONTINUOUS,
            samps_per_chan=self.n_samples_in * 16,
        )
        write_task.timing.cfg_samp_clk_timing(
            rate=self.sample_rate_out,
            source="OnboardClock",
            active_edge=Edge.RISING,
            sample_mode=AcquisitionType.CONTINUOUS,
            samps_per_chan=self.n_samples_out,
        )

        # This is necessary to synchronise reading and wrting
        read_task.triggers.start_trigger.cfg_dig_edge_start_trig(
            "/Dev1/ao/StartTrigger", Edge.RISING
        )

    def check_start_plane(self):
        if self.scanning_parameters.scanning_state == ScanningState.EXPERIMENT_RUNNING:
            while not self.experiment_start_event.is_set():
                sleep(0.0001)

    def wait_next_parameters(self):
        if self.new_parameters != self.scanning_parameters:
            return
        while not self.stop_event.is_set():
            try:
                self.new_parameters = self.parameter_queue.get(timeout=0.001)
                return
            except Empty:
                pass

    def scan_loop(self, read_task, write_task):
        writer = AnalogMultiChannelWriter(write_task.out_stream)
        reader = AnalogMultiChannelReader(read_task.in_stream)

        i_acquired = 0
        try:
            writer.write_many_sample(self.write_signals)

            read_task.start()
            if i_acquired == 0:
                self.check_start_plane()
            write_task.start()

            while not self.stop_event.is_set() and (
                    not self.scanning_parameters.scanning_state
                        == ScanningState.EXPERIMENT_RUNNING
                    or i_acquired < self.scanning_parameters.n_frames
            ):
                reader.read_many_sample(
                    self.read_buffer,
                    number_of_samples_per_channel=self.n_samples_in,
                    timeout=max(1.0, self.plane_duration * 2 + 0.1),
                )
                self.time_queue.put(perf_counter())
                i_acquired += 1

                raw_frame = self.read_buffer.copy()
                self.data_queue.put(raw_frame)

                try:
                    self.new_parameters = self.parameter_queue.get(timeout=0.0001)
                    if self.new_parameters != self.scanning_parameters and (
                            self.scanning_parameters.scanning_state
                            != ScanningState.EXPERIMENT_RUNNING
                            or self.new_parameters.scanning_state in (
                                    ScanningState.PREVIEW,
                                    ScanningState.PAUSED,
                            )
                    ):
                        break
                except Empty:
                    pass

        except DaqError as e:
            print(e)

        return (
                not self.stop_event.is_set()
                and self.scanning_parameters.scanning_state == ScanningState.EXPERIMENT_RUNNING
                and i_acquired >= self.scanning_parameters.n_frames
        )

    def pause_loop(self):
        while not self.stop_event.is_set():
            try:
                self.new_parameters = self.parameter_queue.get(timeout=0.001)
                if self.new_parameters != self.scanning_parameters:
                    break
            except Empty:
                pass

    def run_scanning(self):
        while not self.stop_event.is_set():
            self.scanning_parameters = self.new_parameters
            self.compute_scan_parameters()
            plane_finished = False
            with Task() as write_task, Task() as read_task:
                self.setup_tasks(read_task, write_task)
                if self.scanning_parameters.scanning_state == ScanningState.PAUSED:
                    self.pause_loop()
                else:
                    plane_finished = self.scan_loop(read_task, write_task)

            if plane_finished:
                self.wait_next_parameters()


class ImageReconstructor(Process):
    def __init__(self, data_in_queue, stop_event, max_mbytes_queue=300):
        super().__init__()
        self.data_in_queue = data_in_queue
        self.parameter_queue = Queue()
        self.stop_event = stop_event
        self.output_queue = ArrayQueue(max_mbytes_queue)
        self.scanning_parameters = None
        self.waveform = None

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.scanning_parameters = self.parameter_queue.get(timeout=0.001)
                self.waveform = compute_waveform(self.scanning_parameters)
            except Empty:
                pass

            try:
                images = self.data_in_queue.get(timeout=0.001)
                if self.scanning_parameters is None or self.waveform is None:
                    continue
                recon_images = []
                for image in images:
                    recon_images.append(
                        scanning_patterns.reconstruct_image_pattern(
                            np.roll(
                                image,
                                signal_delay_samples(self.scanning_parameters),
                            ),
                            *self.waveform,
                            (self.scanning_parameters.n_y, self.scanning_parameters.n_x),
                            self.scanning_parameters.n_bin,
                        )
                    )
                self.output_queue.put(np.stack(recon_images))
            except Empty:
                pass
