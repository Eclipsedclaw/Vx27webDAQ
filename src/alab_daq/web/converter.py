"""Converting .raw files to ROOT in the background, one after the other: after every run (all its files) and
when a user asks on the Tools page.

python -m alab_daq.core.rootconv writes NAME.root.part next to NAME.raw, at the lowest priority (nice 19) and
with one thread, reads it back and compares it with the raw file (the events, their trigger IDs, samples);
only then is it renamed NAME.root, so a NAME.root is always complete. The .raw file stays, and an existing
NAME.root is never written over. What the converter prints goes to rootconv.log in the work folder (the run
folder, or the user's folder), and an after-run result into the run's run.json. The queue is kept in
root_queue.json, so a server restart goes on with it.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
import sys
import time
from collections import deque

from alab_daq.web.settings import PROJECT, write_atomic

PROGRESS = re.compile(r"processed\s+([0-9.]+)\s*%")
CHECK = re.compile(r"check: (\d+) events in the raw file, (\d+) in the ROOT file")
BUILT = re.compile(r"check: (\d+) triggers of \S+, (\d+) entries")


def root_names(raw: str) -> tuple[str, str]:
    """(NAME.root, NAME.root.part) next to NAME.raw."""
    base = raw[:-4] if raw.lower().endswith(".raw") else raw
    return base + ".root", base + ".root.part"


class ConvertError(ValueError):
    """A file that cannot be added to the queue (the message says why)."""


class RootConverter:
    def __init__(self, station):
        self.station = station
        self.queue: list[dict] = []         # {raw, workdir, run_json, by}
        self.current: dict | None = None
        self.percent = 0.0
        self.number = 0
        self.total = 0
        self.history: deque = deque(maxlen=40)
        self._proc: asyncio.subprocess.Process | None = None
        self._cancelled = False
        self._wake = asyncio.Event()
        self._last_publish = 0.0

    # ---- the queue ----
    def busy(self) -> bool:
        return self.current is not None or bool(self.queue)

    def left(self) -> int:
        return len(self.queue) + (self.current is not None)

    def queued(self, raw: str) -> bool:
        conv = lambda j: j is not None and j.get("kind") != "build"          # noqa: E731
        return any(conv(j) and j["raw"] == raw for j in self.queue) or (conv(self.current) and self.current["raw"] == raw)

    def add(self, files, workdir: str, run_json: str | None = None, by: str = "") -> None:
        for raw in files:
            if not self.queued(raw):
                self.queue.append({"raw": raw, "workdir": workdir, "run_json": run_json, "by": by})
                self.total += 1
        self._save()
        self._wake.set()
        self.publish(force=True)

    def add_build(self, raw: str, workdir: str, run_json: str | None = None, by: str = "") -> None:
        """RUN_built.root of the run of this file (core/eventbuild.py), after the conversions before it."""
        if any(j.get("kind") == "build" and j["raw"] == raw for j in self.queue):
            return
        self.queue.append({"kind": "build", "raw": raw, "workdir": workdir, "run_json": run_json, "by": by})
        self.total += 1
        self._save()
        self._wake.set()
        self.publish(force=True)

    def request_build(self, raw: str, workdir: str, by: str) -> str:
        from alab_daq.core.eventbuild import built_name
        if not raw.lower().endswith(".raw") or not os.path.isfile(raw):
            raise ConvertError(f"{raw} is not a .raw file")
        out = built_name(raw)
        if os.path.exists(out):
            raise ConvertError(f"{os.path.basename(out)} exists already; it is never written over")
        active = self.station.runs.active
        if active is not None and os.path.realpath(raw) in {os.path.realpath(p) for p in active.data_files()}:
            raise ConvertError(f"{os.path.basename(raw)} is still being written by the run")
        os.makedirs(workdir, exist_ok=True)
        self.add_build(raw, workdir, by=by)
        return out

    def request(self, raw: str, workdir: str, by: str) -> None:
        """A conversion asked for on the Tools page: never over an existing ROOT file, never of a file still
        being written."""
        root, _ = root_names(raw)
        if not raw.lower().endswith(".raw") or not os.path.isfile(raw):
            raise ConvertError(f"{raw} is not a .raw file")
        if os.path.exists(root):
            raise ConvertError(f"{os.path.basename(root)} exists already; it is never written over")
        if self.queued(raw):
            raise ConvertError(f"{os.path.basename(raw)} is being converted already")
        active = self.station.runs.active
        if active is not None and os.path.realpath(raw) in {os.path.realpath(p) for p in active.data_files()}:
            raise ConvertError(f"{os.path.basename(raw)} is still being written by the run")
        os.makedirs(workdir, exist_ok=True)
        self.add([raw], workdir, by=by)

    def cancel(self) -> None:
        dropped = self.queue
        self.queue = []
        for job in dropped:
            self._record(job, "", "cancelled", -1, -1, "stopped before it began")
        if self._proc is not None and self._proc.returncode is None:
            self._cancelled = True
            self._proc.kill()
        self._save()
        self.publish(force=True)

    def _save(self) -> None:
        jobs = ([self.current] if self.current else []) + self.queue
        try:
            write_atomic(self.station.state_dir / "root_queue.json", json.dumps(jobs))
        except OSError:
            pass

    def restore(self) -> None:
        try:
            with open(self.station.state_dir / "root_queue.json", encoding="utf-8") as f:
                jobs = [j for j in json.load(f) if isinstance(j, dict) and "raw" in j]
        except (OSError, ValueError):
            return
        for j in jobs:
            self.add([j["raw"]], j.get("workdir") or os.path.dirname(j["raw"]), j.get("run_json"), j.get("by", ""))

    # ---- converting ----
    async def run(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            while self.queue:
                self.current = self.queue.pop(0)
                self.number += 1
                self.percent, self._cancelled = 0.0, False
                self._save()
                self.publish(force=True)
                try:
                    await self._convert(self.current)
                except asyncio.CancelledError:         # the server stops: this file again after the restart
                    await self._stop_process()
                    self.queue.insert(0, self.current)
                    raise
                except Exception as err:            # a converter that breaks must not stop the server
                    self._record(self.current, "", "failed", -1, -1, f"internal error: {err}")
                finally:
                    self.current = None
                    self._proc = None
                    self._save()
            self.number = self.total = 0
            self.publish(force=True)

    async def _stop_process(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            self._proc.kill()
            await self._proc.wait()
        if self.current is not None:
            self._remove(root_names(self.current["raw"])[1])

    async def _convert(self, job: dict) -> None:
        if job.get("kind") == "build":
            await self._build(job)
            return
        raw, workdir = job["raw"], job["workdir"]
        root, part = root_names(raw)
        if os.path.exists(root):
            self._record(job, root, "exists", -1, -1, "a ROOT file of this name exists already")
            return
        if not os.path.isfile(raw):
            self._record(job, "", "failed", -1, -1, "the .raw file is gone")
            return
        os.makedirs(workdir, exist_ok=True)
        log = open(os.path.join(workdir, "rootconv.log"), "a", encoding="utf-8")
        try:
            cmd = [sys.executable, "-m", "alab_daq.core.rootconv", raw, part]
            if shutil.which("nice"):
                cmd = ["nice", "-n", "19"] + cmd
            code, output = await self._run(cmd, workdir, log)
            if self._cancelled:
                self._remove(part)
                self._record(job, "", "cancelled", -1, -1, "stopped", log)
                return
            m = CHECK.search(output)
            n_raw, n_root = (int(m[1]), int(m[2])) if m else (-1, -1)
            lines = [line for line in output.strip().splitlines() if line.strip()]
            if code == 0 and m and os.path.exists(part):
                os.replace(part, root)
                self._record(job, root, "ok", n_raw, n_root, lines[-1], log)
            elif code == 1 and os.path.exists(part):        # written, but it differs from the raw file: kept, marked
                os.replace(part, root)
                self._record(job, root, "mismatch", n_raw, n_root, lines[-1] if lines else "", log)
            else:
                self._remove(part)
                self._record(job, "", "failed", n_raw, n_root,
                             "cannot start the converter" if code == 127 else (lines[-1] if lines else f"exit code {code}"), log)
        finally:
            log.close()

    async def _build(self, job: dict) -> None:
        from alab_daq.core.eventbuild import built_name
        raw, workdir = job["raw"], job["workdir"]
        try:
            out = built_name(raw)
        except (OSError, ValueError) as err:
            self._record(job, "", "failed", -1, -1, str(err))
            return
        part = out + ".part"
        if os.path.exists(out):
            self._record(job, out, "exists", -1, -1, "a ROOT file of this name exists already")
            return
        os.makedirs(workdir, exist_ok=True)
        log = open(os.path.join(workdir, "rootconv.log"), "a", encoding="utf-8")
        try:
            cmd = [sys.executable, "-m", "alab_daq.core.eventbuild", raw, part]
            if shutil.which("nice"):
                cmd = ["nice", "-n", "19"] + cmd
            code, output = await self._run(cmd, workdir, log)
            if self._cancelled:
                self._remove(part)
                self._record(job, "", "cancelled", -1, -1, "stopped", log)
                return
            m = BUILT.search(output)
            n_ref, n_root = (int(m[1]), int(m[2])) if m else (-1, -1)
            lines = [line for line in output.strip().splitlines() if line.strip()]
            built = next((line for line in lines if line.startswith("built ")), lines[-1] if lines else "")
            if code == 0 and m and os.path.exists(part):
                os.replace(part, out)
                self._record(job, out, "ok", n_ref, n_root, built, log)
            else:
                self._remove(part)
                self._record(job, "", "failed", n_ref, n_root, lines[-1] if lines else f"exit code {code}", log)
        finally:
            log.close()

    async def _run(self, cmd, cwd, log) -> tuple[int, str]:
        log.write("$ " + " ".join(shlex.quote(c) for c in cmd) + "\n")
        log.flush()
        env = dict(os.environ, PYTHONPATH=str(PROJECT / "src"), OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1",
                   MKL_NUM_THREADS="1")
        try:
            self._proc = await asyncio.create_subprocess_exec(*cmd, cwd=cwd, env=env, stdin=asyncio.subprocess.DEVNULL,
                                                              stdout=asyncio.subprocess.PIPE,
                                                              stderr=asyncio.subprocess.STDOUT)
        except OSError as err:
            log.write(f"ERROR: cannot start {cmd[0]}: {err}\n")
            return 127, ""
        out = []
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace")
            out.append(text)
            log.write(text)
            m = PROGRESS.search(text)
            if m:
                self.percent = float(m[1])
                self.publish()
        code = await self._proc.wait()
        log.flush()
        return code, "".join(out)

    def _record(self, job: dict, root: str, status: str, n_raw: int, n_root: int, message: str, log=None) -> None:
        raw = job["raw"]
        result = {"raw": raw, "root": root, "status": status, "events_raw": n_raw, "events_root": n_root,
                  "message": message, "by": job.get("by", ""), "time": time.time()}
        self.history.append(result)
        if log is not None:
            log.write(f"--- {os.path.basename(raw)}: {status} {message}\n")
        if job.get("run_json"):
            key = raw if job.get("kind") != "build" else os.path.join(os.path.dirname(raw), "built")
            self._add_to_run_json(job["run_json"], key, root, status, message)
        self.station.publish({"type": "root_done", "result": result})
        self.publish(force=True)

    @staticmethod
    def _add_to_run_json(path: str, raw: str, root: str, status: str, message: str) -> None:
        try:
            with open(path, encoding="utf-8") as f:
                summary = json.load(f)
        except (OSError, ValueError):
            summary = {}
        summary.setdefault("root_files", {})[os.path.basename(raw)] = {"root": root, "status": status, "check": message}
        try:
            write_atomic(path, json.dumps(summary, indent=2) + "\n", private=False)
        except OSError:
            pass

    @staticmethod
    def _remove(path: str) -> None:
        try:
            os.remove(path)
        except OSError:
            pass

    def status(self) -> dict:
        return {"busy": self.busy(), "left": self.left(), "number": self.number, "total": self.total,
                "percent": self.percent, "current": self.current["raw"] if self.current else None,
                "history": list(self.history)[-12:]}

    def publish(self, force: bool = False) -> None:
        now = time.time()
        if force or now - self._last_publish >= 0.5:
            self._last_publish = now
            self.station.publish({"type": "root", "root": self.status()})
