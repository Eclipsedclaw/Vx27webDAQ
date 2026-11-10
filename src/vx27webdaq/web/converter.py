"""Converting .raw files to ROOT in the background, one after the other: after every run (all its files) and
when a user asks on the Tools page.

python -m vx27webdaq.core.rootconv writes NAME.root.part next to NAME.raw, at the lowest priority (nice 19) and
with one thread, reads it back and compares it with the raw file (the events, their trigger IDs, samples);
only then is it renamed NAME.root, so a NAME.root is always complete. The .raw file stays, and an existing
NAME.root is never written over. What the converter prints goes to rootconv.log in the work folder (the run
folder, or the user's folder), and an after-run result into the run's run.json ("root_files", and "built" for
RUN_built.root). The queue is kept in root_queue.json, so a server restart goes on with it.

A conversion is skipped (status "skipped", with the reason) when the data folder has less free space than about
twice the data, and an event building when the run's boards cannot be matched (eventbuild exits with 3: boards
with clocks of their own). Each user has at most MAX_QUEUED_PER_USER conversions waiting.
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
from concurrent.futures import ThreadPoolExecutor

from vx27webdaq.core import rawdata
from vx27webdaq.core.system import disk_free
from vx27webdaq.web.settings import PROJECT, write_atomic

PROGRESS = re.compile(r"processed\s+([0-9.]+)\s*%")
CHECK = re.compile(r"check: (\d+) events in the raw file, (\d+) in the ROOT file")
BUILT = re.compile(r"check: (\d+) triggers of \S+, (\d+) entries")
RUN_NAME = re.compile(r"^run\d{5,}$")
MAX_QUEUED_PER_USER = 20        # conversions one user (or all guests together) may have waiting
SPACE_FACTOR = 2.0              # a conversion needs about this many times the size of its data free
EXIT_CANNOT_MATCH = 3           # eventbuild: the run's boards cannot be matched (no common clock)


def root_names(raw: str) -> tuple[str, str]:
    """(NAME.root, NAME.root.part) next to NAME.raw."""
    base = raw[:-4] if raw.lower().endswith(".raw") else raw
    return base + ".root", base + ".root.part"


def built_path(raw: str) -> str:
    """RUN_built.root beside the run's files. The run's name comes from the file's header, so only a run's name
    (runNNNNN) is taken: a header naming "../x" cannot put the file anywhere else."""
    header, _ = rawdata.read_header(raw)
    run = header.get("run") if isinstance(header, dict) else None
    if not (isinstance(run, str) and RUN_NAME.match(run)):
        raise ConvertError(f"{os.path.basename(raw)} names no run (runNNNNN) in its header: the events of its run "
                           "cannot be put into one file")
    return os.path.join(os.path.dirname(os.path.abspath(raw)), f"{run}_built.root")


class ConvertError(ValueError):
    """A file that cannot be added to the queue (the message says why)."""


class RootConverter:
    def __init__(self, station):
        self.station = station
        self.queue: list[dict] = []         # {raw, workdir, run_json, by} (and "kind": "build")
        self.current: dict | None = None
        self.percent = 0.0
        self.number = 0
        self.total = 0
        self.history: deque = deque(maxlen=40)
        self._proc: asyncio.subprocess.Process | None = None
        self._cancelled = False
        self._wake = asyncio.Event()
        self._last_publish = 0.0
        self._records = ThreadPoolExecutor(max_workers=1, thread_name_prefix="vx27-runjson")   # run.json, one at a time

    # ---- the queue ----
    def busy(self) -> bool:
        return self.current is not None or bool(self.queue)

    def left(self) -> int:
        return len(self.queue) + (self.current is not None)

    def queued(self, raw: str) -> bool:
        conv = lambda j: j is not None and j.get("kind") != "build"          # noqa: E731
        return any(conv(j) and j["raw"] == raw for j in self.queue) or (conv(self.current) and self.current["raw"] == raw)

    def queued_by(self, who: str) -> int:
        return sum(1 for j in self.queue if j.get("by") == who) + int(bool(self.current and self.current.get("by") == who))

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

    def _asked(self, raw: str, by: str) -> None:
        """What every request of a user must pass, besides the files: not of a file the run is writing, and not more
        than MAX_QUEUED_PER_USER waiting of this user (guests count as one)."""
        active = self.station.runs.active
        if active is not None and os.path.realpath(raw) in {os.path.realpath(p) for p in active.data_files()}:
            raise ConvertError(f"{os.path.basename(raw)} is still being written by the run")
        if self.queued_by(by) >= MAX_QUEUED_PER_USER:
            raise ConvertError(f"{MAX_QUEUED_PER_USER} conversions of yours are waiting already: ask for more when some "
                               "are done")

    @staticmethod
    def check_request(raw: str) -> None:
        """(in a worker thread: the files may be on the NAS) A conversion may be asked for this file: a .raw file
        without its NAME.root."""
        root, _ = root_names(raw)
        if not raw.lower().endswith(".raw") or not os.path.isfile(raw):
            raise ConvertError(f"{raw} is not a .raw file")
        if os.path.exists(root):
            raise ConvertError(f"{os.path.basename(root)} exists already; it is never written over")

    def request(self, raw: str, workdir: str, by: str) -> None:
        """A conversion asked for on the Tools page (check_request() passed): never over an existing ROOT file, never
        of a file still being written."""
        if self.queued(raw):
            raise ConvertError(f"{os.path.basename(raw)} is being converted already")
        self._asked(raw, by)
        self.add([raw], workdir, by=by)

    @staticmethod
    def check_build(raw: str) -> str:
        """(in a worker thread) RUN_built.root may be made for the run of this file: its path."""
        if not raw.lower().endswith(".raw") or not os.path.isfile(raw):
            raise ConvertError(f"{raw} is not a .raw file")
        out = built_path(raw)
        if os.path.exists(out):
            raise ConvertError(f"{os.path.basename(out)} exists already; it is never written over")
        return out

    def request_build(self, raw: str, workdir: str, by: str) -> None:
        """RUN_built.root asked for on the Tools page (check_build() passed)."""
        if any(j.get("kind") == "build" and j["raw"] == raw for j in self.queue):
            raise ConvertError(f"the events of the run of {os.path.basename(raw)} are being put into one file already")
        self._asked(raw, by)
        self.add_build(raw, workdir, by=by)

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
        """The queue of the server before this one: conversions and event buildings, each as it was."""
        try:
            with open(self.station.state_dir / "root_queue.json", encoding="utf-8") as f:
                jobs = [j for j in json.load(f) if isinstance(j, dict) and isinstance(j.get("raw"), str)]
        except (OSError, ValueError, TypeError):
            return
        for j in jobs:
            workdir = j.get("workdir") or os.path.dirname(j["raw"])
            if j.get("kind") == "build":
                self.add_build(j["raw"], workdir, j.get("run_json"), j.get("by", ""))
            else:
                self.add([j["raw"]], workdir, j.get("run_json"), j.get("by", ""))

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
                    try:
                        await self._convert(self.current)
                    except asyncio.CancelledError:
                        raise
                    except Exception as err:        # a converter that breaks must not stop the server
                        self._record(self.current, "", "failed", -1, -1, f"internal error: {err}")
                    # busy until its result is in run.json too (the writes go one after the other in one thread)
                    await asyncio.wrap_future(self._records.submit(lambda: None))
                except asyncio.CancelledError:         # the server stops: this file again after the restart
                    await self._stop_process()
                    self.queue.insert(0, self.current)
                    raise
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
            part = (root_names(self.current["raw"])[1] if self.current.get("kind") != "build"
                    else self.current.get("out", "") + ".part")
            if part != ".part":
                self._remove(part)

    @staticmethod
    def _space(paths: list[str], folder: str) -> str:
        """(in a worker thread) Why there is not room for the ROOT file of these data files in folder, or ""."""
        need = 0
        for p in paths:
            try:
                need += os.path.getsize(p)
            except OSError:
                pass
        need = int(need * SPACE_FACTOR)
        free = disk_free(folder)
        if free is not None and free < need:
            return (f"not enough free space in {folder}: {free / 1e9:.2f} GB free, about {need / 1e9:.2f} GB needed "
                    "(twice the data); make room and convert it again on the Tools page")
        return ""

    @staticmethod
    def _run_raw_files(raw: str) -> list[str]:
        """(in a worker thread) The .raw files of the run of this file (for the free space an event building needs)."""
        folder, name = os.path.split(raw)
        prefix = name.split("_", 1)[0] + "_"
        try:
            return [os.path.join(folder, n) for n in os.listdir(folder) if n.startswith(prefix) and n.endswith(".raw")]
        except OSError:
            return [raw]

    async def _convert(self, job: dict) -> None:
        if job.get("kind") == "build":
            await self._build(job)
            return
        raw, workdir = job["raw"], job["workdir"]
        root, part = root_names(raw)
        if await asyncio.to_thread(os.path.exists, root):
            self._record(job, root, "exists", -1, -1, "a ROOT file of this name exists already")
            return
        if not await asyncio.to_thread(os.path.isfile, raw):
            self._record(job, "", "failed", -1, -1, "the .raw file is gone")
            return
        room = await asyncio.to_thread(self._space, [raw], os.path.dirname(raw))
        if room:
            self._record(job, "", "skipped", -1, -1, room)
            return
        await asyncio.to_thread(os.makedirs, workdir, exist_ok=True)
        log = open(os.path.join(workdir, "rootconv.log"), "a", encoding="utf-8")
        try:
            cmd = [sys.executable, "-m", "vx27webdaq.core.rootconv", raw, part]
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
            elif code == 1 and m and os.path.exists(part):  # written and read back, but it differs: kept, marked
                os.replace(part, root)
                self._record(job, root, "mismatch", n_raw, n_root, lines[-1] if lines else "", log)
            else:                                           # anything else (a crash exits 1 too, without its check)
                self._remove(part)
                self._record(job, "", "failed", n_raw, n_root,
                             "cannot start the converter" if code == 127 else (lines[-1] if lines else f"exit code {code}"), log)
        finally:
            log.close()

    async def _build(self, job: dict) -> None:
        raw, workdir = job["raw"], job["workdir"]
        try:
            out = await asyncio.to_thread(built_path, raw)
        except (OSError, ValueError) as err:
            self._record(job, "", "failed", -1, -1, str(err))
            return
        job["out"] = out
        part = out + ".part"
        if await asyncio.to_thread(os.path.exists, out):
            self._record(job, out, "exists", -1, -1, "a ROOT file of this name exists already")
            return
        files = await asyncio.to_thread(self._run_raw_files, raw)
        room = await asyncio.to_thread(self._space, files, os.path.dirname(raw))
        if room:
            self._record(job, "", "skipped", -1, -1, room)
            return
        await asyncio.to_thread(os.makedirs, workdir, exist_ok=True)
        log = open(os.path.join(workdir, "rootconv.log"), "a", encoding="utf-8")
        try:
            cmd = [sys.executable, "-m", "vx27webdaq.core.eventbuild", raw, part]
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
            elif code == EXIT_CANNOT_MATCH:                # e.g. independent boards: no common clock to match by
                self._remove(part)
                self._record(job, "", "skipped", -1, -1, lines[-1] if lines else "the run's boards cannot be matched", log)
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
        if job.get("kind") == "build":
            result["kind"] = "build"
        self.history.append(result)
        if log is not None:
            log.write(f"--- {os.path.basename(raw)}: {status} {message}\n")
        if job.get("run_json"):             # in a thread of its own (the NAS), one update after the other
            args = (job["run_json"], raw, root, status, message, job.get("kind") == "build")
            try:
                self._records.submit(self._add_to_run_json, *args)
            except RuntimeError:
                self._add_to_run_json(*args)
        self.station.publish({"type": "root_done", "result": result})
        self.publish(force=True)

    @staticmethod
    def _add_to_run_json(path: str, raw: str, root: str, status: str, message: str, built: bool = False) -> None:
        """The result into the run's run.json: "root_files" by data file, and RUN_built.root under "built". A run.json
        that cannot be read is left as it is (never replaced by one with only this entry, and never made again in
        a run folder that was removed)."""
        try:
            with open(path, encoding="utf-8") as f:
                summary = json.load(f)
        except (OSError, ValueError):
            return
        if not isinstance(summary, dict):
            return
        entry = {"root": root, "status": status, "check": message}
        if built:
            summary["built"] = entry
        else:
            files = summary.get("root_files")
            if not isinstance(files, dict):
                files = summary["root_files"] = {}
            files[os.path.basename(raw)] = entry
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

    def close(self) -> None:
        """The server stops: the run.json updates still waiting are written by their thread (never waited for here:
        a NAS that hangs must not hold up the stop)."""
        self._records.shutdown(wait=False)
