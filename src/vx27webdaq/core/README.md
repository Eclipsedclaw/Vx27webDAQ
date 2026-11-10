# The data files

Back to the [README](../../../README.md). The code here needs no web server: `rawdata.py` (the `.raw` format), `config.py` (the configuration, its check, and every board's parameters: the sync recipe and the trigger setups), `models.py` (the board models), `rootconv.py` (ROOT files), `eventbuild.py` (every board per trigger).

## Where they are

| What | Where |
|---|---|
| Where the file choosers open | `/NAS/ALAB_DAQ_DATA/` (opening a data file, a configuration, the data folder, …), when it exists and is a data folder (it is by default); else where they did before |
| The configuration the server opens first | `/NAS/ALAB_DAQ_DATA/alab_daq_test.json` (Settings page: *configuration file*). When it is not there yet, the server starts with the lab's setup and **Save** writes it there |
| Each board's data | `DATA_FOLDER/RUN_BOARD.raw` (the data folder of the configuration; by default `/NAS/ALAB_DAQ_DATA/`, which must exist and be writable by the service account), e.g. `run00012_b0.raw` (the run number is one more than the highest in the data folder); a new file `_001.raw`, `_002.raw`, … at the file size of the configuration |
| Their settings and note | the header of each `.raw` file, and `NAME.json` next to it (the configuration, the note — it follows edits during the run — and the run's facts) |
| ROOT files | `NAME.root` next to `NAME.raw`, after the run (`NAME.root.part` until it is checked) |
| Every board per trigger | `RUN_built.root` beside the run's files, after every run of several boards on the clock chain (Settings), or from the Data file tool (below); not for boards on their own clocks |
| Text export | the Data file tool: events *from … count* (at most 1000) and channels as a download, laid out like WaveDump2's ASCII files |
| Board housekeeping | `RUN_BOARD_hk.csv` next to the board's data (in the run folder's `hk/` when no data are kept), below |
| Run records | `<save folder>/<date>_<time>/`: `config.json`, `daq.log`, `status.json`, `run.json`, `live/`, `rootconv.log`; the save folder is a server setting (default `/NAS/ALAB_DAQ_DATA`, beside the data files; empty means the same) |
| Simulator data | `DATA_FOLDER/simulated/` (by default `/NAS/ALAB_DAQ_DATA/simulated/`), numbered on their own (`run00001`, …), never among the real runs' files; the data folder must exist (the NAS mounted): only `simulated/` is made |
| Server settings | `~/.config/vx27webdaq/web.json` of the service account |
| Accounts, sign-ins, activity log, the configuration being edited, the run being followed, board checks | `~/.local/state/vx27webdaq/` of the service account (only that account can read it) |

## `.raw` files

They keep the board's stream exactly as the board sent it (lossless, like WaveDump2's Fast Save): a 16-byte prefix (`ALABRAW1`, the header length), a JSON header (the board's model, serial, firmware, sample time, every parameter as it was set, the run's configuration and note), then the board's 64-bit big-endian words. Each event:

- word 1 = format 1 (bits 63–60), board fail (56), trigger ID (55–32), size in words (31–0);
- word 2 = flags (63–51), samples overlapped (50–48), timestamp in 8 ns (47–0);
- word 3 = the channel mask;
- then, for every 4 samples, one word per channel of the mask with 4 samples of 16 bits, the first in the lowest bits.

Run start and stop records are special events (format 3). This is the layout of CAEN's decoder (`caen-dig2/src/endpoints/scope.cpp`, `events.cpp`). `python -m vx27webdaq.core.rawdata FILE.raw` says what a file holds; `vx27webdaq.core.rawdata.RawFile` reads one in Python (`len(f)`, `f.event(k)`, `f.headers()`, `f.batches()`). A file whose end is cut off or filled with zeros (a full disk, a crash) is read up to there: every complete event before it is kept, and the file counts as *truncated*. (The `ALABRAW1` mark and the `alab-daq-*` format names date from the project's first name and stay, so every file stays readable.)

**Board names** are part of the file names and of the ROOT branch names: letters, digits and `_ . -`, at most 32. A name may not be `built`, may not end in `_` and three digits (`b0_001` is the second file of `b0`), and no two boards may have names that are the same in lower case or as branch names (`b.2` and `b_2`). A run or board name read from a file's header names other files (its housekeeping file, `RUN_built.root`) only when it is such a name.

## ROOT files

One TTree `events` per board file, one entry per trigger: `trigger_id` (UInt_t), `timestamp` (ULong64_t, 8 ns since the start of the run, the same clock on every board of a chain), `time_ns`, `flags`, `board_fail`, `samples_overlapped`, `ch_mask`, `n_samples`, and `ch0`, `ch1`, … (UShort_t[n_samples], raw ADC counts) for each channel read out; plus the strings `settings` (the header) and `note`, and a TTree `hk`: the board's housekeeping file, one entry per reading, a Double_t branch per column (an empty cell is NaN). In PyROOT: `t = ROOT.TFile("run00012_b0.root").events; t.GetEntry(0); list(t.ch0)`. Each file is read back and compared with the `.raw` file before it gets its name. By hand: `python -m vx27webdaq.core.rootconv NAME.raw`.

## Every board per trigger: `RUN_built.root`

WaveDump2's multi-board single file. TTree `events`, one entry per event of the first board (the reference): `trigger_id`, `timestamp`, `time_ns`, `n_boards`, `complete`, and per board `BOARD_found`, `BOARD_trigger_id`, `BOARD_timestamp`, `BOARD_dt_ns` (0 where the board has no event) and `BOARD_chN` (UShort_t[n], zeros where the board has no event).

Each other board's event is the one with the **nearest timestamp** within the sync window (`sync_window_ns`, 1000 ns by default): a board that missed a trigger misses only that one, and boards that trigger by themselves give their coincidences. An event goes into one entry at most; after a timestamp reset the stretches are matched one by one. The `settings` string says `"match": "timestamp"`, how many events of each board were `found`, and in how many the trigger IDs differ from the reference's (`ids_differ`; with a common trigger they should not). Boards on their own clocks (synchronization *independent boards*) have no common time, so their runs are not built.

By hand: `python -m vx27webdaq.core.eventbuild RUN_BOARD.raw [OUT.root] [--window NS]`; exit code 0 built and checked, 1 built but its entries differ from the triggers, 2 failed, 3 skipped (the boards cannot be matched).

## Board housekeeping: `RUN_BOARD_hk.csv`

Every *housekeeping* seconds (Configuration, Run; 5 s) a row: `time` (Unix s), `run_s`, the temperatures in °C (`temp_air_in`, `temp_air_out`, `temp_core`, `temp_first_adc`, `temp_last_adc`, `temp_hottest_adc`, `temp_dcdc`), fans, DC-DC, `error_flags`, `acq_status`, `trigger_cnt`, `lost_trigger_cnt`, `realtime`, `deadtime`, `livetime` (as the board gives them: CAEN FELib's TempSens…, ErrorFlags, TriggerCnt, LostTriggerCnt, …Monitor), each channel's `chN_trg_rate` (SelfTrgRate, Hz), and `events`, `icr_hz`, `rate_hz`, `dead_frac` worked out between two readings. A value the board does not have is left out. The last row is read after the stop.
