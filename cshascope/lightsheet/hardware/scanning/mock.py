from cshascope.lightsheet.hardware.scanning.__init__ import AbstractScanInterface
from contextlib import contextmanager
import numpy as np
from time import sleep


class MockBoard(AbstractScanInterface):
    def __init__(self, sample_rate, n_samples, conf):
        super().__init__(sample_rate, n_samples, conf)
        self.piezo_array = np.zeros(n_samples)
        self.playback_waveform = None

    def start(self):
        pass

    def stop(self):
        pass

    def read(self):
        sleep(0.05)

    def write(self):
        sleep(0.05)

    def configure_playback(self, waveform):
        self.playback_waveform = waveform.copy()

    def start_playback(self):
        pass

    def measure_piezo_response(self, waveform, n_cycles, timeout=None):
        del timeout

        waveform = np.asarray(waveform, dtype=np.float64)
        if waveform.ndim != 2:
            raise ValueError("waveform must have shape (n_channels, n_samples).")
        if waveform.shape[0] < 3:
            raise ValueError("waveform must contain the piezo AO channel.")

        one_cycle = waveform[2, :] / self.conf["piezo"]["scale"]
        return np.tile(one_cycle, int(n_cycles))

    @property
    def piezo(self):
        len_sampling = len(self.piezo_array)
        return np.ones(len_sampling)

    @piezo.setter
    def piezo(self, waveform):
        self.piezo_array[:] = waveform

    @property
    def z_galvo(self):
        return None

    @z_galvo.setter
    def z_galvo(self, waveform):
        pass

    @property
    def camera_trigger(self):
        return None

    @camera_trigger.setter
    def camera_trigger(self, waveform):
        pass

    @property
    def xy_galvo(self):
        return None

    @xy_galvo.setter
    def xy_galvo(self, waveform):
        pass

@contextmanager
def open_mockboard(sample_rate, n_samples, conf) -> MockBoard:
    try:
        yield MockBoard(sample_rate, n_samples, conf)
    finally:
        pass
