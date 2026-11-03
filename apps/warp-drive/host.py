"""Small read-only host signals for warp-drive admission."""

import ctypes
import os
import pathlib
import re
import sys
import time


def _mac_cpu_sample():
    class CpuLoad(ctypes.Structure):
        _fields_ = [("ticks", ctypes.c_uint * 4)]

    lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
    host = lib.mach_host_self()
    load = CpuLoad()
    count = ctypes.c_uint(4)
    if lib.host_statistics(host, 3, ctypes.byref(load), ctypes.byref(count)) != 0:
        return None
    current = tuple(load.ticks)
    prior = getattr(_mac_cpu_sample, "prior", None)
    _mac_cpu_sample.prior = current
    if prior is None:
        return None
    deltas = [max(0, new - old) for new, old in zip(current, prior)]
    total = sum(deltas)
    return (1 - (deltas[2] / total)) if total else None


def _linux_cpu_sample():
    first = pathlib.Path("/proc/stat").read_text().splitlines()[0].split()[1:]
    values = tuple(map(int, first))
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    total = sum(values)
    prior = getattr(_linux_cpu_sample, "prior", None)
    _linux_cpu_sample.prior = (total, idle)
    if prior is None or total <= prior[0]:
        return None
    return max(0.0, min(1.0, 1 - (idle - prior[1]) / (total - prior[0])))


def cpu_busy_sample():
    """Return total busy fraction when the platform exposes a cheap counter."""
    try:
        if sys.platform == "darwin":
            return _mac_cpu_sample()
        if pathlib.Path("/proc/stat").exists():
            return _linux_cpu_sample()
    except (OSError, AttributeError, ValueError):
        return None
    return None


def memory_pressure():
    """Return normal/pressure when available, otherwise preserve unknown."""
    if sys.platform != "darwin":
        return None
    try:
        lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        value = ctypes.c_int()
        size = ctypes.c_size_t(ctypes.sizeof(value))
        result = lib.sysctlbyname(b"kern.memorystatus_vm_pressure_level",
                                  ctypes.byref(value), ctypes.byref(size), None, 0)
        if result != 0:
            return None
        return "normal" if value.value == 1 else "pressure"
    except Exception:
        return None


def swap_usage():
    """Read macOS swap usage without retaining command output or secrets."""
    if sys.platform != "darwin":
        return None
    try:
        import subprocess
        output = subprocess.check_output(["/usr/sbin/sysctl", "vm.swapusage"],
                                         text=True, timeout=1.0)
        match = re.search(r"used\s*=\s*([0-9.]+)M", output)
        return float(match[1]) if match else None
    except Exception:
        return None


def snapshot():
    busy = cpu_busy_sample()
    pressure = memory_pressure()
    swap = swap_usage()
    return {
        "at": time.time(),
        "logical_cpus": os.cpu_count() or 1,
        "cpu_busy": busy,
        "memory_pressure": pressure,
        "swap_used_mb": swap,
        "thermal_state": None,
        "availability": {
            "cpu": busy is not None,
            "memory_pressure": pressure is not None,
            "swap": swap is not None,
            "thermal": False,
        },
    }
