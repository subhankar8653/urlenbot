"""
VideoEncoder package __init__.py
---------------------------------
Sab core variables yahan define hote hain:
app (Pyrogram Client), LOGGER, env vars, etc.
"""

import logging
import os
import time
from io import BytesIO, StringIO

from dotenv import load_dotenv
from pyrogram import Client

# ── Load config.env if present ───────────────────────────────────────────────
load_dotenv("config.env", override=False)  # Railway vars ko override mat karo

# ── Logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("VideoEncoder/utils/extras/logs.txt"),
        logging.StreamHandler(),
    ],
    level=logging.INFO,
)
LOGGER = logging.getLogger(__name__)

# ── Required env vars ─────────────────────────────────────────────────────────
api_id    = int(os.environ.get("API_ID", 0))
api_hash  = os.environ.get("API_HASH", "")
bot_token = os.environ.get("BOT_TOKEN", "")
mongo_uri = os.environ.get("MONGO_URI", "")

if not api_id or not api_hash or not bot_token:
    LOGGER.error("API_ID, API_HASH aur BOT_TOKEN set karo! config.env ya Railway Variables mein.")
    raise ValueError("Missing required env vars: API_ID / API_HASH / BOT_TOKEN")

# ── Auth users ────────────────────────────────────────────────────────────────
owner_raw    = os.environ.get("OWNER_ID", "")
sudo_raw     = os.environ.get("SUDO_USERS", "")
everyone_raw = os.environ.get("EVERYONE_CHATS", "")

try:
    owner = int(owner_raw)
except (ValueError, TypeError):
    owner = 0

sudo_users = []
for uid in sudo_raw.split():
    try:
        sudo_users.append(int(uid))
    except ValueError:
        pass

everyone = []
for uid in everyone_raw.split():
    try:
        everyone.append(int(uid))
    except ValueError:
        pass

all = sudo_users + [owner]  # noqa: A001

# ── Log channel ───────────────────────────────────────────────────────────────
try:
    log = int(os.environ.get("LOG_CHANNEL", "0"))
except (ValueError, TypeError):
    log = 0

# ── Directories ───────────────────────────────────────────────────────────────
download_dir = os.environ.get("DOWNLOAD_DIR", "VideoEncoder/downloads/")
encode_dir   = os.environ.get("ENCODE_DIR",   "VideoEncoder/encodes/")

os.makedirs(download_dir, exist_ok=True)
os.makedirs(encode_dir,   exist_ok=True)

# ── Misc ──────────────────────────────────────────────────────────────────────
PROGRESS     = "[{0}{1}] {2}%\n"
botStartTime = time.time()
data         = []          # Queue list
video_mimetype = [
    "video/mp4", "video/x-matroska", "video/webm",
    "video/avi", "video/quicktime", "video/x-msvideo",
    "video/mpeg", "video/3gpp",
]

# memory_file factory — used by pyexec plugin
def memory_file(bytes=True):
    return BytesIO() if bytes else StringIO()

# ── Google Drive (optional) ───────────────────────────────────────────────────
drive_dir = os.environ.get("DRIVE_DIR",  "")
index     = os.environ.get("INDEX_URL",  "")

# ── Pyrogram Client ───────────────────────────────────────────────────────────
session_name = os.environ.get("SESSION_NAME", "VideoEncoderBot")

app = Client(
    name=session_name,
    api_id=api_id,
    api_hash=api_hash,
    bot_token=bot_token,
    plugins=dict(root="VideoEncoder/plugins"),
    sleep_threshold=60,
)

LOGGER.info("VideoEncoder package initialised ✅")
