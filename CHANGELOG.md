# Changelog

What changed in each version of Vx27webDAQ (ALAB DAQ until v0.0.18). Write the notes of the next version under "Unreleased";
`./version.sh patch|minor|major` (or `commit`) moves them under the new version.

## Unreleased

## v0.1.33 (2026-11-10)

- Close file: the Run page's plots have a Close file button beside Open file… (shown while a file is open). It lets go of the file: every plot drops its waveform, means and histogram, the page shows Live again and forgets the file (no reopening at the next visit), and the server closes the files opened there (this one and the run's other boards' files chosen in Board), so their indexes and ROOT caches no longer use memory. A file another page is showing just opens again at its next request. New route `POST /api/files/close`.

## v0.1.32 (2026-11-10)

- Every board's file of a run: a file opened on the Run page (Source: File) brings the other boards of its run. The Board menu lists every board's file beside it (in the run's order, the master first; the part of the same number), and choosing a board opens its file at the same event number, as the Board menu does live; before, a file showed its own board only, and each board's file had to be opened by hand. The files are found by their names (RUN_BOARD.raw, RUN_BOARD_001.raw, …), so each one is opened, and uses memory, only when its board is chosen. `/api/files/info` gives `run_files`.

## v0.1.31 (2026-11-10)

- Close a plot: each channel plot on the Run page has a close button (×). Its channel leaves the channels shown (as in Channels…), the server is no longer asked for it, and the page lets go of what the plot held: its waveform, the means or FFT sums, the histogram, the FFT or histogram plot, and at Pause its samples. The other plots keep their channel, means and histogram (before, a channel taken out moved the plots after it to other cards and their means began again). With every plot closed the page sends no waveform requests and says how to open plots again; a plot that shows no channel now holds nothing.

## v0.1.30 (2026-11-10)

- Negative thresholds, as CAEN's TriggerThr: every threshold of the page is shown and typed as the board gets it, counts from the baseline, negative for negative pulses (e.g. −100 for a SiPM, falling edge), positive for positive ones (rising edge). The sign is the pulse direction: a negative number unticks *positive*, ticking it flips the sign. This holds on the Configuration page (Trigger panel and *same for all*, Channels table and its *all channels* row, Set all thresholds…), in Tune… during a run (only the run's direction: the edge does not change during a run; the server refuses the other sign) and in threshold scans (e.g. −50, −100, …). Before, a negative number was refused ("from 0 to 65535") although the board got −N for negative pulses. The configuration files keep the size and the direction apart (`threshold`, `positive`), so older files and run records mean the same; `/api/run/control` and `/api/live/boards` now take and give the signed number.

## v0.1.29 (2026-11-10)

- Documents for the system test fixes: the READMEs say what changed: event building by time, the stop, the board lock, versioned configuration edits, what Save refuses, the CAEN library folder (admins), data folders and conversions, the sign-in rules. The main README's "If something goes wrong" has two more rows.
- New: `docs/HOW_IT_WORKS_FOR_KIDS.md`, how the program works explained for kids (the boards, the programs, the folders, a run, how to use it, words to know, how bugs are hunted), linked from the README; `build.sh` copies `docs/` into the build.

## v0.1.28 (2026-11-10)

- Paths too long for a file system: a request whose path no file can have (a name too long, or too many links in a row) is refused with a message (400), on every route; it was a server error (500). The repeated system test found it, after the fixes.

## v0.1.27 (2026-11-10)

