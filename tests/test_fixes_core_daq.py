"""Regression tests for the fixes of the core and DAQ code review (2026-11-10): each test fails on the code before
its fix. The finding of each is named in its docstring (C1, D2, ...).

Everything the tests write stays in .test_tmp/core/<test>.
"""

import contextlib
import copy
import io
import json
import os
import signal
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

import numpy as np

from vx27webdaq.core import config as C
from vx27webdaq.core import models
from vx27webdaq.core import rawdata as R
from vx27webdaq.web.settings import PROJECT
from tests.test_core import folder, run_daq, small


def errors(cfg, **kw):
    return {p.where for p in C.check_config(cfg, check_paths=False, **kw) if p.level == "error"}


def warnings(cfg, **kw):
    return {p.where for p in C.check_config(cfg, check_paths=False, **kw) if p.level == "warning"}


def self_trigger_cfg(groups: str, combine: str = "A") -> C.DaqConfig:
    """One V2740 that triggers on its own channels: channel c triggers in group groups[c] (A or B), the others do
    not trigger."""
    cfg = C.default_config()
    cfg.boards = cfg.boards[:1]
    cfg.run.sync = "independent"
    cfg.trigger.setup, cfg.trigger.combine = "self", combine
    for c, ch in enumerate(cfg.boards[0].channels):
        ch.self_trigger = c < len(groups)
        ch.itl = groups[c] if c < len(groups) else "A"
    return cfg


class ConfigFixTests(unittest.TestCase):
    def test_numbers_that_are_no_settings(self):
        """C11: NaN, Infinity and 1e400 (JSON reads it as Infinity) are refused as ConfigError, never an
        OverflowError, and never stored."""
        too_long = "1" + "0" * 400
        for text in ('{"run": {"duration_s": 1e400}}', '{"run": {"hk_s": 1e400}}', '{"run": {"hk_s": NaN}}',
                     '{"run": {"hk_s": Infinity}}', '{"run": {"duration_s": "Infinity"}}', '{"run": {"hk_s": "nan"}}',
                     '{"boards": [{"record_length": 1e400}]}', '{"boards": [{"channels": [{"dc_offset": -Infinity}]}]}',
                     '{"run": {"duration_s": %s}}' % too_long, '{"boards": [{"clock_out_delay_ps": 1e400}]}'):
            with self.assertRaises(C.ConfigError, msg=text):
                C.from_dict(json.loads(text))
        self.assertEqual(C.from_dict({"run": {"duration_s": 12.0, "hk_s": "2.5"}}).run.hk_s, 2.5)

    def test_board_names_and_count_in_from_dict(self):
        """A3, K11: a board name that fails the name rule is refused (it reaches file paths), and so are more boards
        than the maximum (they were cut to the first 32 without a word)."""
        d = C.to_dict(C.default_config())
        for bad in ("../users", "", "a b", "x" * 40, "-b", "b/0"):
            d2 = copy.deepcopy(d)
            d2["boards"][0]["name"] = bad
            with self.assertRaises(C.ConfigError, msg=bad):
                C.from_dict(d2)
        many = copy.deepcopy(d)
        many["boards"] = [dict(copy.deepcopy(d["boards"][0]), name=f"b{k}", uri=f"dig2://10.0.0.{k + 1}")
                          for k in range(C.MAX_BOARDS + 1)]
        with self.assertRaises(C.ConfigError):
            C.from_dict(many)
        many["boards"] = many["boards"][:C.MAX_BOARDS]
        self.assertEqual(len(C.from_dict(many).boards), C.MAX_BOARDS)

    def test_board_names_that_collide(self):
        """C5, C9: names that become the same branch name in RUN_built.root, or the same file on a disk that does
        not tell upper from lower case, names like the further files of a board (b0_001) and the name built."""
        cases = ((["b.0", "b_0"], 1), (["b-1", "b_1"], 1), (["B0", "b0"], 1), (["b0", "b0_001"], 1),
                 (["Built", "b1"], 0), (["b0", "built"], 1), (["b0_123", "b1"], 0))
        for names, bad in cases:
            cfg = C.default_config()
            for b, n in zip(cfg.boards, names):
                b.name = n
            self.assertIn(f"boards[{bad}].name", errors(cfg), names)
        for names in (["b0", "b1"], ["vx2740_1234", "vx2730"], ["b_01", "b.1x"], ["built2", "b0_12"]):
            cfg = C.default_config()
            for b, n in zip(cfg.boards, names):
                b.name = n
            self.assertFalse({w for w in errors(cfg) if w.endswith(".name")}, names)

    def test_switched_off_board(self):
        """C12: a board switched off takes no part in the run: its address, model or what it said when checked must
        not stop the run (only its name is checked)."""
        cfg = C.default_config()
        off = cfg.boards[1]
        off.enabled, off.uri, off.model = False, "nonsense", "V1725"
        self.assertEqual(errors(cfg, facts={"b1": {"model": "V2745", "channels": 7}}), set())
        off.model, off.uri = "V2730", cfg.boards[0].uri           # the same address as a board in the run
        self.assertEqual(errors(cfg, facts={"b1": {"model": "V2745", "channels": 7}}), set())
        off.name = "a b"                                           # its name still counts
        self.assertIn("boards[1].name", errors(cfg))
        on = C.default_config()                                    # switched on, the same problems stop the run
        on.boards[1].uri = "nonsense"
        self.assertIn("boards[1].uri", errors(on))
        self.assertIn("boards[1].model", errors(C.default_config(), facts={"b1": {"model": "V2745"}}))

    def test_switched_off_board_inside_a_chain(self):
        """M3: a board switched off between boards that are on breaks the clock and trigger chain: a warning."""
        cfg = C.default_config()
        cfg.boards.append(C.Board("b2", "dig2://10.0.0.9", "V2740", channels=C.channels_for("V2740")))
        cfg.boards[1].enabled = False
        self.assertIn("boards[1].enabled", warnings(cfg))
        cfg.run.sync = "independent"
        self.assertNotIn("boards[1].enabled", warnings(cfg))
        cfg.run.sync = "chain"
        cfg.boards[1].enabled, cfg.boards[0].enabled = True, False      # the first one off: not between others
        self.assertNotIn("boards[0].enabled", warnings(cfg))

    def test_relative_data_folder(self):
        """R13: a relative data folder is a warning (the DAQ program runs in the run's folder, the server elsewhere)."""
        cfg = C.default_config()
        cfg.run.data_dir = "data/runs"
        self.assertIn("run.data_dir", warnings(cfg))
        cfg.run.data_dir = "/NAS/ALAB_DAQ_DATA"
        self.assertNotIn("run.data_dir", warnings(cfg))
        cfg.run.data_dir = "~/data"                                 # the home folder is a whole path
        self.assertNotIn("run.data_dir", warnings(cfg))

    def test_majority_per_channel_group(self):
        """C7: each logic counts only the channels of its own group, a logic the board trigger does not use is not
        checked, and a group the board trigger uses without any triggering channel is an error."""
        cfg = self_trigger_cfg("AABBBBB", "A_OR_B")            # majority 3 of A with only 2 channels in group A
        cfg.trigger.logic, cfg.trigger.majority = "Majority", 3
        self.assertIn("trigger.majority", errors(cfg))
        cfg = self_trigger_cfg("BBBB", "B")                     # an unused logic A at majority 5: no error
        cfg.trigger.logic, cfg.trigger.majority = "Majority", 5
        self.assertEqual(errors(cfg), set())
        cfg = self_trigger_cfg("BBBB", "A")                     # every triggering channel in group B, the trigger A
        self.assertIn("boards[0].channels", errors(cfg))
        for combine in ("A_AND_B", "A_OR_B"):                   # a used group without channels
            self.assertIn("boards[0].channels", errors(self_trigger_cfg("AAAA", combine)), combine)
        cfg = self_trigger_cfg("AABB", "B")                     # majority of B over group B only
        cfg.trigger.logic_b, cfg.trigger.majority_b = "Majority", 3
        self.assertIn("trigger.majority_b", errors(cfg))
        cfg.trigger.majority_b = 2
        self.assertEqual(errors(cfg), set())
        self.assertIn("boards[0].channels", warnings(cfg))     # group A channels with trigger B: they do not trigger
        good = self_trigger_cfg("AAABB", "A_AND_B")
        good.trigger.logic, good.trigger.majority = "Majority", 3
        self.assertEqual(errors(good), set())

    def test_itlb_mask_for_older_firmware(self):
        """C1 (plan): firmware without ITLConnect gets the channel masks of both groups: ITLBMask too when group B
        has channels."""
        cfg = self_trigger_cfg("AAAABB", "A_OR_B")
        first = next(s for s in C.plan(cfg, 0) if s.path.endswith("/ITLConnect"))
        self.assertEqual({a.path: a.value for a in first.alt}, {"/par/ITLAMask": "0xf", "/par/ITLBMask": "0x30"})
        first = next(s for s in C.plan(self_trigger_cfg("AAAA"), 0) if s.path.endswith("/ITLConnect"))
        self.assertEqual([a.path for a in first.alt], ["/par/ITLAMask"])


