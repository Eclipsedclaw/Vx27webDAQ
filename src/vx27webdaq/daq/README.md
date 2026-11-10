# The DAQ program: how a run works

Back to the [README](../../../README.md). The code here: `engine.py` (one run of all boards), `boards.py` (a board through CAEN FELib), `sim.py` (the simulated boards), `sync.py` (the sync check), `__main__.py` (the commands).

## The commands

The web server never talks to the boards itself; it starts these, each a process of its own (so a problem of the CAEN library never touches the server):

| Command | Used by |
|---|---|
| `python -m vx27webdaq.daq run FOLDER [--simulate "OPTIONS"]` | a run: the configuration in `FOLDER/config.json` |
| `python -m vx27webdaq.daq check CONFIG.json [--trees FOLDER]` | **Check boards**: one JSON line per board (model, serial, firmware, temperatures), and its parameter tree |
| `python -m vx27webdaq.daq apply CONFIG.json` | **Send to boards**: every board switched on reset and given every parameter, no data taken; one JSON line per board |
| `python -m vx27webdaq.daq param CONFIG.json --board NAME (--get PATH \| --set PATH VALUE \| --cmd COMMAND)` | the manual controller (Tools → Boards) |

## A run

For a run the server writes the configuration (with the note and the run's name, e.g. `run00012`) into a new run folder and starts the DAQ program apart from itself, so **a run goes on when the server stops or restarts**: the next server finds it and follows it to its end. The DAQ program follows CAEN's own multi-board recipe (the sync demo of [caen-dig2](https://github.com/caenspa/caen-dig2), `demo/caen-felib-demo-sync`):

1. connects to every board, one after the other, checks that it runs the Scope firmware and is the model the configuration says;
2. resets it and sets its parameters (the list under *What this board gets*), activates the raw data endpoint;
3. arms every board, the last of the chain first, starts one reader per board, and sends the start command to the **master** only; the run signal travels along the clock cable and every board starts with it (the run delays line the starts up), so **all timestamps count the same clock**;
4. each reader writes its board's data exactly as the board sends them; the run ends at its run time, at the event limit, on **Stop** or when a board fails: the stop command goes to the master, every board sends its last data and its **stop record**, and is disarmed.

**Stop** sends the DAQ program a SIGTERM (the clean end above; a SIGHUP is a Stop too, and a Stop during the setup ends the run before the boards start). A board still sending 10 s after the stop gets the stop and disarm itself, and every board's reading ends after 60 s at the latest (*time-out*); a second Stop during the stop means "do not wait". **Kill** becomes possible 15 s after Stop, for when it does not end. A board that sends nothing is reported (*it did not start: was it armed, is its CLK-IN cable in place?*), as is a board without a stop record. A board whose stop record comes before the stop stopped by itself: that is an error and ends the run (except at the end of the master's SINlevel start, which is the run's end). With a hardware start of the master (S-IN), the run time counts from the master's first data; until then the log says that it waits for the start signal. After the run each `.raw` file is converted to ROOT (unless switched off on the Run page). During the run the program also reads each board's housekeeping, keeps a matched set of events for the live combined view (at most 8 MB of an event and 32 MB per board are kept for the live views), checks the sync of the boards and carries out the page's live commands (Force trigger, Tune). Any error during the run still stops every board and writes the run's end; a disk error while writing a board's data stops the run.

## The synchronization and the trigger setups

| Setting | Cables | What happens |
|---|---|---|
| **clock chain** (synchronization) | CLK-OUT of each board → CLK-IN of the next (CAEN A319A) | the first board is the master: ClockSource Internal, SyncOutMode Run, StartSource SWcmd; the others FPClkIn, SyncIn (the last Disabled), EncodedClkIn; RunDelay 8 ns × (2 × boards after it, + 4 on the master); clock-out delay −2148 ps on the master, −3111 ps in the middle (CAEN's values; both can be set per board) |
| independent boards | none | each board on its own clock, started one after the other; timestamps do not line up |
| **the master self-triggers, the others follow** (the lab's usual setup) | CLK-OUT → CLK-IN and TRG-OUT → TRG-IN from the master (the VX2730) to each next board | the master triggers on its own channels (AcqTriggerSource ITLA) and sends its trigger out (TrgOutMode ITLA, or AcceptTrg: only the triggers it took, so the trigger IDs stay equal when it is busy); every other board takes it on TRG-IN and passes it on (TrgIn); every board records the same moments, a few ns later down the chain |
| **common external trigger** | trigger pulse → TRG-IN of the first board; TRG-OUT → TRG-IN of the next | every board takes every trigger (AcqTriggerSource TrgIn, TrgOutMode TrgIn): the same trigger IDs; boards further down get it a fixed few ns later |
| **each board self-triggers** | none | each board triggers on its own channels (ITLA: OR, AND or majority of the channels marked *in trigger*, with their thresholds); events of different boards are matched by timestamp |
| **OR in a logic unit** | TRG-OUT of every board → a logic unit (e.g. CAEN V976, OR) → TRG-IN of every board, equal cable lengths | every board sends out its channel triggers (TrgOutMode ITLA) and records the common trigger (TrgIn) |
| periodic | none (or the TRG chain, for "master, passed on") | software triggers from the DAQ program (to each board, or to the master which passes them on TRG-OUT → TRG-IN), or each board's internal test pulse: for pedestals and tests |
| busy veto (option) | one cable along the GPIO of all boards, 50 Ω at both ends (NIM) | GPIOMode Busy, VetoSource GPIO: a busy board vetoes all, so at high rates they keep the same events |

With channel triggers the **board trigger** is logic A (ITLA), logic B (ITLB), A AND B or A OR B, each logic with its own OR/AND/majority and gate; each channel belongs to group A or B, and a majority is checked against the channels of its own group.

The **sync check** (Progress tab) compares the boards' timestamps per trigger ID only when one trigger reaches every board on the clock chain; boards on their own clocks have no common time (*independent boards: no common clock*). An ID that a board sends late is counted as late, not missing.

**Thresholds** count from the channel's own baseline in ADC counts, in the direction of the pulse: TriggerThrMode Relative, TriggerThr −N with SelfTriggerEdge Fall for negative pulses, +N with Rise for positive ones. **DC offset** is where the baseline sits in % of the input range (for negative pulses a high value leaves room below).

## The lab's boards

**The lab's setup** is the starting configuration (and **New**): the VX2730 (16 inputs, 500 MS/s) at `dig2://192.168.0.72` is the master and self-triggers; its CLK and TRG go along the chain to the VX2740s (64 inputs, 125 MS/s) at `dig2://192.168.0.75` and `dig2://192.168.0.85`; every board records 4.096 µs, 1.024 µs of it before the trigger. **Add board** for more VX2740s.

**The boards' network:** the boards are at 192.168.0.x, the lab's computers at 192.168.1.x. On one switch the DAQ computer reaches the boards only with an address in 192.168.0.0/24 too, e.g. `sudo ip addr add 192.168.0.10/24 dev ETH` (lasting: in netplan), then `ping 192.168.0.72`.

**The first run with real boards:** check each board's address (`dig2://IP` or `dig2://usb:PID`) and model, cable the chain, press **Check boards** (every row green with the right model and serial), start a short run with the *periodic* trigger, and read *What this board gets* and the DAQ output if a board refuses something.

**To be checked on the real boards** (the simulator cannot tell):
- the parameter names that are not in CAEN's demos but come from the WaveDump2 manual: `IOlevel`, `DecimationFactor`, `GPIOMode`, `VetoSource` (Vx27webDAQ tries `BoardVetoSource` if refused), and the trigger and output values (`TrgIn`, `ITLA`, `SwTrg`, `TestPulse`). A board that refuses a parameter stops the configuration with its name, and the Boards tool shows the board's own parameter list; **Expert parameters** set anything directly;
- the threshold convention (relative, −N with Fall for negative pulses), and whether the run delays and clock-out delays need tuning for the cables (CAEN's values are the defaults);
- `ITLConnect` per channel: firmware without it gets `ITLAMask` and `ITLBMask` instead (tested with the simulator). A refused setting tries its alternatives in turn (e.g. `AcqTriggerSource` `ITLA|SwTrg`, then `ITLA`), and the log says which one the board took;
- the stop and disarm that a board still sending gets after 10 s, and the hardware start of the master on S-IN.

## The simulator

`--simulate "OPTIONS"` replaces the boards with simulated ones (the Settings page passes its *simulator options*): `--rate HZ` hits per second, `--share P` the part of the channels a hit reaches, `--amplitude COUNTS`, `--noise SIGMA`, `--seed N`; `--generator staircase` the pulse generator (one pulse per record and channel: negative and all at once on a VX2730; positive on a VX2740 with `--rise-us` 5 and `--decay-us` 1000, channel k at k × `--spacing-us` 5); `--fail open:NAME`, `param:NAME:PARAM`, `read:NAME:N`, `nostop:NAME`, `unarmed:NAME`, `errflags:NAME:S`, `value:NAME:PARAM:TEXT` (it refuses a value of PARAM that contains TEXT) or `selfstop:NAME:S` (it stops by itself S seconds into the run) imitates a failure of board NAME, and `--sin A[:B]` is the S-IN signal of a board started by it (high from A s after arming, low again from B s) (for the tests).
