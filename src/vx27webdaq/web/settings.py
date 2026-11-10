"""Settings of the web server and where it keeps its files, all in the account that runs it (the service
account):

    ~/.config/vx27webdaq/web.json      the program settings (operators change them on the Settings page)
    ~/.local/state/vx27webdaq/          accounts, sign-ins, the configuration being edited, the current run, the
                                      ROOT queue, the board check, the activity log

XDG_CONFIG_HOME and XDG_STATE_HOME move them (the tests do).
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from vx27webdaq.core.config import DEFAULT_CONFIG_FILE, DEFAULT_DATA_DIR

PROJECT = Path(__file__).resolve().parents[3]      # the source folder, or the build
DEFAULT_PORT = 8770
DEFAULT_RUNS = DEFAULT_DATA_DIR                     # the save folder when none is given: the lab's NAS


NAME = "vx27webdaq"
OLD_NAME = "alab_daq"                              # the name of these folders until v0.0.18 (ALAB DAQ)
OLD_BUILDS = ("ALAB_DAQ_WEB", "ALAB_DAQ")           # the old build and source folders


def _folder(base: str) -> Path:
    """base/vx27webdaq; the first time, the folder of the old name moves there (settings, accounts, state)."""
    new, old = Path(base) / NAME, Path(base) / OLD_NAME
    if not new.exists() and old.is_dir():
        try:
            os.rename(old, new)
        except OSError:
            pass                        # e.g. not allowed: the new folder starts empty, the old one stays
    return new


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return _folder(base)


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return _folder(base)


@dataclass
class ServerSettings:
    config_file: str = DEFAULT_CONFIG_FILE     # opened when the server starts with nothing being edited
    caen_lib_dir: str = str(PROJECT / "caen" / "lib")      # libCAEN_FELib.so and libCAEN_Dig2.so (caen_libs.sh)
    use_simulator: bool = False
    simulator_options: str = "--rate 20"
    simulator_pulses: str = ""      # "": random hits; staircase: the simulator's pulse generator (one pulse per record)
    runs_dir: str = DEFAULT_RUNS    # run records and users' outputs; empty: /NAS/ALAB_DAQ_DATA too
    data_dirs: list[str] = field(default_factory=lambda: [DEFAULT_DATA_DIR])  # more folders of data files to open
    convert_to_root: bool = True    # after every run, its .raw files are converted to .root
    build_events: bool = True       # after a run of several boards, also RUN_built.root: every board per trigger
    refresh_ms: int = 500           # how often the live view asks for new waveforms
    guests: bool = True             # people who are not signed in may view and use the viewer tools

    def runs_folder(self) -> str:
        """The save folder (empty: /NAS/ALAB_DAQ_DATA): a relative path counts from the home folder of the service
        account."""
        return os.path.join(os.path.expanduser("~"), os.path.expanduser(self.runs_dir.strip()) or DEFAULT_RUNS)

    def sim_options(self) -> str:
        """The simulator's options for a run: the ones given, and the pulse generator chosen."""
        extra = f" --generator {self.simulator_pulses}" if self.simulator_pulses else ""
        return (self.simulator_options or "") + extra

    def lib_env(self) -> dict:
        """The environment for programs that use CAEN FELib: its folder first on LD_LIBRARY_PATH, but only a folder
        no other account can change (unsafe_lib_dir): else it is left out and the boards say that CAEN FELib
        cannot be loaded, and the Settings page says why."""
        env = {}
        d = os.path.expanduser(self.caen_lib_dir.strip())
        if d and unsafe_lib_dir(d) is None:
            old = os.environ.get("LD_LIBRARY_PATH", "")
            env["LD_LIBRARY_PATH"] = d + (":" + old if old else "")
        return env


def inside(path: str, folder: str) -> bool:
    """path is folder or lies inside it (both real paths)."""
    folder = folder.rstrip("/")
    return not folder or path == folder or path.startswith(folder + "/")


def _changeable_by_others(path: str, st: os.stat_result, sticky_ok: bool) -> str | None:
    owners = (0, os.geteuid())
    if st.st_uid not in owners:
        return f"{path} belongs to another account (uid {st.st_uid})"
    if st.st_mode & 0o022 and not (sticky_ok and st.st_mode & stat.S_ISVTX):
        return f"{path} may be changed by other accounts (its group or everyone may write in it)"
    return None