class RawFixTests(unittest.TestCase):
    def write(self, d, n=12, tail=b""):
        w = R.RawWriter(os.path.join(d, "run00001_b0"), {"board": {"name": "b0"}})
        w.write(R.encode_start(8, 0b11))
        for k in range(n):
            w.write(R.encode_scope(k, 100 * k, [0, 1], np.full((2, 8), k, np.uint16)))
        w.write(tail)
        w.close()
        return w.files[0]

    def test_zero_filled_tail(self):
        """C6: a file whose end is zeros (a crash or a full disk: the file system made the file longer than the
        data that reached it) keeps every complete event before the zeros and is truncated, not unreadable."""
        d = folder("fix_zero_tail")
        path = self.write(d, tail=b"\0" * 4096)
        with R.RawFile(path) as f:
            self.assertEqual((len(f), f.truncated), (12, True))
            self.assertEqual(f.event(11).trigger_id, 11)
            self.assertEqual(f.start_record()["kind"], "start")
        self.assertTrue(R.describe(path)["truncated"])
        # a size running past the end of the file (also one too large for any event) is the same: a cut tail
        for word in ((R.FORMAT_SCOPE << 60) | 500, (R.FORMAT_SCOPE << 60) | 0xFFFFFFFF, (R.FORMAT_SPECIAL << 60) | (9 << 32) | 1):
            path = self.write(folder("fix_cut_tail"), tail=word.to_bytes(8, "big"))
            with R.RawFile(path) as f:
                self.assertEqual((len(f), f.truncated), (12, True), hex(word))

    def test_scan_raises_only_raw_format_errors(self):
        """Decision 5 (D8): a Scope header claiming fewer than its 3 header words, and a special event whose extra
        words run past the buffer, are RawFormatError, never struct.error (which killed the DAQ's reader)."""
        ev = R.encode_scope(1, 2, [0], np.zeros((1, 4), np.uint16))
        short = ((R.FORMAT_SCOPE << 60) | 2).to_bytes(8, "big") + b"\0" * 8
        special = ((R.FORMAT_SPECIAL << 60) | (5 << 32) | 1).to_bytes(8, "big")
        for data in (short, ev + short, ev + special, special):
            with self.assertRaises(R.RawFormatError):
                R.scan(data)
        with self.assertRaises(R.RawFormatError):
            R.decode_special(special)

    def test_writer_after_close(self):
        """Decision 5 (D3): a write after close() raises instead of opening a new file part."""
        d = folder("fix_writer_closed")
        w = R.RawWriter(os.path.join(d, "run00001_b0"), {"board": {"name": "b0"}})
        w.write(R.encode_start(8, 1))
        w.close()
        with self.assertRaises(ValueError):
            w.write(R.encode_stop(5))
        self.assertEqual(sorted(os.listdir(d)), ["run00001_b0.raw"])
        w.close()                                               # closing twice is fine


class RootconvFixTests(unittest.TestCase):
    def test_a_crash_is_exit_code_2(self):
        """C10: any failure of the conversion is exit code 2 (failed); exit code 1 means "written, but it differs
        from the raw file" and keeps the file."""
        from vx27webdaq.core import rootconv
        d = folder("fix_rootconv_crash")
        out = io.StringIO()
        for exc in (RuntimeError("boom"), MemoryError(), KeyError("x"), TypeError("y")):
            with mock.patch.object(rootconv, "convert", side_effect=exc), contextlib.redirect_stdout(out):
                self.assertEqual(rootconv.main([os.path.join(d, "x.raw"), os.path.join(d, "x.root")]), 2, repr(exc))
        self.assertIn("ERROR: the conversion failed", out.getvalue())


def fake_run(d: str, boards: list, sync: str = "chain", run: str = "run00009", window_ns: int = 1000,
             channels=(0, 1), samples: int = 8, file_run: str | None = None) -> str:
    """The raw files of a run: boards = [(name, [(trigger ID, timestamp), ...])]. Every sample of an event is its
    timestamp (mod 60000), so the waveforms tell which event went where. Returns the first board's file. file_run:
    the run's name in the file names, when it is not the one in the headers."""
    cfg = {"run": {"sync": sync, "sync_window_ns": window_ns}}
    for k, (name, events) in enumerate(boards):
        header = {"run": run, "sync": sync, "config": cfg,
                  "board": {"name": name, "index": k, "model": "V2740", "sample_ns": 8.0, "pre_trigger": 2}}
        w = R.RawWriter(os.path.join(d, f"{file_run or run}_{name}"), header)
        w.write(R.encode_start(samples, R.channels_mask(channels)))
        for tid, ts in events:
            w.write(R.encode_scope(tid, ts, list(channels), np.full((len(channels), samples), ts % 60000, np.uint16)))
        w.write(R.encode_stop(max([ts for _, ts in events] or [0]) + 10))
        w.close()
    return os.path.join(d, f"{file_run or run}_{boards[0][0]}.raw")


