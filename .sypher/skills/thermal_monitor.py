#!/usr/bin/env python3
"""
thermal_monitor.py — SYPHER Thermal Heartbeat HUD feeder.

Runs `sensors -j`, extracts CPU package temperature, and prints minified JSON:
{"temp_c":72.4,"status":"WARM","color":"yellow","throttle":false}
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any


def _status_for_temp(temp_c: float) -> tuple[str, str, bool]:
    if temp_c < 70.0:
        return "OPTIMAL", "neon_green", False
    if temp_c < 85.0:
        return "WARM", "yellow", False
    return "CRITICAL", "pulsing_red", True


def _extract_temp_recursive(node: Any) -> float | None:
    if isinstance(node, dict):
        # Prefer exact package label first.
        if "Package id 0" in node and isinstance(node["Package id 0"], dict):
            package = node["Package id 0"]
            for k in ("temp1_input", "temp2_input", "temp_input"):
                v = package.get(k)
                if isinstance(v, (int, float)):
                    return float(v)

        # Generic fallback: look for package-like keys and *_input values.
        for key, value in node.items():
            key_l = str(key).lower()
            if "package id 0" in key_l and isinstance(value, dict):
                for k, v in value.items():
                    if str(k).endswith("_input") and isinstance(v, (int, float)):
                        return float(v)

        for value in node.values():
            found = _extract_temp_recursive(value)
            if found is not None:
                return found

    elif isinstance(node, list):
        for item in node:
            found = _extract_temp_recursive(item)
            if found is not None:
                return found

    return None


def read_thermal() -> dict[str, Any]:
    try:
        proc = subprocess.run(
            ["sensors", "-j"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except FileNotFoundError:
        return {
            "ok": False,
            "error": "sensors command not found (install lm-sensors)",
        }
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error": "sensors -j timed out",
        }

    if proc.returncode != 0:
        return {
            "ok": False,
            "error": (proc.stderr or proc.stdout or "sensors failed").strip(),
        }

    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        return {"ok": False, "error": f"Invalid sensors JSON: {e}"}

    temp_c = _extract_temp_recursive(payload)
    if temp_c is None:
        return {
            "ok": False,
            "error": "Package id 0 temperature not found",
        }

    status, color, throttle = _status_for_temp(temp_c)
    return {
        "ok": True,
        "temp_c": round(float(temp_c), 1),
        "status": status,
        "color": color,
        "throttle": throttle,
    }


if __name__ == "__main__":
    result = read_thermal()
    sys.stdout.write(json.dumps(result, separators=(",", ":"), ensure_ascii=False))

