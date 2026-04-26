import os
import sys
from pathlib import Path
from warnings import warn

import numpy as np

from cshascope.lightsheet.hardware.cameras.interface import (
    AbstractCamera,
    CameraException,
    CameraWarning,
)
from cshascope.lightsheet.config import read_config

conf = read_config()


def _prepare_thorlabs_runtime():
    if os.name != "nt":
        raise CameraException(
            "The Thorlabs backend is currently implemented for Windows only."
        )

    sdk_conf = conf.get("thorlabs_sdk", {})

    dll_dir = sdk_conf.get("dll_dir", "") or os.environ.get(
        "THORLABS_SDK_DLL_DIR", ""
    )
    if not dll_dir:
        raise CameraException(
            "Missing Thorlabs DLL path. Set "
            "[thorlabs_sdk].dll_dir in hardware_config.toml "
        )

    dll_dir = Path(dll_dir)
    if not dll_dir.exists():
        raise CameraException(
            f"Thorlabs DLL directory does not exist: {dll_dir}"
        )

    # Make native DLLs visible on Windows
    if hasattr(os, "add_dll_directory"):
        os.add_dll_directory(str(dll_dir))

    # Also prepend PATH for libraries that still resolve through PATH
    os.environ["PATH"] = str(dll_dir) + os.pathsep + os.environ.get("PATH", "")


_prepare_thorlabs_runtime()

try:
    from thorlabs_tsi_sdk.tl_camera import TLCameraSDK, ROI, TLCameraError
    from thorlabs_tsi_sdk.tl_camera_enums import (
        OPERATION_MODE,
        TRIGGER_POLARITY,
        USB_PORT_TYPE,
    )
except Exception as exc:
    raise CameraException(
        "Could not import Thorlabs SDK. Check python_sdk_dir and dll_dir."
    ) from exc


