"""
Mega.nz Downloader + Encoder
mega.nz links se file download → Telegram pe direct upload → Encode bhi karo

Commands:
  /mega <mega_link>          — Download, upload as-is (OG quality)
  /meganow <mega_link>       — Download, upload, phir encode bhi karo
                               (user ki current /settings ke hisab se)
"""
import os
import re
import time
import asyncio
import subprocess

from pyrogram import Client, filters
from pyrogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
)

from .. import app, download_dir, encode_dir
from ..utils.database.access_db import db
from ..utils.uploads import upload_worker
from ..utils.auto_caption import build_auto_caption
from ..utils.helper import handle_encode


# ─── Mega link validator ──────────────────────────────────────────────────────

def is_mega_link(url: str) -> bool:
    return 'mega.nz' in url or 'mega.co.nz' in url


# ─── Mega downloader ──────────────────────────────────────────────────────────

async def download_mega(url: str, dest_dir: str, msg) -> str | None:
    """megadl tool se mega download karo, progress update karo"""
    os.makedirs(dest_dir, exist_ok=True)

    # Tool check
    check = subprocess.run(['which', 'megadl'], capture_output=True)
    tool = 'megadl' if check.returncode == 0 else None
    if not tool:
        check2 = subprocess.run(['which', 'megatools'], capture_output=True)
        tool = 'megatools' if check2.returncode == 0 else None

    if not tool:
        await msg.edit(
            "❌ **Mega downloader install nahi hai!**\n\n"
            "Dockerfile mein `megatools` add karo:\n"
            "`RUN apt-get install -y megatools`"
        )
        return None

    await msg.edit("📥 **Mega se download ho raha hai...**\n⏳ Please wait...")

    cmd = ['megadl', '--path', dest_dir, url]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    # Progress updater — har 10 sec mein message update karo
    async def progress_updater():
        dots = 0
        while proc.returncode is None:
            await asyncio.sleep(10)
            dots = (dots % 3) + 1
            try:
                await msg.edit(
                    f"📥 **Mega se download ho raha hai{'.' * dots}**\n"
                    f"⏳ Please wait..."
                )
            except Exception:
                pass

    progress_task = asyncio.create_task(progress_updater())

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=3600)
    except asyncio.TimeoutError:
        proc.kill()
        progress_task.cancel()
        await msg.edit("❌ Download timeout! File bahut badi hai ya slow connection.")
        return None
    finally:
        progress_task.cancel()

    if proc.returncode != 0:
        err = stderr.decode().strip()
        await msg.edit(f"❌ **Mega download failed!**\n`{err[:300]}`")
        return None

    # Downloaded file dhundo
    files = []
    for root, _, fnames in os.walk(dest_dir):
        for f in fnames:
            full = os.path.join(root, f)
            if os.path.isfile(full):
                files.append(full)

    if not files:
        await msg.edit("❌ Koi file nahi mili download folder mein!")
        return None

    files.sort(key=os.path.getmtime, reverse=True)
    return files[0]


# ─── Metadata cleaner ─────────────────────────────────────────────────────────

