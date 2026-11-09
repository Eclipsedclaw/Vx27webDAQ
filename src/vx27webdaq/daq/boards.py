"""One board for the DAQ program: the real thing through CAEN FELib (the official Python binding caen_felib,
with libCAEN_FELib.so and libCAEN_Dig2.so found through LD_LIBRARY_PATH), or a simulated one (sim.py). Both
offer the same few calls, so the DAQ does not know which it drives:

    get(path) -> str          set(path, value)          cmd(name)            ("Reset" -> /cmd/Reset)
    open_raw()                read_raw(timeout_ms) -> (data, n_events) or (None, 0) on timeout
    identify() -> dict        device_tree() -> dict or None         close()

The raw endpoint gives the board's stream as it arrives (see core/rawdata.py), in buffers of at most
/par/MaxRawDataSize bytes: nothing is decoded on the way, so the DAQ keeps up with the link.
"""

from __future__ import annotations

# parameters read for the board table and the status (a board that lacks one simply does not show it)
IDENTITY = {"model": "/par/ModelName", "serial": "/par/SerialNum", "fw_type": "/par/FwType",
            "fw_version": "/par/FPGA_FwVer", "cup": "/par/CupVer", "form": "/par/FormFactor",
            "channels": "/par/NumCh", "adc_bits": "/par/ADC_Nbit", "adc_msps": "/par/ADC_SamplRate",
            "max_raw": "/par/MaxRawDataSize"}
MONITORS = {"temp_air_in": "/par/TempSensAirIn", "temp_air_out": "/par/TempSensAirOut",
            "temp_core": "/par/TempSensCore", "temp_first_adc": "/par/TempSensFirstADC",
            "temp_last_adc": "/par/TempSensLastADC", "temp_dcdc": "/par/TempSensDCDC",
            "acq_status": "/par/AcquisitionStatus"}


# Housekeeping: what a board says about itself, read every few seconds during a run (engine.py) and by Check boards.
# Board parameters of the Digitizer 2.0 (CAEN FELib, the Scope firmware); a board that lacks one simply leaves it out.
HK = {"temp_air_in": "/par/TempSensAirIn", "temp_air_out": "/par/TempSensAirOut", "temp_core": "/par/TempSensCore",
      "temp_first_adc": "/par/TempSensFirstADC", "temp_last_adc": "/par/TempSensLastADC",
      "temp_hottest_adc": "/par/TempSensHottestADC", "temp_dcdc": "/par/TempSensDCDC",
      "fan1_speed": "/par/SpeedSensFan1", "fan2_speed": "/par/SpeedSensFan2",
      "dcdc_vin": "/par/VInSensDCDC", "dcdc_vout": "/par/VOutSensDCDC", "dcdc_iout": "/par/IOutSensDCDC",
      "dcdc_duty": "/par/DutyCycleSensDCDC", "core_freq": "/par/FreqSensCore",
      "error_flags": "/par/ErrorFlags", "acq_status": "/par/AcquisitionStatus",
      "trigger_cnt": "/par/TriggerCnt", "lost_trigger_cnt": "/par/LostTriggerCnt",
      "realtime": "/par/RealtimeMonitor", "deadtime": "/par/DeadtimeMonitor", "livetime": "/par/LivetimeMonitor"}
HK_INTS = ("error_flags", "acq_status", "trigger_cnt", "lost_trigger_cnt", "realtime", "deadtime", "livetime")
CH_RATE = "/ch/{c}/par/SelfTrgRate"      # a channel's self-trigger rate (Hz)


class BoardError(RuntimeError):
    """A board that cannot be reached, or refuses a parameter or a command."""


def identify(board) -> dict:
    """The facts of a board, as numbers where they are numbers; the ones it lacks are left out."""
    out = {}
    for key, path in IDENTITY.items():
        try:
            value = board.get(path)
        except BoardError:
            continue
        if key in ("channels", "adc_bits", "max_raw"):
            try:
                out[key] = int(float(value))
            except ValueError:
                continue
        elif key == "adc_msps":
            try:
                out[key] = float(value)
            except ValueError:
                continue
        else:
            out[key] = value.strip()
    return out


def number(value: str):
    """A parameter's text as a number: an int for whole numbers and masks (0x...), else a float; else the text."""
    text = str(value).strip()
    try:
        return int(text, 0)
    except ValueError:
        pass
    try:
        x = float(text)
    except ValueError:
        return text
    return int(x) if x.is_integer() and abs(x) < 1 << 53 and "." not in text else x