class HeaderNameTests(unittest.TestCase):
    def test_names_from_a_header_stay_in_the_run_folder(self):
        """A11, the core side: the housekeeping file and RUN_built.root are named from the header's run and board, so a
        header naming its run "../../secret" could put a file from outside the run's folder into the ROOT file, or
        name the built file outside it. Such a name is not taken; a run named by hand (not runNNNNN) still is."""
        import uproot
        from vx27webdaq.core import eventbuild, rootconv
        d = folder("fix_header_names")
        sub = os.path.join(d, "a", "b")
        os.makedirs(sub)
        with open(os.path.join(d, "secret_m_hk.csv"), "w") as f:
            f.write("time,temp_core\n1,42\n")
        raw = fake_run(sub, [("m", [(0, 100), (1, 200)])], run="../../secret", file_run="run00003")
        header, _ = R.read_header(raw)
        self.assertEqual(rootconv.hk_path(raw, header), "")
        out = os.path.join(sub, "run00003_m.root")
        self.assertEqual(rootconv.convert(raw, out), 2)
        with uproot.open(out) as r:
            self.assertNotIn("hk", {k.split(";")[0] for k in r.keys()})
        with self.assertRaises(R.RawFormatError):
            eventbuild.built_name(raw)
        with contextlib.redirect_stdout(io.StringIO()) as said:
            self.assertEqual(eventbuild.main([raw]), 2)
        self.assertIn("cannot be part of a file name", said.getvalue())
        self.assertEqual(sorted(os.listdir(d)), ["a", "secret_m_hk.csv"])
        for good in ("run00003", "run1", "my-run.2", "b_0"):
            self.assertEqual(R.name_part(good), good)
        for bad in ("../x", "a/b", "..", ".hidden", "", "run1\n", None, 3, "x" * 65):
            self.assertEqual(R.name_part(bad), "", repr(bad))


class EventBuildTests(unittest.TestCase):
    def built(self, raw, **kw):
        import uproot
        from vx27webdaq.core import eventbuild
        out = os.path.join(os.path.dirname(raw), "out.root")
        s = eventbuild.build(raw, out, **kw)
        with uproot.open(out) as r:
            return s, r["events"].arrays(library="np"), json.loads(str(r["settings"]))

    def test_a_board_that_missed_a_trigger(self):
        """C2: with a common trigger, a board that missed one trigger has every later trigger ID one lower: matched
        by trigger ID, all its later events were lost; by timestamp, only the missed one is missing (and the
        differing IDs are counted)."""
        d = folder("fix_build_missed")
        ts = [1000 + 10000 * i for i in range(20)]
        ref = [(i, t) for i, t in enumerate(ts)]
        b1 = [(j, t + 2) for j, t in enumerate(t for i, t in enumerate(ts) if i != 5)]   # it lacks trigger 5
        b2 = [(i, t + 1) for i, t in enumerate(ts)]
        s, a, settings = self.built(fake_run(d, [("m", ref), ("b1", b1), ("b.2", b2)]))
        self.assertEqual(list(np.flatnonzero(~a["b1_found"])), [5])
        self.assertEqual((s["triggers"], s["complete"], s["match"]), (20, 19, "timestamp"))
        self.assertTrue(np.array_equal(a["b1_timestamp"][a["b1_found"]], np.array(ts)[a["b1_found"]] + 2))
        self.assertEqual(set(a["b1_dt_ns"][a["b1_found"]]), {16})
        self.assertEqual(list(a["b1_trigger_id"][6:]), list(range(5, 19)))     # its own IDs, one lower
        self.assertEqual((s["ids_differ"], settings["ids_differ"]), ({"m": 0, "b1": 14, "b.2": 0}, {"m": 0, "b1": 14, "b.2": 0}))
        self.assertEqual((settings["match"], settings["found"]["b1"]), ("timestamp", 19))
        for i in (0, 7, 19):                                   # the samples are those of the matched event
            self.assertEqual(int(a["b1_ch1"][i][0]), (ts[i] + 2) % 60000)
            self.assertEqual(int(a["b_2_ch0"][i][0]), (ts[i] + 1) % 60000)
        self.assertFalse(a["b1_ch0"][5].any())                 # zeros where the board lacks the trigger

    def test_coincidences_of_self_triggering_boards(self):
        """C2: boards that trigger by themselves have unrelated trigger IDs: an event is matched by the nearest
        timestamp within the window; every event of a board goes into one entry at most (the nearer one)."""
        d = folder("fix_build_self")
        ref = [(0, 100), (1, 5000), (2, 9000), (3, 20000), (4, 40000), (5, 40050)]
        b1 = [(0, 102), (1, 7000), (2, 9001), (3, 15000), (4, 20003), (5, 40045), (6, 90000)]
        s, a, _ = self.built(fake_run(d, [("m", ref), ("b1", b1)]))
        self.assertEqual(list(a["b1_found"]), [True, False, True, True, False, True])
        self.assertEqual(list(a["b1_trigger_id"]), [0, 0, 2, 4, 0, 5])
        self.assertEqual(list(a["b1_dt_ns"]), [16, 0, 8, 24, 0, -40])
        self.assertEqual(s["ids_differ"]["b1"], 1)               # entry 3: IDs 3 and 4
        self.assertEqual(int(a["b1_ch0"][5][0]), 40045)          # the nearer reference event (40050) kept it

    def test_timestamps_that_restart(self):
        """C2: a timestamp reset (S-IN or GPIO) makes the timestamps start again: the events are matched within each
        stretch, not mixed up."""
        d = folder("fix_build_reset")
        ref = [(0, 100), (1, 200), (2, 300), (3, 50), (4, 150)]
        b1 = [(0, 101), (1, 201), (2, 250), (3, 301), (4, 51), (5, 151)]        # one event more: other IDs
        s, a, settings = self.built(fake_run(d, [("m", ref), ("b1", b1)]))
        self.assertEqual(list(a["b1_timestamp"]), [101, 201, 301, 51, 151])
        self.assertTrue(a["b1_found"].all())
        self.assertEqual(settings["boards"][1]["timestamp_restarts"], 1)

    def test_independent_boards_are_not_built(self):
        """Decision 2: boards on their own clocks have no common time: the building is refused with exit code 3
        (the converter: skipped), and no file is written."""
        from vx27webdaq.core import eventbuild
        d = folder("fix_build_independent")
        raw = fake_run(d, [("m", [(0, 100)]), ("b1", [(0, 100)])], sync="independent")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(eventbuild.main([raw]), 3)
        self.assertIn("skipped: run run00009: its boards ran on their own clocks", out.getvalue())
        self.assertFalse(os.path.exists(os.path.join(d, "run00009_built.root")))
        with contextlib.redirect_stdout(io.StringIO()):         # one board alone: nothing to match, it is built
            self.assertEqual(eventbuild.main([fake_run(folder("fix_build_one"), [("m", [(0, 100)])], sync="independent")]), 0)

    def test_batches_by_bytes(self):
        """C3: the batches of entries are sized by bytes, not 500 events (500 events of 64 channels of 40960 samples
        are 2.6 GB per board)."""
        from uproot.writing.writable import WritableTree
        from vx27webdaq.core import eventbuild
        d = folder("fix_build_batches")
        ts = [1000 + 10000 * i for i in range(40)]
        raw = fake_run(d, [("m", list(enumerate(ts))), ("b1", list(enumerate(ts)))], channels=(0, 1, 2, 3), samples=1024)
        per_entry = 2 * (2 * 4 * 1024 + 64)
        sizes, real = [], WritableTree.extend

        def extend(tree, data, **kw):
            sizes.append(sum(v.nbytes for k, v in data.items() if "_ch" in k))
            return real(tree, data, **kw)
        with mock.patch.object(eventbuild, "BATCH_BYTES", 5 * per_entry), mock.patch.object(WritableTree, "extend", extend):
            s, a, _ = self.built(raw)
        self.assertEqual((s["triggers"], s["complete"], len(a["m_ch0"])), (40, 40, 40))
        self.assertEqual(len(sizes), 8)
        self.assertLessEqual(max(sizes), 5 * per_entry)
        self.assertEqual(int(a["b1_ch3"][39][1023]), ts[39] % 60000)


