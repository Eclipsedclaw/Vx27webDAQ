# The server and the page

Back to the [README](../../../README.md). The code here: the server (`server.py` the JSON interface and live events, `station.py`, `runs.py` the run, `sequence.py`, `boardcheck.py`, `converter.py` ROOT conversion, `files.py` data files, `accounts.py`, `settings.py`) and the page in `static/` (HTML, CSS, JavaScript modules; `i18n/zh.json` the Chinese and `i18n/ja.json` the Japanese texts; `vendor/uplot` the plot library, uPlot, MIT).

## Who may do what

| Role | May |
|---|---|
| **viewer** (and anyone not signed in, while guests are allowed) | watch the run and everything on every page; open data files (waveforms, overview); convert a `.raw` file to ROOT |
| **operator** | and control the DAQ: check the boards, start, stop and kill runs; edit, save and open the configuration; change the server settings |
| **admin** | and manage the accounts (add, role, new password, remove), decide whether guests may look in, and read the activity log |

Passwords are set by an admin (Users page, or `vx27webdaq user …`); each user can change their own (account menu, top right). Every action is written to the activity log with the account that did it, and every run's `run.json` says who started and stopped it.

## Run page

The plots take the left; the right column has the tabs **Plot** (every control of the plots), **Progress** and **Setup**, above the DAQ program's output.

**The run bar** (operators):
- **Check boards**: connects to every board and reads its model, serial, firmware and temperatures.
- **Send to boards** (also on the Configuration page): every board switched on is reset and given every parameter of the configuration being edited, now, as at the start of a run, but no data are taken. A line per board says it took them (how many, and the record length it made), a warning (e.g. a test signal it does not have), or the parameter it refused. Start run does the same first anyway, so a run always uses the configuration on the page, saved to a file or not. Not during a run or a sequence.
- **Start run**, **Stop**, **Kill** (15 s after Stop).
- **Sequence…**: runs one after the other without anyone at the page, like WaveDump2's settings scripts: N repeats of T seconds, or a scan of every channel's threshold or DC offset, or of any board parameter, over a list of values, R runs each. *Show the runs* lists them first; each run is a copy of the configuration with its step's change, its note names the step; a failed run stops the sequence; Stop ends the run and the sequence, *Stop after this run* lets it finish.
- **Force trigger** (during a run): one software trigger to the boards that make the triggers: the master when it sends AcceptTrg on TRG-OUT, so every board records it; every board when each triggers by itself. Not with an external trigger or a logic unit, and not when the master sends ITLA: its trigger IDs would no longer match the other boards'.

