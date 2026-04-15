from sashimi.hardware.scanning.__init__ import AbstractScanInterface

from contextlib import contextmanager

from nidaqmx.task import Task
from nidaqmx.constants import Edge, AcquisitionType
from nidaqmx.stream_readers import AnalogSingleChannelReader
from nidaqmx.stream_writers import AnalogMultiChannelWriter

import numpy as np


@contextmanager
def open_niboard(sample_rate, n_samples, conf):
    with Task() as read_task, Task() as write_task:
        try:
            yield NIBoards(
                sample_rate,
                n_samples,
                conf,
                read_task=read_task,
                write_task=write_task,
            )
        finally:
            pass


class NIBoards(AbstractScanInterface):
    def __init__(self, *args, read_task, write_task):
        super().__init__(*args)
        self.read_task = read_task
        self.write_task = write_task

        self.writer = AnalogMultiChannelWriter(write_task.out_stream)
        self.z_reader = AnalogSingleChannelReader(read_task.in_stream)

        self.ao_array = np.zeros((4, self.n_samples))

        self.read_array = np.zeros(self.n_samples)

        self.setup_tasks()

    def setup_tasks(self):
        # Configure the channels

        # read channel is only the piezo position
        self.read_task.ai_channels.add_ai_voltage_chan(
            self.conf["scan_board"]["read"]["channel"],
            min_val=self.conf["scan_board"]["read"]["min_val"],
            max_val=self.conf["scan_board"]["read"]["max_val"],
        )

        # write channels are on xy_galvo, z_galvo, piezo and camera_trigger.
        write_conf = self.conf["scan_board"]["write"]
        for ch, vmin, vmax in zip(
                write_conf["channels"],
                write_conf["min_vals"],
                write_conf["max_vals"],
        ):
            self.write_task.ao_channels.add_ao_voltage_chan(
                ch,
                min_val=vmin,
                max_val=vmax,
            )

        # Set the timing of both to the onboard clock so that they are synchronised
        self.read_task.timing.cfg_samp_clk_timing(
            rate=self.sample_rate,
            source=self.conf["scan_board"]["sync"]["sample_clock"],
            active_edge=Edge.RISING,
            sample_mode=AcquisitionType.CONTINUOUS,
            samps_per_chan=self.n_samples,
        )
        self.write_task.timing.cfg_samp_clk_timing(
            rate=self.sample_rate,
            source="OnboardClock",
            active_edge=Edge.RISING,
            sample_mode=AcquisitionType.CONTINUOUS,
            samps_per_chan=self.n_samples,
        )

        # This is necessary to synchronise reading and writing
        self.read_task.triggers.start_trigger.cfg_dig_edge_start_trig(
            self.conf["scan_board"]["sync"]["start_trigger"], Edge.RISING
        )

    def start(self):
        self.read_task.start()
        self.write_task.start()

    def write(self):
        self.writer.write_many_sample(self.ao_array)

    def read(self):
        self.z_reader.read_many_sample(
            self.read_array,
            number_of_samples_per_channel=self.n_samples,
            timeout=1,
        )
        self.read_array[:] = self.read_array

    @property
    def xy_galvo(self):
        return self.ao_array[0, :]

    @xy_galvo.setter
    def xy_galvo(self, waveform):
        self.ao_array[0, :] = waveform

    @property
    def z_galvo(self):
        return self.ao_array[1, :]

    @z_galvo.setter
    def z_galvo(self, waveform):
        self.ao_array[1, :] = waveform

    @property
    def piezo(self):
        return self.read_array / self.conf["piezo"]["scale"]

    @piezo.setter
    def piezo(self, waveform):
        self.ao_array[2, :] = waveform * self.conf["piezo"]["scale"]

    @property
    def camera_trigger(self):
        return self.ao_array[3, :]

    @camera_trigger.setter
    def camera_trigger(self, waveform):
        self.ao_array[3, :] = waveform