async def clean_and_rename(filepath: str, proper_filename: str, msg) -> str:
    """ffmpeg se metadata clean karo aur proper naam se save karo"""
    await msg.edit("🧹 **Metadata clean ho raha hai...**")

    dir_name = os.path.dirname(filepath)
    cleaned_path = os.path.join(dir_name, proper_filename)

    if cleaned_path == filepath:
        # Naam same hai, sirf metadata clean karo
        temp_path = filepath + ".cleaning.mkv"
        out_path = temp_path
    else:
        out_path = cleaned_path

    cmd = [
        'ffmpeg', '-y', '-i', filepath,
        '-map', '0', '-c', 'copy',
        '-metadata', 'title=',
        '-metadata', 'comment=',
        '-metadata', 'description=',
        '-metadata:s:v:0', 'title=',
        '-metadata:s:a:0', 'title=',
        out_path
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    try:
        _, _ = await asyncio.wait_for(proc.communicate(), timeout=600)
    except asyncio.TimeoutError:
        proc.kill()
        return filepath

    if proc.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        os.remove(filepath)
        if out_path != cleaned_path:
            os.rename(out_path, cleaned_path)
        return cleaned_path
    else:
        if os.path.exists(out_path):
            os.remove(out_path)
        return filepath


# ─── URL parser ───────────────────────────────────────────────────────────────

def extract_mega_url(message: Message) -> str | None:
    """Message se mega URL nikalo"""
    if len(message.command) > 1:
        return message.command[1]
    if message.reply_to_message and message.reply_to_message.text:
        m = re.search(r'https?://mega\.(?:nz|co\.nz)/\S+', message.reply_to_message.text)
        if m:
            return m.group(0)
    return None


# ─── /mega handler — Download + Upload only ──────────────────────────────────

@Client.on_message(filters.command("mega"))
async def mega_handler(client: Client, message: Message):
    """
    /mega <link>  →  Mega se download karo, Telegram pe upload karo (OG quality)
    Encode nahi hoga — jaise hai waise bhejega.
    """
    url = extract_mega_url(message)

    if not url or not is_mega_link(url):
        await message.reply(
            "**📌 Usage:**\n"
            "`/mega https://mega.nz/file/xxxxx#yyyyy`\n\n"
            "**Encode bhi karna hai?** Use `/meganow` instead.\n"
            "Ya mega link reply karke `/mega` ya `/meganow` bhejo."
        )
        return

    msg = await message.reply("🔍 **Mega link check ho raha hai...**")
    dest = os.path.join(download_dir, f"mega_{int(time.time())}")

    filepath = await download_mega(url, dest, msg)
    if not filepath:
        return

    fname = os.path.basename(filepath)
    size_mb = os.path.getsize(filepath) / (1024 * 1024)
    resolution = 'OG'
    caption = build_auto_caption(filepath, resolution=resolution)
    proper_filename = re.sub(r'[<>:"/\\|?*]', '', caption).strip()

    await msg.edit(
        f"✅ **Downloaded:** `{fname}`\n"
        f"📦 **Size:** {size_mb:.1f} MB\n\n"
        f"🧹 Metadata clean ho raha hai..."
    )

    filepath = await clean_and_rename(filepath, proper_filename, msg)
    size_mb = os.path.getsize(filepath) / (1024 * 1024)

    await msg.edit(
        f"✅ **Downloaded & Cleaned**\n"
        f"📦 **Size:** {size_mb:.1f} MB\n\n"
        f"⏳ **Telegram pe upload ho raha hai...**"
    )

    try:
        link = await upload_worker(filepath, message, msg, resolution=resolution)
        await msg.edit(
            f"✅ **Upload complete!**\n"
            f"📦 Size: {size_mb:.1f} MB\n"
            f"🔗 {link}\n\n"
            f"💡 **Encode karna hai?** `/meganow {url}` bhejo."
        )
    except Exception as e:
        await msg.edit(f"❌ Upload failed: `{e}`")
    finally:
        _cleanup(filepath, dest)


# ─── /meganow handler — Download + Upload + Encode ───────────────────────────

@Client.on_message(filters.command("meganow"))
async def meganow_handler(client: Client, message: Message):
    """
    /meganow <link>  →  Mega se download → Telegram pe upload → Encode karo

    Flow:
    1. Mega se file download karo
    2. Metadata clean karo
    3. Telegram pe OG quality mein upload karo (as-is)
    4. Uploaded message reply pe user ki encode settings se encode shuru karo
    5. Encoded file bhi Telegram pe upload karo
    """
    url = extract_mega_url(message)

    if not url or not is_mega_link(url):
        await message.reply(
            "**📌 Usage:**\n"
            "`/meganow https://mega.nz/file/xxxxx#yyyyy`\n\n"
            "Yeh command:\n"
            "1️⃣ Mega se download karega\n"
            "2️⃣ Telegram pe OG quality mein upload karega\n"
            "3️⃣ Phir tumhari `/settings` ke hisab se encode bhi karega\n\n"
            "Sirf upload chahiye? `/mega` use karo."
        )
        return

    msg = await message.reply("🔍 **Mega link check ho raha hai...**")
    dest = os.path.join(download_dir, f"mega_{int(time.time())}")

    # ── Step 1: Download ──
    filepath = await download_mega(url, dest, msg)
    if not filepath:
        return

    fname = os.path.basename(filepath)
    size_mb = os.path.getsize(filepath) / (1024 * 1024)
    resolution = 'OG'
    caption = build_auto_caption(filepath, resolution=resolution)
    proper_filename = re.sub(r'[<>:"/\\|?*]', '', caption).strip()

    await msg.edit(
        f"✅ **Step 1/3: Downloaded**\n"
        f"📄 `{fname}`\n"
        f"📦 {size_mb:.1f} MB\n\n"
        f"🧹 Metadata clean ho raha hai..."
    )

    # ── Step 2: Clean metadata ──
    filepath = await clean_and_rename(filepath, proper_filename, msg)
    size_mb = os.path.getsize(filepath) / (1024 * 1024)

    await msg.edit(
        f"✅ **Step 1/3: Downloaded & Cleaned**\n"
        f"📦 {size_mb:.1f} MB\n\n"
        f"⏳ **Step 2/3: Telegram pe upload ho raha hai...**"
    )

    # ── Step 3: Upload OG to Telegram ──
    try:
        og_link = await upload_worker(filepath, message, msg, resolution=resolution)
        await msg.edit(
            f"✅ **Step 2/3: OG Upload Complete!**\n"
            f"🔗 {og_link}\n\n"
            f"⚙️ **Step 3/3: Tumhari settings se encode shuru ho raha hai...**"
        )
    except Exception as e:
        await msg.edit(f"❌ Upload failed: `{e}`")
        _cleanup(filepath, dest)
        return

    # ── Step 4: Encode using user settings ──
    try:
        encode_msg = await message.reply(
            f"⚙️ **Encoding shuru ho raha hai...**\n"
            f"📄 `{os.path.basename(filepath)}`\n"
            f"🔧 Tumhari `/settings` ke hisab se encode hoga"
        )
        await handle_encode(filepath, message, encode_msg)
        await msg.edit(
            f"🎉 **Sab kuch ho gaya!**\n\n"
            f"✅ OG Upload: {og_link}\n"
            f"✅ Encoded file bhi upload ho gayi!"
        )
    except Exception as e:
        await msg.edit(
            f"⚠️ OG upload toh ho gaya:\n🔗 {og_link}\n\n"
            f"❌ **Encoding fail ho gaya:** `{e}`"
        )
    finally:
        _cleanup(filepath, dest)


# ─── Cleanup helper ───────────────────────────────────────────────────────────

def _cleanup(filepath: str, dest_dir: str):
    """Downloaded files clean karo"""
    try:
        if filepath and os.path.isfile(filepath):
            os.remove(filepath)
    except Exception:
        pass
    try:
        if dest_dir and os.path.isdir(dest_dir):
            import shutil
            shutil.rmtree(dest_dir, ignore_errors=True)
    except Exception:
        pass
