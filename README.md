# 🎬 VideoEncoder Bot

> A powerful Telegram bot for compressing, encoding, and manipulating video files. Built with Python (Pyrofork) and FFmpeg.

<p align="center">
  <b>⭐ Powered & Maintained by <a href="https://t.me/suhanibots">SuhaniBots</a> ⭐</b><br>
  Join our official channel for updates, support, and more bots!<br>
  <a href="https://t.me/suhanibots">📢 @SuhaniBots</a>
</p>

---

## 🚀 Features

### 🎥 Video Encoding
- **Formats**: Supports encoding to **MKV**, **MP4**, **AVI**.
- **Codecs**: Choose between **H264** (x264) and **H265** (HEVC).
- **Quality Control**:
  - Custom **CRF** (Constant Rate Factor).
  - **Presets** (UltraFast to VerySlow).
  - **10-bit** encoding support.
- **Resolution**: Downscale videos to 1080p, 720p, 540p, 480p, 360p, or keep original.
- **Audio**:
  - Change audio codecs (AAC, AC3, OPUS, MP3, etc.).
  - Custom bitrate and sample rates.
  - Mix/Remix audio channels (Stereo, Mono, 5.1).

### 🎛 Audio Rearrangement (`/af`)
- Interactively **reorder audio streams** in a video file using an inline button menu.
- Set the default audio track by moving it to the top.

### 📥 Download Methods
- **Telegram Files** (`/dl`): Reply to a video or document to process it.
- **Direct Links** (`/ddl`): Download files from direct URLs.
- **Batch Processing** (`/batch`): Process multiple links or files.

### 🛠 Utilities
- **Speedtest** (`/speedtest`): Check the server's internet speed and view a graphical report.
- **System Status** (`/status`): View real-time CPU, RAM, Disk usage, and active tasks queue.
- **Settings**: Per-user settings menu (`/settings`) to customize encoding preferences.
- **Watermark**: Add custom hardsub watermarks or metadata.
- **Subtitles**: Hardsub or copy soft subtitles.

---

## 🤖 BotFather Commands

Copy and paste these commands directly into [@BotFather](https://t.me/BotFather) using `/setcommands`:

```
start - Check if the bot is online
help - Show help message
settings - Open personal encoding settings menu
reset - Reset your settings to default
vset - View current video settings summary
dl - Download and process a Telegram file (Reply to message)
af - Interactive audio stream rearrangement (Reply to message)
ddl - Download and process a file from a direct link
batch - Encode multiple files in batch
queue - Check current encode queue
speedtest - Run an internet speed test
status - Show server stats and active queue
stats - Show bot statistics (Users, Uptime)
clean - (Sudo) Clean download/encode directories
restart - (Sudo) Restart the bot
update - (Sudo) Update the bot from git
exec - (Sudo) Execute Python code
sh - (Sudo) Execute Shell command
vupload - (Sudo) Upload as video
dupload - (Sudo) Upload as document
gupload - (Sudo) Upload to Google Drive
logs - (Sudo) View bot logs
clear - (Sudo) Clear encode queue
addchat - (Owner) Add allowed chat
addsudo - (Owner) Add sudo user
rmsudo - (Owner) Remove sudo user
rmchat - (Owner) Remove allowed chat
```

---

## ⚙️ Configuration

Configure the bot via environment variables or `config.env` file:

| Variable | Description |
| :--- | :--- |
| `API_ID` | Telegram API ID |
| `API_HASH` | Telegram API Hash |
| `BOT_TOKEN` | Telegram Bot Token from @BotFather |
| `MONGO_URI` | MongoDB connection string |
| `OWNER_ID` | Your Telegram User ID |
| `SUDO_USERS` | List of admin user IDs |
| `LOG_CHANNEL` | Channel ID for logging tasks |
| `DOWNLOAD_DIR` | Path for download directory |
| `ENCODE_DIR` | Path for encode directory |

---

## 🏃 How to Run

### Normal Execution

> Requires Python 3.9+ and FFmpeg installed.

```bash
# Step 1: Install dependencies
pip3 install -r requirements.txt

# Step 2: Configure environment
cp config.env.example config.env
# Edit config.env with your values

# Step 3: Start the bot
python3 -m VideoEncoder
```

### 🐳 Docker

```bash
# Build image
docker build -t video-encoder .

# Run container
docker run -d --env-file config.env video-encoder
```

---

## 📝 Notes

- **Task Limit**: Each user is limited to one active task at a time to ensure fair usage.
- **Settings Isolation**: Users cannot modify each other's settings via the interactive menu.

---

## 📢 Support & Updates

<p align="center">
  <b>Join <a href="https://t.me/suhanibots">SuhaniBots</a> for support, updates, and more powerful bots!</b>
</p>

> This bot is maintained and supported by **SuhaniBots**. For queries, suggestions, or issues — join our Telegram channel.

[![SuhaniBots Channel](https://img.shields.io/badge/Telegram-SuhaniBots-blue?logo=telegram)](https://t.me/suhanibots)
