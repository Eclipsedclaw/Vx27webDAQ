# ALAB DAQ

A web page for multi-board data taking with CAEN Digitizer 2.0 boards — **V2740** (64 channels, 16 bit, 125 MS/s), **V2730** (32 channels, 14 bit, 500 MS/s), also V2745 — running the **Scope firmware** (the firmware CAEN's WaveDump2 uses). It configures the boards, runs them synchronized on one clock (a CLK-OUT → CLK-IN chain), shows the run live (every board's state and rates, the waveforms of any board and channel), keeps every board's data losslessly, and converts them to ROOT. One server runs on the DAQ computer for the whole lab, and everyone opens it in their own browser. It is built on the design of XIAOLAB DAQ, the lab's page for its AD9694 readout (the same pages, accounts and safety), with its own DAQ program in place of testDAQ.

**State (v0.0.0, 2026-11-07):** complete and tested with simulated boards; **not yet tried on real boards** (none was reachable from dbar, where it was built). See [What is verified](#what-is-verified-and-what-is-not).

## Open the page

The server listens on the DAQ computer's localhost only (port 8770). Forward that port to your computer and open it in your browser:

```bash
ssh -L 8770:localhost:8770 DAQ-COMPUTER          # on your computer; keep this open
```

Then open **http://localhost:8770**. With VS Code Remote-SSH, add port 8770 on the **Ports** tab instead. Without signing in you watch as a viewer; **Sign in** (top right) gives you the role of your account.

## Who may do what

| Role | May |
|---|---|
| **viewer** (and anyone not signed in, while guests are allowed) | watch the run and everything on every page; open data files (waveforms, overview); convert a `.raw` file to ROOT |
| **operator** | and control the DAQ: check the boards, start, stop and kill runs; edit, save and open the configuration; change the server settings |
| **admin** | and manage the accounts (add, role, new password, remove), decide whether guests may look in, and read the activity log |

Passwords are set by an admin (Users page, or the command line below); each user can change their own (account menu, top right). Every action is written to the activity log with the account that did it, and every run's `run.json` says who started and stopped it. The account list is ALAB DAQ's own (not XIAOLAB DAQ's).

## The pages

| Page | Use it to |
|---|---|
| **Run** (the front page) | **Check boards** (operators: connects to every board and reads its model, serial, firmware and temperatures), start and stop runs, **Force trigger** (operators, during a run: one software trigger to the boards that make the triggers: the master when it sends AcceptTrg on TRG-OUT, so every board records it; every board when each triggers by itself; not with an external trigger or a logic unit, and not when the master sends ITLA: its trigger IDs would no longer match the other boards'); the **waveforms** in the big part; beside them the progress above the DAQ program's output. Progress: run time, events and event rate of the first board, data rate of all boards (and its plot), data written, and one row per board (click it to see its waveforms): its place in the chain (**M** = master), state, events, rate, MB/s, the trigger ID of its newest event, its hottest temperature, and how it ended (*stop record* when it sent its last data cleanly). **Sync check** (with one trigger for all boards, like WaveDump2's build matching window): for every trigger ID every board has passed, whether every board has it with its timestamp within the *sync window* of the first board's: *N on every board · M out of sync · missing per board*. **Statistics** per board (WaveDump2's Statistics panel): the triggers it counted, the triggers it lost (busy, memory full), the incoming trigger rate, the dead time, error flags, and its timestamps against the first board (mean, and lowest … highest: a fixed few ns is the cabling). Waveforms, two views. **Channels**: the channels of one board, each in its own plot, chosen with **Channels…** (click channels to show or hide them, at most 16 at a time, quick choices 0–15, 16–31, …, *read out*, *in trigger*; each board keeps its own choice in this browser), live (the newest event) or from a `.raw` or ROOT file, event by event; the x axes give each sample and its time from the trigger at the board's own sample time (2 ns on a V2730, 8 ns × decimation on a V2740); ADC range Auto, Full scale (the board's 14 or 16 bits), Custom or baseline ± N; dashed trigger and threshold lines; drag or wheel to zoom all plots, or **Measure** a range (min, max, mean, σ, peak, the values at both edges and their difference like WaveDump2's two cursors, area in ADC·ns); **Plot**: Waveforms, **FFT** (each waveform's spectrum, its mean taken off and a Hann window, in dB of full scale up to half the sampling rate: 250 MHz on a VX2730, 62.5 MHz on a VX2740) or **Histogram** (how often each ADC value came, over the events so far); **Average**: each event, or the mean of the newest 4, 16 or 64 (waveforms and FFT; **Restart** starts the means and histograms again); **Y axis** ADC counts or mV from the baseline; **Tune…** (operators, during a run: a shown channel's DC offset or threshold goes to the board at once, like WaveDump2's oscilloscope tools; *keep for the next runs* puts it into the configuration too; every change is in the DAQ output, run.json and the run's status). **Combined**: channels of several boards in one plot (up to 16, each its own color), from one moment: live, the DAQ keeps the first event of every 0.27 s timestamp cell of each board and shows the newest cell all boards have, so with the clock chain and a common trigger every trace is the same trigger (the page says the trigger IDs and how far the timestamps differ); from files, event *k* of the file opened and the event with the same trigger ID in the run's other files. The time axis is µs from the trigger and covers every chosen board's window (2 ns and 8 ns boards together); **Time 0** = each board's trigger, or the boards placed by their **timestamps** (a pulse split into two boards lines up). **Y axis**: mV from each channel's baseline (each model's input range, 2 Vpp: one count is 0.122 mV on a VX2730, 0.031 mV on a VX2740), ADC counts from the baseline, or raw ADC counts; point at the plot for every trace's value, drag to zoom |
| **Configuration** | everything a run does: the **Run** (run time, event limit, clock chain or independent boards, data folder, file size), the **Trigger** setup (the cabling of each is explained next to it; with channel triggers: the **board trigger** from logic A (ITLA), logic B (ITLB), A AND B or A OR B, each logic with its own OR/AND/majority and gate), and each **board** in chain order: name, address, model, channels (its inputs; **Use what the boards said** takes each board's model and channels from the last board check), record length and pre-trigger (in samples, with the time), decimation, the chain delays, **Front panel and test signals** (WaveDump2's Board settings: TRG-OUT and GPIO as the setup needs them or any signal, e.g. the internal test pulse for a scope or for feeding back into channels; an acquisition veto from S-IN, GPIO or LVDS with width and polarity; a hardware start of the master on S-IN, its edge or the first TRG-IN pulse; the timestamp reset on S-IN or GPIO; DAC OUT: a level, ramp, 5 MHz sine, square, pulses or a copy of a channel; a value a board does not know is a warning and the run goes on without it), its **Channels** (read out, DC offset, threshold, pulse direction, in the board trigger, its group A or B when the board trigger uses both channel logics, gain on a 2745, with *apply to all*), **Expert parameters** (any CAEN FELib parameter, `PATH = VALUE`), and **What this board gets**: the exact FELib parameters it will be given, in order. The **Note** says what the runs are about; the **Check** says what the DAQ program would refuse. **Same window on all…** gives every board the same window in time; **Set all thresholds…** one threshold to every channel. Edits are kept on the server (also over a restart); **Save** writes a `.json` file |
| **Tools** | **Overview of all channels** (baseline, noise σ or peak of every channel of every board as a heat map, live or for a file; 32 channels a row, each board its own color range), **Board housekeeping** (live or for a data file: each board's temperatures, trigger rates, dead time, counters, fans and DC-DC, channel trigger rates over the run, and every value of the newest reading), **Data file** (open a `.raw` or ROOT file: its waveforms on the Run page, its overview, a ROOT file of it, **every board per trigger** (its run's `RUN_built.root`), a **text file** of some of its events), **Boards** (what each board said at the last check, and its whole parameter tree with a filter: the names and allowed values for the expert parameters; the **manual controller** (WaveDump2's, operators, no run going): get or set one parameter of a board or send it a command, a click on a parameter fills it in; the next run resets the board and sets it from the configuration), **ROOT conversions** |
| **Settings** | the server's settings (the CAEN library folder, with whether both libraries are found; the simulator and its options; folders; ROOT after the run) and this browser's theme, language and layout |
| **Users** | (admins) the accounts, whether guests may look in, and the activity log |

Every page works in light and dark and in English and 中文; the dividers can be dragged.

## How a run works

The server never talks to the boards itself. For a run it writes the configuration (with the note and the run's name, e.g. `run00012`) into a new run folder, and starts the **DAQ program** (`python -m alab_daq.daq run FOLDER`) apart from itself, so **a run goes on when the server stops or restarts**: the next server finds it and follows it to its end. The DAQ program follows CAEN's own multi-board recipe (the sync demo of [caen-dig2](https://github.com/caenspa/caen-dig2), `demo/caen-felib-demo-sync`):

1. connects to every board, one after the other, checks that it runs the Scope firmware and is the model the configuration says;
2. resets it and sets its parameters (the list under *What this board gets*), activates the raw data endpoint;
3. arms every board, the last of the chain first, starts one reader per board, and sends the start command to the **master** only; the run signal travels along the clock cable and every board starts with it (the run delays line the starts up), so **all timestamps count the same clock**;
4. each reader writes its board's data exactly as the board sends them; the run ends at its run time, at the event limit, on **Stop** or when a board fails: the stop command goes to the master, every board sends its last data and its **stop record**, and is disarmed.

**Stop** sends the DAQ program a SIGTERM (the clean end above). **Kill** becomes possible 15 s after Stop, for when it does not end. A board that sends nothing is reported (*it did not start: was it armed, is its CLK-IN cable in place?*), as is a board without a stop record. After the run each `.raw` file is converted to ROOT (unless switched off on the Run page).

### The synchronization and the trigger setups

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

**Thresholds** count from the channel's own baseline in ADC counts, in the direction of the pulse: TriggerThrMode Relative, TriggerThr −N with SelfTriggerEdge Fall for negative pulses, +N with Rise for positive ones. **DC offset** is where the baseline sits in % of the input range (for negative pulses a high value leaves room below).

## The data

| What | Where |
|---|---|
| The configuration the server opens first | `/NAS/ALAB_DAQ_DATA/alab_daq_test.json` (Settings page: *configuration file*). When it is not there yet, the server starts with the lab's setup and **Save** writes it there |
| Each board's data | `DATA_FOLDER/RUN_BOARD.raw` (the data folder of the configuration; by default `/NAS/ALAB_DAQ_DATA/`, which must exist and be writable by the service account), e.g. `run00012_b0.raw` (the run number is one more than the highest in the data folder); a new file `_001.raw`, `_002.raw`, … at the file size of the configuration |
| Their settings and note | the header of each `.raw` file, and `NAME.json` next to it (the configuration, the note — it follows edits during the run — and the run's facts) |
| ROOT files | `NAME.root` next to `NAME.raw`, after the run (`NAME.root.part` until it is checked) |
| Every board per trigger | `RUN_built.root` beside the run's files (WaveDump2's multi-board single file), after every run of several boards (Settings) or from the Data file tool: TTree `events`, one entry per event of the first board: `trigger_id`, `timestamp`, `time_ns`, `n_boards`, `complete`, and per board `BOARD_found`, `BOARD_timestamp`, `BOARD_dt_ns` and `BOARD_chN` (UShort_t[n], zeros where the board lacks the trigger); matched by trigger ID within the sync window. By hand: `python -m alab_daq.core.eventbuild RUN_BOARD.raw` |
| Text export | the Data file tool: events *from … count* (at most 1000) and channels as a download, laid out like WaveDump2's ASCII files |
| Board housekeeping | `RUN_BOARD_hk.csv` next to the board's data (in the run folder's `hk/` when no data are kept): every *housekeeping* seconds (Configuration, Run; 5 s) a row: `time` (Unix s), `run_s`, the temperatures in °C (`temp_air_in`, `temp_air_out`, `temp_core`, `temp_first_adc`, `temp_last_adc`, `temp_hottest_adc`, `temp_dcdc`), fans, DC-DC, `error_flags`, `acq_status`, `trigger_cnt`, `lost_trigger_cnt`, `realtime`, `deadtime`, `livetime` (as the board gives them: CAEN FELib's TempSens…, ErrorFlags, TriggerCnt, LostTriggerCnt, …Monitor), each channel's `chN_trg_rate` (SelfTrgRate, Hz), and `events`, `icr_hz`, `rate_hz`, `dead_frac` worked out between two readings. A value the board does not have is left out. The last row is read after the stop |
| Run records | `<save folder>/<date>_<time>/`: `config.json`, `daq.log`, `status.json`, `run.json`, `live/`, `rootconv.log`; the save folder is a server setting (default `~/ALAB_DAQ_runs` of the service account) |
| Simulator data | inside the run folder (`raw/`), never in the real data folder |
| Server settings | `~/.config/alab_daq/web.json` of the service account |
| Accounts, sign-ins, activity log, the configuration being edited, the run being followed, board checks | `~/.local/state/alab_daq/` of the service account (only that account can read it) |

**`.raw` files** keep the board's stream exactly as the board sent it (lossless, like WaveDump2's Fast Save): a 16-byte prefix (`ALABRAW1`, the header length), a JSON header (the board's model, serial, firmware, sample time, every parameter as it was set, the run's configuration and note), then the board's 64-bit big-endian words. Each event: word 1 = format 1 (bits 63–60), board fail (56), trigger ID (55–32), size in words (31–0); word 2 = flags (63–51), samples overlapped (50–48), timestamp in 8 ns (47–0); word 3 = the channel mask; then, for every 4 samples, one word per channel of the mask with 4 samples of 16 bits, the first in the lowest bits. Run start and stop records are special events (format 3). This is the layout of CAEN's decoder (`caen-dig2/src/endpoints/scope.cpp`, `events.cpp`). `python -m alab_daq.core.rawdata FILE.raw` says what a file holds; `alab_daq.core.rawdata.RawFile` reads one in Python (`len(f)`, `f.event(k)`, `f.headers()`, `f.batches()`).

**ROOT files:** one TTree `events` per board file, one entry per trigger: `trigger_id` (UInt_t), `timestamp` (ULong64_t, 8 ns since the start of the run, the same clock on every board of a chain), `time_ns`, `flags`, `board_fail`, `samples_overlapped`, `ch_mask`, `n_samples`, and `ch0`, `ch1`, … (UShort_t[n_samples], raw ADC counts) for each channel read out; plus the strings `settings` (the header) and `note`, and a TTree `hk`: the board's housekeeping file, one entry per reading, a Double_t branch per column (an empty cell is NaN). In PyROOT: `t = ROOT.TFile("run00012_b0.root").events; t.GetEntry(0); list(t.ch0)`. Each file is read back and compared with the `.raw` file before it gets its name. By hand: `python -m alab_daq.core.rootconv NAME.raw`.

## What is verified and what is not

Verified here, with the simulated boards (`./run_tests.sh`, 37 tests; and in headless Chromium): the raw format against CAEN's decoder layout (round trips, cut files, split files), CAEN's sync recipe value by value, every trigger setup (common triggers give the same trigger IDs and timestamps on every board), Stop, Kill, a server restart during a run, failures (a board that does not answer, refuses a parameter, is not armed, loses its link), ROOT files (also read with ROOT 6.36 itself), the board check, and every page in light, dark, English, Chinese and on a phone.

**Not verified: the real boards.** To be checked on the first run with them:
- the parameter names that are not in CAEN's demos but come from the WaveDump2 manual: `IOlevel`, `DecimationFactor`, `GPIOMode`, `VetoSource` (ALAB DAQ tries `BoardVetoSource` if refused), and the trigger and output values (`TrgIn`, `ITLA`, `SwTrg`, `TestPulse`). A board that refuses a parameter stops the configuration with its name, and the Boards tool shows the board's own parameter list; **Expert parameters** set anything directly;
- the threshold convention (relative, −N with Fall for negative pulses), and whether the run delays and clock-out delays need tuning for the cables (CAEN's values are the defaults);
- `ITLConnect` per channel: firmware without it gets `ITLAMask` instead (tested with the simulator).

## Install and update (on the DAQ computer)

The DAQ computer is an Ubuntu computer: Ubuntu 24.04 LTS or newer (Python 3.12 or 3.13; PyPI has the pinned versions of `requirements.lock` for Python 3.11–3.13). Ubuntu 22.04 has Python 3.10, too old: install `python3.11` and `python3.11-venv` (deadsnakes PPA) and run `PYTHON=python3.11 ./setup.sh`, `sudo PYTHON=/usr/bin/python3.11 ./build.sh`. Once, what the scripts need:

```bash
sudo apt install git python3-venv build-essential libboost-dev
git clone git@github.com:OWNER/ALAB_DAQ.git      # the GitHub repository of this folder
cd ALAB_DAQ
```

1. Once, as your normal user (no sudo), set up this folder: the exact packages of `requirements.lock` from PyPI into `wheels/`, and the Python environment `ALAB_ENV/`:

   ```bash
   ./setup.sh
   ```

2. Once, the CAEN libraries (FELib v1.4.0 and Dig2 v1.9, open source from CAEN's GitHub, pinned in the script) into `caen/`, as your normal user (no sudo; a few minutes):

   ```bash
   ./caen_libs.sh                    # --jobs N, --with-boost DIR (see --help)
   ```

   Or install CAEN FELib and CAEN Dig2 from CAEN's site and give their folder on the Settings page. Without them only the simulator runs.

3. The first build: it goes to `/opt/ALAB_DAQ_WEB` (with the CAEN libraries), adds the command `alab-daq-web`, installs the systemd service `alab-daq-web` that runs the server as the account you name (the one that writes the data folder), and makes the first admin account:

   ```bash
   sudo ./build.sh --service-user ACCOUNT --admin NAME
   ```

4. Every update: `git pull`, then `sudo ./build.sh` (a run that is going goes on; `./setup.sh` first when `requirements.lock` changed). Check a build with `./run_tests.sh --build /opt/ALAB_DAQ_WEB`; `BUILD_INFO` says its version, git commit and CAEN libraries.

5. Accounts from the command line, as the service account: `sudo -u ACCOUNT alab-daq-web user add alice --role operator` (also `user list`, `user password NAME`, `user role NAME ROLE`, `user remove NAME`).

6. The service: `sudo systemctl status alab-daq-web`, `sudo systemctl restart alab-daq-web`, `sudo journalctl -u alab-daq-web`. Remove everything: `sudo systemctl disable --now alab-daq-web; sudo rm -rf /opt/ALAB_DAQ_WEB /usr/local/bin/alab-daq-web /etc/systemd/system/alab-daq-web.service`.

To try the page from this folder without the service, as yourself: `./run_web.sh serve --port 8771` (your own settings and accounts; turn on the simulator on the Settings page to run without boards).

**The lab's setup** is the starting configuration (and **New**): the VX2730 (16 inputs, 500 MS/s) at `dig2://192.168.0.72` is the master and self-triggers; its CLK and TRG go along the chain to the VX2740s (64 inputs, 125 MS/s) at `dig2://192.168.0.75` and `dig2://192.168.0.85`; every board records 4.096 µs, 1.024 µs of it before the trigger. **Add board** for more VX2740s.

**The boards' network:** the boards are at 192.168.0.x, the lab's computers at 192.168.1.x. On one switch the DAQ computer reaches the boards only with an address in 192.168.0.0/24 too, e.g. `sudo ip addr add 192.168.0.10/24 dev ETH` (lasting: in netplan), then `ping 192.168.0.72`.

**The first run with real boards:** check each board's address (`dig2://IP` or `dig2://usb:PID`) and model, cable the chain, press **Check boards** (every row green with the right model and serial), start a short run with the *periodic* trigger, and read *What this board gets* and the DAQ output if a board refuses something.

## Versions and git

vMAJOR.MINOR.PATCH (on the page, top left; in `alab-daq-web --version`, `BUILD_INFO`, every `run.json` and every data file's header). Every change adds 1 to PATCH; MINOR and MAJOR change only when you choose so. Each version is one commit with the tag `vX.Y.Z`, and [CHANGELOG.md](CHANGELOG.md) says what changed. `version.sh` never pushes: `git push origin main --tags` sends the commits and tags to GitHub when you choose. `./version.sh` shows the version; `./version.sh minor -m "..."`, `./version.sh major -m "..."`, `./version.sh commit` (after changing the number in `src/alab_daq/__init__.py` by hand).

**Only the code, the scripts and their documents are in git**, never data: `.gitignore` keeps out the Python environment, the downloaded packages, the CAEN libraries, the test files, and every data file wherever it lands (`*.raw`, `*.root`, `*.evt`, `*.log`, `data/`, `runs/`, run records, configurations saved into this folder), and `./run_tests.sh` fails if git tracks anything else or a file over 200 kB. `git status --short --ignored` shows what is kept out.

## Safety

The server listens on 127.0.0.1 only and answers only to local addresses, so it is reached through ssh and cannot be tricked into answering another website. Passwords are kept as salted scrypt hashes; the sign-in cookie cannot be read by scripts and is never sent by other websites; every change must come from the page itself; five wrong passwords pause a name for 30 s. The server runs as the service account, never as root; the CAEN library runs only in the DAQ program and in the board check, never in the server.

## Tests

```bash
./run_tests.sh                                # this source folder (about a minute)
./run_tests.sh --build /opt/ALAB_DAQ_WEB      # a build
```

They need no browser and no hardware (simulated boards). Everything they write stays in `.test_tmp/`.

## Files

| Path | Content |
|---|---|
| `setup.sh`, `caen_libs.sh`, `build.sh`, `run_web.sh`, `run_tests.sh` | setup, the CAEN libraries, build and service, start (or the account commands), tests |
| `version.sh`, `CHANGELOG.md` | the version number and what changed in each version |
| `src/alab_daq/core/` | no web: `rawdata.py` (the data format), `config.py` (the configuration, its check, and every board's parameters: the sync recipe and the trigger setups), `models.py`, `rootconv.py` |
| `src/alab_daq/daq/` | the DAQ program: `engine.py` (one run of all boards), `boards.py` (a board through CAEN FELib), `sim.py` (the simulated boards) |
| `src/alab_daq/web/` | the server: accounts, the run, the board check, ROOT conversion, data files, the JSON interface and live events |
| `src/alab_daq/web/static/` | the page (HTML, CSS, JavaScript modules); `i18n/zh.json` the Chinese texts; `vendor/uplot` the plot library (uPlot, MIT) |
| `tests/` | automatic tests |
| `wheels/`, `ALAB_ENV/`, `caen/` | made by `setup.sh` and `caen_libs.sh` (not in git) |

A new tool for the Tools page is one module in `src/alab_daq/web/static/js/tools/`, listed in `tools/index.js` (see the comment there), with its texts in `i18n/zh.json` (`./run_tests.sh` says which are missing).

## If something goes wrong

| What you see | What to do |
|---|---|
| The page does not open | Is the ssh -L connection still open? Does the server run: `sudo systemctl status alab-daq-web`? |
| A board's row stays red after **Check boards** | Its error is in the Boards tool: the address, the cable or network (can the DAQ computer `ping` it? the boards are on 192.168.0.x), the board's power; `CAEN FELib cannot be loaded` means step 2 of the install |
| "it has 32 channels; the configuration has 16" | **Use what the boards said** on the Configuration page (after Check boards), or set that board's channels |
| "it runs the … firmware; ALAB DAQ needs the Scope firmware" | Load the Scope firmware (the one WaveDump2 uses) with CAEN's tools |
| "the board refused /par/… " | The firmware does not know that parameter or value: see the board's own list in Tools → Boards, and correct it with an expert parameter (or tell the maintainer) |
| "… sent no data in this run: it did not start" | In a chain: the CLK-IN cable of that board, and the order of the boards on the Configuration page (the first is the master) |
| "no stop record within 10 s" | The end of that board's data may be missing; check its link |
| A red ROOT line | The `.raw` file is fine; see `rootconv.log` in the run folder, and convert it again on the Tools page |
