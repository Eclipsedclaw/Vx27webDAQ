"""Small facts about the computer: memory, a DAQ program already running, free disk space, writable folders."""

from __future__ import annotations

import os
import shutil


def memory() -> dict[str, float]:
    """Memory in use as 1 - MemAvailable/MemTotal."""
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            key, value = line.split(":", 1)
            info[key] = int(value.split()[0])          # kB
    total, avail = info["MemTotal"], info["MemAvailable"]
    return {"used_percent": 100.0 * (total - avail) / total,
            "available_gib": avail / 1048576,
            "swap_used_mib": (info.get("SwapTotal", 0) - info.get("SwapFree", 0)) / 1024}


def running_daq(marker: str = "vx27webdaq.daq run") -> list[tuple[int, str]]:
    """(pid, command line) of every process whose command line contains marker, except this one and its parent."""
    found = []
    skip = {os.getpid(), os.getppid()}
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) in skip:
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as f:
                argv = [a.decode(errors="replace") for a in f.read().split(b"\0") if a]
        except OSError:
            continue
        line = " ".join(argv)
        if marker in line and "vx27webdaq.web.wrapper" not in line:
            found.append((int(entry), line))
    return found


def disk_free(path: str) -> int | None:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def folder_writable(path: str) -> bool:
    """True if path is a folder this user can write in, or one that can be made: its first existing parent
    is a writable folder."""
    path = os.path.abspath(path)
    while not os.path.exists(path):
        parent = os.path.dirname(path)
        if parent == path:
            return False
        path = parent
    return os.path.isdir(path) and os.access(path, os.W_OK | os.X_OK)
