"""
bare_link.py — sirf link bhejo, /url likhne ki zaroorat nahi (All-Url-Uploader jaisa)
=====================================================================================
Private chat mein message ki *shuruaat* http(s):// link se ho to:
  * YouTube / Instagram / X / Facebook / Vimeo / Reddit ... -> /ytdl (quality buttons)
  * baaki sab                                               -> /url (saved settings se auto-process)
`link | naam`, `link * naam`, `link | naam | user | pass` bhi chalta hai (url_upload ka parser).
Sirf authorised users (Auth/Sudo/Owner). Band karna ho: env BARE_LINK=0.
Slash-commands, aur jo text kisi session ka jawab hai (rename/metadata input) ko ye nahi chhoota.
"""
import os
import re
from urllib.parse import urlparse

from pyrogram import Client, filters, StopPropagation
from pyrogram.types import Message

from ..utils.helper import check_chat

_START_RE = re.compile(r"^\s*https?://\S+", re.IGNORECASE)
_YTDL_HOSTS = ("youtube.com", "youtu.be", "instagram.com", "twitter.com", "x.com", "facebook.com",
               "fb.watch", "vimeo.com", "reddit.com", "tiktok.com", "dailymotion.com")


def _enabled() -> bool:
    return (os.getenv("BARE_LINK") or "1").strip().lower() not in ("0", "false", "no", "off")


def _awaiting_input(uid: int) -> bool:
    try:
        from .url_upload import _url_sessions
        s = _url_sessions.get(uid)
        if s and any(str(k).startswith("awaiting") for k in s):
            return True
    except Exception:
        pass
    return False


@Client.on_message(filters.text & filters.private & ~filters.regex(r"^/") & ~filters.via_bot, group=9)
async def bare_link_handler(bot: Client, message: Message):
    if not _enabled() or not message.from_user:
        return
    text = (message.text or "").strip()
    if not _START_RE.match(text):
        return
    if _awaiting_input(message.from_user.id):
        return
    if not await check_chat(message, chat="Both"):
        return

    host = (urlparse(text.split()[0]).hostname or "").lower()
    is_yt = any(host == h or host.endswith("." + h) for h in _YTDL_HOSTS)
    cmd = "ytdl" if is_yt else "url"
    if is_yt and not await check_chat(message, chat="Sudo"):
        cmd = "url"                      # /ytdl sirf Sudo/Owner ke liye hai

    message.text = f"/{cmd} {text}"
    message.command = [cmd] + text.split()
    if cmd == "ytdl":
        from .ytdl_downloader import ytdl_command
        await ytdl_command(bot, message)
    else:
        from .url_upload import url_upload_cmd
        await url_upload_cmd(bot, message)
    raise StopPropagation
