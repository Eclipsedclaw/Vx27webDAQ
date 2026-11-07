"""The DAQ program of ALAB DAQ.

    python -m alab_daq.daq run RUN_FOLDER [--simulate "OPTIONS"]
        one run of the boards of RUN_FOLDER/config.json (see engine.py); the web server starts it
    python -m alab_daq.daq check CONFIG.json [--simulate "OPTIONS"] [--trees FOLDER] [--board NAME]...
        connect to each board of a configuration (or the ones named), print one JSON line per board with what
        it says it is (model, serial, firmware, channels, temperatures), and save its parameter tree as
        FOLDER/NAME.json; the Check boards button of the page uses it

For the real boards, libCAEN_FELib.so and libCAEN_Dig2.so must be found (LD_LIBRARY_PATH; the server sets it
to the folder caen_libs.sh builds them into).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time


def check(args) -> int:
    from alab_daq.core import config as C
    from alab_daq.daq.boards import BoardError, monitors, open_board
    try:
        cfg, _ = C.load_config(args.config)
    except (OSError, C.ConfigError) as err:
        print(json.dumps({"error": str(err)}), flush=True)
        return 1
    boards = [b for b in cfg.boards if not args.board or b.name in args.board]
    crate = None
    if args.simulate is not None:
        from alab_daq.daq.sim import Crate
        crate = Crate(args.simulate, "none", {b.name: b.model for b in cfg.boards})
    code = 0
    for b in boards:
        t0 = time.monotonic()
        out = {"name": b.name, "uri": b.uri, "ok": False, "time": time.time()}
        board = None
        try:
            board = open_board(b.uri, crate, b.name)
            out["facts"] = board.identify()
            out["monitors"] = monitors(board)
            out["ok"] = True
            if args.trees:
                tree = board.device_tree()
                if tree is not None:
                    os.makedirs(args.trees, exist_ok=True)
                    path = os.path.join(args.trees, f"{b.name}.json")
                    with open(f"{path}.part", "w", encoding="utf-8") as f:
                        json.dump(tree, f)
                    os.replace(f"{path}.part", path)
                    out["tree"] = path
        except BoardError as err:
            out["error"] = str(err)
            code = 1
        finally:
            if board is not None:
                board.close()
        out["seconds"] = round(time.monotonic() - t0, 3)
        print(json.dumps(out), flush=True)
    return code


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m alab_daq.daq", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run", help="one run")
    r.add_argument("run_dir")
    r.add_argument("--simulate", default=None, metavar="OPTIONS", help="simulated boards (no hardware)")
    c = sub.add_parser("check", help="what each board of a configuration says it is")
    c.add_argument("config")
    c.add_argument("--simulate", default=None, metavar="OPTIONS")
    c.add_argument("--trees", default=None, metavar="FOLDER", help="save each board's parameter tree here")
    c.add_argument("--board", action="append", default=[], help="only this board (by name); may repeat")
    args = ap.parse_args(argv)
    if args.command == "run":
        from alab_daq.daq.engine import Run
        try:
            run = Run(args.run_dir, args.simulate)
        except (OSError, ValueError, KeyError) as err:
            print(f"ERROR: cannot read the run's configuration: {err}", flush=True)
            return 1
        return run.main()
    return check(args)


if __name__ == "__main__":
    sys.exit(main())
