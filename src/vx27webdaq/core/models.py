"""The CAEN Digitizer 2.0 boards Vx27webDAQ knows, for the Configuration page and the simulator, before a board
has been asked. A connected board always tells its own facts (/par/ModelName, NumCh, ADC_Nbit,
ADC_SamplRate), and those count. Sources: the WaveDump2 manual (UM7934 rev. 7) and the CAEN data sheets.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

DECIMATIONS = (1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024)


@dataclass(frozen=True)
class Model:
    family: str             # "2730", "2740", ...
    channels: int
    adc_bits: int
    sample_ns: float        # one sample without decimation
    decimation: bool        # has DecimationFactor (2740, 2745)
    gain: bool              # has the variable gain amplifier, ChGain in dB (2745)
    text: str
    input_vpp: float = 2.0  # the input range in volts peak to peak (CAEN data sheets): for the mV axis

    @property
    def adc_max(self) -> int:
        return (1 << self.adc_bits) - 1

    @property
    def msps(self) -> float:
        return 1000.0 / self.sample_ns

    @property
    def mv_per_adc(self) -> float:
        return self.input_vpp * 1000.0 / (1 << self.adc_bits)


MODELS = {
    "2730": Model("2730", 32, 14, 2.0, False, False, "32 channels, 14 bit, 500 MS/s"),
    "2740": Model("2740", 64, 16, 8.0, True, False, "64 channels, 16 bit, 125 MS/s"),
    "2745": Model("2745", 64, 16, 8.0, True, True, "64 channels, 16 bit, 125 MS/s, variable gain"),
}
FORMS = ("V", "VX", "DT")       # VME, VME64X, desktop: the same board for Vx27webDAQ
CHOICES = [f"{form}{fam}" for fam in MODELS for form in ("V", "VX")]


def family(name: str) -> str | None:
    """'V2740', 'VX2740', 'DT2740', '2740' -> '2740'; None for a model Vx27webDAQ does not know."""
    digits = "".join(ch for ch in str(name) if ch.isdigit())[:4]
    return digits if digits in MODELS else None


def model(name: str) -> Model | None:
    fam = family(name)
    return MODELS[fam] if fam else None


def from_board(info: dict) -> Model:
    """The facts a connected board gave (identify() of the DAQ), as a Model."""
    known = model(info.get("model", ""))
    msps = float(info.get("adc_msps") or (known.msps if known else 125.0))
    return Model(family(info.get("model", "")) or "?", int(info.get("channels") or (known.channels if known else 64)),
                 int(info.get("adc_bits") or (known.adc_bits if known else 16)), 1000.0 / msps,
                 known.decimation if known else False, known.gain if known else False,
                 info.get("model", "unknown model"), known.input_vpp if known else 2.0)


def table() -> list[dict]:
    return [dict(asdict(m), adc_max=m.adc_max, mv_per_adc=m.mv_per_adc) for m in MODELS.values()]