- System test fixes, the page: the 39 defects that a robot clicking every control (in English, Chinese and Japanese) and a review found in the page are fixed. Each fix was checked by a scripted run in headless Chromium, and the integrated page once more by the robot.
- Configuration page: an edit carries the version of the configuration it started from. When someone else changed the configuration meanwhile, this page's changes are put onto theirs and sent again; only a real clash is refused, with a message. An edit that could not be sent stays on the page, marked "not sent to the server", and is sent again. New, Open and Reload ask before they drop unsent edits, and Save and Send to boards first send what is pending. The channel count is sent when you leave the field (0–64), and the page asks before it removes channels. The Trigger panel follows every change of the channels. A model change resets settings the new model does not have, and a board that is no longer first loses the master's hardware start. Copy is off at 32 boards. Use what the boards said needs a check at the same address. A rename keeps the board's "What this board gets".
- Run page and plots: the live view shows the boards of the run (from the server), not those of the configuration being edited. Means, FFT averages and histograms are kept per board, run or file, and a late answer for another board is dropped. Pause keeps the event for zoom and Measure, and Measure in "mean of N" measures the mean shown. The histogram's last bin is drawn, and a zoom of the FFT or histogram stays. Times use the server's clock. Start and Sequence… are off during Check boards; Check boards and Send to boards are off during a run or sequence. The Setup tab follows changes. The MB/s plot keeps about 2000 points. Averages keep 32-bit copies (half the memory).
- The page and the server: requests time out after 30 s (3 min for the slow ones). The live connection notices 15 s of silence and a sign-out elsewhere, and then shows the sign-in instead of retrying. Dialogs keep the keyboard focus. Edits are sent before the page reloads for a language or theme change. The top bar fits a phone (no sideways scroll).
- Fitting the fixes together: on the Settings page the CAEN library folder is read-only for operators, and the page says why a folder is not used. The data-file chooser says how many files it does not show past 5000. A skipped event building shows its reason (in Tools → ROOT conversions and as a note). Tune… starts from the DC offset and threshold the run has (the server's live board list now gives them). The models' descriptions and 16 new texts are translated, and the page says "a Vx27webDAQ configuration".

## v0.1.26 (2026-11-10)

- System test fixes, web server: the defects that the system test (thousands of odd requests, run scenarios, server restarts) and a review found in the server are fixed, each with a test that fails on the code before (`tests/test_fixes_web.py`).
- Safety: Save refuses the server's own folders, a run's record files and anything inside a run folder, `NAME.json` next to a data file, and any existing file that is not a configuration without a run. It writes through a temporary file with a random name. Board names are checked before they name a file, and paths from a file's header stay inside the data folders. A request's channel list is bounded. Requests with NaN, Infinity or numbers of more than 30 digits are refused, and everything sent to the page is strict JSON. Only clients on this computer are answered, also with `--host 0.0.0.0` (a warning at start-up). The CAEN library folder is a setting for admins only, and other accounts must not be able to write to it.
- Sign-in: wrong passwords tried in parallel are all counted (at most two checks at a time), and changing your password is throttled too. The wrong tries of a name are forgotten after an hour, and a name that cannot be an account gets no check. A new password or a removed account signs out every other browser, and open pages follow role changes at once. The activity log is cut back once it passes 5 MB.
- Runs: the end of a run is always recorded; when `run.json` cannot be written, the summary goes to the state folder's `run_records/`. The boards have one user at a time: Check boards, Send to boards and the manual controller wait for each other, for runs and for sequences. A second Stop is ignored; Stop and Kill survive a server restart, and signals go only to the run's own processes. After a restart the run's live changes come back, its log is followed from its last MB, and an unreadable run folder is tried again every 5 s. A live change that the DAQ program does not take within 6 s is withdrawn. The programs for checking the boards, sending them the configuration and the manual controller are always ended.
- Sequences: no manual Start during a sequence; a step waits up to 2 minutes for busy boards; the simulator setting is fixed for the whole sequence; a note edit keeps the step's part; a first step that cannot start returns an error.
- Files and conversions: the combined view of files pairs the boards by time within the sync window, not by trigger ID. Files in use stay open. The text export is streamed, one at a time. A listing shows at most 5000 entries, and special files (FIFOs, devices) are refused. Housekeeping falls back to the ROOT file's `hk` tree. At most 20 conversions are queued per user, and a conversion is skipped when the free space is less than twice the data. Event building runs after a run only on a clock chain of several boards, and is "skipped" when the boards cannot be matched; the reason is shown in Tools → ROOT conversions, and the result goes to `run.json` "built". Folders on the NAS are looked at outside the server's event loop, so a network folder that hangs no longer stops the server.
- Translations: every action of the activity log (Users page) in Chinese and Japanese (only "Force trigger" had them); the test now finds the actions in the server's code.

## v0.1.25 (2026-11-10)

- System test fixes, core and DAQ program: the 37 defects that a system test of the simulated DAQ and a review found in the core and the DAQ program are fixed, each with a test that fails on the code before (`tests/test_fixes_core_daq.py`).
- Settings sent to the boards: a setting the firmware refuses tries its alternatives first. Before, an alternative for the same parameter was never sent, so a board could get no trigger source at all. Firmware without ITLConnect gets ITLAMask and ITLBMask.
- Event building (`RUN_built.root`): events are matched by the nearest timestamp within the window, not by trigger ID. Before, a board that missed one trigger lost every later event, and self-triggering boards were never matched. Each event goes into one entry at most and is matched stretch by stretch after a timestamp reset. New branch `BOARD_trigger_id`; `BOARD_dt_ns` is 0 when the board has no event there; the settings gain `match`, `found` and `ids_differ`. The batches are sized by bytes (64 MB), not 500 events. A run of boards on their own clocks is not built (exit code 3: skipped).
- Configuration check: board names are refused when they clash in lower case or as ROOT branch names, end in `_NNN`, or are "built". The majority of a channel logic is checked per channel group (A, B). Numbers that are not finite (NaN, Infinity, 1e400) are refused. A switched-off board is checked by its name only, with a warning when it sits inside a chain. A relative data folder always warns. A configuration file with more than 32 boards or a bad board name is refused when read.
- Data files: a zero-filled or cut end of a file makes it "truncated" and keeps every event before it. A crash of the ROOT conversion is exit code 2 (failed), never 1. A run or board name from a file's header names the housekeeping file and `RUN_built.root` only when it cannot lead out of the run's folder.
- The DAQ program: the boards are always stopped and the run's end is always written, also after an error in the run, a disk error at a flush or an unexpected reader error. A board still sending 10 s after the stop gets the stop and disarm itself, and every reader ends after 60 s at the latest. A second Stop during the stop means "do not wait". SIGHUP is a Stop, and a Stop during the setup ends the run before the boards start. A board that stops by itself is an error that ends the run (except at the end of a SINlevel start). With a hardware start the run time counts from the master's first data. The live views are capped (8 MB per live event, 32 MB of kept events per board), and the combined view goes on after a timestamp reset. A housekeeping value that fails once keeps its column. `status.json` is strict JSON (null for a reading that is no number).
- Sync check: only for a common trigger on a clock chain (otherwise "independent boards: no common clock"); IDs that arrive late are counted as late, not missing. Check boards: bad simulator options or a tree folder that cannot be made give a message, not a traceback. `running_daq` counts only real DAQ runs. The simulator forgets old software triggers and has new failure options for the tests (`--fail value:…`, `--fail selfstop:…`, `--sin`).

## v0.1.24 (2026-11-10)

- Version script and ignored files: a version step that fails before its commit (no git identity, a hook, a full disk) puts CHANGELOG.md and `__init__.py` back as they were, so the next try gets the same number; a step that adds new files names them (they go to GitHub once pushed). The boards' housekeeping tables (`*.csv`), the live combined event (`*.set`) and the run's control answers (`*.done`) stay out of git, wherever they land; the repository test checks them.

## v0.1.23 (2026-11-09)

- Simulator: a command takes its time under the crate lock (a reader could produce events past the moment of a stop that the other boards then lacked: the rare failure of the logic-OR test, one board with one trigger more); a test checks it.

## v0.0.22 (2026-11-09)

- A short README for the lab's users (open the page, a run in five steps, the pages, where the data go, install, what to do when something goes wrong); the details moved next to what they describe: INSTALL.md (install, update, upgrading, versions and git, the folders), src/vx27webdaq/web/README.md (every page and control, roles, safety), src/vx27webdaq/daq/README.md (how a run works, the trigger setups, the lab's boards and the first run with them, the simulator), src/vx27webdaq/core/README.md (the data files and their formats), tests/README.md (the tests and what is verified). build.sh copies INSTALL.md into the build too.