class RunningDaqTests(unittest.TestCase):
    def test_only_real_daq_runs_count(self):
        """R4: only a process that is a DAQ run on the real boards counts (PYTHON -m vx27webdaq.daq run ...): not a
        simulated run, the wrapper, a shell, an editor or a grep whose line contains the text."""
        from vx27webdaq.core.system import is_daq_run, running_daq
        py = "/opt/Vx27webDAQ/VX27_ENV/bin/python"
        self.assertTrue(is_daq_run([py, "-m", "vx27webdaq.daq", "run", "/runs/x"]))
        self.assertTrue(is_daq_run(["/usr/bin/python3.11", "-m", "vx27webdaq.daq", "run", "/runs/x"]))
        for argv in ([py, "-m", "vx27webdaq.daq", "run", "/runs/x", "--simulate", "--rate 20"],
                     [py, "-m", "vx27webdaq.daq", "run", "/runs/x", "--simulate=--rate 20"],
                     [py, "-m", "vx27webdaq.web.wrapper", "--pid-file", "p", "--", py, "-m", "vx27webdaq.daq", "run", "x"],
                     ["grep", "vx27webdaq.daq run"], ["vim", "notes about vx27webdaq.daq run"],
                     ["bash", "-c", f"{py} -m vx27webdaq.daq run x"], [py, "-m", "vx27webdaq.daq", "check", "c.json"],
                     ["tail", "-m", "vx27webdaq.daq", "run"], [py]):
            self.assertFalse(is_daq_run(argv), argv)
        # a process whose command line contains the text, but is no DAQ run
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "vx27webdaq.daq run"])
        try:
            for _ in range(50):                 # until its command line is in /proc
                try:
                    with open(f"/proc/{p.pid}/cmdline", "rb") as f:
                        if b"vx27webdaq.daq run" in f.read():
                            break
                except OSError:
                    pass
                time.sleep(0.02)
            self.assertNotIn(p.pid, [pid for pid, _ in running_daq()])
        finally:
            p.kill()
            p.wait()


# ---- the DAQ program ----
def two_boards(seconds: int = 2, setup: str = "self") -> C.DaqConfig:
    cfg = small(C.default_config(), seconds=seconds)
    cfg.trigger.setup = setup
    return cfg


def make_run(name: str, cfg: C.DaqConfig, options: str | None = "--rate 40 --seed 5"):
    """A run of the DAQ program in this process (engine.Run), in a folder of its own: (run, folder)."""
    from vx27webdaq.daq import engine
    d = folder(name)
    cfg.run.data_dir = os.path.join(d, "raw")
    os.makedirs(cfg.run.data_dir)
    C.save_config(cfg, os.path.join(d, "config.json"), run_name="run00007")
    return engine.Run(d, options), d


@contextlib.contextmanager
def captured():
    """The DAQ's log (it writes to sys.stdout, also from its reader threads), and the signal handlers as before
    (Run.main installs its own)."""
    out = io.StringIO()
    old = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    try:
        with contextlib.redirect_stdout(out):
            yield out
    finally:
        for s, h in old.items():
            signal.signal(s, h)


def status_of(d: str) -> dict:
    with open(os.path.join(d, "status.json"), encoding="utf-8") as f:
        return json.load(f)


class FakeBoard:
    """A board for the engine without the simulator: it takes every command and setting, has no housekeeping, and
    sends the buffers given, then nothing (or, chatty, an event every 10 ms whatever it is told: a board that never
    got the stop)."""

    def __init__(self, reads=(), chatty=False, fail_read=None):
        self.reads, self.chatty, self.fail_read = list(reads), chatty, fail_read
        self.commands, self.closed = [], False
        self.event = R.encode_scope(1, 100, [0], np.zeros((1, 4), np.uint16))

    def read_raw(self, timeout_ms):
        if self.fail_read is not None:
            raise self.fail_read
        if self.reads:
            return memoryview(self.reads.pop(0)), 1
        time.sleep(0.01)
        return (memoryview(self.event), 1) if self.chatty else (None, 0)

    def cmd(self, name):
        self.commands.append(name)

    def set(self, path, value):
        pass

    def get(self, path):
        from vx27webdaq.daq.boards import BoardError
        raise BoardError(f"get {path}: not on this board")

    def close(self):
        self.closed = True


