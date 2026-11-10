# Install, update and versions

Back to the [README](README.md).

## The computer

The DAQ computer is an Ubuntu computer: Ubuntu 24.04 LTS or newer (Python 3.12 or 3.13; PyPI has the pinned versions of `requirements.lock` for Python 3.11–3.13). Ubuntu 22.04 has Python 3.10, too old: install `python3.11` and `python3.11-venv` (deadsnakes PPA) and run `PYTHON=python3.11 ./setup.sh`, `sudo PYTHON=/usr/bin/python3.11 ./build.sh`. Once, what the scripts need:

```bash
sudo apt install git python3-venv build-essential libboost-dev
git clone git@github.com:Eclipsedclaw/Vx27webDAQ.git
cd Vx27webDAQ
```

## Install

1. Once, as your normal user (no sudo), set up this folder: the exact packages of `requirements.lock` from PyPI into `wheels/`, and the Python environment `VX27_ENV/`:

   ```bash
   ./setup.sh
   ```

2. Once, the CAEN libraries (FELib v1.4.0 and Dig2 v1.9, open source from CAEN's GitHub, pinned in the script) into `caen/`, as your normal user (no sudo; a few minutes):

   ```bash
   ./caen_libs.sh                    # --jobs N, --with-boost DIR (see --help)
   ```

   Or install CAEN FELib and CAEN Dig2 from CAEN's site and give their folder on the Settings page (an admin does: its libraries run as the service account, so the folder, the folders above it and its files must belong to root or to that account and must not be writable by others; otherwise the folder is not used and the page says why). Without them only the simulator runs.

3. The first build: it goes to `/opt/Vx27webDAQ` (with the CAEN libraries), adds the command `vx27webdaq`, installs the systemd service `vx27webdaq` that runs the server as the account you name (the one that writes the data folder), and makes the first admin account:

   ```bash
   sudo ./build.sh --service-user ACCOUNT --admin NAME
   ```

4. Every update: `git pull`, then `sudo ./build.sh` (a run that is going goes on; `./setup.sh` first when `requirements.lock` changed). Check a build with `./run_tests.sh --build /opt/Vx27webDAQ`; `BUILD_INFO` says its version, git commit and CAEN libraries.

5. Accounts from the command line, as the service account: `sudo -u ACCOUNT vx27webdaq user add alice --role operator` (also `user list`, `user password NAME`, `user role NAME ROLE`, `user remove NAME`). Passwords are set by an admin (Users page, or this command); each user can change their own (account menu, top right).

6. The service: `sudo systemctl status vx27webdaq`, `sudo systemctl restart vx27webdaq`, `sudo journalctl -u vx27webdaq`. Remove everything: `sudo systemctl disable --now vx27webdaq; sudo rm -rf /opt/Vx27webDAQ /usr/local/bin/vx27webdaq /etc/systemd/system/vx27webdaq.service`.

To try the page from this folder without the service, as yourself: `./run_web.sh serve --port 8771` (your own settings and accounts; turn on the simulator on the Settings page to run without boards).

## Upgrading from ALAB DAQ

For v0.0.18 or older (the project was renamed in v0.0.19): in the old clone, `git remote set-url origin git@github.com:Eclipsedclaw/Vx27webDAQ.git`, `git pull`, then `./setup.sh` (the environment is now `VX27_ENV/`; `rm -rf ALAB_ENV` afterwards) and `sudo ./build.sh`. The first build takes the account and port of the old service `alab-daq-web`, stops and removes it and its command, and installs `vx27webdaq` in `/opt/Vx27webDAQ`; the server moves the service account's `~/.config/alab_daq` and `~/.local/state/alab_daq` (settings, accounts, the configuration being edited, the run being followed) to `vx27webdaq`, and each browser keeps its choices. Everyone signs in once more. The data folder `/NAS/ALAB_DAQ_DATA` and the data files' formats stay as they were. Then `sudo rm -rf /opt/ALAB_DAQ_WEB` (build.sh says so).

## Versions and git

vMAJOR.MINOR.PATCH (on the page, top left; in `vx27webdaq --version`, `BUILD_INFO`, every `run.json` and every data file's header). Every change adds 1 to PATCH; MINOR and MAJOR change only when you choose so. Each version is one commit with the tag `vX.Y.Z`, and [CHANGELOG.md](CHANGELOG.md) says what changed. `version.sh` never pushes: `git push origin BRANCH --tags` sends the commits and tags to GitHub when you choose. `./version.sh` shows the version; `./version.sh minor -m "..."`, `./version.sh major -m "..."`, `./version.sh commit` (after changing the number in `src/vx27webdaq/__init__.py` by hand).

**Only the code, the scripts and their documents are in git**, never data: `.gitignore` keeps out the Python environment, the downloaded packages, the CAEN libraries, the test files, and every data file wherever it lands (`*.raw`, `*.root`, `*.evt`, `*.log`, `data/`, `runs/`, run records, configurations saved into this folder), and `./run_tests.sh` fails if git tracks anything else or a file over 200 kB. `git status --short --ignored` shows what is kept out.

## The folders

| Path | Content | Details |
|---|---|---|
| `setup.sh`, `caen_libs.sh`, `build.sh`, `run_web.sh`, `run_tests.sh` | setup, the CAEN libraries, build and service, start (or the account commands), tests | each script's `--help` |
| `version.sh`, `CHANGELOG.md` | the version number and what changed in each version | above |
| `src/vx27webdaq/core/` | no web: the data format, the configuration and every board's parameters, ROOT conversion, event building | [core/README.md](src/vx27webdaq/core/README.md) |
| `src/vx27webdaq/daq/` | the DAQ program: one run of all boards, a board through CAEN FELib, the simulated boards | [daq/README.md](src/vx27webdaq/daq/README.md) |
| `src/vx27webdaq/web/` | the server and the page | [web/README.md](src/vx27webdaq/web/README.md) |
| `tests/` | automatic tests | [tests/README.md](tests/README.md) |
| `docs/` | how it works, explained for kids (also copied into the build) | [docs/HOW_IT_WORKS_FOR_KIDS.md](docs/HOW_IT_WORKS_FOR_KIDS.md) |
| `wheels/`, `VX27_ENV/`, `caen/` | made by `setup.sh` and `caen_libs.sh` (not in git) | |
