# Changelog

What changed in each version of ALAB DAQ. Write the notes of the next version under "Unreleased";
`./version.sh patch|minor|major` (or `commit`) moves them under the new version.

## Unreleased

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

