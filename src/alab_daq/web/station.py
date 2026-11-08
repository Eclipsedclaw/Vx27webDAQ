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
import time
from collections import deque
from dataclasses import asdict

from alab_daq import __version__
from alab_daq.core import config as C
from alab_daq.core import models
from alab_daq.core.system import memory
from alab_daq.web.accounts import Accounts, Sessions
from alab_daq.web.boardcheck import BoardCheck
from alab_daq.web.converter import RootConverter
from alab_daq.web.files import DataFiles
from alab_daq.web.runs import RunManager
from alab_daq.web.sequence import SequenceManager
from alab_daq.web.settings import SETTING_CHOICES, SETTING_KINDS, ServerSettings, coerce, load_settings, save_settings, state_dir, write_atomic

CLIENT_QUEUE = 400          # messages waiting for one page; a page that falls this far behind gets the state anew


class Client:
    def __init__(self, user: str | None, role: str | None):
        self.user, self.role = user, role
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=CLIENT_QUEUE)
        self.overflow = False


class Station:
    def __init__(self):
        self.settings: ServerSettings = load_settings()
        self.state_dir = state_dir()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.state_dir, 0o700)
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
        self.runs.load_last()
        self.runs.reattach()
        self.converter.restore()
        self._tasks = [asyncio.create_task(self._run_loop()), asyncio.create_task(self.converter.run()),
                       asyncio.create_task(self._memory_loop())]

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

    # ---- pages ----
    def add_client(self, user: str | None, role: str | None) -> Client:
        c = Client(user, role)
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
        self.cfg, self.cfg_path, self.run_note, self.cfg_dirty = cfg, path, note, False
        self.cfg_version += 1
        self._save_draft()

    def config_payload(self) -> dict:
        cfg = self.cfg
        facts = self.boards.facts() if hasattr(self, "boards") else {}
        problems = C.check_config(cfg, check_paths=not self.settings.use_simulator, facts=facts)
        active = cfg.active()
        n = len(active)
        chained = cfg.run.sync == "chain" and n > 1
        plans, order = [], {}
        for k, (i, b) in enumerate(active):
            order[b.name] = k
            try:
                rows = [[s.path, s.value, s.why] for s in C.plan(cfg, k)] if models.model(b.model) else []
            except (IndexError, KeyError, ValueError):
                rows = []
            plans.append({"name": b.name, "role": C.role(k, n) if chained else "single", "settings": rows,
                          "run_delay_ns": C.caen_run_delay_ns(k, n) if chained else 0,
                          "clock_out_delay_ps": C.caen_clock_out_delay_ps(k, n) if chained else 0})
        info = []
        for b in cfg.boards:
            m = models.model(b.model)
            ns = C.sample_ns(b, m) if m else None
            info.append({"name": b.name, "sample_ns": ns, "window_ns": ns * b.record_length if ns else None,
                         "pre_ns": ns * b.pre_trigger if ns else None, "event_bytes": C.event_bytes(b),
                         "channels": len(b.channels), "adc_max": m.adc_max if m else None,
                         "decimation": bool(m and m.decimation), "gain": bool(m and m.gain),
                         "place": order.get(b.name), "usual_channels": m.channels if m else None,
                         "self_trigger": C.triggers_on_channels(cfg, order.get(b.name) if b.enabled else None)})
        return {"version": self.cfg_version, "path": self.cfg_path, "dirty": self.cfg_dirty, "note": self.run_note,
                "config": C.to_dict(cfg), "problems": [asdict(p) for p in problems], "plans": plans,
                "boards": info, "models": models.table(), "model_choices": models.CHOICES,
                "decimations": list(models.DECIMATIONS), "setups": list(C.SETUPS), "sync_modes": list(C.SYNC_MODES),
                "logics": list(C.LOGICS), "io_levels": list(C.IO_LEVELS), "max_boards": C.MAX_BOARDS,
                "master_outs": list(C.MASTER_OUTS), "combines": list(C.COMBINES),
                "io_choices": {"trg_out": list(C.TRGOUT_CHOICES), "gpio": list(C.GPIO_CHOICES), "veto": list(C.VETO_CHOICES),
                               "polarity": list(C.VETO_POLARITIES), "start": list(C.START_CHOICES),
                               "tsreset": list(C.TSRESET_CHOICES), "dac": list(C.DAC_CHOICES)},
                "uses_self_trigger": C.uses_self_trigger(cfg), "start_order": C.start_order(cfg) if active else [],
                "software_targets": C.software_targets(cfg) if active else []}

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

    def save_config(self, path: str | None, user: str) -> str:
        path = os.path.abspath(os.path.expanduser(path or self.cfg_path)) if (path or self.cfg_path) else ""
        if not path:
            raise C.ConfigError("give a file name to save the configuration in")
        if not path.lower().endswith(".json"):
            raise C.ConfigError("a configuration file ends in .json")
        C.save_config(self.cfg, path, note=self.run_note, saved_by=user, saved=time.strftime("%Y-%m-%d %H:%M:%S"),
                      origin=f"ALAB DAQ v{__version__}")
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
                "default_folder": self.default_folder(),
                "runs_folder": self.settings.runs_folder(),
                "caen_libs": libs}

    def update_settings(self, changes: dict, user: str, is_admin: bool) -> None:
        new = ServerSettings(**asdict(self.settings))
        for key, value in changes.items():
            if key not in SETTING_KINDS:
                raise ValueError(f"unknown setting {key}")
            if SETTING_KINDS[key] == "admin" and not is_admin:
                raise PermissionError(f"only admins may change {key}")
            setattr(new, key, coerce(key, value, getattr(new, key)))
        old = self.settings
        self.settings = new
        save_settings(new)
        changed = [k for k in changes if getattr(old, k) != getattr(new, k)]
        if changed:
            self.activity(user, "changed settings", ", ".join(changed))
        self.publish({"type": "settings", "settings": self.settings_payload()})
        self.config_changed()               # the check depends on the simulator setting
        self.publish({"type": "roots", "roots": [{"label": a, "path": b} for a, b in self.data_roots()]})

    def default_folder(self) -> str:
        """Where every file chooser opens: the lab's /NAS/ALAB_DAQ_DATA, when it is there and a data folder; else \"\"."""
        d = os.path.realpath(C.DEFAULT_DATA_DIR)
        if not os.path.isdir(d):
            return ""
        return d if any(os.path.realpath(p) == d for _, p in self.data_roots()) else ""

    def data_roots(self) -> list[tuple[str, str]]:
        """The folders whose data files users may open."""
        out = [("data", os.path.abspath(os.path.expanduser(self.cfg.run.data_dir))),
               ("runs", self.settings.runs_folder())]
        out += [("data", os.path.abspath(os.path.expanduser(d))) for d in self.settings.data_dirs]
        seen, roots = set(), []
        for label, path in out:
            if path not in seen and os.path.isdir(path):
                seen.add(path)
                roots.append((label, path))
        return roots

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
            if path not in seen and os.path.isdir(path):
                seen.add(path)
                places.append((label, path))
        return places

    # ---- who did what ----
    def activity(self, user: str | None, action: str, detail: str = "") -> None:
        entry = {"time": time.time(), "user": user or "guest", "action": action, "detail": detail}
        self.activity_log.append(entry)
        try:
            with open(self.state_dir / "activity.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
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
