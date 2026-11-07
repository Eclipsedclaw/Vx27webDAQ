"""The data of a CAEN Digitizer 2.0 board (V2730, V2740, V2745, ...) running the Scope firmware, exactly as the
board sends it (the raw endpoint of CAEN FELib + CAEN Dig2), and the files ALAB DAQ keeps it in.

The stream is a sequence of events made of 64-bit words, big-endian. Every event starts with a header word:
    bits 63-60  format: 1 = a Scope event (common trigger mode), 3 = a special event (run start or stop)
    bits 31-0   the size of the event in words, this word included
A Scope event has 3 header words, then the samples:
    word 1  format (4) | unused (3) | board fail (1) | trigger ID (24) | size in words (32)
    word 2  flags (13) | samples overlapped (3) | timestamp (48, in 8 ns)
    word 3  channel mask (64): the channels in this event
    then, for every 4 samples, one word per channel of the mask (lowest channel first) with 4 samples of 16
    bits, the first sample in the lowest bits. Every channel of an event has the same number of samples.
A special event: word 1 = format (4) | id (4) | unused (16) | extra words n (8) | size in words (32), then n
words of type (8) | data (56). Id 0 is the run start: acquisition width (25 bits), traces (2), log2 of the
decimation (5); channel mask 31-0; channel mask 63-32. Id 2 is the run stop: the time tag (48 bits) and the
dead time (32 bits). The board sends the stop after its last event.
(All from caen-dig2 v1.9: src/endpoints/scope.cpp, events.cpp, raw.cpp, github.com/caenspa/caen-dig2.)

A data file of ALAB DAQ, NAME.raw:
    8 bytes   b"ALABRAW1"
    4 bytes   the length L of the header (unsigned, little-endian), a multiple of 8
    4 bytes   zero
    L bytes   the header: JSON in UTF-8 (the board, its settings, the run), padded with spaces
    then the board's stream as it was read, buffer after buffer.
A file that was cut (a crash, a full disk) ends inside an event: readers stop at the last complete event.

    python -m alab_daq.core.rawdata FILE.raw       what a file holds
"""

from __future__ import annotations

import json
import mmap
from array import array
import os
import struct
import sys
from dataclasses import dataclass, field

import numpy as np

MAGIC = b"ALABRAW1"
PREFIX = 16                     # magic, header length, zero
WORD = 8
FORMAT_SCOPE = 1
FORMAT_SPECIAL = 3
SPECIAL_START = 0
SPECIAL_STOP = 2
SCOPE_HEADER_WORDS = 3
SAMPLES_PER_WORD = 4
TIMESTAMP_NS = 8                # one timestamp unit, on every Digitizer 2.0
MAX_EVENT_WORDS = 1 << 31       # a size larger than this is not an event: the stream is broken


class RawFormatError(ValueError):
    """Bytes that are not a stream of the Scope firmware, or a file that is not an ALAB DAQ data file."""


# ---- one event ----
@dataclass
class ScopeEvent:
    trigger_id: int
    timestamp: int              # in 8 ns
    flags: int
    samples_overlapped: int
    board_fail: bool
    ch_mask: int
    channels: list[int]         # the channels of the mask, lowest first
    waveforms: np.ndarray       # uint16, shape (len(channels), samples)
    size: int = 0               # bytes

    @property
    def samples(self) -> int:
        return int(self.waveforms.shape[1]) if self.waveforms.ndim == 2 else 0

    def waveform(self, channel: int) -> np.ndarray | None:
        try:
            return self.waveforms[self.channels.index(channel)]
        except ValueError:
            return None


def mask_channels(mask: int) -> list[int]:
    return [c for c in range(64) if mask >> c & 1]


def channels_mask(channels) -> int:
    out = 0
    for c in channels:
        if not 0 <= int(c) < 64:
            raise ValueError(f"channel {c} is not 0-63")
        out |= 1 << int(c)
    return out


def header_word(buf, offset: int = 0) -> tuple[int, int]:
    """(format, size in words) of the event at offset."""
    (w,) = struct.unpack_from(">Q", buf, offset)
    return w >> 60, w & 0xFFFFFFFF