def _folder_chain(path: str) -> str | None:
    """Why someone else could change this folder (or, when it is not there yet, make it), or a folder above it."""
    p = path
    while not os.path.exists(p) and p != os.path.dirname(p):
        p = os.path.dirname(p)
    first = p
    while True:
        try:
            st = os.stat(p)
        except OSError as err:
            return f"{p} cannot be looked at ({err.strerror})"
        why = _changeable_by_others(p, st, sticky_ok=p != first)     # e.g. /tmp above it: others cannot rename in it
        if why:
            return why
        if p == os.path.dirname(p):
            return None
        p = os.path.dirname(p)


def unsafe_lib_dir(path: str) -> str | None:
    """Why the CAEN library folder must not go into LD_LIBRARY_PATH, or None. Every library in that folder is loaded
    before the system's by the DAQ program and the board check, so whoever may change the folder, a file in it or a
    folder above it could run their own code as the service account: each must belong to root or to the service
    account, and must not be writable by its group or by everyone (a folder above it with the sticky bit is fine)."""
    if not path.strip():
        return None
    real = os.path.realpath(os.path.abspath(os.path.expanduser(path.strip())))
    why = _folder_chain(real)
    if why or not os.path.isdir(real):
        return why
    try:
        entries = list(os.scandir(real))
    except OSError as err:
        return f"{real} cannot be read ({err.strerror})"
    for e in entries:
        try:
            st = os.stat(e.path)                # through a link: the file it points to
        except OSError:
            continue
        why = _changeable_by_others(e.path, st, sticky_ok=False)
        if not why and e.is_symlink():
            why = _folder_chain(os.path.dirname(os.path.realpath(e.path)))
        if why:
            return why
    return None


# what each setting is, for the Settings page; "admin": only admins may change it
SETTING_KINDS = {"config_file": "file", "caen_lib_dir": "folder", "runs_dir": "folder", "data_dirs": "folders",
                 "use_simulator": "bool", "simulator_options": "text", "simulator_pulses": "choice", "convert_to_root": "bool", "build_events": "bool", "refresh_ms": "int",
                 "guests": "admin"}
# only admins change these: whether guests may look in, and the CAEN library folder (it goes into LD_LIBRARY_PATH
# of every DAQ program and board check, so it decides what code runs as the service account)
ADMIN_SETTINGS = ("guests", "caen_lib_dir")


SETTING_CHOICES = {"simulator_pulses": ["", "staircase"]}


def settings_path() -> Path:
    return config_dir() / "web.json"


def coerce(name: str, value, default):
    """value as the type of the setting's default; ValueError (with a message to show) if it cannot be."""
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        if isinstance(value, (str, int)) and str(value).lower() in ("1", "true", "yes", "on"):
            return True
        if isinstance(value, (str, int)) and str(value).lower() in ("0", "false", "no", "off"):
            return False
        raise ValueError(f"{name} must be true or false")
    if isinstance(default, int):
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ValueError(f"{name} must be a whole number")
        try:
            number = int(value)
        except (ValueError, OverflowError):
            raise ValueError(f"{name} must be a whole number") from None
        if name == "refresh_ms" and not 100 <= number <= 5000:
            raise ValueError("refresh_ms must be from 100 to 5000")
        return number
    if isinstance(default, list):
        if isinstance(value, str):
            value = [line for line in value.splitlines()]
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ValueError(f"{name}: one folder per line (text), or a list of folders")
        return [v.strip() for v in value if v.strip()]
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text")
    if name in SETTING_CHOICES:
        if value not in SETTING_CHOICES[name]:
            raise ValueError(f"{name} is one of {', '.join(repr(c) for c in SETTING_CHOICES[name])}")
        return value
    if name == "simulator_options":
        from vx27webdaq.daq.sim import parse_options
        parse_options(value)                # ValueError for options the simulator does not take
    return value.strip()


def load_settings(path: Path | None = None) -> ServerSettings:
    path = path or settings_path()
    out = ServerSettings()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return out
    lib = str(data.get("caen_lib_dir") or "").rstrip("/")
    if lib.endswith("/caen/lib") and Path(lib).parent.parent.name in OLD_BUILDS:
        data.pop("caen_lib_dir")            # the old build's libraries: this build's own folder instead
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
