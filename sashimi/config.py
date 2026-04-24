from pathlib import Path
import click
import toml
from lightparam import set_nested, get_nested

CONFIG_FILENAME = "hardware_config.toml"
CONFIG_DIR_PATH = Path.home() / ".cshascope"
CONFIG_DIR_PATH.mkdir(exist_ok=True)
LOGS_DIR_PATH = CONFIG_DIR_PATH / "logs"
LOGS_DIR_PATH.mkdir(exist_ok=True)

CONFIG_PATH = CONFIG_DIR_PATH / CONFIG_FILENAME

# 2 level dictionary for sections and values:
TEMPLATE_CONF_DICT = {
    "scanning": "ni",
    "scopeless": False,
    "sample_rate": 40000,
    "voxel_size": {
        "x": 0.252,
        "y": 0.252,
    },
    "default_paths": {
        "data": str(Path.home() / "Desktop"),
        "log": str(LOGS_DIR_PATH),
    },
    "scan_board": {
        "read": {
            "channel": "Dev1/ai0:0",
            "min_val": 0,
            "max_val": 10,
        },
        "write": {
            "names": ["xy_galvo", "z_galvo", "piezo", "camera_trigger"],
            "channels": ["Dev1/ao0", "Dev1/ao1", "Dev1/ao2", "Dev1/ao3"],
            "min_vals": [-5, -5, 0, 0],
            "max_vals": [5, 5, 10, 5],
        },
        "sync": {
        "start_trigger": "/Dev1/ao/StartTrigger",
        "sample_clock": "/Dev1/ao/SampleClock",
        }
    },
    "piezo": {
        "scale": 1 / 45,
    },
    "camera": {
        "id": 0,
        "name": "thorlabs",
        "max_sensor_resolution": [1080, 1920],
        "default_exposure": 60,
        "default_binning": 2,
    },
    "thorlabs_sdk": {
        "dll_dir": r"C:\Users\huang\Downloads\scientific_camera_interfaces_windows-2.1\Scientific Camera Interfaces\SDK\Python Toolkit\dlls\32_lib",
    },
    "array_ram_MB": 450,
}


def write_default_config(file_path=CONFIG_PATH, template=TEMPLATE_CONF_DICT):
    with open(file_path, "w") as f:
        toml.dump(template, f)


def read_config(file_path=CONFIG_PATH):
    if not file_path.exists():
        write_default_config()

    return toml.load(file_path)


def write_config_value(dict_path, val, file_path=CONFIG_PATH):
    """Write a new value in the config file. To make things simple, ignore
    sections and look directly for matching parameters names.

    Parameters
    ----------
    dict_path : str or list of strings
        Full path of the section to configure
        (e.g., ["piezo", "position_read", "min_val"])
    val :
        New value.
    file_path : Path object
        Path of the config file (optional).

    """
    # Ensure path to entry is always a string:
    if type(dict_path) is str:
        dict_path = [dict_path]

    # Read and set:
    conf = read_config(file_path=file_path)
    set_nested(conf, dict_path, val)

    # Write:
    with open(file_path, "w") as f:
        toml.dump(conf, f)


@click.command()
@click.argument("command")
@click.option("-n", "--name", help="Path (section/name) of parameter to be changed")
@click.option("-v", "--val", help="Value of parameter to be changed")
@click.option(
    "-p",
    "--file_path",
    default=CONFIG_PATH,
    help="Path to the config file (optional)",
)
def cli_modify_config(command, name=None, val=None, file_path=CONFIG_PATH):
    file_path = Path(file_path)
    if command == "edit":
        cli_edit_config(name, val, file_path)

    elif command == "show":
        click.echo(_print_config(file_path=file_path))


def cli_edit_config(name=None, val=None, file_path=CONFIG_PATH):
    conf = read_config(file_path=file_path)

    # Cast the type of the previous variable
    # (to avoid overwriting values with strings)
    dict_path = name.split(".")
    old_val = get_nested(conf, dict_path)
    val = type(old_val)(val)  # Convert to keep the same type

    write_config_value(dict_path, val, file_path)


def _print_config(file_path=CONFIG_PATH):
    """Return configuration string for printing."""
    config = read_config(file_path=file_path)
    return toml.dumps(config)
