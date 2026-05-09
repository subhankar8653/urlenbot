"""
Mega.nz Downloader
mega.nz links se file download karke Telegram pe upload karta hai
Command: /mega <mega_link>
"""
import os
import re
import time
import asyncio
import subprocess

from pyrogram import Client, filters
from pyrogram.types import Message

from .. import app, download_dir
from ..utils.database.access_db import db
from ..utils.uploads import upload_worker
from ..utils.auto_caption import build_auto_caption


def is_mega_link(url):
    return 'mega.nz' in url or 'mega.co.nz' in url


async def download_mega(url, dest_dir, msg):
    """megadl tool se mega download karo"""
    os.makedirs(dest_dir, exist_ok=True)

    check = subprocess.run(['which', 'megadl'], capture_output=True)
    tool = 'megadl' if check.returncode == 0 else None

    if not tool:
        check2 = subprocess.run(['which', 'megatools'], capture_output=True)
        tool = 'megatools dl' if check2.returncode == 0 else None

    if not tool:
        await msg.edit("❌ Mega downloader install nahi hai!\n\nDockerfile mein `megatools` add karo.")
        return None

    await msg.edit("📥 **Mega se download ho raha hai...**")

    cmd = ['megadl', '--path', dest_dir, url]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=1800)
    except asyncio.TimeoutError:
        proc.kill()
        await msg.edit("❌ Download timeout! File bahut badi hai ya slow connection.")
        return None

    if proc.returncode != 0:
        err = stderr.decode().strip()
        await msg.edit(f"❌ Mega download failed!\n`{err[:200]}`")
        return None

    files = []
    for f in os.listdir(dest_dir):
        full = os.path.join(dest_dir, f)
        if os.path.isfile(full):
            files.append(full)

    if not files:
        await msg.edit("❌ Koi file nahi mili!")
        return None

    files.sort(key=os.path.getmtime, reverse=True)
    return files[0]


async def clean_metadata(filepath, msg):
    """
    ffmpeg se metadata clean karo:
    - Title tag hatao (Visit - RareToonsIndia jaisi garbage)
    - Language code sirf rakho (hin, jpn etc)
    - Video/Audio stream data same rakho (re-encode nahi)
    """
    await msg.edit("🧹 **Metadata clean ho raha hai...**")

    dir_name = os.path.dirname(filepath)
    basename = os.path.basename(filepath)
    name, ext = os.path.splitext(basename)
    cleaned_path = os.path.join(dir_name, f"{name}_clean{ext}")

    cmd = [
        'ffmpeg', '-y',
        '-i', filepath,
        '-map', '0',           # Saare streams copy karo
        '-c', 'copy',          # Re-encode mat karo (fast)
        # Global title hatao
        '-metadata', 'title=',
        '-metadata', 'comment=',
        '-metadata', 'description=',
        # Video stream title hatao
        '-metadata:s:v:0', 'title=',
        # Audio stream title hatao (Visit - RareToonsIndia yahan hota hai)
        '-metadata:s:a:0', 'title=',
        cleaned_path
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
    except asyncio.TimeoutError:
        proc.kill()
        # Clean fail hui toh original use karo
        return filepath

    if proc.returncode == 0 and os.path.exists(cleaned_path):
        # Original hatao, cleaned use karo
        os.remove(filepath)
        return cleaned_path
    else:
        # Fail hua toh original se kaam chalao
        if os.path.exists(cleaned_path):
            os.remove(cleaned_path)
        return filepath


@Client.on_message(filters.command("mega"))
async def mega_handler(client, message: Message):
    """
    Usage: /mega <mega_link>
    Ya reply karo mega link wale message ko /mega se
    """
    url = None

    if len(message.command) > 1:
        url = message.command[1]
    elif message.reply_to_message and message.reply_to_message.text:
        text = message.reply_to_message.text
        m = re.search(r'https?://mega\.(?:nz|co\.nz)/\S+', text)
        if m:
            url = m.group(0)

    if not url or not is_mega_link(url):
        await message.reply(
            "**Usage:**\n"
            "`/mega https://mega.nz/file/xxxxx#yyyyy`\n\n"
            "Ya mega link wale message ko reply karo `/mega` se."
        )
        return

    msg = await message.reply("🔍 **Mega link check ho raha hai...**")

    dest = os.path.join(download_dir, f"mega_{int(time.time())}")
    filepath = await download_mega(url, dest, msg)

    if not filepath:
        return

    fname = os.path.basename(filepath)
    size_mb = os.path.getsize(filepath) / (1024 * 1024)
    await msg.edit(f"✅ **Downloaded:** `{fname}`\n📦 Size: {size_mb:.1f} MB\n\n🧹 Metadata clean ho raha hai...")

    # Metadata clean karo - RareToons/group title hatao
    filepath = await clean_metadata(filepath, msg)

    await msg.edit(f"✅ **Downloaded:** `{fname}`\n📦 Size: {size_mb:.1f} MB\n\n⏳ Uploading...")

    # Mega files hamesha OG quality - ffprobe se actual quality detect hogi
    resolution = 'OG'

    # Upload karo
    try:
        link = await upload_worker(filepath, message, msg, resolution=resolution)
        await msg.edit(f"✅ **Upload complete!**\n🔗 {link}")
    except Exception as e:
        await msg.edit(f"❌ Upload failed: `{e}`")
    finally:
        try:
            os.remove(filepath)
            os.rmdir(dest)
        except Exception:
            pass
