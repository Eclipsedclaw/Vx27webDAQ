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

    def test_lab_setup(self):
        """The VX2730 (16 inputs) self-triggers as the master; the VX2740s take its trigger on TRG-IN and pass it on."""
        cfg = C.lab_setup()
        self.assertEqual([(b.model, len(b.channels), b.uri) for b in cfg.boards],
                         [("VX2730", 16, "dig2://192.168.0.72"), ("VX2740", 64, "dig2://192.168.0.75"),
                          ("VX2740", 64, "dig2://192.168.0.85")])
        self.assertEqual([p for p in C.check_config(cfg, check_paths=False) if p.level == "error"], [])
        self.assertEqual({round(b.record_length * C.sample_ns(b)) for b in cfg.boards}, {4096})     # one window
        v = [self.values(cfg, k) for k in range(3)]
        self.assertEqual([(x["/par/AcqTriggerSource"], x["/par/TrgOutMode"]) for x in v],
                         [("ITLA", "ITLA"), ("TrgIn", "TrgIn"), ("TrgIn", "Disabled")])
        self.assertEqual(C.force_targets(cfg), [])        # ITLA on TRG-OUT: a forced trigger would reach only the master
        self.assertEqual(C.live_setting(cfg.boards[0], 3, "threshold", 80), ("/ch/3/par/TriggerThr", "-80"))
        self.assertEqual(C.live_setting(cfg.boards[0], 3, "dc_offset", 61.25), ("/ch/3/par/DCOffset", "61.25"))
        with self.assertRaises(C.ConfigError):
            C.live_setting(cfg.boards[0], 16, "threshold", 80)       # a VX2730 of 16 inputs
        self.assertEqual((v[0]["/ch/0..15/par/TriggerThr"], v[0]["/ch/0..15/par/ITLConnect"]), ("-100", "ITLA"))
        self.assertNotIn("/par/ITLAMainLogic", v[1])                       # only the master triggers on channels
        self.assertFalse(any("TriggerThr" in p for p in v[2]))
        self.assertEqual([C.triggers_on_channels(cfg, k) for k in range(3)], [True, False, False])
        cfg.trigger.master_out = "AcceptTrg"
        self.assertEqual(self.values(cfg, 0)["/par/TrgOutMode"], "AcceptTrg")
        self.assertEqual((self.values(cfg, 0)["/par/AcqTriggerSource"], C.force_targets(cfg)), ("ITLA|SwTrg", [0]))
        # a board that says it has 32 channels where the configuration has 16
        where = {p.where for p in C.check_config(cfg, check_paths=False, facts={"vx2730": {"model": "VX2730", "channels": 32}})}
        self.assertIn("boards[0].n_channels", where)
        d = C.to_dict(cfg)
        d["boards"][0]["n_channels"] = 32
        self.assertEqual(len(C.from_dict(d).boards[0].channels), 32)
        d["boards"][0]["n_channels"] = 65
        with self.assertRaises(C.ConfigError):
            C.from_dict(d)

    def test_models(self):
        self.assertEqual(models.family("VX2740"), "2740")
        self.assertEqual(models.family("DT2730B"), "2730")
        self.assertIsNone(models.family("V1725"))
        m = models.from_board({"model": "V2730", "channels": 32, "adc_bits": 14, "adc_msps": 500.0})
        self.assertEqual((m.channels, m.adc_max, m.sample_ns), (32, 16383, 2.0))