## v0.0.21 (2026-11-09)

- Japanese (日本語): a third language beside English and 中文 (top bar and Settings → This browser); every text in i18n/ja.json, the page in Japanese fonts (kanji in their Japanese shapes); the tests check that both translations have every text and every {value}.

## v0.0.20 (2026-11-09)

- Fixed: the page moved only some of the browser's old alab.* choices to vx27.* (adding keys may reorder localStorage: the keys are listed first now).

## v0.0.19 (2026-11-09)

- Renamed ALAB DAQ to Vx27webDAQ: the repository and folder (Vx27webDAQ), the Python package (vx27webdaq), the command and service (vx27webdaq), the build folder (/opt/Vx27webDAQ), the environment (VX27_ENV), the settings and state folders (vx27webdaq: the old alab_daq folders move there by themselves, and a saved CAEN library folder of the old build becomes the new build's). build.sh takes over the old service alab-daq-web (its account and port) and removes it. The data folder /NAS/ALAB_DAQ_DATA and the data files' formats (ALABRAW1, alab-daq-*) are unchanged, so every file stays readable. The build server's name is no longer in the README.

## v0.0.18 (2026-11-08)

- The save folder (run records and users' outputs) is /NAS/ALAB_DAQ_DATA by default, also when the saved setting is empty (it was ~/ALAB_DAQ_runs of the service account); a run that cannot make its folder there says why.

## v0.0.17 (2026-11-08)

- Simulated runs write their data into the data folder's `simulated/` subfolder (e.g. /NAS/ALAB_DAQ_DATA/simulated/run00001_BOARD.raw), numbered on their own, instead of inside the run folder; the Start question and the summary say where. A run does not make the data folder itself (the NAS not mounted).
- Send to boards (Run page and Configuration page, operators): every board switched on is reset and given every parameter of the configuration being edited, now, as at the start of a run, without taking data (`python -m alab_daq.daq apply`); a line per board: taken, warnings, or the parameter refused. Not during a run or a sequence; a run does not start while it goes.

## v0.0.16 (2026-11-08)

- With the simulator on, Start run (and a sequence) warns plainly that simulated boards are used, not the CAEN cards, says how to switch it off (Settings → simulator → Save settings) and offers Open Settings; the start button reads Start simulated run.

## v0.0.15 (2026-11-08)

- Trigger panel on the Configuration page (WaveDump2's style): on every board that triggers on its own channels, the mask as channel buttons (Select all, None, Read out only) and every channel's threshold in a grid with "same for all"; it and the Channels table edit the same values and follow each other.

## v0.0.14 (2026-11-08)

- Pulse generator: on a VX2730 every channel has a fast negative pulse at the same time (the trigger); on a VX2740 a positive pulse with a 5 µs rise (10–90 %) and a 1 ms decay, channel k at k × 5 µs (`--rise-us`, `--decay-us`, `--spacing-us`); no wrapping back to the trigger.

## v0.0.13 (2026-11-08)

- Every file chooser opens at /NAS/ALAB_DAQ_DATA/ (when it exists and is a data folder): data files, configurations, folders.

## v0.0.12 (2026-11-08)

- Pulse generator: one pulse per channel and record (the staircase only; the train is gone)

## v0.0.11 (2026-11-08)

- Plots first, the boards' setup, and a pulse generator. The Run page gives the plots the left and puts their controls into the right column's tab **Plot**, beside **Progress** and the new **Setup** (setupview.js: the boards and their clock, trigger and GPIO cables drawn from the configuration's plans, with live state and sync offsets). Channel plots share one dashed cursor line labelled with sample and time, and each can have its own y range; the combined view gives sample numbers on its time axis, at its cursor and per trace, and has a y range (Auto, from–to, ± N). The simulator's pulse generator (Settings: simulated signal; `--generator staircase`, `--spacing-us`): regular hits, and in every record one full pulse on each channel, negative on a V2730 and positive on a V2740, channel k at k µs after the trigger. Fixed: the DAQ program's log lines could mix when board readers logged at once; the build-events toggle showed the wrong label.

## v0.0.10 (2026-11-08)

- Run sequences (WaveDump2's settings scripts): N repeats, or a scan of every channel's threshold or DC offset (one board or all) or of any board parameter over a list of values, R runs each, every run a copy of the configuration with its step's change and a note naming it (`web/sequence.py`); a failed run stops it, Stop ends the run and the sequence; after a server restart a sequence is marked interrupted.

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

