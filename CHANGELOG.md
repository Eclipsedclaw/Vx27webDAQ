# Changelog

What changed in each version of ALAB DAQ. Write the notes of the next version under "Unreleased";
`./version.sh patch|minor|major` (or `commit`) moves them under the new version.

## Unreleased

## v0.0.9 (2026-11-08)

- Every board per trigger, and text export. `core/eventbuild.py` writes `RUN_built.root` (WaveDump2's multi-board single file): one entry per event of the first board, each board's event with the same trigger ID (continuous over the 24-bit wrap) within the sync window, with found flags, timestamp differences and the samples; queued after every run of several boards (Settings: build_events) and from the Data file tool. The Data file tool also downloads events as text (WaveDump2's ASCII layout).

## v0.0.8 (2026-11-08)

- Front panel, test signals and the second channel logic. Each board's TRG-OUT, GPIO, acquisition veto (source, width, polarity), hardware start of the master (S-IN level or edge, first trigger, LVDS, P0), timestamp reset and DAC OUT (level, ramp, sine, square, pulse emulator, channel copy) can be set (WaveDump2's Board settings), with checks where they would break the trigger chain; settings whose names a board may not have are optional (a refusal is a warning). The board trigger can use ITLB too: logic A, B, A AND B or A OR B, each channel in group A or B. A hardware start makes the DAQ wait for the master's start signal instead of sending SwStartAcquisition.

## v0.0.7 (2026-11-08)

- Scope tools (WaveDump2's plot panel) on the waveforms: Plot Waveforms, FFT (mean power of the newest N events, mean taken off, Hann window, dBFS) or Histogram (sample values over the events so far); the mean of the newest 4/16/64 waveforms; the y axis in ADC or mV from the baseline; the values at both edges of a measured range and their difference. The server sends every sample when asked (`full=1`, at most 65536 per channel) and each board's mV per count.

## v0.0.6 (2026-11-08)

- Live controls and manual controller. During a run the server gives the DAQ program commands through the run's `control/` folder: **Force trigger** (a software trigger to `C.force_targets`: the master with AcceptTrg on TRG-OUT, which then also accepts SwTrg; every board when each triggers by itself) and **Tune…** on the waveforms (a channel's DCOffset or TriggerThr at once; *keep for the next runs* into the configuration); every change is logged and kept in status.json and run.json. The Boards tool's **manual controller** gets or sets one parameter of an idle board or sends it a command (`python -m alab_daq.daq param`); the simulated boards' parameter trees have types, allowed values, ranges and descriptions.

## v0.0.5 (2026-11-08)

- Housekeeping, statistics and sync check. Every board's housekeeping (all temperature sensors, fans, DC-DC, ErrorFlags, AcquisitionStatus, TriggerCnt, LostTriggerCnt, real/dead/live time, each channel's SelfTrgRate) every `run.hk_s` seconds into `RUN_BOARD_hk.csv` beside its data and into its ROOT file (TTree `hk`), shown in the new tool **Board housekeeping** (live or for a data file) and by Check boards; warnings for error flags, the board-fail bit of the events and an optional temperature (`run.temp_warn_c`). Statistics per board on the Run page: triggers counted, lost, incoming rate, dead time. The sync check (`daq/sync.py`): with one trigger for all boards, every trigger ID every board has passed is counted as on every board within `run.sync_window_ns`, out of sync, or missing on a board, with each board's timestamp difference from the first; shown on the Run page and summed up in the DAQ output and run.json.

## v0.0.4 (2026-11-08)

- Channels and combined view. The waveforms show the channels chosen with **Channels…** (at most 16 at a time, each board its own choice, quick choices by 16, read out, in trigger) instead of pages of 8 or 16. A **Combined** view puts channels of several boards in one plot, from one moment: the DAQ program keeps the first event of every timestamp cell (2^25 ticks, 0.27 s) of each board and writes the newest cell all boards share as `live/combined.set`; from files the run's boards are paired by trigger ID (`.raw` and ROOT). Time in µs from each board's trigger or lined up by timestamps, the window of every board; y in mV from the baseline (each model's 2 Vpp input range), ADC from the baseline or raw ADC. Defaults: the configuration file `/NAS/ALAB_DAQ_DATA/alab_daq_test.json` (the lab's setup until it is saved there) and the data folder `/NAS/ALAB_DAQ_DATA/`.

## v0.0.3 (2026-11-07)

- Master trigger: the lab's setup. A new trigger setup, the master self-triggers and the others follow: the first board (the VX2730) triggers on its own channels and sends its trigger on TRG-OUT (ITLA, or AcceptTrg: only the triggers it took); the VX2740s take it on TRG-IN and pass it on. Each board has its own number of channels (a VX2730 with 16 inputs), with **Use what the boards said** to take model and channels from the board check; the starting configuration is the lab's (VX2730 at 192.168.0.72, VX2740s at .75 and .85, the same 4.096 µs window); thresholds and the threshold lines only where a board triggers on its own channels. README: the boards' network (192.168.0.x).

## v0.0.2 (2026-11-07)

- Ubuntu and GitHub: only code goes into git: .gitignore keeps out every data file wherever it lands (`*.raw`, `*.root`, `*.evt`, `*.log`, `data/`, `runs/`, run records, configurations saved into this folder), and a test fails if git tracks anything but code, scripts and their documents, or a file over 200 kB. setup.sh checks for Python 3.11 or newer and its venv module (Ubuntu: python3-venv) and takes `PYTHON=...`; caen_libs.sh names the Ubuntu packages; the README installs on Ubuntu from the GitHub repository (PyPI has every pinned version for Python 3.11-3.13).

## v0.0.1 (2026-11-07)

- Build check: build.sh's check of the build quoted its test of the caen_felib package wrongly (a Python syntax error inside the shell's quotes), so every build stopped; it now checks the package without loading the CAEN library. Found by a test build into JC/tmp (all 37 tests pass on the read-only build).

## v0.0.0 (2026-11-07)

- First version: a web page for multi-board runs of CAEN Digitizer 2.0 boards (V2740, V2730, V2745) with the Scope firmware, built on the design of XIAOLAB DAQ v1.0.12.
- Its own DAQ program (`python -m alab_daq.daq run`) through CAEN FELib (Python binding caen-felib 1.4.0): connects, checks the firmware and model, sets each board's parameters, arms all boards and starts the master (CAEN's sync recipe from caen-dig2 v1.9), reads every board's raw stream losslessly into `RUN_BOARD.raw` with a JSON header, waits for each board's stop record.
- Clock chain or independent boards; trigger setups: common external trigger (TRG chain), each board self-triggers (ITLA), OR in a logic unit, periodic (software or test pulse); busy veto over GPIO.
- Run page (board table, live waveforms of 8 or 16 channels of any board, file view event by event), Configuration page (run, trigger, boards with channels, expert parameters, and the exact parameters each board gets), Tools (overview heat map, data file, boards with their parameter tree, ROOT conversions), Settings, Users; English and Chinese, light and dark.
- ROOT files per board after the run (uproot; one TTree "events", one entry per trigger), checked against the raw file; the configuration being edited survives a server restart.
- Simulated boards (Settings: simulator) with the parameters, chain start, trigger cables and failures; `caen_libs.sh` builds CAEN FELib and CAEN Dig2 from CAEN's GitHub without sudo; setup, build (systemd service alab-daq-web, port 8770), tests (37).

