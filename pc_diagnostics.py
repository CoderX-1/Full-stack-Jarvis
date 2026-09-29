"""Read-only PC health snapshot with explicit unsupported-sensor reporting."""

from __future__ import annotations

import ctypes
import json
import platform
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class _FileTime(ctypes.Structure):
    _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]


class _MemoryStatus(ctypes.Structure):
    _fields_ = [
        ("length", ctypes.c_uint32), ("load", ctypes.c_uint32),
        ("total_phys", ctypes.c_uint64), ("avail_phys", ctypes.c_uint64),
        ("total_page", ctypes.c_uint64), ("avail_page", ctypes.c_uint64),
        ("total_virtual", ctypes.c_uint64), ("avail_virtual", ctypes.c_uint64),
        ("avail_extended", ctypes.c_uint64),
    ]


class _PowerStatus(ctypes.Structure):
    _fields_ = [
        ("ac", ctypes.c_ubyte), ("battery_flag", ctypes.c_ubyte),
        ("battery_percent", ctypes.c_ubyte), ("saver", ctypes.c_ubyte),
        ("battery_seconds", ctypes.c_uint32), ("battery_full_seconds", ctypes.c_uint32),
    ]


def _ticks(value: _FileTime) -> int:
    return (int(value.high) << 32) | int(value.low)


def _cpu_sample(kernel: Any) -> tuple[int, int, int]:
    idle, system, user = _FileTime(), _FileTime(), _FileTime()
    if not kernel.GetSystemTimes(ctypes.byref(idle), ctypes.byref(system), ctypes.byref(user)):
        raise OSError("GetSystemTimes failed")
    return _ticks(idle), _ticks(system), _ticks(user)


def _cpu_percent(before: tuple[int, int, int], after: tuple[int, int, int]) -> float:
    idle = after[0] - before[0]
    total = (after[1] - before[1]) + (after[2] - before[2])
    if total <= 0 or idle < 0:
        raise ValueError("invalid CPU time interval")
    return round(max(0.0, min(100.0, 100.0 * (total - idle) / total)), 1)


def _windows_readings(path: Path) -> tuple[float, dict[str, Any], dict[str, Any], dict[str, Any]]:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    before = _cpu_sample(kernel)
    time.sleep(0.12)
    cpu = _cpu_percent(before, _cpu_sample(kernel))
    memory = _MemoryStatus()
    memory.length = ctypes.sizeof(_MemoryStatus)
    if not kernel.GlobalMemoryStatusEx(ctypes.byref(memory)):
        raise OSError("GlobalMemoryStatusEx failed")
    disk = shutil.disk_usage(path.resolve())
    power = _PowerStatus()
    if not kernel.GetSystemPowerStatus(ctypes.byref(power)) or power.battery_flag in {128, 255} or power.battery_percent == 255:
        battery: dict[str, Any] = {"status": "unavailable_or_no_battery"}
    else:
        battery = {"charge_percent": int(power.battery_percent),
                   "power_plugged": True if power.ac == 1 else False if power.ac == 0 else None}
    return (
        cpu,
        {"used_percent": int(memory.load), "available_bytes": int(memory.avail_phys),
         "total_bytes": int(memory.total_phys)},
        {"used_percent": round(100 * disk.used / disk.total, 1),
         "free_bytes": int(disk.free), "total_bytes": int(disk.total)},
        battery,
    )


def snapshot(path: Path, sensor: Any = None) -> str:
    if platform.system() != "Windows" and sensor is None:
        return "error: native PC diagnostics are currently supported on Windows only"
    result: dict[str, Any] = {
        "observed_at_utc": datetime.now(timezone.utc).isoformat(),
        "note": "Read-only point-in-time measurements; unavailable sensors are not estimated.",
    }
    try:
        if sensor is None:
            cpu, memory_data, disk_data, battery_data = _windows_readings(path)
            result["cpu_percent"] = cpu
            result["memory"] = memory_data
            result["workspace_drive"] = disk_data
            result["battery"] = battery_data
        else:
            result["cpu_percent"] = round(float(sensor.cpu_percent(interval=0.1)), 1)
            memory = sensor.virtual_memory()
            result["memory"] = {"used_percent": round(float(memory.percent), 1),
                                "available_bytes": int(memory.available), "total_bytes": int(memory.total)}
            disk = sensor.disk_usage(str(path.resolve()))
            result["workspace_drive"] = {"used_percent": round(float(disk.percent), 1),
                                         "free_bytes": int(disk.free), "total_bytes": int(disk.total)}
            battery = sensor.sensors_battery()
            result["battery"] = ({"charge_percent": round(float(battery.percent), 1),
                                  "power_plugged": battery.power_plugged}
                                 if battery is not None else {"status": "unavailable_or_no_battery"})
    except (OSError, ValueError, AttributeError, TypeError, ZeroDivisionError) as exc:
        return f"error: PC measurement failed ({type(exc).__name__})"
    if platform.system() == "Windows":
        result["temperature"] = {"status": "not_available_in_core_adapter"}
        result["fans"] = {"status": "not_available_in_core_adapter"}
    else:
        result["temperature"] = {"status": "not_measured"}
        result["fans"] = {"status": "not_measured"}
    return json.dumps(result, ensure_ascii=False)
