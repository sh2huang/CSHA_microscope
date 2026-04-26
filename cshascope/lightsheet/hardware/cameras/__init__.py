from cshascope.lightsheet.hardware.cameras.mock import MockCamera
from cshascope.lightsheet.hardware.cameras.thorlabs.interface import ThorlabsCamera

# Update this dictionary and add the import above when adding a new camera
camera_class_dict = dict(
    thorlabs=ThorlabsCamera,
    mock=MockCamera,
)
