"""The command vx27webdaq (run_web.sh): the web server, and its accounts.

    vx27webdaq [serve] [--port 8765] [--host 127.0.0.1]   run the server (its systemd service does this)
    vx27webdaq user list
    vx27webdaq user add NAME [--role viewer|operator|admin]
    vx27webdaq user password NAME
    vx27webdaq user role NAME ROLE
    vx27webdaq user remove NAME

The accounts belong to the account that runs the server: run the user commands as that account, e.g.
sudo -u ACCOUNT vx27webdaq user add alice --role operator. Passwords are asked for twice (or read from
standard input with --password-stdin).
"""

from __future__ import annotations

import argparse
import getpass
import os
import socket
import sys

from vx27webdaq import __version__
from vx27webdaq.web.accounts import ROLES, AccountError, Accounts
from vx27webdaq.web.settings import DEFAULT_PORT, state_dir


def _password(args) -> str:
    if args.password_stdin:
        return sys.stdin.readline().rstrip("\n")
    first = getpass.getpass("Password: ")
    if getpass.getpass("Password again: ") != first:
        raise AccountError("the two passwords differ")
    return first


def _accounts() -> Accounts:
    return Accounts(state_dir() / "users.json")


def serve(args) -> int:
    if os.geteuid() == 0:
        print("vx27webdaq: the server never runs as root: run it as a normal account (its systemd service does)",
              file=sys.stderr)
        return 1
    for var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ.setdefault(var, "1")
    from aiohttp import web

    from vx27webdaq.web.server import create_app, is_loopback
    app = create_app()
    station = app["station"]
    print(f"Vx27webDAQ web v{__version__}: http://{args.host}:{args.port}  (server account: {getpass.getuser()})")
    if not (args.host == "localhost" or is_loopback(args.host)):
        print(f"WARNING: --host {args.host} listens on the network, but the server answers only requests from this "
              "computer itself: the others are refused. Use the default 127.0.0.1 and ssh -L.", file=sys.stderr)
    print(f"On your computer: ssh -L {args.port}:localhost:{args.port} {socket.gethostname()}, then open http://localhost:{args.port}")
    if not station.accounts.names():
        print("No accounts yet: make the first admin with  vx27webdaq user add NAME --role admin")
    sys.stdout.flush()
    web.run_app(app, host=args.host, port=args.port, print=None, access_log=None, shutdown_timeout=5)
    return 0


def user(args) -> int:
    acc = _accounts()
    if args.action == "list":
        rows = acc.listing()
        if not rows:
            print("no accounts yet")
        for u in rows:
            print(f"{u['name']:<20} {u['role']:<9} last sign-in {u.get('last_login') or 'never'}")
    elif args.action == "add":
        name = acc.add(args.name, _password(args), args.role)
        print(f"added {name} ({args.role})")
    elif args.action == "password":
        acc.set_password(args.name, _password(args))
        print(f"new password for {args.name}: every browser signed in as {args.name} is signed out")
    elif args.action == "role":
        acc.set_role(args.name, args.role)
        print(f"{args.name} is {args.role} now")
    elif args.action == "remove":
        acc.remove(args.name)
        print(f"removed {args.name}: every browser signed in as {args.name} is signed out")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="vx27webdaq", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"Vx27webDAQ web v{__version__}")
    sub = ap.add_subparsers(dest="command")
    sv = sub.add_parser("serve", help="run the web server")
    sv.add_argument("--host", default="127.0.0.1", help="address to listen on (default 127.0.0.1: this computer only)")
    sv.add_argument("--port", type=int, default=DEFAULT_PORT)
    us = sub.add_parser("user", help="the accounts of the page")
    us.add_argument("action", choices=["list", "add", "password", "role", "remove"])
    us.add_argument("name", nargs="?")
    us.add_argument("role_arg", nargs="?", metavar="ROLE", help="for role: the new role")
    us.add_argument("--role", choices=ROLES, default="viewer", help="for add (default viewer)")
    us.add_argument("--password-stdin", action="store_true", help="read the password from standard input")
    args = ap.parse_args(argv)
    if args.command in (None, "serve"):
        if args.command is None:
            args = sv.parse_args([])
        return serve(args)
    if args.action != "list" and not args.name:
        ap.error(f"user {args.action} needs a NAME")
    if args.action == "role":
        if args.role_arg not in ROLES:
            ap.error(f"user role NAME ROLE: ROLE is one of {', '.join(ROLES)}")
        args.role = args.role_arg
    try:
        return user(args)
    except AccountError as err:
        print(f"vx27webdaq: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