def housekeeping(board, wanted: dict | None = None) -> dict:
    """The board's sensors, counters and error flags (HK); the keys it does not know are dropped from wanted."""
    wanted = dict(HK) if wanted is None else wanted
    out = {}
    for key, path in list(wanted.items()):
        try:
            out[key] = number(board.get(path))
        except BoardError:
            wanted.pop(key, None)
    return out


def channel_rates(board, channels: list[int], state: dict) -> dict:
    """{channel: self-trigger rate (Hz)} of these channels; state[\"off\"] after the first refusal (the board's
    firmware has no such counter): later calls ask nothing."""
    out = {}
    if state.get("off"):
        return out
    for c in channels:
        try:
            v = number(board.get(CH_RATE.format(c=c)))
        except BoardError:
            state["off"] = True
            return {}
        if isinstance(v, (int, float)):
            out[c] = v
    return out


def monitors(board, wanted: dict | None = None) -> dict:
    """Temperatures and the acquisition status; the keys a board does not know are dropped from wanted."""
    wanted = MONITORS if wanted is None else wanted
    out = {}
    for key, path in list(wanted.items()):
        try:
            value = board.get(path)
        except BoardError:
            wanted.pop(key, None)
            continue
        try:
            out[key] = float(value)
        except ValueError:
            out[key] = value
    return out


class RealBoard:
    """A Digitizer 2.0 board through CAEN FELib."""

    def __init__(self, uri: str):
        try:
            from caen_felib import device, error
        except ImportError as err:          # the module, or the C library it loads
            raise BoardError(f"CAEN FELib is not available ({err}): run caen_libs.sh and setup.sh, "
                             "or install CAEN FELib and CAEN Dig2") from None
        except (OSError, RuntimeError) as err:      # caen_felib loads libCAEN_FELib.so when imported
            raise BoardError(f"CAEN FELib cannot be loaded ({err}). Build it with ./caen_libs.sh (or install CAEN "
                             "FELib and CAEN Dig2) and set its folder on the Settings page") from None
        self._error = error
        self.uri = uri
        try:
            self.dev = device.connect(uri)
        except error.Error as err:
            raise BoardError(f"cannot connect to {uri}: {err}") from None
        self.ep = None
        self.data = None

    def _call(self, what: str, fn, *args):
        try:
            return fn(*args)
        except self._error.Error as err:
            raise BoardError(f"{what}: {err}") from None

    def get(self, path: str) -> str:
        return self._call(f"get {path}", self.dev.get_value, path)

    def set(self, path: str, value: str) -> None:
        self._call(f"set {path} = {value}", self.dev.set_value, path, str(value))

    def cmd(self, name: str) -> None:
        self._call(f"command {name}", self.dev.send_command, f"/cmd/{name}")

    def open_raw(self) -> None:
        from caen_felib import device
        size = int(float(self.get("/par/MaxRawDataSize")))
        self.ep = self._call("raw endpoint", self.dev.get_node, "/endpoint/raw")
        fmt = [{"name": "DATA", "type": device.DataType.U8, "dim": 1, "shape": [size]},
               {"name": "SIZE", "type": device.DataType.SIZE_T},
               {"name": "N_EVENTS", "type": device.DataType.U32}]
        self.data = self._call("raw data format", self.ep.set_read_data_format, fmt)
        self.set("/endpoint/par/ActiveEndpoint", "raw")

    def read_raw(self, timeout_ms: int):
        try:
            self.ep.read_data(int(timeout_ms), self.data)
        except self._error.Error as err:
            if err.code is self._error.ErrorCode.TIMEOUT:
                return None, 0
            if err.code is self._error.ErrorCode.STOP:
                return b"", 0
            raise BoardError(f"reading data: {err}") from None
        size = int(self.data[1].value)
        return memoryview(self.data[0].value)[:size], int(self.data[2].value)

    def identify(self) -> dict:
        return identify(self)

    def device_tree(self) -> dict | None:
        try:
            return self.dev.get_device_tree()
        except self._error.Error:
            return None

    def close(self) -> None:
        try:
            self.dev.close()
        except Exception:           # closing after a lost link must not hide the real error
            pass


def open_board(uri: str, crate=None, name: str = ""):
    """A board: simulated when a simulator crate is given (sim.Crate), else the real one at uri."""
    if crate is not None:
        return crate.open(uri, name)
    return RealBoard(uri)
