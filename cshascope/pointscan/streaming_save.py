from multiprocessing import Process, Event, Queue
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from queue import Empty
import flammkuchen as fl
import numpy as np
import shutil
import json
import time


@dataclass
class SavingParameters:
    output_dir: Path
    plane_size: tuple
    n_t: int = 100
    n_z: int = 1
    plane_z_um: tuple = ()


@dataclass
class SavingStatus:
    target_params: SavingParameters
    i_t: int = 0
    i_z: int = 0


class StackSaver(Process):
    def __init__(self, stop_signal, data_queue, time_queue):
        super().__init__()
        self.stop_signal = stop_signal
        self.data_queue = data_queue
        self.time_queue = time_queue
        self.saving_signal = Event()
        self.busy_signal = Event()
        self.saving = False
        self.saving_parameter_queue = Queue()
        self.save_parameters: Optional[SavingParameters] = None
        self.i_in_plane = 0
        self.i_block = 0
        self.current_data = None
        self.saved_status_queue = Queue()
        self.dtype = np.int16
        # self.dtype = float
        self.current_time = None
        self.timestamps = None

    def run(self):
        while not self.stop_signal.is_set():
            if self.saving_signal.is_set() and self.save_parameters is not None:
                self.save_loop()
            else:
                self.receive_save_parameters()

    def save_loop(self):
        self.busy_signal.set()
        try:
            # remove files if some are found at the save location
            if (
                    Path(self.save_parameters.output_dir) / "original" / "stack_metadata.json"
            ).is_file():
                shutil.rmtree(Path(self.save_parameters.output_dir) / "original")

            (Path(self.save_parameters.output_dir) / "original").mkdir(
                parents=True, exist_ok=True
            )

            i_received = 0
            self.i_in_plane = 0
            self.i_block = 0
            self.current_data = np.empty(
                (self.save_parameters.n_t, 1, *self.save_parameters.plane_size),
                dtype=self.dtype,
            )
            self.current_time = np.empty(self.save_parameters.n_t)
            n_total = self.save_parameters.n_t * self.save_parameters.n_z
            while (
                    i_received < n_total
                    and self.saving_signal.is_set()
                    and not self.stop_signal.is_set()
            ):
                self.receive_save_parameters()
                try:
                    frame = self.data_queue.get(timeout=0.01)
                    self.fill_dataset(frame)
                    i_received += 1
                except Empty:
                    pass

            t_end = time.time()
            while time.time() - t_end < 5:
                try:
                    frame = self.data_queue.get(timeout=0.01)
                    self.fill_dataset(frame)
                    break
                except Empty:
                    pass

            if self.i_block > 0:
                self.finalize_dataset()
        finally:
            self.save_parameters = None
            self.busy_signal.clear()

    def cast(self, frame):
        """
        Conversion into a format appropriate for saving
        """
        if self.dtype == np.int16:
            frame = (frame / (2 / 2**12)).astype(self.dtype)
        return frame

    def fill_dataset(self, frame):
        self.current_data[self.i_in_plane, :, :, :] = self.cast(frame)
        try:
            t = self.time_queue.get(timeout=0.001)
            self.current_time[self.i_in_plane] = t
        except Empty:
            print('time queue is empty')
        self.i_in_plane += 1
        self.saved_status_queue.put(
            SavingStatus(
                target_params=self.save_parameters,
                i_t=self.i_in_plane,
                i_z=self.i_block,
            )
        )
        if self.i_in_plane == self.save_parameters.n_t:
            self.complete_plane()

    def dump_metadata(self, file):
        json.dump(
            {
                "shape_full": (
                    self.save_parameters.n_t,
                    self.i_block,
                    *self.current_data.shape[2:],
                ),
                "shape_block": (
                    self.save_parameters.n_t,
                    1,
                    *self.current_data.shape[2:],
                ),
                "crop_start": [0, 0, 0, 0],
                "crop_end": [0, 0, 0, 0],
                "padding": [0, 0, 0, 0],
                "plane_z_um": list(self.save_parameters.plane_z_um),
            },
            file,
        )

    def finalize_dataset(self):
        with open(
                (
                        Path(self.save_parameters.output_dir)
                        / "original"
                        / "stack_metadata.json"
                ),
                "w",
        ) as f:
            self.dump_metadata(f)

    def complete_plane(self):
        if self.i_block == 0:
            self.timestamps = self.current_time.copy() - self.current_time[0]
        else:
            self.timestamps = np.vstack((self.timestamps, self.current_time - self.current_time[0]))
        # save each time because the computer might crash before all planes are acquired
        fl.save(
            Path(self.save_parameters.output_dir)
            / "time.h5",
            self.timestamps.T,
            compression="blosc",
        )
        fl.save(
            Path(self.save_parameters.output_dir)
            / "original/{:04d}.h5".format(self.i_block),
            {"stack_4D": self.current_data[:,:1,:,:]},
            compression="blosc",
        )
        self.i_block += 1

        self.i_in_plane = 0

    def receive_save_parameters(self):
        try:
            self.save_parameters = self.saving_parameter_queue.get(timeout=0.001)
        except Empty:
            pass