class ThorlabsCamera(AbstractCamera):
    """
    Thorlabs implementation of the lightsheet camera interface.

    Important runtime convention inside lightsheet:
    ROI is handled as:
        (vpos, hpos, vsize, hsize)

    and those values are expressed in displayed coordinates, i.e. after binning.

    Therefore, when applying ROI to the hardware, this backend converts back to
    sensor-pixel coordinates by multiplying the ROI values by self.binning.
    """

    _N_FRAMES_TO_BUFFER = 1000

    def __init__(self, camera_id, max_sensor_resolution):
        super().__init__(camera_id, max_sensor_resolution)

        self.sdk = None
        self.camera = None
        self.available_serials = []
        self.serial_number = None

        self._trigger_mode = None
        self._is_armed = False

        try:
            self.sdk = TLCameraSDK()
            self.available_serials = self.sdk.discover_available_cameras()

            if len(self.available_serials) == 0:
                raise CameraException("No Thorlabs cameras were detected.")

            if camera_id < 0 or camera_id >= len(self.available_serials):
                raise CameraException(
                    f"Requested camera_id={camera_id}, but only "
                    f"{len(self.available_serials)} Thorlabs camera(s) were found: "
                    f"{self.available_serials}"
                )

            self.serial_number = self.available_serials[camera_id]
            self.camera = self.sdk.open_camera(self.serial_number)

            # Return quickly from polling if no frame is pending
            self.camera.image_poll_timeout_ms = 1

            # Basic sanity checks for the modes lightsheet needs
            if not self.camera.get_is_operation_mode_supported(
                OPERATION_MODE.SOFTWARE_TRIGGERED
            ):
                raise CameraException(
                    "This Thorlabs camera does not support SOFTWARE_TRIGGERED mode."
                )

            if not self.camera.get_is_operation_mode_supported(
                OPERATION_MODE.HARDWARE_TRIGGERED
            ):
                raise CameraException(
                    "This Thorlabs camera does not support HARDWARE_TRIGGERED mode."
                )

            if self.camera.usb_port_type != USB_PORT_TYPE.USB3_0:
                warn(
                    "The Thorlabs camera is not connected through USB 3.0. "
                    "This may cause dropped frames or failure to acquire.",
                    CameraWarning,
                )

            # Apply defaults in a safe order
            self.binning = conf["camera"]["default_binning"]
            self._roi = self._full_frame_roi_display()
            self.roi = self._roi
            self.exposure_time = conf["camera"]["default_exposure"]

            # Keep the same startup behavior as the Hamamatsu backend:
            # default trigger mode is external
            self.trigger_mode = 2

        except Exception:
            self.shutdown()
            raise

    def _full_frame_roi_display(self):
        return (
            0,
            0,
            int(self.max_sensor_resolution[0] // self.binning),
            int(self.max_sensor_resolution[1] // self.binning),
        )

    # ------------------------------------------------------------------
    # Basic properties
    # ------------------------------------------------------------------
    @property
    def binning(self):
        return int(self.camera.binx)

    @binning.setter
    def binning(self, n_bin):
        n_bin = int(n_bin)
        try:
            self.camera.binx = n_bin
            self.camera.biny = n_bin
        except TLCameraError as exc:
            raise CameraException(
                f"Could not set Thorlabs binning to {n_bin}"
            ) from exc

        if hasattr(self, "_roi") and self._roi is not None:
            self._roi = self._full_frame_roi_display()

    @property
    def exposure_time(self):
        # Thorlabs uses microseconds, lightsheet uses milliseconds
        return float(self.camera.exposure_time_us) / 1000.0

    @exposure_time.setter
    def exposure_time(self, exp_val):
        exp_val_ms = float(exp_val)
        try:
            self.camera.exposure_time_us = int(round(exp_val_ms * 1000.0))
        except TLCameraError as exc:
            raise CameraException(
                f"Could not set Thorlabs exposure time to {exp_val_ms} ms"
            ) from exc

    @property
    def frame_rate(self):
        """
        Read-only frame rate.

        Prefer the SDK measured frame rate. If unavailable, fall back to a rough
        estimate from exposure time.
        """
        try:
            return float(self.camera.get_measured_frame_rate_fps())
        except Exception:
            exp_s = max(self.exposure_time * 1e-3, 1e-6)
            return 1.0 / exp_s

    @property
    def roi(self):
        return self._roi

    @roi.setter
    def roi(self, exp_val: tuple):
        """
        exp_val uses lightsheet convention:
            (vpos, hpos, vsize, hsize)
        in displayed / post-binning coordinates.
        """
        if len(exp_val) != 4:
            raise CameraException(f"ROI must have length 4, got {exp_val}")

        vpos, hpos, vsize, hsize = [int(v) for v in exp_val]

        if vsize <= 0 or hsize <= 0:
            raise CameraException(f"Invalid ROI size: {exp_val}")

        max_v = int(self.max_sensor_resolution[0] // self.binning)
        max_h = int(self.max_sensor_resolution[1] // self.binning)

        # clamp in displayed coordinates
        vpos = max(0, min(vpos, max_v - 1))
        hpos = max(0, min(hpos, max_h - 1))
        vsize = max(1, min(vsize, max_v - vpos))
        hsize = max(1, min(hsize, max_h - hpos))

        actual_top_px, actual_left_px, actual_vsize, actual_hsize = (
            self._apply_roi_display_units(
                top_disp=vpos,
                left_disp=hpos,
                height_disp=vsize,
                width_disp=hsize,
            )
        )

        # store back in lightsheet coordinates
        self._roi = (
            actual_top_px // self.binning,
            actual_left_px // self.binning,
            actual_vsize,
            actual_hsize,
        )

    @property
    def trigger_mode(self):
        return self._trigger_mode

    @trigger_mode.setter
    def trigger_mode(self, exp_val):
        """
        Accept either lightsheet TriggerMode enums or raw integer values.
        We store the original object for compatibility, but normalize by .value
        when applying it.
        """
        mode_value = getattr(exp_val, "value", exp_val)
        mode_value = int(mode_value)

        if mode_value not in (1, 2):
            raise CameraException(f"Unsupported trigger mode value: {mode_value}")

        self._trigger_mode = exp_val

    @property
    def frame_shape(self):
        return (
            int(self.camera.image_height_pixels),
            int(self.camera.image_width_pixels),
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _normalize_trigger_mode(exp_val):
        return int(getattr(exp_val, "value", exp_val))

    def _apply_roi_display_units(self, top_disp, left_disp, height_disp, width_disp):
        """
        Receive ROI in displayed/post-binning coordinates.
        Convert once to sensor coordinates, try both inclusive and exclusive
        lower-right conventions, then read back actual start position and
        actual output size from the SDK.
        """
        top_px = top_disp * self.binning
        left_px = left_disp * self.binning
        height_px = height_disp * self.binning
        width_px = width_disp * self.binning

        sensor_h = int(self.max_sensor_resolution[0])
        sensor_w = int(self.max_sensor_resolution[1])

        top_px = max(0, min(top_px, sensor_h - self.binning))
        left_px = max(0, min(left_px, sensor_w - self.binning))
        height_px = max(self.binning, min(height_px, sensor_h - top_px))
        width_px = max(self.binning, min(width_px, sensor_w - left_px))

        candidates = [
            ROI(
                left_px,
                top_px,
                left_px + width_px - 1,
                top_px + height_px - 1,
            ),
            ROI(
                left_px,
                top_px,
                left_px + width_px,
                top_px + height_px,
            ),
        ]

        best_result = None
        best_error = None
        last_error = None

        for candidate in candidates:
            try:
                self.camera.roi = candidate

                actual_roi = self.camera.roi
                actual_h_disp = int(self.camera.image_height_pixels)
                actual_w_disp = int(self.camera.image_width_pixels)

                actual_top_px = int(actual_roi.upper_left_y_pixels)
                actual_left_px = int(actual_roi.upper_left_x_pixels)

                err = abs(actual_h_disp - height_disp) + abs(actual_w_disp - width_disp)

                result = (
                    actual_top_px,
                    actual_left_px,
                    actual_h_disp,
                    actual_w_disp,
                )

                if best_error is None or err < best_error:
                    best_error = err
                    best_result = result

                if err == 0:
                    return result

            except TLCameraError as exc:
                last_error = exc

        if best_result is not None:
            return best_result

        if last_error is not None:
            raise CameraException(
                f"Could not set ROI: top={top_disp}, left={left_disp}, "
                f"height={height_disp}, width={width_disp}"
            ) from last_error

        raise CameraException("Thorlabs SDK did not accept any ROI candidate.")

    # ------------------------------------------------------------------
    # Acquisition control
    # ------------------------------------------------------------------
    def start_acquisition(self):
        """
        Prepare camera acquisition.

        lightsheet only needs two modes:
        - FREE: continuous acquisition in software-triggered mode
        - EXTERNAL_TRIGGER: one frame per hardware trigger pulse
        """
        if self._is_armed:
            return

        try:
            trigger_mode_value = self._normalize_trigger_mode(self._trigger_mode)

            if trigger_mode_value == 1:
                # FREE mode -> continuous video via one software trigger
                self.camera.operation_mode = OPERATION_MODE.SOFTWARE_TRIGGERED
                self.camera.frames_per_trigger_zero_for_unlimited = 0
                self.camera.arm(self._N_FRAMES_TO_BUFFER)
                self.camera.issue_software_trigger()

            elif trigger_mode_value == 2:
                # EXTERNAL_TRIGGER -> one frame per external pulse
                self.camera.operation_mode = OPERATION_MODE.HARDWARE_TRIGGERED
                self.camera.frames_per_trigger_zero_for_unlimited = 1
                self.camera.trigger_polarity = TRIGGER_POLARITY.ACTIVE_HIGH
                self.camera.arm(self._N_FRAMES_TO_BUFFER)

            else:
                raise CameraException(
                    f"Unsupported trigger mode value: {trigger_mode_value}"
                )

            self._is_armed = True

        except TLCameraError as exc:
            raise CameraException("Could not start Thorlabs acquisition") from exc

    def get_frames(self):
        """
        Poll all currently pending frames and return them as a list of numpy arrays.

        The Thorlabs SDK explicitly states that Frame.image_buffer becomes invalid
        after another poll / disarm / close, so each frame must be copied.
        """
        frames = []

        while True:
            try:
                frame = self.camera.get_pending_frame_or_null()
            except TLCameraError as exc:
                raise CameraException(
                    "Error while polling Thorlabs frames"
                ) from exc

            if frame is None:
                break

            copied = np.copy(frame.image_buffer).reshape(self.frame_shape)
            frames.append(copied)

        return frames

    def stop_acquisition(self):
        """
        Disarm the camera. This is required before changing roi or operation_mode.
        """
        if not self.camera or not self._is_armed:
            return

        try:
            self.camera.disarm()
        except TLCameraError as exc:
            raise CameraException("Could not stop Thorlabs acquisition") from exc
        finally:
            self._is_armed = False

    def shutdown(self):
        """
        Safe shutdown even if initialization was only partially completed.
        """
        try:
            if self.camera is not None:
                try:
                    self.stop_acquisition()
                except Exception:
                    pass

                try:
                    self.camera.dispose()
                except Exception:
                    pass
                finally:
                    self.camera = None

        finally:
            if self.sdk is not None:
                try:
                    self.sdk.dispose()
                except Exception:
                    pass
                finally:
                    self.sdk = None