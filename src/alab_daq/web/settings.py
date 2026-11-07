"""Settings of the web server and where it keeps its files, all in the account that runs it (the service
account):

    ~/.config/alab_daq/web.json      the program settings (operators change them on the Settings page)
    ~/.local/state/alab_daq/          accounts, sign-ins, the configuration being edited, the current run, the
                                      ROOT queue, the board check, the activity log

XDG_CONFIG_HOME and XDG_STATE_HOME move them (the tests do).
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[3]      # the source folder, or the build
DEFAULT_PORT = 8770
DEFAULT_RUNS = "ALAB_DAQ_runs"                      # in the home folder, when no save folder is given


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "alab_daq"


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return Path(base) / "alab_daq"


@dataclass
class ServerSettings:
    config_file: str = ""           # a configuration (.json) opened when the server starts with nothing being edited
    caen_lib_dir: str = str(PROJECT / "caen" / "lib")      # libCAEN_FELib.so and libCAEN_Dig2.so (caen_libs.sh)
    use_simulator: bool = False
    simulator_options: str = "--rate 20"
    runs_dir: str = ""              # run records and users' outputs; empty: ~/ALAB_DAQ_runs
    data_dirs: list[str] = field(default_factory=list)     # more folders whose data files users may open
    convert_to_root: bool = True    # after every run, its .raw files are converted to .root
    refresh_ms: int = 500           # how often the live view asks for new waveforms
    guests: bool = True             # people who are not signed in may view and use the viewer tools

    def runs_folder(self) -> str:
        """The save folder: a relative path counts from the home folder of the service account."""
        return os.path.join(os.path.expanduser("~"), os.path.expanduser(self.runs_dir.strip()) or DEFAULT_RUNS)

    def lib_env(self) -> dict:
        """The environment for programs that use CAEN FELib: its folder first on LD_LIBRARY_PATH."""
        env = {}
        d = os.path.expanduser(self.caen_lib_dir.strip())
        if d:
            old = os.environ.get("LD_LIBRARY_PATH", "")
            env["LD_LIBRARY_PATH"] = d + (":" + old if old else "")
        return env


# what each setting is, for the Settings page; "admin": only admins may change it
SETTING_KINDS = {"config_file": "file", "caen_lib_dir": "folder", "runs_dir": "folder", "data_dirs": "folders",
                 "use_simulator": "bool", "simulator_options": "text", "convert_to_root": "bool", "refresh_ms": "int",
                 "guests": "admin"}


def settings_path() -> Path:
    return config_dir() / "web.json"


def coerce(name: str, value, default):
    """value as the type of the setting's default; ValueError if it cannot be."""
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        if str(value).lower() in ("1", "true", "yes", "on"):
            return True
        if str(value).lower() in ("0", "false", "no", "off"):
            return False
        raise ValueError(f"{name} must be true or false")
    if isinstance(default, int):
        number = int(value)
        if name == "refresh_ms" and not 100 <= number <= 5000:
            raise ValueError("refresh_ms must be from 100 to 5000")
        return number
    if isinstance(default, list):
        if isinstance(value, str):
            value = [line for line in value.splitlines()]
        return [str(v).strip() for v in value if str(v).strip()]
    if name == "simulator_options":
        from alab_daq.daq.sim import parse_options
        parse_options(str(value))           # ValueError for options the simulator does not take
    return str(value).strip()


def load_settings(path: Path | None = None) -> ServerSettings:
    path = path or settings_path()
    out = ServerSettings()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return out
    for f in fields(ServerSettings):
        if f.name in data:
            try:
                setattr(out, f.name, coerce(f.name, data[f.name], getattr(out, f.name)))
            except (TypeError, ValueError):
                pass                    # a broken value: the default stays
    return out


def save_settings(settings: ServerSettings, path: Path | None = None) -> None:
    write_atomic(path or settings_path(), json.dumps(asdict(settings), indent=2) + "\n", private=False)


def write_atomic(path: Path | str, text: str, private: bool = True) -> None:
    """Write a whole file at once (a reader sees the old or the new one); private: only its owner may read."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if private:
        os.chmod(path.parent, 0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".part")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, 0o600 if private else 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