def popen_daq(name: str, cfg: C.DaqConfig, options: str):
    """The DAQ program as the server starts it, with simulated boards: (process, folder), once it is running."""
    d = folder(name)
    cfg.run.data_dir = os.path.join(d, "raw")
    os.makedirs(cfg.run.data_dir)
    C.save_config(cfg, os.path.join(d, "config.json"), run_name="run00007")
    env = dict(os.environ, PYTHONPATH=str(PROJECT / "src"), OMP_NUM_THREADS="1", PYTHONUNBUFFERED="1")
    p = subprocess.Popen([sys.executable, "-m", "vx27webdaq.daq", "run", d, "--simulate", options], cwd=d, env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(300):
        time.sleep(0.05)
        try:
            if status_of(d)["phase"] == "running":
                break
        except (OSError, ValueError, KeyError):
            pass
    return p, d


class SettingRuleTests(unittest.TestCase):
    """C1: a refused setting with alternatives (boards.send_setting, the same rule for a run and Send to boards)."""

    def sim_board(self, options: str, name: str = "b0", model: str = "V2740"):
        from vx27webdaq.daq.sim import Crate
        crate = Crate(options, "none", {name: model}, {name: 0})
        board = crate.open("dig2://sim", name)
        board.open_raw()
        return board

    def test_an_alternative_of_the_same_parameter_is_sent(self):
        """AcqTriggerSource ITLA|SwTrg refused, its alternative ITLA (the same parameter) was skipped as refused while
        the log said "using the alternative": the board kept its default source."""
        from vx27webdaq.daq.boards import send_setting
        cfg = self_trigger_cfg("AAAA")
        force = next(s for s in C.plan(cfg, 0) if s.path == "/par/AcqTriggerSource")
        self.assertEqual((force.value, force.alt[0].value), ("ITLA|SwTrg", "ITLA"))
        board, warnings, took = self.sim_board("--fail value:b0:AcqTriggerSource:SwTrg"), [], []
        refused: set = set()
        send_setting(board, force, refused, took.append, warnings.append)
        self.assertEqual(board.p("acqtriggersource"), "ITLA")
        self.assertEqual([s.value for s in took], ["ITLA"])
        self.assertEqual(refused, set())                       # the parameter is there: only that value is not
        self.assertIn("sending instead /par/AcqTriggerSource = ITLA", warnings[0])
        # the run's configuration (engine.Run.apply) by the same rule
        run, _ = make_run("fix_alt_same", cfg)
        br = run.boards[0]
        br.board = self.sim_board("--fail value:b0:AcqTriggerSource:SwTrg")
        with captured() as out:
            self.assertTrue(all(run.apply(br, s, set()) for s in C.plan(cfg, 0)))
        self.assertIn(["/par/AcqTriggerSource", "ITLA"], br.applied)
        self.assertIn("b0: the board refused /par/AcqTriggerSource = ITLA|SwTrg", out.getvalue())

    def test_a_missing_parameter_still_gets_its_alternatives(self):
        """VetoSource missing on the firmware: BoardVetoSource stands in for it, also for a later setting of
        VetoSource (the busy veto GPIO, then the board's own veto SIN): the later one was skipped."""
        from vx27webdaq.daq.boards import send_setting
        cfg = self_trigger_cfg("AAAA")
        cfg.trigger.busy_veto, cfg.boards[0].io_veto = True, "SIN"
        board, refused = self.sim_board("--fail param:b0:VetoSource"), set()
        for s in C.plan(cfg, 0):
            send_setting(board, s, refused)
        self.assertEqual(board.p("boardvetosource"), "SIN")
        self.assertIn("vetosource", refused)
        # firmware without ITLConnect: the masks of both channel groups
        cfg = self_trigger_cfg("AAAABB", "A_OR_B")
        board, refused = self.sim_board("--fail param:b0:ITLConnect"), set()
        for s in C.plan(cfg, 0):
            send_setting(board, s, refused)
        self.assertEqual((board.p("itlamask"), board.p("itlbmask")), ("0xf", "0x30"))
        from vx27webdaq.daq.boards import BoardError
        with self.assertRaises(BoardError):                    # an alternative refused too: the end of the board
            send_setting(self.sim_board("--fail value:b0:AcqTriggerSource:ITLA"),
                         next(s for s in C.plan(self_trigger_cfg("AAAA"), 0) if s.path == "/par/AcqTriggerSource"), set())

    def test_send_to_boards_sends_the_alternative(self):
        """C1 in python -m vx27webdaq.daq apply: the alternative is sent and counted."""
        d = folder("fix_apply_alt")
        cfg = C.lab_setup()
        cfg.trigger.master_out = "AcceptTrg"                   # the master's trigger is ITLA|SwTrg, else ITLA
        path = os.path.join(d, "lab.json")
        C.save_config(cfg, path)
        env = dict(os.environ, PYTHONPATH=str(PROJECT / "src"), OMP_NUM_THREADS="1")
        p = subprocess.run([sys.executable, "-m", "vx27webdaq.daq", "apply", path, "--simulate",
                            "--fail value:vx2730:AcqTriggerSource:SwTrg", "--board", "vx2730"],
                           capture_output=True, text=True, env=env, timeout=60)
        res = [json.loads(x) for x in p.stdout.splitlines() if x.startswith("{")]
        self.assertEqual((p.returncode, res[0]["ok"]), (0, True), p.stdout + p.stderr)
        self.assertEqual(res[0]["sent"], len(C.plan(cfg, 0, models.from_board({"model": "VX2730", "channels": 16}))))
        self.assertIn("sending instead /par/AcqTriggerSource = ITLA", res[0]["warnings"][0])

    def test_a_run_with_the_alternative(self):
        """C1 in a whole run: the board whose value was refused takes the alternative and triggers on its channels."""
        code, out, status, d = run_daq("fix_run_alt", two_boards(), "--rate 40 --seed 5 --fail value:b0:AcqTriggerSource:SwTrg")
        self.assertEqual(code, 0, out)
        self.assertGreater(status["boards"][0]["events"], 10)
        with R.RawFile(os.path.join(d, "raw", "run00007_b0.raw")) as f:
            self.assertIn(["/par/AcqTriggerSource", "ITLA"], f.header["applied"])


class HousekeepingRuleTests(unittest.TestCase):
    def test_a_transient_error_keeps_the_column(self):
        """D15: a parameter is dropped only when its first reading fails; one failing channel rate no longer turns
        every channel's rate off."""
        from vx27webdaq.daq.boards import BoardError, channel_rates, housekeeping

        class Flaky:
            def __init__(self):
                self.fail = set()

            def get(self, path):
                if path in self.fail:
                    raise BoardError(f"get {path}: timeout")
                return "41.5" if "Temp" in path else "7"
        b = Flaky()
        wanted, seen = {"temp_core": "/par/TempSensCore", "fan1_speed": "/par/SpeedSensFan1"}, set()
        b.fail = {"/par/SpeedSensFan1"}                         # its first reading fails: the board has none
        self.assertEqual(housekeeping(b, wanted, seen), {"temp_core": 41.5})
        self.assertEqual(list(wanted), ["temp_core"])
        b.fail = {"/par/TempSensCore"}                          # read once before: only this reading misses it
        self.assertEqual(housekeeping(b, wanted, seen), {})
        b.fail = set()
        self.assertEqual(housekeeping(b, wanted, seen), {"temp_core": 41.5})
        state: dict = {}
        b.fail = {"/ch/2/par/SelfTrgRate"}
        self.assertEqual(channel_rates(b, [0, 1, 2, 3], state), {0: 7, 1: 7, 3: 7})
        b.fail = {"/ch/0/par/SelfTrgRate"}                      # a transient error of a channel read before
        self.assertEqual(channel_rates(b, [0, 1, 2, 3], state), {1: 7, 3: 7})
        b.fail = set()
        self.assertEqual(channel_rates(b, [0, 1, 2, 3], state), {0: 7, 1: 7, 3: 7})     # channel 2: not asked again


class DaqCommandTests(unittest.TestCase):
    def daq(self, *args):
        env = dict(os.environ, PYTHONPATH=str(PROJECT / "src"), OMP_NUM_THREADS="1")
        p = subprocess.run([sys.executable, "-m", "vx27webdaq.daq", *args], capture_output=True, text=True, env=env,
                           timeout=60)
        return p.returncode, [json.loads(x) for x in p.stdout.splitlines() if x.startswith("{")], p.stdout + p.stderr

    def test_bad_simulator_options_and_tree_folder(self):
        """D16: check and param answer bad simulator options with a JSON line (it was a traceback); a --trees folder
        that cannot be made is a warning of each board, and every board is still checked."""
        d = folder("fix_commands")
        path = os.path.join(d, "c.json")
        C.save_config(C.default_config(), path)
        code, res, text = self.daq("check", path, "--simulate", "--nonsense 3")
        self.assertEqual((code, len(res)), (1, 1), text)
        self.assertIn("simulator", res[0]["error"])
        self.assertNotIn("Traceback", text)
        code, res, text = self.daq("param", path, "--board", "b0", "--get", "/par/ModelName", "--simulate", "--rate x")
        self.assertEqual((code, res[0]["ok"]), (1, False), text)
        self.assertIn("simulator", res[0]["error"])
        blocker = os.path.join(d, "a_file")
        with open(blocker, "w") as f:
            f.write("not a folder")
        code, res, text = self.daq("check", path, "--simulate", "--rate 5", "--trees", os.path.join(blocker, "trees"))
        self.assertEqual((code, [r["name"] for r in res], [r["ok"] for r in res]), (0, ["b0", "b1"], [True, True]), text)
        self.assertIn("cannot save its parameter tree", res[1]["warnings"][0])


class SimulatorFixTests(unittest.TestCase):
    def test_software_triggers_are_forgotten(self):
        """D18: the simulator keeps the software triggers only until every board has produced past them (the list
        grew by every trigger of the run and was searched in full on every read)."""
        from vx27webdaq.daq.sim import Crate
        crate = Crate("--rate 0", "none", {"b0": "V2740"}, {"b0": 4})
        b = crate.open("dig2://sim", "b0")
        b.set("/par/RecordLengthS", "16")
        b.set("/par/AcqTriggerSource", "SwTrg")
        b.open_raw()
        b.cmd("ArmAcquisition")
        b.cmd("SwStartAcquisition")
        events = 0
        for _ in range(3000):
            b.cmd("SendSwTrigger")
            data, n = b.read_raw(0)
            events += n
        b.cmd("SwStopAcquisition")
        while True:
            data, n = b.read_raw(0)
            if data is None:
                break
            events += n
        self.assertLess(len(b.sw_times), 300)
        self.assertEqual(events, 3000)                          # and every one of them became an event


class SyncFixTests(unittest.TestCase):
    def test_a_board_far_behind(self):
        """D10: the IDs of a board more than LAG_IDS behind were counted missing on it, and counted again as missing
        on every other board when they came: now they are taken off its count (late) and not compared."""
        from vx27webdaq.daq import sync
        with mock.patch.object(sync, "LAG_IDS", 500):
            s = sync.SyncCheck(["m", "a"], 1000)
            ids = np.arange(1000, dtype=np.int64)
            s.add(0, ids, ids * 1000)
            s.add(1, ids[:100], ids[:100] * 1000)
            s.process()                                         # a is 900 behind: up to 499 settled, 400 missing on a
            self.assertEqual(s.missing, [0, 400])
            s.add(1, ids[100:], ids[100:] * 1000)               # they come after all
            s.process(final=True)
        v = s.view()
        self.assertEqual(v["missing"], {"m": 0, "a": 0})
        self.assertEqual((v["checked"], v["built"], v["out_of_sync"], v["late"]), (1000, 600, 0, {"m": 0, "a": 400}))
        self.assertIn("late (not compared, the board was far behind): a 400", s.summary())


class EngineFixTests(unittest.TestCase):
    """The DAQ program's run (engine.Run): in this process with simulated or fake boards, or as the server starts
    it."""

    def test_independent_boards_have_no_sync_check(self):
        """Decision 3, M2: a common trigger on boards with their own clocks: no sync check (it warned "out of sync"
        on every trigger), with the reason the page shows."""
        cfg = two_boards(setup="external")
        cfg.run.sync = "independent"
        run, d = make_run("fix_no_sync_check", cfg)
        self.assertIsNone(run.sync)
        run.status(force=True)
        self.assertEqual(status_of(d)["matching"], {"applies": False, "why": "independent boards: no common clock"})
        cfg.run.sync = "chain"
        self.assertIsNotNone(make_run("fix_sync_check", cfg)[0].sync)

    def test_a_disk_error_at_the_flush_ends_the_run(self):
        """D2: an error at the reader's flush (ENOSPC, EIO) killed the reader thread silently: the state stayed
        running and the run went on. Now the board fails and the run stops."""
        run, d = make_run("fix_flush_error", two_boards(seconds=8))

        def flush(writer):
            raise OSError(28, "No space left on device")
        with captured() as out, mock.patch.object(R.RawWriter, "flush", flush):
            t0 = time.monotonic()
            code = run.main()
        self.assertEqual((code, run.stop_reason), (1, "a board failed"))
        self.assertIn("cannot write the data: [Errno 28] No space left on device", out.getvalue())
        self.assertLess(time.monotonic() - t0, 6)
        self.assertTrue(all(b.done for b in run.boards))

    def test_an_unexpected_reader_error(self):
        """D2: any exception of the reader ends its board with an error, and the reader is done."""
        run, d = make_run("fix_reader_error", two_boards())
        br = run.boards[0]
        br.board = FakeBoard(fail_read=RuntimeError("a driver bug"))
        with captured() as out:
            run.reader(br)
        self.assertEqual((br.state, br.ended, br.done), ("error", "error", True))
        self.assertIn("b0: its data reader failed: RuntimeError('a driver bug')", out.getvalue())

    def test_the_file_first_then_the_analysis(self):
        """D8, decision 5: a buffer the analysis does not expect (a Scope event of 1 word: struct.error killed the
        reader) is written to the file first, a warning once, and the reader goes on."""
        run, d = make_run("fix_write_first", two_boards())
        br = run.boards[0]
        bad = ((R.FORMAT_SCOPE << 60) | 1).to_bytes(8, "big")
        good = R.encode_scope(3, 500, [0, 1], np.full((2, 4), 7, np.uint16))
        stop = R.encode_stop(600)
        br.board = FakeBoard(reads=[bad, bad, good, stop])
        br.writer = R.RawWriter(os.path.join(d, "raw", "run00007_b0"), {"board": {"name": "b0"}})
        run.stop_sent = True                                    # (the stop record answers a stop)
        with captured() as out:
            run.reader(br)
        br.writer.close()
        self.assertEqual((br.ended, br.state, br.events), ("stop record", "stopped", 3))
        self.assertEqual(out.getvalue().count("data that are not Scope events"), 1)
        header, start = R.read_header(br.writer.files[0])
        with open(br.writer.files[0], "rb") as f:
            self.assertEqual(f.read()[start:], bad + bad + good + stop)

    def test_an_error_in_the_run_still_stops_the_boards(self):
        """D3: an exception in take_data() skipped stop_all(): the boards were closed while their readers still ran.
        Now the boards are stopped (stop records) before they are closed."""
        run, d = make_run("fix_internal_error", two_boards(seconds=6))
        calls = []

        def boom():
            calls.append(1)
            if len(calls) > 15:
                raise RuntimeError("boom")
        with captured() as out, mock.patch.object(run, "poll_control", boom):
            code = run.main()
        self.assertEqual(code, 1)
        self.assertIn("internal error: RuntimeError('boom')", out.getvalue())
        self.assertEqual([(b.ended, b.done) for b in run.boards], [("stop record", True), ("stop record", True)])
        self.assertEqual(status_of(d)["phase"], "failed")

    def test_a_housekeeping_file_that_cannot_be_written(self):
        """D3: a disk or NAS error on the housekeeping file is a warning, once; the housekeeping stops, the run goes
        on (it raised out of take_data)."""
        run, d = make_run("fix_hk_error", two_boards())
        br = run.boards[0]

        class Broken:
            def write(self, text):
                raise OSError(5, "Input/output error")

            def flush(self):
                pass

            def close(self):
                pass
        br.hk_out, br.hk_cols, br.hk_file = Broken(), [], "/somewhere/run00007_b0_hk.csv"
        with captured():
            run.write_hk(br, 1.0, {}, {})
            run.write_hk(br, 2.0, {}, {})
        self.assertIs(br.hk_out, False)
        self.assertEqual(len([w for w in run.warnings if "cannot write its housekeeping file" in w]), 1)

    def test_a_board_that_keeps_sending_after_the_stop(self):
        """D4: the stop's time-out was checked only when a read returned nothing: a board that kept sending (its
        stop never came) made the stop wait forever. Now: after STOP_WAIT_S the stop and the disarm go to it
        directly, and its reader ends at STOP_LIMIT_S."""
        from vx27webdaq.daq import engine
        run, d = make_run("fix_keeps_sending", two_boards())
        for br in run.boards:
            br.board = FakeBoard(chatty=True)
        with captured(), mock.patch.object(engine, "STOP_WAIT_S", 0.3), \
                mock.patch.object(engine, "STOP_LIMIT_S", 1.0, create=True):
            run.t_start, run.mono_start = time.time(), time.monotonic()
            for br in run.boards:
                br.thread = threading.Thread(target=run.reader, args=(br,), daemon=True)
                br.thread.start()
            stopper = threading.Thread(target=run.stop_all, daemon=True)
            t0 = time.monotonic()
            stopper.start()
            stopper.join(10)
            self.assertFalse(stopper.is_alive(), "the stop waits forever")
        self.assertLess(time.monotonic() - t0, 5)
        for br in run.boards:
            self.assertEqual((br.ended, br.done, br.still_sending), ("time-out", True, True))
            self.assertIn("DisarmAcquisition", br.board.commands)
        self.assertEqual(run.boards[1].board.commands.count("SwStopAcquisition"), 1)    # directly: not the master
        self.assertTrue(any("still sending data" in w for w in run.warnings))

    def test_a_failed_board_stops_the_run(self):
        """D5: the main loop also stops for a board that ended with an error (its state could be overwritten)."""
        run, d = make_run("fix_failed_board", two_boards(seconds=4))
        for br in run.boards:
            br.board, br.state = FakeBoard(), "running"
        run.boards[1].ended = "error"
        run.t_start, run.mono_start = time.time(), time.monotonic()
        with captured():
            t0 = time.monotonic()
            run.take_data()
        self.assertEqual(run.stop_reason, "a board failed")
        self.assertLess(time.monotonic() - t0, 2)

    def test_a_reader_error_stays_at_the_start(self):
        """D5: arm_and_start() set every board running after the readers started, over the error of a board whose
        first read failed; now only armed boards become running."""
        from vx27webdaq.daq import engine
        run, d = make_run("fix_first_read", two_boards())
        for br in run.boards:
            br.board = FakeBoard()

        class AtOnce:                   # a thread that runs at start(): the reader's error comes before the state change
            def __init__(self, target, args=(), **kw):
                self.target, self.args = target, args

            def start(self):
                self.target(*self.args)

            def is_alive(self):
                return False

        def reader(br):
            if br.k == 1:
                br.state, br.ended = "error", "error"
        with captured(), mock.patch.object(engine.threading, "Thread", AtOnce), mock.patch.object(run, "reader", reader):
            self.assertTrue(run.arm_and_start())
        self.assertEqual([b.state for b in run.boards], ["running", "error"])

    def test_a_board_that_stops_by_itself(self):
        """D6: a board that sends its stop record during the run (before the stop) ended its reader quietly and the
        run went on without it, exit code 0. Now it is an error and the run stops."""
        code, out, status, d = run_daq("fix_self_stop", two_boards(seconds=6), "--rate 40 --seed 3 --fail selfstop:b1:1")
        self.assertEqual(code, 1, out)
        self.assertEqual(status["stop_reason"], "a board stopped by itself")
        self.assertIn("ERROR: b1 stopped by itself during the run", out)
        self.assertLess(status["t"], 5)

    def test_the_end_of_the_s_in_level_ends_the_run(self):
        """D6, D11: a master started by the S-IN level (SINlevel) stops with it: the end of the run, not an error."""
        cfg = two_boards(seconds=20)
        cfg.boards[0].io_start = "SINlevel"
        code, out, status, d = run_daq("fix_sin_level", cfg, "--rate 40 --seed 3 --sin 0.5:2.5")
        self.assertEqual(code, 0, out)
        self.assertEqual(status["stop_reason"], "the start signal of the master ended (SINlevel)")
        self.assertEqual([b["ended"] for b in status["boards"]], ["stop record", "stop record"])
        self.assertLess(status["t"], 10)

    def test_a_second_stop_before_the_stop(self):
        """D7: a second SIGTERM before the stop command went out made the idle readers end at once: no stop records,
        the end of the data lost. A second Stop counts only during the stop (then: do not wait)."""
        run, d = make_run("fix_double_stop", two_boards())
        run.on_signal(signal.SIGTERM, None)
        run.on_signal(signal.SIGTERM, None)                     # twice before the stop command
        self.assertEqual((run.stop_requested, run.kill_requested), ("SIGTERM", False))
        br = run.boards[0]
        br.board = FakeBoard()                                  # quiet: every read gives nothing
        run.kill_requested = True                               # even so: before the stop command no reader ends
        reader = threading.Thread(target=run.reader, args=(br,), daemon=True)
        with captured():
            reader.start()
            time.sleep(0.4)
            self.assertTrue(reader.is_alive())
            run.stopping_since = time.monotonic()               # the stop command is out: now it ends at once
            reader.join(2)
        self.assertFalse(reader.is_alive())
        self.assertEqual(br.ended, "time-out")
        run.kill_requested = False
        run.on_signal(signal.SIGTERM, None)                     # a second Stop during the stop: do not wait
        self.assertTrue(run.kill_requested)

    def test_sighup_is_a_stop(self):
        """D17: SIGHUP (the wrapper passes it on) killed the DAQ without a stop; it is a Stop like SIGTERM."""
        p, d = popen_daq("fix_sighup", two_boards(seconds=0, setup="self"), "--rate 2 --seed 4")
        time.sleep(0.5)
        p.send_signal(signal.SIGHUP)
        out, _ = p.communicate(timeout=60)
        self.assertEqual(p.returncode, 0, out)
        st = status_of(d)
        self.assertEqual((st["phase"], st["stop_reason"]), ("finished", "Stop (SIGHUP)"))

    def test_close_all_goes_on_after_a_failure(self):
        """D9: one error in close_all() (the last write of a data file) left the boards after it armed and open."""
        run, d = make_run("fix_close_all", two_boards())
        for br in run.boards:
            br.board = FakeBoard()

        class BadFile:
            def close(self):
                raise OSError(5, "Input/output error")
        run.boards[0].writer, run.boards[0].hk_out = BadFile(), BadFile()
        with captured() as out:
            run.close_all()
        self.assertEqual([(b.board.closed, b.board.commands) for b in run.boards],
                         [(True, ["DisarmAcquisition"]), (True, ["DisarmAcquisition"])])
        self.assertIn("b0: cannot close its data file", out.getvalue())
        self.assertEqual(run.boards[0].ended, "error")         # its last data may be lost: the run failed

    def test_the_end_of_the_run_is_always_written(self):
        """D9: an exception in close_all() skipped the summary and the final phase (status.json stayed "stopping")."""
        from vx27webdaq.daq import engine
        run, d = make_run("fix_final_phase", two_boards(seconds=1))
        with captured() as out, mock.patch.object(engine.Run, "close_all", side_effect=RuntimeError("boom")):
            code = run.main()
        self.assertEqual(code, 1)
        self.assertIn("run run00007 FAILED", out.getvalue())
        self.assertEqual(status_of(d)["phase"], "failed")

    def test_the_run_time_counts_from_the_hardware_start(self):
        """D11: with a hardware start the run time counted from the arming, so it included the wait for S-IN. Now it
        counts from the master's first data."""
        cfg = two_boards(seconds=2)
        cfg.boards[0].io_start = "SINedge"
        code, out, status, d = run_daq("fix_hw_start", cfg, "--rate 40 --seed 3 --sin 1.5")
        self.assertEqual(code, 0, out)
        self.assertIn("waiting for the start signal", out)
        self.assertIn("run started: the master saw its start signal (SINedge)", out)
        self.assertGreater(status["boards"][0]["last_timestamp"] * 8e-9, 1.7)     # two seconds of data, not 0.5

    def test_a_stop_before_the_hardware_start(self):
        """D11: Stop before the start signal came: a normal stop, and the boards without data are not blamed on
        their CLK-IN cable."""
        from vx27webdaq.daq import engine
        cfg = two_boards(seconds=0)
        cfg.boards[0].io_start = "SINedge"
        run, d = make_run("fix_no_hw_start", cfg, "--rate 40 --sin 100")
        timer = threading.Timer(1.0, lambda: setattr(run, "stop_requested", "SIGTERM"))
        with captured() as out, mock.patch.object(engine, "STOP_WAIT_S", 0.5):
            timer.start()
            code = run.main()
        log = out.getvalue()
        self.assertEqual(code, 1)
        self.assertEqual(log.count("the master's start signal (SINedge) never came before the stop"), 2, log)
        self.assertNotIn("CLK-IN cable", log)
        self.assertEqual(status_of(d)["stop_reason"], "Stop (SIGTERM)")

    def test_live_files_and_kept_events_are_capped(self):
        """D12: a whole event went into every live file and 16 into the combined view's memory, whatever its size
        (up to 800 MB). Now a live file holds at most LIVE_MAX_BYTES (the first samples of every channel) and the
        combined view keeps no event above PICK_MAX_BYTES, at most PICKS_MAX_BYTES per board."""
        from vx27webdaq.daq import engine
        run, d = make_run("fix_caps", two_boards())
        br = run.boards[0]
        os.makedirs(run.live_dir, exist_ok=True)
        waves = (np.arange(64 * 1024, dtype=np.uint16) % 997).reshape(64, 1024)
        big = R.encode_scope(5, 7000, list(range(64)), waves)           # 128 kB
        with captured() as out, mock.patch.object(engine, "LIVE_MAX_BYTES", 16 << 10, create=True):
            run.write_live(br, big, 0)
            run.write_live(br, big, 0)
        with open(os.path.join(run.live_dir, "b0.evt"), "rb") as f:
            data = f.read()
        self.assertLessEqual(len(data), 16 << 10)
        ev = R.decode_scope(data)
        self.assertEqual((ev.trigger_id, ev.timestamp, ev.channels), (5, 7000, list(range(64))))
        self.assertTrue(np.array_equal(ev.waveforms, waves[:, :ev.samples]))
        self.assertGreater(ev.samples, 100)
        self.assertEqual(out.getvalue().count("the live view shows the first"), 1)
        # the combined view: events above the cap are not kept, and a board keeps at most PICKS_MAX_BYTES
        stream = b"".join(R.encode_scope(k, k << engine.GRID_SHIFT, [0, 1], np.zeros((2, 256), np.uint16)) for k in range(10))
        offs = []
        R.scan(stream, offsets=offs)
        words = np.frombuffer(stream, dtype=">u8")
        idx = np.asarray(offs) // 8
        with captured() as out, mock.patch.object(engine, "PICK_MAX_BYTES", 512, create=True):
            run.pick_cells(br, stream, offs, words[idx], words[idx + 1])
        self.assertEqual(len(br.picks), 0)
        self.assertEqual(out.getvalue().count("the combined live view keeps no events this large"), 1)
        br.cell = None
        with captured(), mock.patch.object(engine, "PICKS_MAX_BYTES", 3 * len(stream) // 10, create=True):
            run.pick_cells(br, stream, offs, words[idx], words[idx + 1])
        self.assertEqual([p[0] for p in br.picks], [7, 8, 9])

    def test_the_combined_view_after_a_timestamp_reset(self):
        """D13: after a timestamp reset (S-IN, GPIO) the timestamp cells are smaller again: they were not kept, and
        the combined view showed the last moment before the reset for good."""
        run, d = make_run("fix_reset_cells", two_boards())
        os.makedirs(run.live_dir, exist_ok=True)
        from vx27webdaq.daq import engine

        def feed(br, cells):
            stream = b"".join(R.encode_scope(c, (c << engine.GRID_SHIFT) + 5, [0], np.full((1, 4), c, np.uint16))
                              for c in cells)
            offs = []
            R.scan(stream, offsets=offs)
            words = np.frombuffer(stream, dtype=">u8")
            idx = np.asarray(offs) // 8
            run.pick_cells(br, stream, offs, words[idx], words[idx + 1])

        def shown():
            with open(os.path.join(run.live_dir, "combined.set"), "rb") as f:
                info, events = R.unpack_set(f.read())
            return info["cell"], sorted(e.trigger_id for e in events.values())
        for br in run.boards:
            feed(br, [100, 101])
        run.write_set()
        self.assertEqual(shown(), (101, [101, 101]))
        for br in run.boards:                                   # the reset: the timestamps start again
            feed(br, [0, 1])
        run.write_set()
        self.assertEqual(shown(), (1, [1, 1]))

    def test_a_stop_before_the_boards_start(self):
        """D14: a SIGTERM while the boards were connected or configured was seen only after they had been started.
        Now the run ends without starting them."""
        from vx27webdaq.daq import engine
        run, d = make_run("fix_stop_early", two_boards())
        real = engine.Run.check_board

        def check(self, br):
            self.stop_requested = "SIGTERM"                     # Stop while the first board is being connected
            return real(self, br)
        with captured() as out, mock.patch.object(engine.Run, "check_board", check):
            code = run.main()
        log = out.getvalue()
        self.assertEqual(code, 1)
        self.assertIn("Stop (SIGTERM) before the boards were started", log)
        self.assertNotIn("armed", log)
        self.assertNotIn("run started", log)
        self.assertEqual(status_of(d)["stop_reason"], "Stop (SIGTERM) before the start")
        self.assertEqual(os.listdir(os.path.join(d, "raw")), [])

    def test_status_files_are_strict_json(self):
        """The DAQ program wrote NaN into status.json (a reading a board could not make). JSON has no NaN, and a
        strict reader (the browser's JSON.parse) refuses the whole file for one: such numbers are written as null."""
        from vx27webdaq.daq import engine
        path = os.path.join(folder("fix_status_nan"), "status.json")
        engine.write_json(path, {"boards": [{"temp": float("nan"), "rate": np.float64("inf"), "n": 3,
                                             "dt": (1.5, -float("inf")), "name": "b0"}]})

        def refuse(name):
            raise ValueError(f"{name} in status.json")
        with open(path, encoding="utf-8") as f:
            data = json.loads(f.read(), parse_constant=refuse)
        self.assertEqual(data, {"boards": [{"temp": None, "rate": None, "n": 3, "dt": [1.5, None], "name": "b0"}]})


if __name__ == "__main__":
    unittest.main()
