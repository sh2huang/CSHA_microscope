from multiprocessing import Queue

from sashimi.hardware.scanning.scanloops import (
    ScanningState,
    ScanParameters,
    PlanarScanLoop,
    VolumetricScanLoop,
)
from sashimi.hardware.scanning import ScanningError
from sashimi.hardware.scanning.mock import open_mockboard

try:
    from sashimi.hardware.scanning.ni import open_niboard

    NI_AVAILABLE = True
except ImportError:
    NI_AVAILABLE = False

from warnings import warn
from arrayqueues.shared_arrays import ArrayQueue

from sashimi.utilities import get_last_parameters
from sashimi.config import read_config
from sashimi.processes.logging import LoggingProcess
from sashimi.events import LoggedEvent


conf = read_config()

# Dictionary of options for the context within which the scanning has to run.
scan_conf_dict = dict(mock=open_mockboard)

# Add NI context if available. NI board will be initialized there.
if NI_AVAILABLE:
    scan_conf_dict["ni"] = open_niboard


class ScannerProcess(LoggingProcess):
    """Process that runs the scanning loop.

    Parameters
    ----------
    stop_event
    waiting_event
    restart_event
    start_experiment_from_scanner
    n_samples_waveform
    sample_rate

    The actual implementation of the control of the scanning loop happens in the ScanLoop class and its children.
    In the run method we constantly control the parameters, and we "mount" in the Scanner process a ScanLoop object
    of the suitable ScanLoop subclass depending on the scanning mode.
    Refer to the sashimi.hardware.scanning.scanloops module for details on the scanning implementation.

    """

    def __init__(
        self,
        stop_event: LoggedEvent,
        waiting_event: LoggedEvent,
        restart_event: LoggedEvent,
        prepare_event: LoggedEvent,
        start_experiment_from_scanner=False,
        n_samples_waveform=10000,
        sample_rate=40000,
    ):
        """"""
        super().__init__(name="scanner")

        self.stop_event = stop_event.new_reference(self.logger)
        self.restart_event = restart_event.new_reference(self.logger)
        self.prepare_event = prepare_event.new_reference(self.logger)
        self.wait_signal = waiting_event.new_reference(self.logger)

        self.parameter_queue = Queue()

        self.waveform_queue = ArrayQueue(max_mbytes=100)
        self.n_samples = n_samples_waveform
        self.sample_rate = sample_rate

        self.parameters = ScanParameters()
        self.start_experiment_from_scanner = start_experiment_from_scanner
        self.prepared_volume_waveforms = None

    def retrieve_parameters(self):
        new_params = get_last_parameters(self.parameter_queue)
        if new_params is not None:
            self.parameters = new_params

    def run(self):
        self.logger.log_message("started")
        configurator = scan_conf_dict[conf["scanning"]]
        while not self.stop_event.is_set():
            self.retrieve_parameters()
            if self.parameters.state == ScanningState.PAUSED:
                continue

            force_prepare = self.prepare_event.is_set()
            if force_prepare:
                self.prepare_event.clear()

            with configurator(self.sample_rate, self.n_samples, conf) as board:
                if self.parameters.state == ScanningState.PLANAR:
                    loop = PlanarScanLoop
                    prepared_waveforms = None
                elif self.parameters.state == ScanningState.VOLUMETRIC:
                    loop = VolumetricScanLoop
                    prepared_waveforms = self.prepared_volume_waveforms

                loop_kwargs = {}
                if issubclass(loop, VolumetricScanLoop):
                    loop_kwargs["prepared_waveforms"] = prepared_waveforms

                scanloop = loop(
                    board,
                    self.stop_event,
                    self.restart_event,
                    self.parameters,
                    self.parameter_queue,
                    self.n_samples,
                    self.sample_rate,
                    self.waveform_queue,
                    self.wait_signal,
                    self.logger,
                    self.start_experiment_from_scanner,
                    **loop_kwargs,
                )
                try:
                    if issubclass(loop, VolumetricScanLoop):
                        if (
                            force_prepare
                            or self.prepared_volume_waveforms is None
                        ):
                            self.prepared_volume_waveforms = (
                                scanloop.prepare_waveforms()
                            )
                            scanloop.prepared_waveforms = (
                                self.prepared_volume_waveforms
                            )
                        scanloop.loop()
                    else:
                        scanloop.loop(False)
                except ScanningError as e:
                    warn("NI error " + e.__repr__())
                    scanloop.initialize()
                self.retrieve_parameters()
        self.close_log()
