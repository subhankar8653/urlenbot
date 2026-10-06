"""
profile.py — ek hi code, kisi bhi host pe: Railway, Render, Heroku, Fly, Koyeb,
HF Spaces, Replit, Codespaces, Colab/Kaggle, Termux ya apna VPS.

Auto-detect (2 cheezein):
  * PLATFORM : kaunsa host hai (env vars se)  -> sirf log/health mein dikhta hai
  * TIER     : machine kitni badi hai (CPU + RAM se) -> limits yahi decide karta hai
               tiny  : <=0.7GB RAM ya <=0.5 CPU   (Render free, Heroku eco, Fly 512MB)
               small : ~1-3GB RAM                 (Railway, chhota container)
               mid   : 3-6GB RAM, 2+ CPU
               big   : 6GB+ RAM, 4+ CPU           (VPS)
  * NAME     : purana naam ("railway" / "vps") — baaki code isi ko padhta hai,
               tiny/small => "railway", mid/big => "vps".
Manual override: BOT_PROFILE=railway | vps | tiny | small | mid | big | auto   (default auto)

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


def _cpus_f():
    """CPU quota float mein (0.5 CPU wale free containers pehchanne ke liye)."""
    try:
        with open("/sys/fs/cgroup/cpu.max") as f:
            quota, period = f.read().split()
            if quota != "max":
                return max(0.1, int(quota) / int(period))
    except Exception:
        pass
    try:
        q = int(open("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read())
        p = int(open("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read())
        if q > 0:
            return max(0.1, q / p)
    except Exception:
        pass
    return float(os.cpu_count() or 2)


def _cpus():
    return max(1, int(_cpus_f()))


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


def _platform():
    """Host ka naam env vars se (sirf info ke liye — limits TIER se aati hain)."""
    e = os.environ
    if any(k.startswith("RAILWAY_") for k in e):
        return "railway"
    if e.get("RENDER") or e.get("RENDER_SERVICE_ID"):
        return "render"
    if e.get("DYNO"):
        return "heroku"
    if e.get("FLY_APP_NAME"):
        return "fly"
    if any(k.startswith("KOYEB_") for k in e):
        return "koyeb"
    if e.get("SPACE_ID") or e.get("SPACE_HOST"):
        return "hf-space"
    if e.get("REPL_ID") or e.get("REPL_SLUG"):
        return "replit"
    if e.get("CODESPACES"):
        return "codespaces"
    if e.get("KAGGLE_KERNEL_RUN_TYPE"):
        return "kaggle"
    if e.get("COLAB_GPU") or e.get("COLAB_RELEASE_TAG"):
        return "colab"
    if e.get("TERMUX_VERSION") or "com.termux" in e.get("PREFIX", ""):
        return "termux"
    if e.get("K_SERVICE"):
        return "cloud-run"
    if e.get("WEBSITE_SITE_NAME"):
        return "azure"
    if os.path.exists("/.dockerenv"):
        return "docker"
    return "vps"


CPUS_F = _cpus_f()
CPUS = _cpus()
RAM = _ram_bytes()
RAM_GB = RAM / 1024 ** 3
PLATFORM = _platform()

_TIERS = ("tiny", "small", "mid", "big")
# PaaS hosts: cgroup limit kabhi "max" dikhata hai (host ki poori RAM dikh jaati hai),
# isliye auto-detect yahan kabhi "small" se upar nahi jaata (purana Railway behaviour
# bachta hai). Badi plan ho to BOT_PROFILE=mid ya big se khud upar karo.
_PAAS = ("railway", "render", "heroku", "fly", "koyeb", "hf-space", "replit",
         "codespaces", "kaggle", "colab", "cloud-run", "azure")


def _tier_from_resources():
    if RAM_GB <= 0.7 or CPUS_F <= 0.55:
        return "tiny"
    if RAM_GB < 3:
        return "small"
    if RAM_GB < 6 or CPUS < 4:
        return "mid"
    return "big"


def _detect():
    forced = os.getenv("BOT_PROFILE", "auto").strip().lower()
    auto = _tier_from_resources()
    if forced in _TIERS:
        return forced
    if forced == "railway":
        return "small"
    if forced == "vps":
        # VPS bola hai => kam se kam mid; machine badi ho to big
        return auto if auto in ("mid", "big") else "mid"
    if PLATFORM in _PAAS and auto in ("mid", "big"):
        return "small"
    return auto


TIER = _detect()
NAME = "vps" if TIER in ("mid", "big") else "railway"   # purana naam — baaki code yahi padhta hai
IS_VPS = NAME == "vps"

# ── Per-tier limits ─────────────────────────────────────────────────────
# UP_START / UP_MAX = upload engine (turbo_upload.py) ka total in-flight parts
# (512KB each) — SAARI files milake. Engine apne aap isi range mein host ki
# asli bandwidth ke hisaab se upar-neeche hota hai.
_T = {
    "tiny": dict(
        UPLOAD_SESSIONS=2, UPLOAD_WORKERS=4, DOWNLOAD_THREADS=4,
        PYRO_WORKERS=16, PYRO_TRANSMISSIONS=4,
        JANITOR_STALE_MIN=20, JANITOR_LOW_DISK_GB=1.0, JANITOR_LOW_DISK_PCT=85.0,
        JANITOR_MEM_RESTART_PCT=70.0, JANITOR_MAX_UPTIME_H=12.0, CHROME_IDLE_MIN=5,
        UP_START=6, UP_MAX=16, UP_PER_FILE_MIN=2, UP_IDLE_CLOSE=45,
        DL_CONN_MAX=12,
    ),
    "small": dict(
        UPLOAD_SESSIONS=3, UPLOAD_WORKERS=6, DOWNLOAD_THREADS=8,
        PYRO_WORKERS=32, PYRO_TRANSMISSIONS=8,
        JANITOR_STALE_MIN=30, JANITOR_LOW_DISK_GB=2.0, JANITOR_LOW_DISK_PCT=85.0,
        JANITOR_MEM_RESTART_PCT=65.0, JANITOR_MAX_UPTIME_H=24.0, CHROME_IDLE_MIN=8,
        UP_START=12, UP_MAX=32, UP_PER_FILE_MIN=3, UP_IDLE_CLOSE=60,
        DL_CONN_MAX=24,
    ),
    "mid": dict(
        UPLOAD_SESSIONS=4, UPLOAD_WORKERS=8, DOWNLOAD_THREADS=12,
        PYRO_WORKERS=48, PYRO_TRANSMISSIONS=12,
        JANITOR_STALE_MIN=90, JANITOR_LOW_DISK_GB=5.0, JANITOR_LOW_DISK_PCT=88.0,
        JANITOR_MEM_RESTART_PCT=80.0, JANITOR_MAX_UPTIME_H=72.0, CHROME_IDLE_MIN=10,
        UP_START=20, UP_MAX=56, UP_PER_FILE_MIN=4, UP_IDLE_CLOSE=90,
        DL_CONN_MAX=40,
    ),
    "big": dict(
        UPLOAD_SESSIONS=4, UPLOAD_WORKERS=8, DOWNLOAD_THREADS=16,
        PYRO_WORKERS=64, PYRO_TRANSMISSIONS=16,
        JANITOR_STALE_MIN=180, JANITOR_LOW_DISK_GB=10.0, JANITOR_LOW_DISK_PCT=90.0,
        JANITOR_MEM_RESTART_PCT=85.0, JANITOR_MAX_UPTIME_H=168.0, CHROME_IDLE_MIN=15,
        UP_START=32, UP_MAX=96, UP_PER_FILE_MIN=4, UP_IDLE_CLOSE=120,
        DL_CONN_MAX=64,
    ),
}[TIER]

if TIER == "big" and CPUS < 4:
    _T["UPLOAD_SESSIONS"] = 3

UPLOAD_SESSIONS = _env_int("UPLOAD_SESSIONS", _T["UPLOAD_SESSIONS"])
UPLOAD_WORKERS = _env_int("UPLOAD_WORKERS", _T["UPLOAD_WORKERS"])
DOWNLOAD_THREADS = _env_int("DOWNLOAD_THREADS", _T["DOWNLOAD_THREADS"])      # SmartDL threads per file
PYRO_WORKERS = _env_int("PYRO_WORKERS", _T["PYRO_WORKERS"])
PYRO_TRANSMISSIONS = _env_int("PYRO_TRANSMISSIONS", _T["PYRO_TRANSMISSIONS"])  # download parallel connections
JANITOR_STALE_MIN = _env_int("JANITOR_STALE_MIN", _T["JANITOR_STALE_MIN"])
JANITOR_LOW_DISK_GB = _env_float("JANITOR_LOW_DISK_GB", _T["JANITOR_LOW_DISK_GB"])
JANITOR_LOW_DISK_PCT = _env_float("JANITOR_LOW_DISK_PCT", _T["JANITOR_LOW_DISK_PCT"])
JANITOR_MEM_RESTART_PCT = _env_float("JANITOR_MEM_RESTART_PCT", _T["JANITOR_MEM_RESTART_PCT"])
JANITOR_MAX_UPTIME_H = _env_float("JANITOR_MAX_UPTIME_H", _T["JANITOR_MAX_UPTIME_H"])
CHROME_IDLE_MIN = _env_int("CHROME_IDLE_MIN", _T["CHROME_IDLE_MIN"])

# Upload engine (turbo_upload.py)
UP_MAX = max(4, _env_int("UPLOAD_INFLIGHT_MAX", _T["UP_MAX"]))
UP_START = min(UP_MAX, max(2, _env_int("UPLOAD_INFLIGHT_START", _T["UP_START"])))
UP_PER_FILE_MIN = max(1, _env_int("UPLOAD_PER_FILE_MIN", _T["UP_PER_FILE_MIN"]))
UP_IDLE_CLOSE = max(10, _env_int("UPLOAD_IDLE_CLOSE", _T["UP_IDLE_CLOSE"]))   # sec

# Download engine (turbo_download.py): saari files milake itne parallel connection se zyada nahi
DL_CONN_MAX = max(2, _env_int("DOWNLOAD_CONN_MAX", _T["DL_CONN_MAX"]))


def summary():
    return (f"{PLATFORM.upper()}/{TIER} | {CPUS_F:.1f} CPU | {RAM_GB:.1f}GB RAM | "
            f"upload sessions {UPLOAD_SESSIONS}, window {UP_START}-{UP_MAX} parts | "
            f"dl-threads {DOWNLOAD_THREADS}, dl-conn-max {DL_CONN_MAX}")
