import numpy as np
from numba import jit

CAMERA_TRIGGER_PULSE_S = 200e-6


class Waveform:
    def __init__(self, *args, **kwargs):
        pass

    def values(self, t):
        return np.zeros(len(self.t))


class ConstantWaveform(Waveform):
    def __init__(self, *args, constant_value=0, **kwargs):
        super().__init__()
        self.constant_value = constant_value

    def values(self, t):
        return np.full(len(t), self.constant_value)


class SawtoothWaveform(Waveform):
    def __init__(self, *args, frequency=1, vmin=0, vmax=1, **kwargs):
        super().__init__(*args, **kwargs)
        self.vmin = vmin
        self.vmax = vmax
        self.frequency = frequency

    def values(self, t):
        tf = t * self.frequency
        return (tf - np.floor(tf)) * (self.vmax - self.vmin) + self.vmin


class RecordedWaveform(Waveform):
    def __init__(self, *args, recording, **kwargs):
        super().__init__(*args, **kwargs)
        self.recording = recording
        self.i_sample = 0

    def values(self, t):
        out = self.recording[self.i_sample : self.i_sample + len(t)]
        self.i_sample = (self.i_sample + len(t)) % self.recording.shape[0]
        return out


class TriangleWaveform(Waveform):
    def __init__(self, *args, frequency=1, vmin=0, vmax=1, **kwargs):
        super().__init__(*args, **kwargs)
        self.vmin = vmin
        self.vmax = vmax
        self.frequency = frequency

    def values(self, t):
        tf = t * self.frequency
        return (
            self.vmin
            + (self.vmax - self.vmin) / 2
            + +(self.vmax - self.vmin)
            * (np.abs((tf - np.floor(tf + 1 / 2))) - 0.25)
            * 2
        )


def camera_trigger_pulse_samples(sample_rate, pulse_width_s=CAMERA_TRIGGER_PULSE_S):
    return max(1, int(np.ceil(sample_rate * pulse_width_s)))


def camera_trigger_pulse_duration_s(sample_rate, pulse_width_s=CAMERA_TRIGGER_PULSE_S):
    return camera_trigger_pulse_samples(sample_rate, pulse_width_s) / sample_rate


@jit(nopython=True)
def set_impulses(
    buffer, n_planes, n_skip_start, n_skip_end, width_samples=1, high=5
):
    buffer[:] = 0
    n_between_planes = int(round(len(buffer) / n_planes))
    pulse_width = max(1, min(width_samples, n_between_planes))
    for i in range(n_skip_start, n_planes - n_skip_end):
        start = i * n_between_planes
        stop = min(start + pulse_width, len(buffer))
        buffer[start:stop] = high
