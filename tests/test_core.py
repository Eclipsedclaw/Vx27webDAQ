"""No web: the raw data format, the configuration and the parameters it gives each board (CAEN's sync recipe),
whole runs of the DAQ program with simulated boards (every trigger setup, failures), and ROOT files.

Everything the tests write stays in .test_tmp/core/<test>.
"""

import json
import os
import shutil
import signal
import struct
import subprocess
import sys
import time
import unittest

import numpy as np

from alab_daq.core import config as C
from alab_daq.core import models, rawdata as R
from alab_daq.web.settings import PROJECT

TMP = os.environ.get("ALAB_DAQ_TEST_TMP", os.path.join(os.path.dirname(__file__), "..", ".test_tmp"))


def folder(name: str) -> str:
    d = os.path.abspath(os.path.join(TMP, "core", name))
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    return d


class RawFormatTests(unittest.TestCase):
    def test_scope_event_round_trip(self):
        """The bit layout of caen-dig2's scope.cpp: header fields, the channel mask, 4 samples per word."""
        rng = np.random.default_rng(1)
        for chans, n in (([0], 4), ([0, 1, 2, 3], 16), ([5, 17, 31, 32, 63], 1024), (list(range(64)), 8)):
            w = rng.integers(0, 65536, size=(len(chans), n), dtype=np.uint16)
            data = R.encode_scope(0xABCDEF, (1 << 48) - 5, chans, w, flags=0x1FFF, samples_overlapped=5, board_fail=True)
            self.assertEqual(len(data), 8 * (3 + len(chans) * n // 4))
            fmt, n_words = R.header_word(data)
            self.assertEqual((fmt, n_words * 8), (R.FORMAT_SCOPE, len(data)))
            e = R.decode_scope(data)
            self.assertEqual((e.trigger_id, e.timestamp, e.flags, e.samples_overlapped, e.board_fail),
                             (0xABCDEF, (1 << 48) - 5, 0x1FFF, 5, True))
            self.assertEqual(e.channels, chans)
            self.assertTrue(np.array_equal(e.waveforms, w))
        # the first sample sits in the lowest 16 bits of its word; words are big-endian on the wire
        one = R.encode_scope(1, 2, [0], np.array([[1, 2, 3, 4]], dtype=np.uint16))
        (word,) = struct.unpack_from(">Q", one, 24)
        self.assertEqual(word, 1 | 2 << 16 | 3 << 32 | 4 << 48)

    def test_special_events_and_scan(self):
        start = R.encode_start(4096, 0x8000_0000_0000_00FF, decimation=8)
        self.assertEqual(R.decode_special(start), {"kind": "start", "acq_width": 4096, "n_traces": 0, "decimation": 8,
                                                   "ch_mask": 0x8000_0000_0000_00FF})
        stop = R.encode_stop((1 << 48) - 1, 12345)
        self.assertEqual(R.decode_special(stop), {"kind": "stop", "time_tag": (1 << 48) - 1, "dead_time": 12345})
        ev = R.encode_scope(7, 70, [1, 2], np.ones((2, 8), dtype=np.uint16))
        stream = start + ev + ev + stop
        sc = R.scan(stream)
        self.assertEqual((sc.events, sc.end, sc.first_scope, sc.last_scope), (2, len(stream), len(start), len(start) + len(ev)))
        self.assertEqual([s["kind"] for _, s in sc.specials], ["start", "stop"])
        cut = R.scan(stream[:-5])           # a cut buffer: up to the last complete event
        self.assertEqual((cut.events, cut.end, len(cut.specials)), (2, len(stream) - len(stop), 1))
        with self.assertRaises(R.RawFormatError):
            R.scan(b"\x00" * 16)            # a size of 0 words is no event

    def test_files(self):
        """Header, events, a special event between events, splitting into files, a cut file."""
        d = folder("raw_files")
        rng = np.random.default_rng(2)
        chans = [0, 9, 40]
        waves = [rng.integers(0, 16384, size=(3, 12), dtype=np.uint16) for _ in range(30)]
        w = R.RawWriter(os.path.join(d, "run00001_b0"), {"board": {"name": "b0"}, "note": "x"}, max_bytes=2000)
        w.write(R.encode_start(12, R.channels_mask(chans)))
        for k, x in enumerate(waves):
            w.write(R.encode_scope(k, 1000 + 8 * k, chans, x))
            if k == 10:
                w.write(R.encode_start(12, R.channels_mask(chans)))      # another record between events
        w.write(R.encode_stop(5000))
        w.close()
        self.assertGreater(len(w.files), 1)
        self.assertTrue(w.files[1].endswith("run00001_b0_001.raw"))
        events, stops = [], 0
        for path in w.files:
            with R.RawFile(path) as f:
                self.assertEqual(f.header["board"]["name"], "b0")
                self.assertFalse(f.truncated)
                h = f.headers()
                got = np.concatenate([b[3] for b in f.batches(max_bytes=300)]) if len(f) else np.zeros((0, 3, 12))
                self.assertEqual(len(got), len(f))
                for k in range(len(f)):
                    self.assertEqual(int(h["trigger_id"][k]), f.event(k).trigger_id)
                events += list(got)
                stops += f.stop_record() is not None
        self.assertEqual(stops, 1)
        self.assertTrue(np.array_equal(np.stack(events), np.stack(waves)))
        # a file cut in the middle of an event
        with open(w.files[0], "rb") as f:
            data = f.read()
        cut = os.path.join(d, "cut.raw")
        with open(cut, "wb") as f:
            f.write(data[:-7])
        with R.RawFile(cut) as f:
            n_full = len(R.RawFile(w.files[0]).index())
            self.assertEqual((len(f), f.truncated), (n_full - 1, True))
        with self.assertRaises(FileExistsError):
            R.RawWriter(os.path.join(d, "run00001_b0"), {}).write(b"\x00" * 8)
        with self.assertRaises(R.RawFormatError):
            with open(os.path.join(d, "other.raw"), "wb") as f:
                f.write(b"NOTALAB" * 4)
            R.RawFile(os.path.join(d, "other.raw"))


class ConfigTests(unittest.TestCase):
    def chain(self, n=3, setup="self"):
        cfg = C.default_config()
        cfg.boards = [C.Board(f"b{i}", f"dig2://10.0.0.{i + 1}", "V2740" if i % 2 == 0 else "V2730") for i in range(n)]
        for b in cfg.boards:
            b.channels = C.channels_for(b.model)
        cfg.trigger.setup = setup
        return cfg

    def values(self, cfg, k):
        return {s.path: s.value for s in C.plan(cfg, k)}

    def test_round_trip(self):
        cfg = C.default_config()
        cfg.boards[1].channels[3].threshold = 77
        cfg.boards[0].extra = [["/par/Foo", "1"]]
        back = C.from_dict(json.loads(json.dumps(C.to_dict(cfg))))
        self.assertEqual(C.to_dict(back), C.to_dict(cfg))
        self.assertEqual(len(back.boards[0].channels), 64)      # V2740
        self.assertEqual(len(back.boards[1].channels), 32)      # V2730
        d = C.to_dict(cfg)
        d["boards"][0]["model"] = "V2730"                        # a new model: the channels follow it
        self.assertEqual(len(C.from_dict(d).boards[0].channels), 32)
        for bad in ({"run": {"duration_s": "x"}}, {"boards": [{"enabled": "yes"}]}, {"boards": [{"channels": [{"dc_offset": None}]}]},
                    {"boards": {}}, {"boards": [{"extra": [["/par/x"]]}]}, [1, 2]):
            with self.assertRaises(C.ConfigError, msg=str(bad)):
                C.from_dict(bad)

    def test_caen_sync_recipe(self):
        """caen-dig2 demo/caen-felib-demo-sync/main.c, configure_sync(), for a chain of 4 boards."""
        cfg = self.chain(4)
        want = [("Internal", "Run", "SWcmd", "True", 8 * (2 * 3 + 4), "-2148"),
                ("FPClkIn", "SyncIn", "EncodedClkIn", "True", 8 * 4, "-3111"),
                ("FPClkIn", "SyncIn", "EncodedClkIn", "True", 8 * 2, "-3111"),
                ("FPClkIn", "Disabled", "EncodedClkIn", "False", 0, None)]
        for k, (clock, sync, start, out, delay, clk_delay) in enumerate(want):
            v = self.values(cfg, k)
            self.assertEqual((v["/par/ClockSource"], v["/par/SyncOutMode"], v["/par/StartSource"], v["/par/EnClockOutFP"],
                              int(v["/par/RunDelay"]), v.get("/par/VolatileClockOutDelay")),
                             (clock, sync, start, out, delay, clk_delay), f"board {k}")
            self.assertEqual(v["/par/EnAutoDisarmAcq"], "True")
        self.assertEqual(C.start_order(cfg), [0])
        cfg.boards[2].run_delay_ns, cfg.boards[2].clock_out_delay_ps = 100, -2000.5
        v = self.values(cfg, 2)
        self.assertEqual((v["/par/RunDelay"], v["/par/VolatileClockOutDelay"]), ("100", "-2000.5"))
        cfg.run.sync = "independent"                                      # every board on its own
        for k in range(4):
            v = self.values(cfg, k)
            self.assertEqual((v["/par/ClockSource"], v["/par/StartSource"], v["/par/SyncOutMode"]), ("Internal", "SWcmd", "Disabled"))
        self.assertEqual(C.start_order(cfg), [0, 1, 2, 3])
        one = self.chain(1)
        self.assertEqual(self.values(one, 0)["/par/ClockSource"], "Internal")

    def test_trigger_setups(self):
        ext = self.chain(3, "external")
        self.assertEqual([(self.values(ext, k)["/par/AcqTriggerSource"], self.values(ext, k)["/par/TrgOutMode"]) for k in range(3)],
                         [("TrgIn", "TrgIn"), ("TrgIn", "TrgIn"), ("TrgIn", "Disabled")])
        orl = self.chain(2, "logic_or")
        self.assertEqual([(self.values(orl, k)["/par/AcqTriggerSource"], self.values(orl, k)["/par/TrgOutMode"]) for k in range(2)],
                         [("TrgIn", "ITLA"), ("TrgIn", "ITLA")])
        self.assertIn("/par/ITLAMainLogic", self.values(orl, 0))
        per = self.chain(3, "periodic")
        per.trigger.software_target = "chain"
        self.assertEqual([(self.values(per, k)["/par/AcqTriggerSource"], self.values(per, k)["/par/TrgOutMode"]) for k in range(3)],
                         [("SwTrg", "SwTrg"), ("TrgIn", "TrgIn"), ("TrgIn", "Disabled")])
        self.assertEqual(C.software_targets(per), [0])
        per.trigger.software_target = "each"
        self.assertEqual(C.software_targets(per), [0, 1, 2])
        per.trigger.periodic_source = "test_pulse"
        v = self.values(per, 1)
        self.assertEqual((v["/par/AcqTriggerSource"], v["/par/TestPulsePeriod"]), ("TestPulse", str(per.trigger.test_pulse_period_ns)))
        self.assertEqual(C.software_targets(per), [])
        self.assertNotIn("/par/ITLAMainLogic", v)
        self.assertNotIn("/ch/0..63/par/TriggerThr", self.values(ext, 0))        # thresholds only for self-triggers

    def test_channels_in_the_plan(self):
        cfg = self.chain(1, "self")
        b = cfg.boards[0]
        for c in range(64):
            b.channels[c].enabled = c < 8
            b.channels[c].self_trigger = c < 4
            b.channels[c].positive = c == 2
            b.channels[c].threshold = 50
        v = self.values(cfg, 0)
        self.assertEqual((v["/ch/0..7/par/ChEnable"], v["/ch/8..63/par/ChEnable"]), ("True", "False"))
        self.assertEqual((v["/ch/0..1/par/TriggerThr"], v["/ch/2/par/TriggerThr"], v["/ch/3..63/par/TriggerThr"]), ("-50", "50", "-50"))
        self.assertEqual((v["/ch/2/par/SelfTriggerEdge"], v["/ch/0..1/par/SelfTriggerEdge"]), ("Rise", "Fall"))
        self.assertEqual((v["/ch/0..3/par/ITLConnect"], v["/ch/4..63/par/ITLConnect"]), ("ITLA", "Disabled"))
        first = next(s for s in C.plan(cfg, 0) if s.path.endswith("ITLConnect"))
        self.assertEqual((first.alt[0].path, first.alt[0].value), ("/par/ITLAMask", "0xf"))   # older firmware
        self.assertEqual(v["/par/DecimationFactor"], "1")
        cfg.boards[0] = C.Board("b0", "dig2://x", "V2730", channels=C.channels_for("V2730"))
        self.assertNotIn("/par/DecimationFactor", self.values(cfg, 0))     # a 2730 has none

    def test_check(self):
        cfg = C.default_config()
        self.assertEqual([p for p in C.check_config(cfg, check_paths=False) if p.level == "error"], [])
        cfg.boards[1].name = cfg.boards[0].name
        cfg.boards[0].uri = "192.168.0.1"
        cfg.boards[0].pre_trigger = cfg.boards[0].record_length
        cfg.boards[1].decimation = 4                                       # a V2730
        cfg.boards[0].channels[5].threshold = -3
        cfg.boards[0].extra = [["par/x", "1"]]
        cfg.trigger.setup = "self"
        cfg.trigger.logic, cfg.trigger.majority = "Majority", 99
        where = {p.where for p in C.check_config(cfg, check_paths=False) if p.level == "error"}
        for w in ("boards[1].name", "boards[0].uri", "boards[0].pre_trigger", "boards[1].decimation",
                  "boards[0].channels[5].threshold", "boards[0].extra[0]", "trigger.majority"):
            self.assertIn(w, where)
        good = C.default_config()
        good.run.data_dir = "/does/not/exist"
        self.assertIn("run.data_dir", {p.where for p in C.check_config(good)})
        facts = {"b0": {"model": "V2745"}}
        self.assertIn("boards[0].model", {p.where for p in C.check_config(C.default_config(), check_paths=False, facts=facts)})

    def test_models(self):
        self.assertEqual(models.family("VX2740"), "2740")
        self.assertEqual(models.family("DT2730B"), "2730")
        self.assertIsNone(models.family("V1725"))
        m = models.from_board({"model": "V2730", "channels": 32, "adc_bits": 14, "adc_msps": 500.0})
        self.assertEqual((m.channels, m.adc_max, m.sample_ns), (32, 16383, 2.0))


def run_daq(name: str, cfg: C.DaqConfig, options: str = "--rate 40 --seed 5", note: str = "", stop_after=None,
            timeout=60) -> tuple[int, str, dict, str]:
    """One run of the DAQ program with simulated boards, in a folder of its own: (exit code, log, status, folder)."""
    d = folder(name)
    cfg.run.data_dir = os.path.join(d, "raw")
    os.makedirs(cfg.run.data_dir)
    C.save_config(cfg, os.path.join(d, "config.json"), note=note, run_name="run00007")
    env = dict(os.environ, PYTHONPATH=str(PROJECT / "src"), OMP_NUM_THREADS="1", PYTHONUNBUFFERED="1")
    p = subprocess.Popen([sys.executable, "-m", "alab_daq.daq", "run", d, "--simulate", options], cwd=d, env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if stop_after is not None:
        time.sleep(stop_after)
        p.send_signal(signal.SIGTERM)
    out, _ = p.communicate(timeout=timeout)
    with open(os.path.join(d, "status.json"), encoding="utf-8") as f:
        status = json.load(f)
    return p.returncode, out, status, d


def small(cfg: C.DaqConfig, n_ch: int = 4, seconds: int = 2) -> C.DaqConfig:
    cfg.run.duration_s = seconds
    for b in cfg.boards:
        b.record_length = 64 if models.family(b.model) == "2740" else 256
        b.pre_trigger = 16 if models.family(b.model) == "2740" else 64
        for c, ch in enumerate(b.channels):
            ch.enabled = c < n_ch
    return cfg


class DaqRunTests(unittest.TestCase):
    """Whole runs of python -m alab_daq.daq with simulated boards (a few seconds each)."""

    def files(self, d):
        raw = os.path.join(d, "raw")
        return {n: os.path.join(raw, n) for n in sorted(os.listdir(raw)) if n.endswith(".raw")}

    def test_chain_self_trigger(self):
        cfg = small(C.default_config())
        cfg.trigger.setup = "self"
        code, out, status, d = run_daq("chain_self", cfg, note="Cs-137 源 at 10 cm")
        self.assertEqual(code, 0, out)
        self.assertEqual(status["phase"], "finished")
        self.assertEqual([b["ended"] for b in status["boards"]], ["stop record", "stop record"])
        self.assertIn("SwStartAcquisition sent to the master", out)
        files = self.files(d)
        self.assertEqual(sorted(files), ["run00007_b0.raw", "run00007_b1.raw"])
        with R.RawFile(files["run00007_b0.raw"]) as a, R.RawFile(files["run00007_b1.raw"]) as b:
            self.assertGreater(len(a), 10)
            ha, hb = a.headers(), b.headers()
            # the same hits on boards with one clock and one start: the same moments
            common = np.intersect1d(ha["timestamp"], hb["timestamp"])
            self.assertGreater(len(common), 0.8 * min(len(a), len(b)))
            self.assertEqual(a.header["board"]["role"], "master")
            self.assertEqual(b.header["board"]["model"], "V2730")
            self.assertEqual(b.header["board"]["sample_ns"], 2.0)
            self.assertEqual(a.header["note"], "Cs-137 源 at 10 cm")
            self.assertEqual(a.event(0).channels, [0, 1, 2, 3])
            self.assertEqual(b.event(0).samples, 256)
            self.assertIn(["/par/StartSource", "EncodedClkIn"], b.header["applied"])
            self.assertEqual(a.start_record()["acq_width"], 64)
            self.assertIsNotNone(a.stop_record())
        self.assertTrue(os.path.isfile(os.path.join(d, "live", "b0.evt")))

    def test_external_and_logic_or_triggers(self):
        """A common trigger: every board records every trigger, with the same trigger ID and timestamp."""
        for setup in ("external", "logic_or"):
            cfg = small(C.default_config())
            cfg.boards.append(C.Board("b2", "dig2://10.1.1.3", "V2740", channels=C.channels_for("V2740")))
            small(cfg)
            cfg.trigger.setup = setup
            code, out, status, d = run_daq(f"common_{setup}", cfg)
            self.assertEqual(code, 0, out)
            heads = []
            for path in self.files(d).values():
                with R.RawFile(path) as f:
                    heads.append(f.headers())
            self.assertGreater(len(heads[0]["trigger_id"]), 10)
            for h in heads[1:]:
                self.assertTrue(np.array_equal(h["trigger_id"], heads[0]["trigger_id"]), setup)
                self.assertTrue(np.array_equal(h["timestamp"], heads[0]["timestamp"]), setup)

    def test_periodic(self):
        cfg = small(C.default_config())
        cfg.trigger.setup, cfg.trigger.software_rate_hz, cfg.trigger.software_target = "periodic", 20, "chain"
        code, out, status, d = run_daq("periodic_chain", cfg)
        self.assertEqual(code, 0, out)
        counts = [b["events"] for b in status["boards"]]
        self.assertEqual(counts[0], counts[1])
        self.assertGreater(counts[0], 20)
        cfg.trigger.periodic_source, cfg.trigger.test_pulse_period_ns = "test_pulse", 50_000_000     # 20 Hz
        cfg.run.sync = "independent"
        code, out, status, d = run_daq("test_pulse", cfg)
        self.assertEqual(code, 0, out)
        self.assertTrue(all(30 <= b["events"] <= 50 for b in status["boards"]), status["boards"])

    def test_stop_and_max_events(self):
        cfg = small(C.default_config(), seconds=0)
        cfg.trigger.setup = "periodic"
        code, out, status, d = run_daq("sigterm", cfg, stop_after=2.5)
        self.assertEqual(code, 0, out)
        self.assertEqual(status["stop_reason"], "Stop (SIGTERM)")
        cfg.run.max_events = 15
        code, out, status, d = run_daq("max_events", cfg)
        self.assertEqual(code, 0, out)
        self.assertEqual(status["stop_reason"], "15 events on b0")
        self.assertGreaterEqual(status["boards"][0]["events"], 15)

    def test_failures(self):
        cfg = small(C.default_config(), seconds=2)
        cfg.trigger.setup = "self"
        code, out, status, _ = run_daq("fail_open", cfg, "--fail open:b1")
        self.assertEqual((code, status["phase"]), (1, "failed"))
        self.assertIn("b1: cannot connect", out)
        # an older firmware without ITLConnect: ITLAMask instead, and the run is fine
        code, out, status, _ = run_daq("fail_itlconnect", cfg, "--fail param:b0:ITLConnect --rate 40")
        self.assertEqual(code, 0, out)
        self.assertIn("/par/ITLAMask = 0xf", out)
        code, out, status, _ = run_daq("fail_param", cfg, "--fail param:b1:RunDelay")
        self.assertEqual(code, 1)
        self.assertIn("refused /par/RunDelay", out)
        # a slave that did not take the arm command never starts: a warning, no stop record, exit 1
        code, out, status, _ = run_daq("fail_unarmed", cfg, "--fail unarmed:b1", timeout=90)
        self.assertEqual(code, 1)
        self.assertIn("b1 sent no data in this run: it did not start", out)
        self.assertIn("run run00007 FAILED", out)
        self.assertEqual(status["boards"][1]["ended"], "time-out")
        code, out, status, _ = run_daq("fail_read", cfg, "--fail read:b0:5")
        self.assertEqual(code, 1)
        self.assertIn("connection lost", out)
        self.assertEqual(status["stop_reason"], "a board failed")

    def test_board_checks(self):
        """The firmware must be Scope, the model the configuration's (a real board says what it is)."""
        from alab_daq.daq.engine import Run
        d = folder("board_checks")
        C.save_config(small(C.default_config()), os.path.join(d, "config.json"), run_name="run00001")
        run = Run(d)
        br = run.boards[0]
        br.facts = {"model": "V2740", "fw_type": "DPP_PHA", "channels": 64}
        self.assertIn("needs the Scope firmware", run.check_board(br))
        br.facts = {"model": "V2745", "fw_type": "Scope", "channels": 64}
        self.assertIn("the configuration says V2740", run.check_board(br))
        br.facts = {"model": "VX2740", "fw_type": "Scope", "channels": 64}
        self.assertEqual(run.check_board(br), "")
        self.assertEqual(br.model.channels, 64)

    def test_real_board_errors(self):
        """Without libCAEN_FELib.so (or without a board at the address) a real board gives a BoardError, no crash."""
        code = ("from alab_daq.daq.boards import RealBoard, BoardError\n"
                "try:\n    RealBoard('dig2://127.0.0.1')\nexcept BoardError as e:\n    print('BoardError:', e)")
        env = dict(os.environ, PYTHONPATH=str(PROJECT / "src"), LD_LIBRARY_PATH="")
        p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=90)
        self.assertIn("BoardError:", p.stdout, p.stdout + p.stderr)


class RootTests(unittest.TestCase):
    def test_convert_and_check(self):
        from alab_daq.core import rootconv
        import uproot
        cfg = small(C.default_config())
        cfg.trigger.setup = "self"
        code, out, status, d = run_daq("root", cfg, note="first note")
        self.assertEqual(code, 0, out)
        raw = os.path.join(d, "raw", "run00007_b1.raw")
        side = os.path.join(d, "raw", "run00007_b1.json")
        with open(side, "w", encoding="utf-8") as f:
            json.dump({"note": "edited note"}, f)
        out_root = os.path.join(d, "raw", "run00007_b1.root")
        p = subprocess.run([sys.executable, "-m", "alab_daq.core.rootconv", raw, out_root], capture_output=True, text=True,
                           env=dict(os.environ, PYTHONPATH=str(PROJECT / "src")))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("check:", p.stdout)
        with uproot.open(out_root) as r, R.RawFile(raw) as f:
            t = r["events"]
            self.assertEqual(t.num_entries, len(f))
            self.assertEqual(t["ch0"].typename, "uint16_t[256]")
            self.assertEqual(str(r["note"]), "edited note")
            settings = json.loads(str(r["settings"]))
            self.assertEqual(settings["board"]["name"], "b1")
            k = len(f) // 2
            self.assertTrue(np.array_equal(t["ch3"].array(library="np", entry_start=k, entry_stop=k + 1)[0], f.event(k).waveform(3)))
            self.assertTrue(np.array_equal(t["time_ns"].array(library="np"), f.headers()["timestamp"] * np.uint64(8)))
        n_raw, n_root, same, why = rootconv.check(raw, out_root)
        self.assertTrue(same, why)


if __name__ == "__main__":
    unittest.main()