**Progress**: run time, events and event rate of the first board, data rate of all boards (and its plot), data written, and one row per board (click it to see its waveforms): its place in the chain (**M** = master), state, events, rate, MB/s, the trigger ID of its newest event, its hottest temperature, and how it ended (*stop record* when it sent its last data cleanly).
- **Sync check** (with one trigger for all boards, like WaveDump2's build matching window): for every trigger ID every board has passed, whether every board has it with its timestamp within the *sync window* of the first board's: *N on every board · M out of sync · missing per board*.
- **Statistics** per board (WaveDump2's Statistics panel): the triggers it counted, the triggers it lost (busy, memory full), the incoming trigger rate, the dead time, error flags, and its timestamps against the first board (mean, and lowest … highest: a fixed few ns is the cabling).

**Setup**: the boards and their cables drawn from the configuration: the clock chain CLK-OUT → CLK-IN with the run delays, the trigger TRG-OUT → TRG-IN with what each TRG-OUT sends, a pulser or logic unit, the GPIO busy line, what each board triggers on and starts with, and per board its state, events, rate, temperature, error flags and Δt against the first board.

**Waveforms, view Channels**: the channels of one board, each in its own plot, live (the newest event) or from a `.raw` or ROOT file, event by event.
- **Channels…**: click channels to show or hide them, at most 16 at a time; quick choices 0–15, 16–31, …, *read out*, *in trigger*; each board keeps its own choice in this browser.
- The x axes give each sample and its time from the trigger at the board's own sample time (2 ns on a V2730, 8 ns × decimation on a V2740). The cursor is one dashed line on every channel plot at once, labelled *sample N · time*.
- ADC range Auto, Full scale (the board's 14 or 16 bits), Custom or baseline ± N; each plot can also have its own y range (its sliders button); dashed trigger and threshold lines.
- Drag or wheel to zoom all plots, or **Measure** a range: min, max, mean, σ, peak, the values at both edges and their difference (like WaveDump2's two cursors), area in ADC·ns.
- **Plot**: Waveforms, **FFT** (each waveform's spectrum, its mean taken off and a Hann window, in dB of full scale up to half the sampling rate: 250 MHz on a VX2730, 62.5 MHz on a VX2740) or **Histogram** (how often each ADC value came, over the events so far). **Average**: each event, or the mean of the newest 4, 16 or 64 (waveforms and FFT; **Restart** starts the means and histograms again). **Y axis** ADC counts or mV from the baseline.
- **Tune…** (operators, during a run): a shown channel's DC offset or threshold goes to the board at once, like WaveDump2's oscilloscope tools; *keep for the next runs* puts it into the configuration too; every change is in the DAQ output, `run.json` and the run's status.

**Waveforms, view Combined**: channels of several boards in one plot (up to 16, each its own color), from one moment.
- Live, the DAQ keeps the first event of every 0.27 s timestamp cell of each board and shows the newest cell all boards have, so with the clock chain and a common trigger every trace is the same trigger (the page says the trigger IDs and how far the timestamps differ). From files: event *k* of the file opened and the event with the same trigger ID in the run's other files.
- The time axis is µs from the trigger and covers every chosen board's window (2 ns and 8 ns boards together); **Time 0** = each board's trigger, or the boards placed by their **timestamps** (a pulse split into two boards lines up); it also gives the sample numbers of a board you pick, and the cursor line and readout give each trace's sample.
- **Y axis**: mV from each channel's baseline (each model's input range, 2 Vpp: one count is 0.122 mV on a VX2730, 0.031 mV on a VX2740), ADC counts from the baseline, or raw ADC counts. **Y range** Auto, from–to or ± N. Point at the plot for every trace's value, drag to zoom.

## Configuration page

Everything a run does:
- the **Run**: run time, event limit, clock chain or independent boards, data folder, file size;
- the **Trigger** setup (the cabling of each is explained next to it; with channel triggers the **board trigger** from logic A (ITLA), logic B (ITLB), A AND B or A OR B, each logic with its own OR/AND/majority and gate);
- each **board** in chain order: name, address, model, channels (its inputs; **Use what the boards said** takes each board's model and channels from the last board check), record length and pre-trigger (in samples, with the time), decimation, the chain delays;
- **Trigger** (on a board that triggers on its own channels, as WaveDump2 shows it): the **mask**, a button per channel to put it in the board trigger or take it out, *Select all*, *None*, *Read out only*; and every channel's **threshold** in a grid, with *same for all*; a board that takes its trigger on TRG-IN says so instead;
- **Front panel and test signals** (WaveDump2's Board settings): TRG-OUT and GPIO as the setup needs them or any signal, e.g. the internal test pulse; an acquisition veto from S-IN, GPIO or LVDS with width and polarity; a hardware start of the master on S-IN, its edge or the first TRG-IN pulse; the timestamp reset on S-IN or GPIO; DAC OUT: a level, ramp, 5 MHz sine, square, pulses or a copy of a channel. A value a board does not know is a warning and the run goes on without it;
- its **Channels**: read out, DC offset, threshold, pulse direction, in the board trigger, its group A or B when the board trigger uses both channel logics, gain on a 2745, with *apply to all*;
- **Expert parameters** (any CAEN FELib parameter, `PATH = VALUE`), and **What this board gets**: the exact FELib parameters it will be given, in order.

The **Note** says what the runs are about; the **Check** says what the DAQ program would refuse. **Same window on all…** gives every board the same window in time; **Set all thresholds…** one threshold to every channel. Edits are kept on the server (also over a restart); **Save** writes a `.json` file; **Send to boards** gives the boards the configuration as it is now.

## Tools page

- **Overview of all channels**: baseline, noise σ or peak of every channel of every board as a heat map, live or for a file; 32 channels a row, each board its own color range.
- **Board housekeeping**: live or for a data file: each board's temperatures, trigger rates, dead time, counters, fans and DC-DC, channel trigger rates over the run, and every value of the newest reading.
- **Data file**: open a `.raw` or ROOT file: its waveforms on the Run page, its overview, a ROOT file of it, **every board per trigger** (its run's `RUN_built.root`), a **text file** of some of its events.
- **Boards**: what each board said at the last check, and its whole parameter tree with a filter (the names and allowed values for the expert parameters); the **manual controller** (WaveDump2's; operators, no run going): get or set one parameter of a board or send it a command, a click on a parameter fills it in; the next run resets the board and sets it from the configuration.
- **ROOT conversions**.

A new tool is one module in `static/js/tools/`, listed in `tools/index.js` (see the comment there), with its texts in `i18n/zh.json` and `i18n/ja.json` (`./run_tests.sh` says which are missing).

## Settings and Users

**Settings**: the server's settings (the configuration file, the CAEN library folder with whether both libraries are found, the simulator and its options, the save folder and the data folders, ROOT after the run) and this browser's theme, language (English, 中文, 日本語) and layout. **Users** (admins): the accounts, whether guests may look in, and the activity log.

Every page works in light and dark and in every language; the dividers can be dragged.

## Safety

The server listens on 127.0.0.1 only and answers only to local addresses, so it is reached through ssh and cannot be tricked into answering another website. Passwords are kept as salted scrypt hashes; the sign-in cookie cannot be read by scripts and is never sent by other websites; every change must come from the page itself; five wrong passwords pause a name for 30 s. The server runs as the service account, never as root; the CAEN library runs only in the DAQ program and in the board check, never in the server.
