# Tests

Back to the [README](../README.md).

```bash
./run_tests.sh                                # this source folder (about two minutes)
./run_tests.sh -k NAME                        # only the tests whose name contains NAME
./run_tests.sh --build /opt/Vx27webDAQ        # a build made by build.sh
```

They need no browser and no hardware (simulated boards). Everything they write stays in `.test_tmp/` (set `VX27WEBDAQ_TEST_TMP` for another place). `test_core.py`: the raw format, the configuration and every board's parameters, whole runs of the DAQ program, ROOT files, the repository's contents. `test_web.py`: the server without a browser: accounts and roles, whole runs, Stop and Kill, restarts, the board check, Send to boards, sequences, data files, and that every text of the page has its Chinese and Japanese translation.

## What is verified and what is not

Verified with the simulated boards (these tests, and the page in headless Chromium): the raw format against CAEN's decoder layout (round trips, cut files, split files), CAEN's sync recipe value by value, every trigger setup (common triggers give the same trigger IDs and timestamps on every board), Stop, Kill, a server restart during a run, failures (a board that does not answer, refuses a parameter, is not armed, loses its link), ROOT files (also read with ROOT 6.36 itself), the board check, and every page in light, dark, English, Chinese, Japanese and on a phone.

**Not verified: the real boards.** What to check on the first run with them: [the DAQ program's README](../src/vx27webdaq/daq/README.md#the-labs-boards).
