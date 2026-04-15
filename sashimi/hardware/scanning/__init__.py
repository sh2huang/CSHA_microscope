from contextlib import contextmanager
from abc import ABC, abstractmethod


class ScanningError(Exception):
    pass


class AbstractScanInterface(ABC):
    def __init__(self, sample_rate, n_samples, conf, *args, **kwargs):
        self.sample_rate = sample_rate
        self.n_samples = n_samples
        self.conf = conf

    @abstractmethod
    def start(self):
        pass

    @abstractmethod
    def write(self):
        pass

    @abstractmethod
    def read(self):
        pass

    @property
    @abstractmethod
    def piezo(self):
        return None

    @piezo.setter
    @abstractmethod
    def piezo(self, waveform):
        pass

    @property
    @abstractmethod
    def z_galvo(self):
        return None

    @z_galvo.setter
    @abstractmethod
    def z_galvo(self, waveform):
        pass

    @property
    @abstractmethod
    def camera_trigger(self):
        return None

    @camera_trigger.setter
    @abstractmethod
    def camera_trigger(self, waveform):
        pass

    @property
    @abstractmethod
    def xy_galvo(self):
        return None

    @xy_galvo.setter
    @abstractmethod
    def xy_galvo(self, waveform):
        pass

@contextmanager
def open_abstract_interface(sample_rate, n_samples, conf) -> AbstractScanInterface:
    try:
        yield AbstractScanInterface(sample_rate, n_samples, conf)
    finally:
        pass
