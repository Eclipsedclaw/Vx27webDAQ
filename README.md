# Vx27webDAQ

A web page for multi-board data taking with CAEN Digitizer 2.0 boards (**VX2730**, **VX2740**, also V2745) running the **Scope firmware** (the one WaveDump2 uses). It configures the boards, runs them synchronized on one clock, shows the run live, keeps every board's data losslessly and converts them to ROOT. One server runs on the DAQ computer; everyone opens it in their own browser. You can also run it locally to use it directly. The goal for this development is to have a tool that can completely function as wavedump2 and more!
<img width="2555" height="903" alt="Screenshot 2026-11-09 at 4 51 18 PM" src="https://github.com/user-attachments/assets/d7cf4806-adbd-4b4b-8e61-5a6223f71e78" />


## To use it, Install and update (on the DAQ computer)

```bash
git clone git@github.com:Eclipsedclaw/Vx27webDAQ.git && cd Vx27webDAQ
./setup.sh                                          # once, as yourself: packages and Python environment
./caen_libs.sh                                      # once: the CAEN libraries (only the simulator runs without them)
sudo ./build.sh --service-user ACCOUNT --admin NAME # the first build: /opt/Vx27webDAQ + the service vx27webdaq
git pull && sudo ./build.sh                         # every update (a run that is going goes on)
```

On Ubuntu 22.04 put `PYTHON=/usr/bin/python3.11` before `./setup.sh` and `./build.sh`. Accounts: `sudo -u ACCOUNT vx27webdaq user add NAME --role operator`. Everything else (requirements, the service, upgrading from ALAB DAQ, versions and git): [INSTALL.md](INSTALL.md).

**State (v0.1.29, 2026-11-10):** complete and tested with simulated boards, also by a full system test (every trigger setup, odd requests to every route of the server, whole runs with restarts and board failures, a robot clicking every control); **not yet tried on real boards** (none was reachable from the computer where it was built). See [What is verified](tests/README.md#what-is-verified-and-what-is-not).

## Open the page

```bash
ssh -L 8770:localhost:8770 DAQ-COMPUTER          # on your computer; keep it open
```

Then open **http://localhost:8770** (VS Code Remote-SSH: add port 8770 on the **Ports** tab). Without signing in you watch; **Sign in** (top right) gives you your account's role:

| Role | May |
|---|---|
| **viewer** | watch everything, open data files, convert a file to ROOT |
| **operator** | also check the boards, start and stop runs, edit the configuration and the settings |
| **admin** | also manage the accounts and read the activity log |

## A run in five steps

1. **Configuration** page: the boards in chain order (the first is the master), the trigger setup, each board's record length, channels, thresholds and trigger mask, and the data folder. Edits are kept on the server; **Save** writes them to a `.json` file.
2. **Run** page → **Check boards**: every board's row turns green with its model and serial.
3. **Send to boards** (optional): each board is reset and given the whole configuration now, and says whether it takes it. *Start run* does the same anyway, so a run always uses the configuration on the page.
4. **Start run** (with a note about the run). Watch it on the tabs **Plot** (waveforms), **Progress** (rates, sync check, statistics) and **Setup** (the boards and their cables). **Stop** ends it cleanly.
5. The data: `/NAS/ALAB_DAQ_DATA/runNNNNN_BOARD.raw`, converted to `.root` after the run (and `runNNNNN_built.root` with every board per trigger). Open them on the Run page (**Source: File**) or in **Tools → Data file**.

**Without boards:** Settings → *simulator* on → Save settings. Every run then warns that it is simulated, and its data go to `/NAS/ALAB_DAQ_DATA/simulated/`.

## The pages

| Page | Use it to |
|---|---|
| **Run** | check the boards, send them the configuration, start and stop runs, run sequences (repeats or scans), force a trigger; waveforms (one board's channels, or several boards in one plot), FFT, histograms, measurements, live tuning of thresholds and DC offsets |
| **Configuration** | the run, the trigger setup, every board and channel, the front panel and test signals, expert parameters; *What this board gets* lists the exact CAEN parameters |
| **Tools** | overview of all channels, board housekeeping (temperatures, rates, dead time), data files (ROOT, every board per trigger, text export), the boards' parameter trees and a manual controller, ROOT conversions |
| **Settings** | the server's settings (CAEN libraries, simulator, folders, ROOT after the run) and this browser's theme and language (English, 中文, 日本語) |
| **Users** | (admins) the accounts and the activity log |

Every control in detail: [src/vx27webdaq/web/README.md](src/vx27webdaq/web/README.md).

## Where things are

| What | Where |
|---|---|
| Each board's data | `DATA_FOLDER/RUN_BOARD.raw` (default `/NAS/ALAB_DAQ_DATA/`), its settings in `RUN_BOARD.json` next to it |
| ROOT files | `RUN_BOARD.root` next to it; every board per trigger: `RUN_built.root` |
| Board housekeeping | `RUN_BOARD_hk.csv` next to the data |
| Run records | `/NAS/ALAB_DAQ_DATA/<date>_<time>/` (configuration, DAQ log, `run.json`) |
| Simulated runs | `/NAS/ALAB_DAQ_DATA/simulated/` |
| The configuration opened first | `/NAS/ALAB_DAQ_DATA/alab_daq_test.json` |

The file formats: [src/vx27webdaq/core/README.md](src/vx27webdaq/core/README.md).

## If something goes wrong

| What you see | What to do |
|---|---|
| The page does not open | Is the `ssh -L` still open? Does the server run: `sudo systemctl status vx27webdaq`? |
| A board stays red after **Check boards** | Tools → Boards shows why: the address, the network (`ping` it; the boards are on 192.168.0.x), its power; *CAEN FELib cannot be loaded*: run `./caen_libs.sh` and build again |
| "it has 32 channels; the configuration has 16" | **Use what the boards said** on the Configuration page |
| "the board refused /par/…" | The firmware does not know that parameter or value: see the board's own list in Tools → Boards |
| "… sent no data in this run: it did not start" | That board's CLK-IN cable, and the order of the boards (the first is the master) |
| "… stopped by itself during the run" | That board ended its acquisition on its own, so the run was stopped: its CLK-IN cable from the board before it, its error flags (Tools → Board housekeeping), its network link |
| *not used: …* under the CAEN library folder (Settings) | Other accounts could change that folder: an admin gives one that only root or the service account can change (the build's own `caen/lib` is) |
| A red ROOT line | The `.raw` file is fine: see `rootconv.log` in the run folder and convert it again in Tools |

More: [how a run works, the trigger setups and the real boards](src/vx27webdaq/daq/README.md) · [tests](tests/README.md) · [what changed](CHANGELOG.md) · [how it works, explained for kids](docs/HOW_IT_WORKS_FOR_KIDS.md)
