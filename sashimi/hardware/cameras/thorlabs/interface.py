import os
import sys
from pathlib import Path
from warnings import warn

import numpy as np

from sashimi.hardware.cameras.interface import (
    AbstractCamera,
    CameraException,
    CameraWarning,
)
from sashimi.config import read_config

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
            "or THORLABS_SDK_DLL_DIR in the environment."
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
    Thorlabs implementation of the sashimi camera interface.

    Important runtime convention inside sashimi:
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

        self._roi = (
            0,
            0,
            int(self.max_sensor_resolution[0]),
            int(self.max_sensor_resolution[1]),
        )
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

            # Basic sanity checks for the modes sashimi needs
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
            self.roi = self._roi
            self.exposure_time = conf["camera"]["default_exposure"]

            # Keep the same startup behavior as the Hamamatsu backend:
            # default trigger mode is external
            self.trigger_mode = 2

        except Exception:
            self.shutdown()
            raise

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

    @property
    def exposure_time(self):
        # Thorlabs uses microseconds, sashimi uses milliseconds
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
        exp_val follows sashimi convention:
            (vpos, hpos, vsize, hsize)
        in displayed / post-binning coordinates.

        This is converted back to sensor coordinates before sending to the camera.
        """
        if len(exp_val) != 4:
            raise CameraException(f"ROI must have length 4, got {exp_val}")

        vpos, hpos, vsize, hsize = [int(v) for v in exp_val]

        if vsize <= 0 or hsize <= 0:
            raise CameraException(f"Invalid ROI size: {exp_val}")

        # Convert from display coordinates back to sensor coordinates
        top_px = vpos * self.binning
        left_px = hpos * self.binning
        height_px = vsize * self.binning
        width_px = hsize * self.binning

        actual_top_px, actual_left_px, actual_height_px, actual_width_px = (
            self._apply_roi_sensor_units(
                top_px=top_px,
                left_px=left_px,
                height_px=height_px,
                width_px=width_px,
            )
        )

        # Store back in sashimi coordinates after any hardware nudging/clamping
        self._roi = (
            actual_top_px // self.binning,
            actual_left_px // self.binning,
            actual_height_px // self.binning,
            actual_width_px // self.binning,
        )

    @property
    def trigger_mode(self):
        return self._trigger_mode

    @trigger_mode.setter
    def trigger_mode(self, exp_val):
        """
        Accept either sashimi TriggerMode enums or raw integer values.
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

    def _apply_roi_sensor_units(self, top_px, left_px, height_px, width_px):
        """
        Apply ROI in sensor coordinates.

        Thorlabs ROI is represented as:
            ROI(upper_left_x, upper_left_y, lower_right_x, lower_right_y)

        The Python docs specify the corner representation but do not spell out
        whether lower-right is inclusive or exclusive in a way that is safe to
        assume blindly. To make this robust, we try both conventions and verify
        the resulting reported image shape from the SDK.
        """
        sensor_h = int(self.max_sensor_resolution[0])
        sensor_w = int(self.max_sensor_resolution[1])

        # Clamp start coordinates
        top_px = max(0, min(top_px, sensor_h - 1))
        left_px = max(0, min(left_px, sensor_w - 1))

        # Clamp sizes
        height_px = max(self.binning, min(height_px, sensor_h - top_px))
        width_px = max(self.binning, min(width_px, sensor_w - left_px))

        # Candidate 1: lower-right inclusive
        inclusive_candidate = ROI(
            left_px,
            top_px,
            left_px + width_px - 1,
            top_px + height_px - 1,
        )

        # Candidate 2: lower-right exclusive
        exclusive_candidate = ROI(
            left_px,
            top_px,
            left_px + width_px,
            top_px + height_px,
        )

        last_error = None
        for candidate in (inclusive_candidate, exclusive_candidate):
            try:
                self.camera.roi = candidate

                actual_w = int(self.camera.image_width_pixels)
                actual_h = int(self.camera.image_height_pixels)

                if actual_w == width_px and actual_h == height_px:
                    return top_px, left_px, height_px, width_px

            except TLCameraError as exc:
                last_error = exc

        if last_error is not None:
            raise CameraException(
                f"Could not set ROI in sensor units: "
                f"top={top_px}, left={left_px}, "
                f"height={height_px}, width={width_px}"
            ) from last_error

        raise CameraException(
            "Thorlabs SDK accepted an ROI candidate, but the resulting image shape "
            "did not match the requested ROI."
        )

    # ------------------------------------------------------------------
    # Acquisition control
    # ------------------------------------------------------------------
    def start_acquisition(self):
        """
        Prepare camera acquisition.

        sashimi only needs two modes:
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