class GeneratorTests(unittest.TestCase):
    def test_staircase_and_train(self):
        """The simulator's pulse generator: regular hits, a full pulse on every channel, negative on a V2730 and
        positive on a V2740; staircase: channel k k x 1 us after the trigger, again from the trigger when the
        record is full; train: a pulse every 1 us on every channel."""
        from alab_daq.daq.sim import Crate
        cr = Crate("--generator staircase --rate 10", "none", {"a": "VX2730", "b": "VX2740"}, {"a": 16, "b": 64})
        a, b = cr.open("x", "a"), cr.open("y", "b")
        for board, rec, pre in ((a, 2048, 512), (b, 512, 128)):
            board.set("/par/RecordLengthS", str(rec))
            board.set("/par/PreTriggerS", str(pre))
        self.assertEqual([round(t, 3) for t, _ in cr.hits(0.0, 0.5)], [0.1, 0.2, 0.3, 0.4, 0.5])
        wa, wb = a.waveforms([0, 1, 3, 4], 1, 2048), b.waveforms([0, 1, 5], 1, 512)
        start_a = [int(np.argmax(w[:16].mean() - w > 100)) for w in wa]        # where each pulse begins
        start_b = [int(np.argmax(w - w[:16].mean() > 100)) for w in wb]
        self.assertEqual([s - start_a[0] for s in start_a], [0, 500, 1500, 0])  # 1 us = 500 samples of 2 ns
        self.assertEqual([s - start_b[0] for s in start_b], [0, 125, 125])      # = 125 samples of 8 ns; ch5 like ch1
        self.assertLess(wa[0].min() - wa[0][:16].mean(), -500)                  # negative on the V2730
        self.assertGreater(wb[0].max() - wb[0][:16].mean(), 500)                # positive on the V2740
        tr = Crate("--generator train --rate 10", "none", {"b": "VX2740"}).open("y", "b")
        tr.set("/par/RecordLengthS", "512")
        tr.set("/par/PreTriggerS", "128")
        w = tr.waveforms([7], 1, 512)[0].astype(float) - 32768
        rises = np.nonzero((w[1:] > 300) & (w[:-1] <= 300))[0]
        self.assertEqual(len(rises), 4)                                       # 0, 1, 2, 3 us after the trigger


class SyncCheckTests(unittest.TestCase):
    def test_counts(self):
        from alab_daq.daq.sync import SyncCheck
        ids = np.arange(100, dtype=np.int64)
        s = SyncCheck(["m", "a", "b"], 1000)
        a_ids = np.delete(ids, 50)                              # a lacks trigger 50
        a_ts = a_ids * 1000
        a_ts[a_ids == 60] += 500                               # and its trigger 60 is 4 µs late
        s.add(0, ids[:40], ids[:40] * 1000)
        s.add(1, a_ids[:30], a_ts[:30])
        s.add(2, ids, ids * 1000 + 3)                          # b: always 3 ticks (24 ns) later: the cabling
        s.process()
        self.assertEqual(s.checked, 30)                        # only the IDs every board has passed
        s.add(0, ids[40:], ids[40:] * 1000)
        s.add(1, a_ids[30:], a_ts[30:])
        s.process(final=True)
        v = s.view()
        self.assertEqual((v["checked"], v["built"], v["out_of_sync"]), (100, 98, 1))
        self.assertEqual(v["missing"], {"m": 0, "a": 1, "b": 0})
        self.assertEqual(v["offsets"]["b"]["mean_ns"], 24.0)
        self.assertEqual(v["first_bad"]["trigger_id"], 60)
        self.assertIn("missing: a 1", s.summary())