def _unpack_samples(words_be: np.ndarray, n_channels: int) -> np.ndarray:
    """Sample words (big-endian uint64, as in the stream) of events of n_channels channels, shape (..., n_words)
    -> uint16 samples, shape (..., n_channels, samples)."""
    lead = words_be.shape[:-1]
    n_words = words_be.shape[-1]
    if n_channels == 0 or n_words == 0:
        return np.zeros(lead + (n_channels, 0), dtype=np.uint16)
    if n_words % n_channels:
        raise RawFormatError(f"{n_words} sample words do not divide among {n_channels} channels")
    native = words_be.astype("<u8")                     # the values, now in this computer's order
    s16 = native.view("<u2").reshape(lead + (n_words // n_channels, n_channels, SAMPLES_PER_WORD))
    axes = tuple(range(len(lead))) + (len(lead) + 1, len(lead), len(lead) + 2)
    return np.ascontiguousarray(s16.transpose(axes)).reshape(lead + (n_channels, -1))


def decode_scope(buf, offset: int = 0) -> ScopeEvent:
    """The Scope event that starts at offset."""
    if len(buf) - offset < SCOPE_HEADER_WORDS * WORD:
        raise RawFormatError("an event shorter than its header")
    w1, w2, w3 = struct.unpack_from(">QQQ", buf, offset)
    fmt, n_words = w1 >> 60, w1 & 0xFFFFFFFF
    if fmt != FORMAT_SCOPE:
        raise RawFormatError(f"format {fmt} is not a Scope event")
    size = n_words * WORD
    if n_words < SCOPE_HEADER_WORDS or len(buf) - offset < size:
        raise RawFormatError(f"the event of {n_words} words does not fit")
    channels = mask_channels(w3)
    sample_words = np.frombuffer(buf, dtype=">u8", count=n_words - SCOPE_HEADER_WORDS,
                                 offset=offset + SCOPE_HEADER_WORDS * WORD)
    waves = _unpack_samples(sample_words, len(channels))
    return ScopeEvent(trigger_id=(w1 >> 32) & 0xFFFFFF, timestamp=w2 & 0xFFFFFFFFFFFF, flags=w2 >> 51,
                      samples_overlapped=(w2 >> 48) & 0x7, board_fail=bool(w1 >> 56 & 1), ch_mask=w3,
                      channels=channels, waveforms=waves, size=size)


def decode_special(buf, offset: int = 0) -> dict:
    """A special event: {"kind": "start", "acq_width", "n_traces", "decimation", "ch_mask"} or
    {"kind": "stop", "time_tag", "dead_time"} (both in 8 ns), or {"kind": "other", "id"}."""
    (w1,) = struct.unpack_from(">Q", buf, offset)
    if w1 >> 60 != FORMAT_SPECIAL:
        raise RawFormatError("not a special event")
    event_id, n_extra = (w1 >> 56) & 0xF, (w1 >> 32) & 0xFF
    extra = struct.unpack_from(f">{n_extra}Q", buf, offset + WORD)
    data = [(x >> 56, x & ((1 << 56) - 1)) for x in extra]
    if event_id == SPECIAL_START and len(data) >= 3:
        d0 = data[0][1]
        return {"kind": "start", "acq_width": d0 & 0x1FFFFFF, "n_traces": (d0 >> 25) & 0x3,
                "decimation": 1 << ((d0 >> 27) & 0x1F),
                "ch_mask": (data[1][1] & 0xFFFFFFFF) | ((data[2][1] & 0xFFFFFFFF) << 32)}
    if event_id == SPECIAL_STOP and len(data) >= 2:
        return {"kind": "stop", "time_tag": data[0][1] & 0xFFFFFFFFFFFF, "dead_time": data[1][1] & 0xFFFFFFFF}
    return {"kind": "other", "id": event_id}


@dataclass
class Scan:
    """What scan() found in a part of a stream."""
    events: int = 0             # Scope events
    end: int = 0                # where the last complete event ends
    last_scope: int | None = None       # offset of the newest Scope event
    first_scope: int | None = None
    specials: list = field(default_factory=list)    # (offset, decoded special event)
    other: int = 0              # events of other formats


def scan(buf, start: int = 0, end: int | None = None) -> Scan:
    """Walk the event headers from start: count the Scope events, keep the special ones. Stops at the end of
    the last complete event; a header that cannot be one raises RawFormatError."""
    end = len(buf) if end is None else end
    out, o = Scan(end=start), start
    while end - o >= WORD:
        fmt, n_words = header_word(buf, o)
        if n_words == 0 or n_words > MAX_EVENT_WORDS:
            raise RawFormatError(f"an event of {n_words} words at byte {o}: the stream is broken")
        size = n_words * WORD
        if o + size > end:
            break
        if fmt == FORMAT_SCOPE:
            out.events += 1
            out.last_scope = o
            if out.first_scope is None:
                out.first_scope = o
        elif fmt == FORMAT_SPECIAL:
            out.specials.append((o, decode_special(buf, o)))
        else:
            out.other += 1
        o += size
        out.end = o
    return out


# ---- making events (the simulator, the tests) ----
def encode_scope(trigger_id: int, timestamp: int, channels, waveforms: np.ndarray, flags: int = 0,
                 samples_overlapped: int = 0, board_fail: bool = False) -> bytes:
    """One Scope event: waveforms is uint16 with shape (len(channels), samples); samples a multiple of 4."""
    channels = sorted(int(c) for c in channels)
    waves = np.asarray(waveforms, dtype=np.uint16)
    if waves.ndim != 2 or waves.shape[0] != len(channels):
        raise ValueError("waveforms must have one row per channel")
    if waves.shape[1] % SAMPLES_PER_WORD:
        raise ValueError("the samples of a channel must be a multiple of 4")
    n_ch, n_s = waves.shape
    n_words = SCOPE_HEADER_WORDS + n_ch * n_s // SAMPLES_PER_WORD
    w1 = (FORMAT_SCOPE << 60) | (int(board_fail) << 56) | ((trigger_id & 0xFFFFFF) << 32) | n_words
    w2 = ((flags & 0x1FFF) << 51) | ((samples_overlapped & 0x7) << 48) | (timestamp & 0xFFFFFFFFFFFF)
    head = struct.pack(">QQQ", w1, w2, channels_mask(channels))
    if n_ch == 0:
        return head
    groups = np.ascontiguousarray(waves.reshape(n_ch, n_s // SAMPLES_PER_WORD, SAMPLES_PER_WORD).transpose(1, 0, 2))
    words = groups.astype("<u2").view("<u8").reshape(-1).astype(">u8")
    return head + words.tobytes()


def encode_start(acq_width: int, ch_mask: int, decimation: int = 1, n_traces: int = 0) -> bytes:
    log2 = int(decimation).bit_length() - 1
    d0 = (acq_width & 0x1FFFFFF) | ((n_traces & 0x3) << 25) | ((log2 & 0x1F) << 27)
    return struct.pack(">QQQQ", (FORMAT_SPECIAL << 60) | (SPECIAL_START << 56) | (3 << 32) | 4,
                       (2 << 56) | d0, (1 << 56) | (ch_mask & 0xFFFFFFFF), (1 << 56) | (ch_mask >> 32 & 0xFFFFFFFF))


def encode_stop(time_tag: int, dead_time: int = 0) -> bytes:
    return struct.pack(">QQQ", (FORMAT_SPECIAL << 60) | (SPECIAL_STOP << 56) | (2 << 32) | 3,
                       (0 << 56) | (time_tag & 0xFFFFFFFFFFFF), (1 << 56) | (dead_time & 0xFFFFFFFF))


# ---- files ----
def file_prefix(header: dict) -> bytes:
    text = json.dumps(header, indent=1, sort_keys=False).encode("utf-8")
    text += b" " * (-len(text) % WORD)
    return MAGIC + struct.pack("<II", len(text), 0) + text


def read_header(path: str) -> tuple[dict, int]:
    """(the header of a data file, where its data begin)."""
    with open(path, "rb") as f:
        head = f.read(PREFIX)
        if len(head) < PREFIX or head[:8] != MAGIC:
            raise RawFormatError(f"{os.path.basename(path)} is not an ALAB DAQ data file")
        (n, _) = struct.unpack("<II", head[8:16])
        text = f.read(n)
    if len(text) < n:
        raise RawFormatError(f"{os.path.basename(path)}: the header is cut")
    try:
        header = json.loads(text.decode("utf-8"))
    except ValueError as err:
        raise RawFormatError(f"{os.path.basename(path)}: the header is not JSON ({err})") from None
    return header, PREFIX + n


class RawWriter:
    """Writes one board's stream into NAME.raw, starting a new file (NAME_001.raw, ...) at the next buffer
    when a file has max_bytes. on_file(path) is called for every file it opens."""

    def __init__(self, base: str, header: dict, max_bytes: int = 2 << 30, on_file=None):
        self.base, self.header, self.max_bytes = base, header, max_bytes
        self.on_file = on_file
        self.files: list[str] = []
        self.f = None
        self.size = 0
        self.total = 0              # bytes of data in all files

    def _open(self) -> None:
        k = len(self.files)
        path = f"{self.base}.raw" if k == 0 else f"{self.base}_{k:03d}.raw"
        if os.path.exists(path):
            raise FileExistsError(f"{path} exists already; data are never written over")
        header = dict(self.header, file_index=k)
        self.f = open(path, "xb", buffering=8 << 20)
        self.f.write(file_prefix(header))
        self.size = 0
        self.files.append(path)
        if self.on_file:
            self.on_file(path)

    def write(self, data) -> None:
        if self.f is None or (self.size and self.size + len(data) > self.max_bytes):
            self.close()
            self._open()
        self.f.write(data)
        self.size += len(data)
        self.total += len(data)

    def flush(self) -> None:
        if self.f is not None:
            self.f.flush()

    def close(self) -> None:
        if self.f is not None:
            self.f.close()
            self.f = None


class RawFile:
    """A data file for reading: its header, and its Scope events by number (index() walks the file once and
    keeps the three header words of every event). The file is mapped, not read: a large file costs no memory."""

    def __init__(self, path: str):
        self.path = path
        self.header, self.data_start = read_header(path)
        with open(path, "rb") as f:
            self.size = os.fstat(f.fileno()).st_size
            self._mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) if self.size > self.data_start else None
        self.data_size = self.size - self.data_start
        self.offsets: np.ndarray | None = None      # data offsets of the Scope events
        self._words: tuple | None = None            # their header words 1, 2, 3 (uint64 arrays)
        self.specials: list = []
        self.complete_end = 0
        self.truncated = False

    @property
    def buf(self):
        if self._mm is None:
            return b""
        return memoryview(self._mm)[self.data_start:]

    def index(self) -> np.ndarray:
        if self.offsets is not None:
            return self.offsets
        buf = self.buf
        offs, w1s, w2s, w3s = array("q"), array("Q"), array("Q"), array("Q")
        o, end = 0, len(buf)
        unpack = struct.unpack_from
        while end - o >= WORD:
            (w,) = unpack(">Q", buf, o)
            n_words = w & 0xFFFFFFFF
            if n_words == 0 or n_words > MAX_EVENT_WORDS:
                raise RawFormatError(f"{os.path.basename(self.path)}: an event of {n_words} words at byte "
                                     f"{self.data_start + o}: the file is broken there")
            size = n_words * WORD
            if o + size > end:
                break
            fmt = w >> 60
            if fmt == FORMAT_SCOPE:
                if n_words < SCOPE_HEADER_WORDS:
                    raise RawFormatError(f"{os.path.basename(self.path)}: a Scope event of {n_words} words")
                _, w2, w3 = unpack(">QQQ", buf, o)
                offs.append(o)
                w1s.append(w)
                w2s.append(w2)
                w3s.append(w3)
            elif fmt == FORMAT_SPECIAL:
                self.specials.append((o, decode_special(buf, o)))
            o += size
        del buf
        self.complete_end = o
        self.truncated = o != self.data_size
        self.offsets = np.array(offs, dtype=np.int64)
        self._words = tuple(np.array(a, dtype=np.uint64) for a in (w1s, w2s, w3s))
        return self.offsets

    def __len__(self) -> int:
        return len(self.index())

    def event(self, k: int) -> ScopeEvent:
        offs = self.index()
        if not 0 <= k < len(offs):
            raise IndexError(f"event {k} of {len(offs)}")
        return decode_scope(self.buf, int(offs[k]))

    def headers(self) -> dict[str, np.ndarray]:
        """trigger_id, timestamp, flags, samples_overlapped, board_fail, ch_mask and n_words of every Scope
        event, as arrays."""
        self.index()
        w1, w2, w3 = self._words
        u = np.uint64
        return {"trigger_id": ((w1 >> u(32)) & u(0xFFFFFF)).astype(np.uint32),
                "timestamp": w2 & u(0xFFFFFFFFFFFF),
                "flags": (w2 >> u(51)).astype(np.uint16),
                "samples_overlapped": ((w2 >> u(48)) & u(7)).astype(np.uint8),
                "board_fail": ((w1 >> u(56)) & u(1)).astype(bool),
                "ch_mask": w3.copy(), "n_words": (w1 & u(0xFFFFFFFF)).astype(np.uint32)}

    def batches(self, max_bytes: int = 64 << 20):
        """The Scope events in runs of the same size and channel mask, at most max_bytes each: yields
        (first event number, the header arrays of these events, channels, waveforms uint16 (n, channels, samples))."""
        offs = self.index()
        if not len(offs):
            return
        hdr = self.headers()
        n_words, masks = hdr["n_words"], hdr["ch_mask"]
        change = np.flatnonzero((np.diff(n_words.astype(np.int64)) != 0) | (np.diff(masks) != 0)) + 1
        starts = np.concatenate([[0], change, [len(offs)]]).astype(np.int64)
        for a, b in zip(starts[:-1], starts[1:]):
            nw = int(n_words[a])
            size = nw * WORD
            channels = mask_channels(int(masks[a]))
            per = max(1, max_bytes // size)
            for k0 in range(int(a), int(b), per):
                k1 = min(int(b), k0 + per)
                part = offs[k0:k1]
                n = k1 - k0
                if n == 1 or np.all(np.diff(part) == size):      # back to back: one slice of the file
                    first = self.data_start + int(part[0])
                    block = np.frombuffer(self._mm, dtype=np.uint8, count=n * size, offset=first).reshape(n, size)
                    body = np.ascontiguousarray(block[:, SCOPE_HEADER_WORDS * WORD:])
                    del block
                else:                                           # other events between them
                    body = np.empty((n, size - SCOPE_HEADER_WORDS * WORD), dtype=np.uint8)
                    for i, o in enumerate(part):
                        first = self.data_start + int(o) + SCOPE_HEADER_WORDS * WORD
                        body[i] = np.frombuffer(self._mm, dtype=np.uint8, count=body.shape[1], offset=first)
                words = body.view(">u8")
                waves = _unpack_samples(words, len(channels))
                del body, words
                yield k0, {k: v[k0:k1] for k, v in hdr.items()}, channels, waves

    def stop_record(self) -> dict | None:
        self.index()
        stops = [s for _, s in self.specials if s["kind"] == "stop"]
        return stops[-1] if stops else None

    def start_record(self) -> dict | None:
        self.index()
        return next((s for _, s in self.specials if s["kind"] == "start"), None)

    def close(self) -> None:
        mm, self._mm = self._mm, None
        if mm is not None:
            try:
                mm.close()
            except BufferError:         # an array still looks into it: it closes when that one is gone
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def describe(path: str) -> dict:
    """What a data file holds (for the command line and the tests)."""
    with RawFile(path) as f:
        offs = f.index()
        out = {"path": path, "board": f.header.get("board", {}), "events": int(len(offs)),
               "data_bytes": f.data_size, "truncated": f.truncated, "stop": f.stop_record(),
               "start": f.start_record()}
        if len(offs):
            first, last = f.event(0), f.event(len(offs) - 1)
            out.update(channels=first.channels, samples=first.samples, first_trigger=first.trigger_id,
                       last_trigger=last.trigger_id, first_timestamp=first.timestamp, last_timestamp=last.timestamp,
                       seconds=(last.timestamp - first.timestamp) * TIMESTAMP_NS * 1e-9)
    return out


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print(__doc__)
        return 2
    code = 0
    for path in args:
        try:
            print(json.dumps(describe(path), indent=1, default=str))
        except (OSError, RawFormatError) as err:
            print(f"{path}: {err}", file=sys.stderr)
            code = 1
    return code


if __name__ == "__main__":
    raise SystemExit(main())
