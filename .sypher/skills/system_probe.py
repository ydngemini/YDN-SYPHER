#!/usr/bin/env python3
"""system_probe.py — SYPHER DMI skill. Outputs minified JSON hardware report."""

import json
import subprocess
import sys


def cpu_ram():
    try:
        import psutil
        vm = psutil.virtual_memory()
        return {
            "cpu_percent": psutil.cpu_percent(interval=0.1),
            "ram_total_gb": round(vm.total / 1024 ** 3, 2),
            "ram_avail_gb": round(vm.available / 1024 ** 3, 2),
            "ram_percent": vm.percent,
        }
    except ImportError:
        return {"error": "psutil not installed — pip install psutil"}


def gpu():
    try:
        r = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=name,temperature.gpu,utilization.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=4,
        )
        if r.returncode != 0:
            return {"gpus": [], "error": r.stderr.strip()}
        gpus = []
        for line in r.stdout.strip().splitlines():
            n, t, u, mu, mt = [v.strip() for v in line.split(",")]
            gpus.append({"name": n, "temp_c": int(t), "util_pct": int(u),
                         "vram_used_mb": int(mu), "vram_total_mb": int(mt)})
        return {"gpus": gpus}
    except FileNotFoundError:
        return {"gpus": [], "note": "nvidia-smi not found"}
    except Exception as e:
        return {"gpus": [], "error": str(e)}


if __name__ == "__main__":
    print(json.dumps({**cpu_ram(), **gpu()}, separators=(",", ":")))
