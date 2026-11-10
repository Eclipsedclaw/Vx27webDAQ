"""The DAQ station behind the web page: one for the whole server, shared by every page that is open.

It holds the configuration of the next run (operators edit it, everyone sees it; the edits are kept in the
state folder, so a server restart loses none), the run, the ROOT conversion queue, the board check, the data
files that are open, the settings, the accounts, and the activity log (who did what). Everything that changes
is pushed to every open page as a small JSON message (publish); a page that opens gets the whole state first.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict

from vx27webdaq import __version__
from vx27webdaq.core import config as C
from vx27webdaq.core import models
from vx27webdaq.core.system import memory
from vx27webdaq.web.accounts import Accounts, Sessions
from vx27webdaq.web.boardcheck import BoardCheck
from vx27webdaq.web.converter import RootConverter
from vx27webdaq.web.files import DATA_SUFFIXES, DataFiles
from vx27webdaq.web.runs import RunManager
from vx27webdaq.web.sequence import SequenceManager
from vx27webdaq.web.settings import (ADMIN_SETTINGS, SETTING_CHOICES, SETTING_KINDS, ServerSettings, coerce, config_dir, inside,
                                     load_settings, save_settings, state_dir, unsafe_lib_dir, write_atomic)

CLIENT_QUEUE = 400          # messages waiting for one page; a page that falls this far behind gets the state anew
ACCOUNTS_CHECK_S = 2.0      # how often the server looks whether users.json changed (the command line)
ACTIVITY_MAX_BYTES = 5_000_000      # activity.jsonl is cut to its newest lines when it grows past this
RUN_RECORDS = ("config.json", "run.json", "status.json")           # a run folder's own files
RUN_MARKERS = ("daq.log", "daq.pid", "daq.exit", "status.json", "run.json", "config.json")
RUN_FACTS = ("run_name", "run_folder", "data_file")                  # a configuration document that records a run
MAX_DOCUMENT = 50 << 20     # a file larger than this is not read to see whether it is a configuration


class Folders:
    """What the server knows of the folders it shows and checks (the data folders, the save folder, the lab's
    /NAS/ALAB_DAQ_DATA): whether each is there, its real path, and what the configuration check says of it as the
    data folder. They may be on the NAS, and a hung network mount must never stop the event loop: each folder is
    looked at in a worker thread and the answer kept; a folder asked for the first time is waited for at most
    WAIT_S, and each one asked for is looked at again every REFRESH_S."""

    WAIT_S = 0.5
    REFRESH_S = 3.0
    FORGET_S = 600.0

    def __init__(self):
        self._facts: dict[str, dict] = {}
        self._pending: dict[str, object] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="vx27-folders")
        self.on_change = None           # on_change(path), from a worker thread, when what is known of it changed

    @staticmethod
    def look(path: str) -> dict:
        """(in a worker thread) The facts of one folder, as the configuration check finds them."""
        full = os.path.abspath(os.path.expanduser(path))
        probe = C.DaqConfig()
        probe.run.data_dir, probe.run.write_raw = path, True
        plain = {(p.level, p.where, p.message) for p in C.check_config(probe, check_paths=False)}
        problems = [p for p in C.check_config(probe, check_paths=True) if (p.level, p.where, p.message) not in plain]
        isdir = os.path.isdir(full)
        return {"full": full, "isdir": isdir, "real": os.path.realpath(full) if isdir else full, "problems": problems}

    def _look(self, path: str) -> dict:
        try:
            fact = self.look(path)
        except Exception as err:            # noqa: BLE001 - e.g. a folder that cannot be read: as if not there
            full = os.path.abspath(os.path.expanduser(path))
            fact = {"full": full, "isdir": False, "real": full,
                    "problems": [C.Problem("error", "run.data_dir", f"{full} cannot be looked at: {err}")]}
        fact["seen"] = time.monotonic()
        with self._lock:
            old = self._facts.get(path)
            fact["asked"] = old["asked"] if old else fact["seen"]
            self._facts[path] = fact
            self._pending.pop(path, None)
        if self.on_change is not None and (old is None or any(old.get(k) != fact[k] for k in ("isdir", "real", "problems"))):
            try:
                self.on_change(path)
            except Exception:                # noqa: BLE001
                pass
        return fact

    def _submit(self, path: str):
        fut = self._pending.get(path)
        if fut is None:
            try:
                fut = self._pool.submit(self._look, path)
            except RuntimeError:             # the server is stopping
                return None
            self._pending[path] = fut
        return fut

    def get(self, path: str) -> dict:
        """What is known of this folder: {full, isdir, real, problems}."""
        now = time.monotonic()
        with self._lock:
            fact = self._facts.get(path)
            if fact is not None:
                fact["asked"] = now
                if now - fact["seen"] > self.REFRESH_S:
                    self._submit(path)
                return fact
            fut = self._submit(path)
        try:
            if fut is not None:
                return fut.result(timeout=self.WAIT_S)
        except Exception:                    # noqa: BLE001 - it does not answer in time: it goes on in its thread
            pass
        full = os.path.abspath(os.path.expanduser(path))
        slow = {"full": full, "isdir": False, "real": full, "seen": now, "asked": now, "slow": True,
                "problems": [C.Problem("warning", "run.data_dir", f"{full} does not answer yet (a network folder that "
                                                                  "hangs?): it is looked at again in a few seconds")]}
        with self._lock:                     # asked again meanwhile: this answer at once, until the look ends
            return self._facts.setdefault(path, slow)

    def forget(self, *paths: str) -> None:
        """Look at these folders anew when they are next asked for (the server just made or changed them)."""
        with self._lock:
            for p in paths:
                self._facts.pop(p, None)

    def refresh(self) -> None:
        """Look again at every folder asked for lately; forget the others."""
        now = time.monotonic()
        with self._lock:
            for path in [p for p, f in self._facts.items() if now - f["asked"] > self.FORGET_S]:
                del self._facts[path]
            for path, f in list(self._facts.items()):
                if now - f["seen"] > self.REFRESH_S:
                    self._submit(path)

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


class Client:
    """An open page (its live connection): who it is, with the sign-in token it came with, and its messages."""

    def __init__(self, user: str | None, role: str | None, token: str | None = None):
        self.user, self.role, self.token = user, role, token
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=CLIENT_QUEUE)
        self.overflow = False


class Station:
    RECHECK = "recheck"         # a message to every open page's connection: find out again who it is (never sent on)

    def __init__(self):
        self.settings: ServerSettings = load_settings()
        self.state_dir = state_dir()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.state_dir, 0o700)
        self._own = [os.path.realpath(self.state_dir), os.path.realpath(config_dir())]    # the server's own files
        self._home = os.path.realpath(os.path.expanduser("~"))
        self._loop: asyncio.AbstractEventLoop | None = None
        self.folders = Folders()
        self.folders.on_change = self._folder_changed
        self._folders_publish = False
        self._activity_writes = 0
        self.accounts = Accounts(self.state_dir / "users.json")
        self.sessions = Sessions(self.state_dir / "sessions.json")
        self.clients: set[Client] = set()
        self.activity_log: deque = deque(maxlen=500)
        self._load_activity()
        self.cfg, self.cfg_path, self.run_note = C.lab_setup(), "", ""
        self.cfg_dirty, self.cfg_version = False, 0
        if not self._load_draft() and self.settings.config_file:
            path = os.path.expanduser(self.settings.config_file)
            if os.path.exists(path):
                self._open_config(path, quiet=True)
            else:                   # not made yet (a new computer): the lab's setup, which Save writes there
                self.cfg_path, self.cfg_dirty = path, True
        self.runs = RunManager(self)
        self.converter = RootConverter(self)
        self.boards = BoardCheck(self)
        self.sequence = SequenceManager(self)
        self.files = DataFiles(self.data_roots)
        self._tasks: list[asyncio.Task] = []
        self._edit_logged: dict[str, float] = {}

    # ---- life ----
    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        try:                                # the save folder may be on the NAS: in a worker thread
            await asyncio.to_thread(self.runs.load_last)
        except Exception as err:            # noqa: BLE001 - the server starts in any case
            self.activity("server", "could not read the last run", repr(err))
        await self.runs.reattach()
        self.converter.restore()
        self._tasks = [asyncio.create_task(self._run_loop()), asyncio.create_task(self.converter.run()),
                       asyncio.create_task(self._memory_loop()), asyncio.create_task(self._accounts_loop()),
                       asyncio.create_task(self._folders_loop())]

    async def stop(self) -> None:
        """The server stops: the DAQ program goes on (a new server follows it), the rest is stopped."""
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self.files.close_all()
        self.folders.close()
        self.converter.close()

    async def _folders_loop(self) -> None:
        while True:
            await asyncio.sleep(Folders.REFRESH_S)
            self.folders.refresh()

    def _folder_changed(self, path: str) -> None:
        """(from a worker thread) A folder appeared, went away or its check changed: the pages get the
        configuration's problems and the data folders anew (once, however many folders changed at once)."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._publish_folders)
        except RuntimeError:                # the loop is closing
            pass

    def _publish_folders(self) -> None:
        if self._folders_publish:
            return
        self._folders_publish = True

        def send():
            self._folders_publish = False
            self.publish({"type": "config", "config": self.config_payload()})
            self.publish({"type": "roots", "roots": [{"label": a, "path": b} for a, b in self.data_roots()]})
        self._loop.call_later(0.2, send)

    async def _run_loop(self) -> None:
        while True:
            await asyncio.sleep(0.2)
            try:
                await self.runs.tick()
                await self.sequence.tick()
            except Exception as err:       # following the run must never stop
                self.activity("server", "error while following the run", repr(err))
                await asyncio.sleep(2)

    async def _memory_loop(self) -> None:
        while True:
            try:
                self.publish({"type": "memory", "memory": memory()})
            except OSError:
                pass
            await asyncio.sleep(5)

    async def _accounts_loop(self) -> None:
        """The command line changes users.json too (vx27webdaq user role / password / remove): then every open
        page finds out again who it is."""
        seen = self.accounts.mtime()
        while True:
            await asyncio.sleep(ACCOUNTS_CHECK_S)
            now = self.accounts.mtime()
            if now != seen:
                seen = now
                self.recheck_clients()

    # ---- pages ----
    def add_client(self, user: str | None, role: str | None, token: str | None = None) -> Client:
        c = Client(user, role, token)
        self.clients.add(c)
        return c

    def remove_client(self, c: Client) -> None:
        self.clients.discard(c)

    def publish(self, message: dict) -> None:
        for c in list(self.clients):
            try:
                c.queue.put_nowait(message)
            except asyncio.QueueFull:
                c.overflow = True

    def move_clients(self, old_token: str | None, new_token: str) -> None:
        """This browser got a new sign-in (its own new password): its open pages go on with it."""
        for c in self.clients:
            if old_token and c.token == old_token:
                c.token = new_token

    def recheck_clients(self) -> None:
        """An account's role changed, it was removed, its password changed, a sign-in ended, or guests were turned
        off: every open page finds out again who it is; one that is nobody now is closed (code 4401), one whose
        role changed gets the whole state anew with its new role."""
        self.publish({"type": self.RECHECK})

    def state(self) -> dict:
        try:
            mem = memory()
        except OSError:
            mem = None
        return {"version": __version__, "run": self.runs.snapshot(full=True), "log": self.runs.lines[-3000:],
                "sequence": self.sequence.view(),
                "config": self.config_payload(), "settings": self.settings_payload(), "boards": self.boards.view(),
                "root": self.converter.status(), "memory": mem,
                "roots": [{"label": label, "path": path} for label, path in self.data_roots()]}

    # ---- the configuration of the next run ----
    def _draft_file(self):
        return self.state_dir / "draft.json"

    def _load_draft(self) -> bool:
        try:
            with open(self._draft_file(), encoding="utf-8") as f:
                doc = json.load(f)
            self.cfg = C.from_dict(doc["config"])
        except (OSError, ValueError, KeyError, TypeError):
            return False
        self.cfg_path = doc.get("path") or ""
        self.run_note = doc.get("note") or ""
        self.cfg_dirty = bool(doc.get("dirty"))
        return True

    def _save_draft(self) -> None:
        try:
            write_atomic(self._draft_file(), json.dumps({"config": C.to_dict(self.cfg), "path": self.cfg_path,
                                                         "note": self.run_note, "dirty": self.cfg_dirty}))
        except OSError:
            pass

    def _open_config(self, path: str, quiet: bool = False) -> None:
        try:
            cfg, note = C.load_config(path)
        except (OSError, C.ConfigError):
            if not quiet:
                raise
            return
        # a run's or a data file's record (its config.json, NAME.json): its configuration is taken, but Save asks for
        # a new name instead of writing over the record
        doc = self._document(path)
        record = bool(self.run_record(os.path.realpath(path))) or (isinstance(doc, dict) and any(k in doc for k in RUN_FACTS))
        self.cfg, self.cfg_path, self.run_note, self.cfg_dirty = cfg, "" if record else path, note, False
        self.cfg_version += 1
        self._save_draft()

    def config_payload(self) -> dict:
        """The configuration being edited and everything the page shows of it. A value that breaks one part (e.g.
        a number the check or the plan of a board cannot take) leaves that part empty and says so in the
        problems: the state of the server must always reach the page."""
        cfg = self.cfg
        facts = self.boards.facts() if hasattr(self, "boards") else {}
        try:
            problems = self.check(cfg, facts)
        except Exception as err:            # noqa: BLE001 - one bad value must not stop every page
            problems = [C.Problem("error", "configuration", f"the configuration cannot be checked: {err!r}")]
        active = cfg.active()
        n = len(active)
        chained = cfg.run.sync == "chain" and n > 1
        plans, order = [], {}
        for k, (i, b) in enumerate(active):
            order[b.name] = k
            try:
                rows = [[s.path, s.value, s.why] for s in C.plan(cfg, k)] if models.model(b.model) else []
            except Exception:               # noqa: BLE001 - e.g. a number out of range: no plan for this board
                rows = []
            try:
                delays = (C.caen_run_delay_ns(k, n), C.caen_clock_out_delay_ps(k, n)) if chained else (0, 0)
            except Exception:               # noqa: BLE001
                delays = (0, 0)
            plans.append({"name": b.name, "role": C.role(k, n) if chained else "single", "settings": rows,
                          "run_delay_ns": delays[0], "clock_out_delay_ps": delays[1]})
        info = []
        for b in cfg.boards:
            m = models.model(b.model)
            try:
                ns = C.sample_ns(b, m) if m else None
                info.append({"name": b.name, "sample_ns": ns, "window_ns": ns * b.record_length if ns else None,
                             "pre_ns": ns * b.pre_trigger if ns else None, "event_bytes": C.event_bytes(b),
                             "channels": len(b.channels), "adc_max": m.adc_max if m else None,
                             "decimation": bool(m and m.decimation), "gain": bool(m and m.gain),
                             "place": order.get(b.name), "usual_channels": m.channels if m else None,
                             "self_trigger": C.triggers_on_channels(cfg, order.get(b.name) if b.enabled else None)})
            except Exception:               # noqa: BLE001
                info.append({"name": b.name, "sample_ns": None, "window_ns": None, "pre_ns": None, "event_bytes": None,
                             "channels": len(b.channels), "adc_max": m.adc_max if m else None,
                             "decimation": bool(m and m.decimation), "gain": bool(m and m.gain),
                             "place": order.get(b.name), "usual_channels": m.channels if m else None,
                             "self_trigger": False})
        out = {"version": self.cfg_version, "path": self.cfg_path, "dirty": self.cfg_dirty, "note": self.run_note,
               "config": C.to_dict(cfg), "problems": [asdict(p) for p in problems], "plans": plans,
               "boards": info, "models": models.table(), "model_choices": models.CHOICES,
               "decimations": list(models.DECIMATIONS), "setups": list(C.SETUPS), "sync_modes": list(C.SYNC_MODES),
               "logics": list(C.LOGICS), "io_levels": list(C.IO_LEVELS), "max_boards": C.MAX_BOARDS,
               "master_outs": list(C.MASTER_OUTS), "combines": list(C.COMBINES),
               "io_choices": {"trg_out": list(C.TRGOUT_CHOICES), "gpio": list(C.GPIO_CHOICES), "veto": list(C.VETO_CHOICES),
                              "polarity": list(C.VETO_POLARITIES), "start": list(C.START_CHOICES),
                              "tsreset": list(C.TSRESET_CHOICES), "dac": list(C.DAC_CHOICES)}}
        for key, make in (("uses_self_trigger", lambda: C.uses_self_trigger(cfg)),
                          ("start_order", lambda: C.start_order(cfg) if active else []),
                          ("software_targets", lambda: C.software_targets(cfg) if active else [])):
            try:
                out[key] = make()
            except Exception:               # noqa: BLE001
                out[key] = [] if key != "uses_self_trigger" else False
        return out

    def check(self, cfg: C.DaqConfig, facts: dict | None = None) -> list:
        """The configuration check, with the data folder as the DAQ program will find it (simulated runs write into
        the data folder too): what the folder looks like comes from the folder cache, so a hung NAS never stops the
        page (the Start question checks the folder itself again)."""
        problems = C.check_config(cfg, check_paths=False, facts=facts)
        if cfg.run.write_raw:
            fact = self.folders.get(cfg.run.data_dir)
            problems += fact["problems"]
            why = self.forbidden_folder(fact["real"]) if fact["isdir"] else ""
            if why:
                problems.append(C.Problem("warning", "run.data_dir", f"{fact['full']} is {why}: the page does not "
                                          "open data files there; choose a folder for the data alone"))
        return problems

    def forbidden_folder(self, real: str) -> str:
        """Why this folder cannot be a data folder or the save folder (users could open too much of the computer
        through it), or ""."""
        if real == "/":
            return "the whole computer"
        if inside(self._home, real):
            return "the home folder of the server's account" if real == self._home else \
                "a folder that holds the home folder of the server's account"
        if any(inside(d, real) for d in self._own):
            return "a folder that holds the server's own files (accounts, sign-ins, settings)"
        return ""

    def config_changed(self) -> None:
        self.cfg_version += 1
        self._save_draft()
        self.publish({"type": "config", "config": self.config_payload()})

    def set_config(self, data: dict, user: str) -> None:
        self.cfg = C.from_dict(data)
        self.cfg_dirty = True
        now = time.time()
        if now - self._edit_logged.get(user, 0) > 300:      # one line per user and 5 minutes of editing
            self._edit_logged[user] = now
            self.activity(user, "edited the configuration")
        self.config_changed()

    def set_note(self, note: str, user: str) -> None:
        note = C.clean_note(note)
        if note == self.run_note:
            return
        self.run_note, self.cfg_dirty = note, True
        now = time.time()
        if now - self._edit_logged.get(f"note {user}", 0) > 300:
            self._edit_logged[f"note {user}"] = now
            self.activity(user, "edited the note")
        self.runs.note_changed()
        self.config_changed()

    # ---- where Save may write ----
    @staticmethod
    def run_record(path: str) -> str:
        """Why this .json file is a record of a run or of a data file (never written over by Save), or ""."""
        folder, name = os.path.split(path)
        stem = path[:-5]
        for suffix in DATA_SUFFIXES:
            if os.path.exists(stem + suffix):
                return f"the record kept next to the data file {os.path.basename(stem + suffix)}"
        for d in (folder, os.path.dirname(folder)):            # a run folder, or its control/, live/ or hk/ folder
            if os.path.exists(os.path.join(d, "daq.log")):
                return "in the folder of a run (its record)"
        if name in RUN_RECORDS and any(os.path.exists(os.path.join(folder, m)) for m in RUN_MARKERS if m != name):
            return "the record of the run in this folder"
        return ""

    @staticmethod
    def _document(path: str):
        """The JSON in a file, or None when it is not JSON (or too large to be a configuration)."""
        try:
            if os.path.getsize(path) > MAX_DOCUMENT:
                return None
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def _save_target(self, path: str) -> str:
        """The file Save may write the configuration into: a .json file in a folder that is there, not among the
        server's own files (accounts, sign-ins, settings, the current run), not a run's or a data file's record, and,
        when it is there already, a Vx27webDAQ configuration (anything else is never written over)."""
        full = os.path.abspath(os.path.expanduser(path))
        if not full.lower().endswith(".json"):
            raise C.ConfigError("a configuration file ends in .json")
        folder = os.path.dirname(full)
        if not os.path.isdir(folder):
            raise FileNotFoundError(f"the folder {folder} does not exist")
        real, real_folder = os.path.realpath(full), os.path.realpath(folder)
        if any(inside(real, d) or inside(real_folder, d) for d in self._own):
            raise PermissionError(f"{full} is among the server's own files (accounts, sign-ins, settings): save the "
                                  "configuration somewhere else")
        why = self.run_record(real)
        if why:
            raise PermissionError(f"{full} is {why}: save the configuration under another name")
        if os.path.lexists(full):
            if os.path.isdir(full):
                raise IsADirectoryError(f"{full} is a folder: give a file name")
            doc = self._document(real)
            if not (isinstance(doc, dict) and doc.get("format") == C.FORMAT):
                raise C.ConfigError(f"{full} is there already and is not a Vx27webDAQ configuration: it is never "
                                    "written over; choose another name")
            if any(k in doc for k in RUN_FACTS):
                raise PermissionError(f"{full} is the record of a run: save the configuration under another name")
        return full

    def save_config(self, path: str | None, user: str) -> str:
        if not (path or self.cfg_path):
            raise C.ConfigError("give a file name to save the configuration in")
        path = self._save_target(path or self.cfg_path)
        doc = C.config_document(self.cfg, self.run_note, saved_by=user, saved=time.strftime("%Y-%m-%d %H:%M:%S"),
                                origin=f"Vx27webDAQ v{__version__}")
        # a temporary file of a name nobody can guess, then renamed: no link planted at NAME.json.part is followed
        write_atomic(path, json.dumps(doc, indent=1, ensure_ascii=False) + "\n", private=False)
        self.cfg_path, self.cfg_dirty = path, False
        self.activity(user, "saved the configuration", path)
        self.config_changed()
        return path

    def open_config(self, path: str, user: str) -> None:
        self._open_config(os.path.abspath(os.path.expanduser(path)))
        self.activity(user, "opened a configuration", path)
        self.config_changed()

    def new_config(self, user: str) -> None:
        self.cfg, self.cfg_path, self.cfg_dirty = C.lab_setup(), "", True
        self.activity(user, "started a new configuration")
        self.config_changed()

    # ---- settings ----
    def settings_payload(self) -> dict:
        lib = os.path.expanduser(self.settings.caen_lib_dir)
        libs = {name: os.path.isfile(os.path.join(lib, name)) for name in ("libCAEN_FELib.so", "libCAEN_Dig2.so")}
        return {"values": asdict(self.settings), "kinds": SETTING_KINDS, "choices": SETTING_CHOICES,
                "admin_only": list(ADMIN_SETTINGS),
                "default_folder": self.default_folder(),
                "runs_folder": self.settings.runs_folder(),
                "caen_libs": libs, "caen_lib_problem": unsafe_lib_dir(self.settings.caen_lib_dir)}

    def update_settings(self, changes: dict, user: str, is_admin: bool) -> None:
        if not isinstance(changes, dict):
            raise ValueError("changes must be an object: {setting: new value}")
        new = ServerSettings(**asdict(self.settings))
        for key, value in changes.items():
            if key not in SETTING_KINDS:
                raise ValueError(f"unknown setting {key}")
            if (SETTING_KINDS[key] == "admin" or key in ADMIN_SETTINGS) and not is_admin:
                raise PermissionError(f"only admins may change {key}")
            setattr(new, key, coerce(key, value, getattr(new, key)))
        if "caen_lib_dir" in changes and new.caen_lib_dir.strip() != self.settings.caen_lib_dir.strip():
            why = unsafe_lib_dir(new.caen_lib_dir)
            if why:
                raise ValueError(f"the CAEN library folder {new.caen_lib_dir} is not safe: {why}. Its libraries would run "
                                 "as the service account: use a folder only root or the service account can change")
        folders = (new.data_dirs if "data_dirs" in changes else []) + ([new.runs_folder()] if "runs_dir" in changes else [])
        self.folders.forget(*folders)
        for d in folders:
            fact = self.folders.get(d)
            why = self.forbidden_folder(fact["real"] if fact["isdir"] else os.path.realpath(fact["full"]))
            if why:
                raise ValueError(f"{d} cannot be a data folder or the save folder: it is {why}; choose a folder for "
                                 "the data alone")
        old = self.settings
        self.settings = new
        save_settings(new)
        changed = [k for k in changes if getattr(old, k) != getattr(new, k)]
        if changed:
            self.activity(user, "changed settings", ", ".join(changed))
        if "guests" in changed:
            self.recheck_clients()          # guests off: the pages of people not signed in are closed
        self.publish({"type": "settings", "settings": self.settings_payload()})
        self.config_changed()               # the check depends on the simulator setting
        self.publish({"type": "roots", "roots": [{"label": a, "path": b} for a, b in self.data_roots()]})

    def default_folder(self) -> str:
        """Where every file chooser opens: the lab's /NAS/ALAB_DAQ_DATA, when it is there and a data folder; else \"\"."""
        fact = self.folders.get(C.DEFAULT_DATA_DIR)
        if not fact["isdir"]:
            return ""
        return fact["real"] if any(real == fact["real"] for _, _, real in self._roots()) else ""

    def _roots(self) -> list[tuple[str, str, str]]:
        """(label, path, real path) of the data folders: the configuration's data folder, the save folder, the folders
        of the settings, and the data folders of the run going and of the last run (so their files stay open to the
        page when the configuration's data folder changes). Never / , the service account's home or a folder that
        holds it or the server's own files. From the folder cache: no NAS is touched here."""
        out = [("data", self.cfg.run.data_dir, False), ("runs", self.settings.runs_folder(), False)]
        out += [("data", d, False) for d in self.settings.data_dirs]
        runs = getattr(self, "runs", None)
        if runs is not None:
            for cfg in (runs.active.cfg if runs.active is not None else None, runs.last_cfg):
                if cfg is not None:
                    out.append(("data", cfg.run.data_dir, True))
        roots: list[tuple[str, str, str]] = []
        for label, path, of_a_run in out:
            if not path:
                continue
            fact = self.folders.get(path)
            real = fact["real"]
            if not fact["isdir"] or self.forbidden_folder(real) or any(r == real for _, _, r in roots):
                continue
            if of_a_run and any(inside(real, r) for _, _, r in roots):      # e.g. DATA/simulated inside DATA
                continue
            roots.append((label, fact["full"], real))
        return roots

    def data_roots(self) -> list[tuple[str, str]]:
        """The folders whose data files users may open."""
        return [(label, path) for label, path, _ in self._roots()]

    def browse_places(self) -> list[tuple[str, str]]:
        """Where the operators' chooser starts: the service's home, the configuration's folder, the data folders,
        the CAEN library folder, and the whole computer."""
        out = [("home", os.path.expanduser("~"))]
        if self.cfg_path:
            out.append(("configuration", os.path.dirname(os.path.abspath(self.cfg_path))))
        out += self.data_roots()
        out.append(("CAEN libraries", os.path.expanduser(self.settings.caen_lib_dir)))
        out.append(("computer", "/"))
        seen, places = set(), []
        for label, path in out:
            if path not in seen and self.folders.get(path)["isdir"]:
                seen.add(path)
                places.append((label, path))
        return places

    # ---- who did what ----
    def activity(self, user: str | None, action: str, detail: str = "") -> None:
        entry = {"time": time.time(), "user": user or "guest", "action": action, "detail": detail}
        self.activity_log.append(entry)
        path = self.state_dir / "activity.jsonl"
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
            self._activity_writes += 1
            if self._activity_writes % 200 == 0 and path.stat().st_size > ACTIVITY_MAX_BYTES:
                write_atomic(path, "".join(json.dumps(e) + "\n" for e in self.activity_log))   # the newest 500
        except OSError:
            pass

    def _load_activity(self) -> None:
        path = self.state_dir / "activity.jsonl"
        try:
            with open(path, encoding="utf-8") as f:
                for line in deque(f, maxlen=500):
                    try:
                        self.activity_log.append(json.loads(line))
                    except ValueError:
                        pass
            if path.stat().st_size > 5_000_000:
                write_atomic(path, "".join(json.dumps(e) + "\n" for e in self.activity_log))
        except OSError:
            pass
