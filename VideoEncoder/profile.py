"""
profile.py — ek hi code, do jagah: Railway ya VPS.

Auto-detect:
  * RAILWAY_* env vars mile  -> "railway"  (chhota container, tight limits)
  * warna                    -> "vps"      (poori machine apni, bade limits)
Manual override: BOT_PROFILE=railway | vps | auto   (default auto)

Har value ko alag se env var se bhi override kar sakte ho
(UPLOAD_SESSIONS, UPLOAD_WORKERS, DOWNLOAD_THREADS, ...).
Sirf stdlib use hota hai taaki package ke __init__ se pehle import ho sake.
"""
import os


def _env_int(name, default):
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _env_float(name, default):
    try:
        return float(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _cpus():
    try:
        with open("/sys/fs/cgroup/cpu.max") as f:
            quota, period = f.read().split()
            if quota != "max":
                return max(1, int(int(quota) / int(period)))
    except Exception:
        pass
    try:
        q = int(open("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read())
        p = int(open("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read())
        if q > 0:
            return max(1, q // p)
    except Exception:
        pass
    return os.cpu_count() or 2


def _ram_bytes():
    total = 0
    try:
        for line in open("/proc/meminfo"):
            if line.startswith("MemTotal"):
                total = int(line.split()[1]) * 1024
                break
    except Exception:
        pass
    for lim in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            raw = open(lim).read().strip()
            if raw.isdigit():
                total = min(total, int(raw)) if total else int(raw)
                break
        except Exception:
            continue
    return total or 1024 ** 3


def _detect():
    forced = os.getenv("BOT_PROFILE", "auto").strip().lower()
    if forced in ("railway", "vps"):
        return forced
    if any(k.startswith("RAILWAY_") for k in os.environ):
        return "railway"
    return "vps"


NAME = _detect()
CPUS = _cpus()
RAM = _ram_bytes()
RAM_GB = RAM / 1024 ** 3
IS_VPS = NAME == "vps"

if IS_VPS:
    # Singapore VPS (4 core / 8GB / NVMe): bandwidth + CPU dono bahut, RAM khula
    UPLOAD_SESSIONS = _env_int("UPLOAD_SESSIONS", 4 if CPUS >= 4 else 3)
    UPLOAD_WORKERS = _env_int("UPLOAD_WORKERS", 8)
    DOWNLOAD_THREADS = _env_int("DOWNLOAD_THREADS", 16)      # SmartDL threads per file
    PYRO_WORKERS = _env_int("PYRO_WORKERS", 64)
    PYRO_TRANSMISSIONS = _env_int("PYRO_TRANSMISSIONS", 16)  # download parallel connections
    JANITOR_STALE_MIN = _env_int("JANITOR_STALE_MIN", 180)
    JANITOR_LOW_DISK_GB = _env_float("JANITOR_LOW_DISK_GB", 10.0)
    JANITOR_LOW_DISK_PCT = _env_float("JANITOR_LOW_DISK_PCT", 90.0)
    JANITOR_MEM_RESTART_PCT = _env_float("JANITOR_MEM_RESTART_PCT", 85.0)
    JANITOR_MAX_UPTIME_H = _env_float("JANITOR_MAX_UPTIME_H", 168.0)   # 7 din
    CHROME_IDLE_MIN = _env_int("CHROME_IDLE_MIN", 15)
else:
    # Railway (~1GB RAM): tight & safe
    UPLOAD_SESSIONS = _env_int("UPLOAD_SESSIONS", 3)
    UPLOAD_WORKERS = _env_int("UPLOAD_WORKERS", 6)
    DOWNLOAD_THREADS = _env_int("DOWNLOAD_THREADS", 8)
    PYRO_WORKERS = _env_int("PYRO_WORKERS", 32)
    PYRO_TRANSMISSIONS = _env_int("PYRO_TRANSMISSIONS", 8)
    JANITOR_STALE_MIN = _env_int("JANITOR_STALE_MIN", 30)
    JANITOR_LOW_DISK_GB = _env_float("JANITOR_LOW_DISK_GB", 2.0)
    JANITOR_LOW_DISK_PCT = _env_float("JANITOR_LOW_DISK_PCT", 85.0)
    JANITOR_MEM_RESTART_PCT = _env_float("JANITOR_MEM_RESTART_PCT", 65.0)
    JANITOR_MAX_UPTIME_H = _env_float("JANITOR_MAX_UPTIME_H", 24.0)
    CHROME_IDLE_MIN = _env_int("CHROME_IDLE_MIN", 8)


def summary():
    return (f"{NAME.upper()} | {CPUS} CPU | {RAM_GB:.1f}GB RAM | "
            f"upload {UPLOAD_SESSIONS}x{UPLOAD_WORKERS} | dl-threads {DOWNLOAD_THREADS}")