def plan_values(cfg: C.DaqConfig, k: int) -> dict:
    """path -> value of what plan() gives board k (the last setting of a path wins, as on the board)."""
    return {s.path: s.value for s in C.plan(cfg, k)}


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

    def test_master_trigger(self):
        """The lab's setup: the master's self-trigger, on TRG-OUT -> TRG-IN, makes every board record the same moments."""
        for out in ("ITLA", "AcceptTrg"):
            cfg = small(C.lab_setup())
            cfg.trigger.master_out = out
            code, log, status, d = run_daq(f"master_{out}", cfg)
            self.assertEqual(code, 0, log)
            heads = {}
            for name, path in self.files(d).items():
                with R.RawFile(path) as f:
                    heads[name] = f.headers()
                    if "vx2730" in name:
                        self.assertEqual(f.header["board"]["channels"], 16)
                        self.assertEqual(f.event(0).channels, [0, 1, 2, 3])
            self.assertEqual(sorted(heads), ["run00007_vx2730.raw", "run00007_vx2740a.raw", "run00007_vx2740b.raw"])
            lines = log.splitlines()                         # three reader threads log at once: whole lines only
            self.assertNotIn("", lines)
            self.assertEqual([l.count(": data file ") for l in lines if ": data file " in l], [1, 1, 1])
            master = heads["run00007_vx2730.raw"]
            self.assertGreater(len(master["trigger_id"]), 10)
            for name, h in heads.items():
                self.assertTrue(np.array_equal(h["trigger_id"], master["trigger_id"]), (out, name))
                self.assertTrue(np.array_equal(h["timestamp"], master["timestamp"]), (out, name))

    def test_combined_set(self):
        """The live set of matched events: one event of every board from the same moment, and the combined view
        of the run's files pairs the boards by trigger ID."""
        from alab_daq.web.files import DataFiles, combined_view
        cfg = small(C.lab_setup(), seconds=3)
        code, log, status, d = run_daq("combined", cfg, options="--rate 30 --seed 11")
        self.assertEqual(code, 0, log)
        with open(os.path.join(d, "live", "combined.set"), "rb") as f:
            info, events = R.unpack_set(f.read())
        self.assertEqual(sorted(events), ["vx2730", "vx2740a", "vx2740b"])
        self.assertTrue(info["chained"])
        self.assertEqual(len({e.trigger_id for e in events.values()}), 1)
        self.assertEqual(len({e.timestamp for e in events.values()}), 1)
        files = DataFiles(lambda: [("data", d)])
        path = self.files(d)["run00007_vx2740a.raw"]
        traces = [("vx2730", 0), ("vx2740a", 1), ("vx2740b", 2), ("vx2740b", 40)]
        view = files.combined(path, 5, traces, None, None, 400, "mv", "trigger")
        self.assertTrue(view["matched"])
        self.assertEqual(sorted(x["name"] for x in view["available"]), ["vx2730", "vx2740a", "vx2740b"])
        self.assertEqual(len({b["trigger_id"] for b in view["boards"]}), 1)
        self.assertTrue(view["same_ids"])
        self.assertEqual(view["spread_ns"], 0)
        by = {(t["board"], t["ch"]): t for t in view["traces"]}
        self.assertTrue(by[("vx2730", 0)]["present"])
        self.assertEqual(by[("vx2740b", 40)]["why"], "not read out")
        # the VX2730 records 256 samples of 2 ns, 64 before the trigger; a VX2740 64 of 8 ns, 16 before
        self.assertEqual(by[("vx2730", 0)]["x0_ns"], -128.0)
        self.assertEqual(by[("vx2740a", 1)]["x0_ns"], -128.0)
        self.assertEqual(by[("vx2740a", 1)]["step_ns"], 8.0)
        self.assertEqual(view["range_ns"], [-128.0, 384.0])
        # mV from the baseline: the baseline sits near 0, one count of a 16-bit board is 2000/65536 mV
        b = {x["name"]: x for x in view["boards"]}
        self.assertAlmostEqual(b["vx2740a"]["mv_per_adc"], 2000 / 65536)
        self.assertAlmostEqual(b["vx2730"]["mv_per_adc"], 2000 / 16384)
        # (the baseline is the mean of the first 16 samples; this short record has only 16 before the trigger, so a
        # pulse's first samples can shift it by about 1 mV, against pulses of about 18 mV)
        self.assertLess(abs(np.mean(by[("vx2740a", 1)]["y"][:8])), 3.0)
        raw = files.combined(path, 5, traces, -50, 50, 400, "raw", "timestamp")
        t = {(x["board"], x["ch"]): x for x in raw["traces"]}[("vx2730", 0)]
        self.assertGreater(min(t["y"]), 100)            # raw ADC counts: around the DC offset
        self.assertLessEqual(t["x0_ns"], -50)
        self.assertLess(len(t["y"]), 60)                # only the window asked for
        files.close_all()
        # the set of a single event: each board alone
        ev = R.decode_scope(R.encode_scope(3, 1000, [0, 1], np.full((2, 8), 100, np.uint16)))
        one = combined_view([{"name": "x", "info": {"sample_ns": 8.0, "pre_trigger": 2, "record_length": 8,
                                                    "model": "V2740", "adc_max": 65535}, "ev": ev, "enabled": [True, True]}],
                            [("x", 1)], None, None, 100, "adc", "trigger")
        self.assertEqual(one["traces"][0]["y"], [0.0] * 8)

    def test_housekeeping_and_sync(self):
        """Housekeeping every second into RUN_BOARD_hk.csv (and the ROOT file), the error-flag warning, and the
        sync check of the master trigger: every trigger on every board, at the same timestamp."""
        from alab_daq.core import rootconv
        cfg = small(C.lab_setup(), seconds=3)
        cfg.run.hk_s = 1
        code, log, status, d = run_daq("hk_sync", cfg, options="--rate 30 --seed 4 --fail errflags:vx2740a:1")
        self.assertEqual(code, 0, log)
        self.assertIn("vx2740a: the board reports error flags 0x800", log)
        m = status["matching"]
        self.assertTrue(m["applies"])
        self.assertGreater(m["built"], 20)
        self.assertEqual((m["out_of_sync"], set(m["missing"].values())), (0, {0}))
        self.assertEqual(m["built"], m["checked"])
        self.assertEqual({k: v["mean_ns"] for k, v in m["offsets"].items()}, {"vx2740a": 0.0, "vx2740b": 0.0})
        self.assertIn("sync check:", log)
        for b in status["boards"]:
            path = b["hk_file"]
            self.assertEqual(os.path.basename(path), f"run00007_{b['name']}_hk.csv")
            self.assertEqual(os.path.dirname(path), cfg.run.data_dir)       # next to the data
            cols = rootconv.read_hk(path)
            self.assertGreaterEqual(len(cols["time"]), 3)
            for k in ("temp_core", "temp_hottest_adc", "error_flags", "trigger_cnt", "lost_trigger_cnt", "dead_frac", "ch0_trg_rate"):
                self.assertIn(k, cols)
            self.assertEqual(int(cols["trigger_cnt"][-1]), b["events"])          # the last reading: after the stop
            self.assertEqual(b["stats"]["triggers"], b["events"])
            self.assertGreater(b["monitors"]["temp_core"], 50)
        self.assertEqual(int(rootconv.read_hk(status["boards"][1]["hk_file"])["error_flags"][-1]), 0x800)
        raw = self.files(d)["run00007_vx2730.raw"]
        rootconv.convert(raw, raw[:-4] + ".root")
        import uproot
        with uproot.open(raw[:-4] + ".root") as r:
            self.assertEqual(r["hk"].num_entries, len(rootconv.read_hk(status["boards"][0]["hk_file"])["time"]))
            self.assertIn("temp_core", r["hk"].keys())
        # each board triggering by itself: no sync check
        cfg = small(C.default_config())
        cfg.trigger.setup = "self"
        code, log, status, d = run_daq("hk_self", cfg)
        self.assertEqual(code, 0, log)
        self.assertEqual(status["matching"], {"applies": False, "why": "each board triggers by itself"})

    def test_control_during_the_run(self):
        """The server's commands during a run: a Force trigger reaches every board (the master sends AcceptTrg),
        a channel's DC offset changes, anything else is refused."""
        cfg = small(C.lab_setup(), seconds=0)
        cfg.trigger.master_out = "AcceptTrg"
        d = folder("control")
        cfg.run.data_dir = os.path.join(d, "raw")
        os.makedirs(cfg.run.data_dir)
        C.save_config(cfg, os.path.join(d, "config.json"), run_name="run00007")
        env = dict(os.environ, PYTHONPATH=str(PROJECT / "src"), OMP_NUM_THREADS="1", PYTHONUNBUFFERED="1")
        p = subprocess.Popen([sys.executable, "-m", "alab_daq.daq", "run", d, "--simulate", "--rate 5 --seed 2"], cwd=d,
                             env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            status = os.path.join(d, "status.json")
            for _ in range(200):
                time.sleep(0.05)
                try:
                    if json.load(open(status))["phase"] == "running":
                        break
                except (OSError, ValueError, KeyError):
                    pass

            def ask(n, req):
                with open(os.path.join(d, "control", f"{n}.json.part"), "w") as f:
                    json.dump(req, f)
                os.replace(os.path.join(d, "control", f"{n}.json.part"), os.path.join(d, "control", f"{n}.json"))
                for _ in range(100):
                    time.sleep(0.05)
                    try:
                        return json.load(open(os.path.join(d, "control", f"{n}.done")))
                    except (OSError, ValueError):
                        pass
                self.fail(f"no answer to {req}")
            r = ask("001", {"cmd": "swtrigger", "user": "tester"})
            self.assertEqual((r["ok"], r["boards"]), (True, ["vx2730"]))
            r = ask("002", {"cmd": "set", "user": "tester", "board": "vx2730", "path": "/ch/0/par/DCOffset", "value": "61.5"})
            self.assertEqual((r["ok"], r["value"]), (True, "61.500"))
            r = ask("003", {"cmd": "set", "user": "tester", "board": "vx2730", "path": "/par/RecordLengthS", "value": "64"})
            self.assertFalse(r["ok"])
            time.sleep(0.5)
        finally:
            p.send_signal(signal.SIGTERM)
            out, _ = p.communicate(timeout=60)
        self.assertEqual(p.returncode, 0, out)
        self.assertIn("Force trigger (tester): software trigger to vx2730", out)
        self.assertIn("changed during the run (tester): vx2730 /ch/0/par/DCOffset = 61.5", out)
        st = json.load(open(os.path.join(d, "status.json")))
        self.assertEqual([(c["board"], c["path"], c["ok"]) for c in st["changes"]], [("vx2730", "/ch/0/par/DCOffset", True)])
        m = st["matching"]                                     # the forced trigger is on every board too
        self.assertEqual((m["out_of_sync"], set(m["missing"].values())), (0, {0}))
        counts = {b["name"]: b["events"] for b in st["boards"]}
        self.assertEqual(len(set(counts.values())), 1, counts)

    def test_trigger_logic_b_and_front_panel(self):
        """Two channel logics (ITLA, ITLB) combined, and the front panel of a board: in the plan, in the check, and
        a run whose board refuses an optional test signal (it warns and goes on)."""
        cfg = small(C.lab_setup(), n_ch=16, seconds=2)
        cfg.trigger.combine = "A_OR_B"
        cfg.boards[0].channels[15].itl = "B"                       # group B: channel 15 alone
        v = plan_values(cfg, 0)
        self.assertEqual((v["/par/AcqTriggerSource"], v["/par/TrgOutMode"]), ("ITLA_OR_ITLB", "ITLA_OR_ITLB"))
        self.assertEqual((v["/ch/0..14/par/ITLConnect"], v["/ch/15/par/ITLConnect"], v["/par/ITLBMainLogic"]), ("ITLA", "ITLB", "OR"))
        cfg.boards[2].io_trg_out = "TestPulse"                    # the end of the chain: TRG-OUT is free
        cfg.boards[1].io_dac_out = "Square"
        cfg.boards[0].io_tstamp_reset = "SIN"
        v2 = plan_values(cfg, 2)
        self.assertEqual((v2["/par/TrgOutMode"], v2["/par/TestPulsePeriod"]), ("TestPulse", str(cfg.trigger.test_pulse_period_ns)))
        self.assertEqual(plan_values(cfg, 1)["/par/DACoutMode"], "Square")
        self.assertEqual([p for p in C.check_config(cfg, check_paths=False) if p.level != "info"], [])
        bad = cfg.copy()
        bad.boards[1].io_trg_out = "Busy"                          # it carries the chain's trigger
        bad.boards[1].io_start = "SINedge"                         # only the master starts
        bad.boards[2].io_dac_out = "Nonsense"
        found = {(p.level, p.where) for p in C.check_config(bad, check_paths=False)}
        self.assertTrue({("warning", "boards[1].io_trg_out"), ("error", "boards[1].io_start"), ("error", "boards[2].io_dac_out")} <= found, found)
        code, log, status, d = run_daq("logic_b", cfg, options="--rate 30 --seed 6 --fail param:vx2740a:dacoutmode")
        self.assertEqual(code, 0, log)
        self.assertIn("vx2740a: the board refused /par/DACoutMode = Square", log)
        self.assertGreater(status["boards"][0]["events"], 10)
        self.assertEqual(len({b["events"] for b in status["boards"]}), 1)
        cfg.trigger.combine = "A_AND_B"                           # both groups at once: fewer triggers
        code, log, status_and, d = run_daq("logic_ab", cfg, options="--rate 30 --seed 6")
        self.assertEqual(code, 0, log)
        self.assertLess(status_and["boards"][0]["events"], status["boards"][0]["events"])

    def test_event_building(self):
        """Every board of a run in one ROOT file, one entry per trigger of the first board."""
        import uproot
        from alab_daq.core import eventbuild
        cfg = small(C.lab_setup(), seconds=2)
        code, log, status, d = run_daq("built", cfg, options="--rate 30 --seed 8")
        self.assertEqual(code, 0, log)
        files = self.files(d)
        out = os.path.join(d, "raw", "run00007_built.root")
        self.assertEqual(eventbuild.built_name(files["run00007_vx2740b.raw"]), out)
        env = dict(os.environ, PYTHONPATH=str(PROJECT / "src"), OMP_NUM_THREADS="1")
        p = subprocess.run([sys.executable, "-m", "alab_daq.core.eventbuild", files["run00007_vx2740b.raw"]], env=env,
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        n = status["boards"][0]["events"]
        self.assertIn(f"check: {n} triggers of vx2730, {n} entries", p.stdout)
        with uproot.open(out) as r:
            t = r["events"]
            a = t.arrays(["trigger_id", "complete", "n_boards", "vx2740a_dt_ns", "vx2730_ch0", "vx2740b_ch3"], library="np")
            settings = json.loads(str(r["settings"]))
        self.assertEqual(len(a["trigger_id"]), n)
        self.assertTrue(a["complete"].all() and (a["n_boards"] == 3).all())
        self.assertFalse(a["vx2740a_dt_ns"].any())                   # one clock: the same timestamps
        self.assertEqual((a["vx2730_ch0"].shape[1], a["vx2740b_ch3"].shape[1]), (256, 64))
        self.assertEqual((settings["format"], settings["reference"], settings["window_ns"]), ("alab-daq-built", "vx2730", 1000))
        with R.RawFile(files["run00007_vx2740b.raw"]) as f:          # the samples are the board's own
            self.assertTrue(np.array_equal(a["vx2740b_ch3"][5], f.event(5).waveform(3)))

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



class RepositoryTests(unittest.TestCase):
    """Only the code, the scripts and their documents go into git (and to GitHub): no data, ever."""

    ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    SUFFIXES = (".py", ".js", ".css", ".html", ".svg", ".sh", ".md", ".toml", ".lock", ".txt")
    OTHERS = {".gitignore", "src/alab_daq/web/static/i18n/zh.json", "src/alab_daq/web/static/vendor/uplot/LICENSE"}

    def git(self, *args, stdin=None):
        return subprocess.run(["git", "-C", self.ROOT, "--no-optional-locks", *args], input=stdin,
                              capture_output=True, text=True)

    def setUp(self):
        if self.git("rev-parse", "--is-inside-work-tree").stdout.strip() != "true":
            self.skipTest("not a git repository")

    def test_only_code_is_tracked(self):
        files = self.git("ls-files").stdout.split()
        self.assertGreater(len(files), 30)
        self.assertEqual([f for f in files if not f.endswith(self.SUFFIXES) and f not in self.OTHERS], [])
        self.assertEqual([f for f in files if os.path.getsize(os.path.join(self.ROOT, f)) > 200_000], [])

    def test_data_are_ignored(self):
        data = ["run00001_b0.raw", "elsewhere/run00001_b0_001.raw", "x/run00001_b0.root", "a.root.part", "live/b0.evt",
                "data/run00001_b0.json", "runs/20261107_141418/config.json", "my_config.json", "ALAB_DAQ_data/a.json",
                "ALAB_DAQ_runs/b/run.json", "status.json", "daq.log", "x.wbin", "caen/lib/libCAEN_Dig2.so",
                "wheels/numpy.whl", "ALAB_ENV/bin/python", ".test_tmp/core/a.raw", "src/alab_daq/__pycache__/a.pyc"]
        out = self.git("check-ignore", "--no-index", "--stdin", stdin="\n".join(data)).stdout.split()
        self.assertEqual(sorted(set(data) - set(out)), [])
        code = ["src/alab_daq/web/static/i18n/zh.json", "src/alab_daq/core/rawdata.py", "tests/test_core.py", "setup.sh",
                "README.md", "src/alab_daq/web/static/vendor/uplot/uPlot.iife.min.js"]
        self.assertEqual(self.git("check-ignore", "--no-index", "--stdin", stdin="\n".join(code)).stdout.split(), [])


if __name__ == "__main__":
    unittest.main()
