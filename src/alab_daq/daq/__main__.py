"""The DAQ program of ALAB DAQ.

    python -m alab_daq.daq run RUN_FOLDER [--simulate "OPTIONS"]
        one run of the boards of RUN_FOLDER/config.json (see engine.py); the web server starts it
    python -m alab_daq.daq apply CONFIG.json [--simulate "OPTIONS"] [--board NAME]...
        send the configuration to its boards now (Send to boards): each board switched on is reset and given every
        parameter, as at the start of a run, without taking data; one JSON line per board
    python -m alab_daq.daq param CONFIG.json --board NAME (--get PATH | --set PATH VALUE | --cmd COMMAND)
        one parameter of one board, or one command to it (the Boards tool's manual controller; not during a run)
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
    from alab_daq.daq.boards import BoardError, housekeeping, open_board
    try:
        cfg, _ = C.load_config(args.config)
    except (OSError, C.ConfigError) as err:
        print(json.dumps({"error": str(err)}), flush=True)
        return 1
    boards = [b for b in cfg.boards if not args.board or b.name in args.board]
    crate = None
    if args.simulate is not None:
        from alab_daq.daq.sim import Crate
        crate = Crate(args.simulate, "none", {b.name: b.model for b in cfg.boards}, {b.name: len(b.channels) for b in cfg.boards})
    code = 0
    for b in boards:
        t0 = time.monotonic()
        out = {"name": b.name, "uri": b.uri, "ok": False, "time": time.time()}
        board = None
        try:
            board = open_board(b.uri, crate, b.name)
            out["facts"] = board.identify()
            out["monitors"] = housekeeping(board)
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


def apply(args) -> int:
    """Send the configuration to its boards: each board switched on (in chain order: its place decides its
    settings) is checked like at the start of a run, reset, and given every setting of plan(); a refused setting
    with an alternative, or an optional one, is a warning, any other ends that board (the same rules as a run).
    One JSON line per board: ok, sent (settings set), warnings, error, record_length, pre_trigger."""
    from alab_daq.core import config as C
    from alab_daq.core import models
    from alab_daq.daq.boards import BoardError, open_board
    try:
        cfg, _ = C.load_config(args.config)
    except (OSError, C.ConfigError) as err:
        print(json.dumps({"error": str(err)}), flush=True)
        return 1
    active = [b for _, b in cfg.active()]
    crate = None
    if args.simulate is not None:
        from alab_daq.daq.sim import Crate
        try:
            crate = Crate(args.simulate, "none", {b.name: b.model for b in active},
                          {b.name: len(b.channels) for b in active})
        except ValueError as err:
            print(json.dumps({"error": f"simulator: {err}"}), flush=True)
            return 1
    code = 0
    for k, b in enumerate(active):
        if args.board and b.name not in args.board:
            continue
        out = {"name": b.name, "uri": b.uri, "ok": False, "sent": 0, "warnings": [], "time": time.time()}
        board = None
        try:
            board = open_board(b.uri, crate, b.name)
            f = board.identify()
            if f.get("fw_type", "").lower() != "scope":
                raise BoardError(f"it runs the {f.get('fw_type', 'unknown')} firmware; ALAB DAQ needs the Scope firmware")
            fam, want = models.family(f.get("model", "")), models.family(b.model)
            if fam and want and fam != want:
                raise BoardError(f"it is a {f['model']}; the configuration says {b.model}")
            if f.get("channels") and f["channels"] != len(b.channels):
                raise BoardError(f"it has {f['channels']} channels; the configuration has {len(b.channels)}")
            m = models.from_board(f) if f.get("channels") else models.model(b.model)
            board.cmd("Reset")
            refused: set = set()

            def send(s):
                name = s.path.rsplit("/", 1)[-1].lower()
                if name in refused:
                    return
                try:
                    board.set(s.path, s.value)
                    out["sent"] += 1
                except BoardError as err:
                    if not (s.optional or s.alt):
                        raise BoardError(f"the board refused {s.path} = {s.value}: {err}") from None
                    refused.add(name)
                    out["warnings"].append(f"the board refused {s.path} = {s.value} ({err}); "
                                           + ("its alternative was sent" if s.alt else "a run goes on without it"))
                    for a in s.alt:
                        if a.path:
                            send(a)
            for s in C.plan(cfg, k, m):
                send(s)
            try:
                out["record_length"] = int(float(board.get("/par/RecordLengthS")))
                out["pre_trigger"] = int(float(board.get("/par/PreTriggerS")))
                if out["record_length"] != b.record_length or out["pre_trigger"] != b.pre_trigger:
                    out["warnings"].append(f"the board made the record {out['record_length']} samples and the "
                                           f"pre-trigger {out['pre_trigger']} (asked: {b.record_length}, {b.pre_trigger})")
            except (BoardError, ValueError) as err:
                out["warnings"].append(f"cannot read the record length back: {err}")
            out["ok"] = True
        except BoardError as err:
            out["error"] = str(err)
            code = 1
        finally:
            if board is not None:
                board.close()
        print(json.dumps(out), flush=True)
    return code


def param(args) -> int:
    """Get or set one parameter of one board, or send it a command: one JSON line {ok, value | error}."""
    from alab_daq.core import config as C
    from alab_daq.daq.boards import BoardError, open_board
    try:
        cfg, _ = C.load_config(args.config)
    except (OSError, C.ConfigError) as err:
        print(json.dumps({"ok": False, "error": str(err)}), flush=True)
        return 1
    b = cfg.board(args.board)
    if b is None:
        print(json.dumps({"ok": False, "error": f"no board {args.board} in the configuration"}), flush=True)
        return 1
    crate = None
    if args.simulate is not None:
        from alab_daq.daq.sim import Crate
        crate = Crate(args.simulate, "none", {b.name: b.model}, {b.name: len(b.channels)})
    out, board = {"board": b.name, "ok": False}, None
    try:
        board = open_board(b.uri, crate, b.name)
        if args.get:
            out.update(path=args.get, value=board.get(args.get))
        elif args.set:
            board.set(args.set[0], args.set[1])
            out.update(path=args.set[0], value=board.get(args.set[0]))
        else:
            board.cmd(args.cmd)
            out.update(command=args.cmd)
        out["ok"] = True
    except BoardError as err:
        out["error"] = str(err)
    finally:
        if board is not None:
            board.close()
    print(json.dumps(out), flush=True)
    return 0 if out["ok"] else 1


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
    a = sub.add_parser("apply", help="send the configuration to its boards now, without taking data")
    a.add_argument("config")
    a.add_argument("--simulate", default=None, metavar="OPTIONS")
    a.add_argument("--board", action="append", default=[], help="only this board (by name); may repeat")
    p = sub.add_parser("param", help="get or set one parameter of a board, or send it a command")
    p.add_argument("config")
    p.add_argument("--board", required=True)
    p.add_argument("--simulate", default=None, metavar="OPTIONS")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--get", metavar="PATH")
    g.add_argument("--set", nargs=2, metavar=("PATH", "VALUE"))
    g.add_argument("--cmd", metavar="COMMAND")
    args = ap.parse_args(argv)
    if args.command == "run":
        from alab_daq.daq.engine import Run
        try:
            run = Run(args.run_dir, args.simulate)
        except (OSError, ValueError, KeyError) as err:
            print(f"ERROR: cannot read the run's configuration: {err}", flush=True)
            return 1
        return run.main()
    if args.command == "param":
        return param(args)
    if args.command == "apply":
        return apply(args)
    return check(args)


if __name__ == "__main__":
    sys.exit(main())
