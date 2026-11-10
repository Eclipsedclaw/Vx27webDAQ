"""Accounts of the web page and sign-ins.

Roles, each with the rights of the ones before it:
    viewer    watch everything, open data files, convert them to ROOT
    operator  and control the DAQ: start and stop runs, check the boards, edit and save the configuration,
              change the server settings
    admin     and manage the accounts and whether guests may look in
People who are not signed in are guests: viewers, unless the admins turned guests off.

Passwords are kept only as scrypt hashes (hashlib, salted). users.json and sessions.json are readable by
the service account alone. The command line (python -m vx27webdaq.web user ...) changes the same file
while the server runs; the server reads it again when it changes.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from contextlib import contextmanager
from pathlib import Path

from vx27webdaq.web.settings import write_atomic

ROLES = ("viewer", "operator", "admin")
NAME = re.compile(r"^[a-z][a-z0-9_.-]{1,31}$")
MIN_PASSWORD = 8
RESERVED = {"guest", "server"}     # names the page uses for people not signed in and itself
SCRYPT = (2 ** 14, 8, 1)            # n, r, p: about 16 MB and 50 ms per check
SESSION_IDLE_S = 7 * 24 * 3600      # a sign-in ends after a week without use
SESSION_MAX_S = 30 * 24 * 3600      # and after 30 days in any case


class AccountError(ValueError):
    """A request about an account that cannot be done (the message says why)."""


def rank(role: str | None) -> int:
    """0 for no access; 1 viewer, 2 operator, 3 admin."""
    return ROLES.index(role) + 1 if role in ROLES else 0


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    n, r, p = SCRYPT
    digest = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=32)
    return f"scrypt${n}${r}${p}${_b64(salt)}${_b64(digest)}"


def check_password(password: str, stored: str) -> bool:
    try:
        kind, n, r, p, salt, digest = stored.split("$")
        if kind != "scrypt":
            return False
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                             dklen=len(base64.b64decode(digest)))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, base64.b64decode(digest))


_DUMMY_HASH = hash_password(secrets.token_hex(8))      # checked for unknown names, so they take as long


def check_name(name: str) -> str:
    name = (name or "").strip().lower()
    if not NAME.match(name):
        raise AccountError("a user name has 2 to 32 characters: a letter first, then letters, digits, . _ or -")
    if name in RESERVED:
        raise AccountError(f"{name} is kept for people who are not signed in")
    return name


def check_new_password(password: str) -> None:
    if len(password or "") < MIN_PASSWORD:
        raise AccountError(f"a password needs at least {MIN_PASSWORD} characters")


class Accounts:
    """users.json: {name: {role, hash, created, changed, last_login, pw_changed}}. pw_changed (Unix time) is
    when the account was made or its password last set: a sign-in from before it no longer counts, so a new
    password (here or on the command line) and a removed account sign every browser out."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._users: dict[str, dict] = {}
        self._mtime = None
        self._reload()

    # ---- the file ----
    @contextmanager
    def _locked(self):
        """One writer at a time (the server and the command line), and on the newest contents."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path.parent / (self.path.name + ".lock"), "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self._reload(force=True)
            yield
            write_atomic(self.path, json.dumps({"version": 1, "users": self._users}, indent=2) + "\n")
            self._mtime = self.path.stat().st_mtime_ns

    def _reload(self, force: bool = False) -> None:
        try:
            mtime = self.path.stat().st_mtime_ns
        except OSError:
            self._users, self._mtime = {}, None
            return
        if force or mtime != self._mtime:
            try:
                with open(self.path, encoding="utf-8") as f:
                    data = json.load(f)
                self._users = {k: v for k, v in data.get("users", {}).items() if isinstance(v, dict)}
            except (OSError, ValueError):
                self._users = {}
            self._mtime = mtime

    # ---- reading ----
    def role(self, name: str | None) -> str | None:
        if not name:
            return None
        self._reload()
        user = self._users.get(name)
        return user.get("role") if user and user.get("role") in ROLES else None

    def names(self) -> list[str]:
        self._reload()
        return sorted(self._users)

    def listing(self) -> list[dict]:
        self._reload()
        return [{"name": n, "role": u.get("role"), "created": u.get("created"), "changed": u.get("changed"),
                 "last_login": u.get("last_login")} for n, u in sorted(self._users.items())]

    def admins(self) -> list[str]:
        self._reload()
        return [n for n, u in self._users.items() if u.get("role") == "admin"]

    def verify(self, name: str, password: str) -> bool:
        """True if name has this password. Unknown names take as long, so a guess learns nothing."""
        self._reload()
        user = self._users.get((name or "").strip().lower())
        stored = user.get("hash") if user else None
        ok = check_password(password or "", stored if isinstance(stored, str) else _DUMMY_HASH)
        return bool(user) and isinstance(stored, str) and ok

    def valid_since(self, name: str | None) -> float | None:
        """A sign-in of this account counts only if it was made at or after this time (its password's), else None
        (an account of an older version, without the time: every sign-in counts)."""
        if not name:
            return None
        self._reload()
        user = self._users.get(name)
        t = user.get("pw_changed") if user else None
        return float(t) if isinstance(t, (int, float)) and not isinstance(t, bool) else None

    def mtime(self) -> int | None:
        """When users.json changed last (the command line changes it too), None when there is none."""
        try:
            return self.path.stat().st_mtime_ns
        except OSError:
            return None

    # ---- changing ----
    def add(self, name: str, password: str, role: str = "viewer") -> str:
        name = check_name(name)
        check_new_password(password)
        if role not in ROLES:
            raise AccountError(f"the role is one of {', '.join(ROLES)}")
        with self._locked():
            if name in self._users:
                raise AccountError(f"{name} exists already")
            now = time.strftime("%Y-%m-%d %H:%M:%S")
            self._users[name] = {"role": role, "hash": hash_password(password), "created": now, "changed": now,
                                 "pw_changed": time.time()}     # an old sign-in of a removed account of this name ends
        return name

    def set_password(self, name: str, password: str) -> None:
        check_new_password(password)
        with self._locked():
            self._get(name)["hash"] = hash_password(password)
            self._users[name]["changed"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self._users[name]["pw_changed"] = time.time()    # every sign-in made before ends

    def set_role(self, name: str, role: str) -> None:
        if role not in ROLES:
            raise AccountError(f"the role is one of {', '.join(ROLES)}")
        with self._locked():
            user = self._get(name)
            if user.get("role") == "admin" and role != "admin" and len(self.admins()) <= 1:
                raise AccountError("this is the last admin: make another account admin first")
            user["role"] = role
            user["changed"] = time.strftime("%Y-%m-%d %H:%M:%S")

    def remove(self, name: str) -> None:
        with self._locked():
            user = self._get(name)
            if user.get("role") == "admin" and len(self.admins()) <= 1:
                raise AccountError("this is the last admin: make another account admin first")
            del self._users[name]

    def touch_login(self, name: str) -> None:
        with self._locked():
            if name in self._users:
                self._users[name]["last_login"] = time.strftime("%Y-%m-%d %H:%M:%S")

    def _get(self, name: str) -> dict:
        if name not in self._users:
            raise AccountError(f"there is no account {name}")
        return self._users[name]


class Sessions:
    """Sign-ins: a random token per browser (in a cookie). Only a hash of each token is kept, in
    sessions.json, so that a server restart signs nobody out."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._by_hash: dict[str, dict] = {}
        self._dirty_at = 0.0
        try:
            with open(self.path, encoding="utf-8") as f:
                self._by_hash = json.load(f).get("sessions", {})
        except (OSError, ValueError):
            self._by_hash = {}

    @staticmethod
    def _key(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def create(self, name: str) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        self._by_hash[self._key(token)] = {"name": name, "created": now, "seen": now}
        self.save()
        return token

    def user(self, token: str | None, valid_since=None) -> str | None:
        """The account signed in with this token, or None (also once it has expired). valid_since(name): the
        time before which a sign-in of that account no longer counts (its password changed), or None."""
        if not token:
            return None
        key, now = self._key(token), time.time()
        s = self._by_hash.get(key)
        if s is None:
            return None
        since = valid_since(s["name"]) if valid_since is not None else None
        if now - s["seen"] > SESSION_IDLE_S or now - s["created"] > SESSION_MAX_S or (since is not None and s["created"] < since):
            del self._by_hash[key]
            self.save()
            return None
        if now - s["seen"] > 60:        # a "last seen" a minute old is good enough; saved now and then
            s["seen"] = now
            if now - self._dirty_at > 600:
                self.save()
        return s["name"]

    def end(self, token: str | None) -> None:
        if token and self._by_hash.pop(self._key(token), None) is not None:
            self.save()

    def end_user(self, name: str, keep: str | None = None) -> None:
        """Sign this account out everywhere (but in the browser with token keep)."""
        keep_key = self._key(keep) if keep else None
        self._by_hash = {k: s for k, s in self._by_hash.items() if s["name"] != name or k == keep_key}
        self.save()

    def save(self) -> None:
        self._dirty_at = time.time()
        write_atomic(self.path, json.dumps({"sessions": self._by_hash}) + "\n")
