"""
save_restrict.py
================
Save Restricted Content — Encode Bot ke liye.

FLOW:
  1. User account se DOWNLOAD (restricted content access)
  2. User account se LOG_CHANNEL mein UPLOAD (user admin hai, max speed)
  3. Bot se LOG_CHANNEL → target chat FORWARD (instant, no re-upload)

KEY TRICK:
  User client pehle bot se LOG_CHANNEL ka InputPeer leta hai (resolve karta hai).
  Bina resolve ke unknown peer error aata hai even with integer chat_id.
"""

import asyncio
import os
import shutil
import time

from pyrogram import Client, filters
from pyrogram.errors import (
    PhoneNumberInvalid,
    PhoneCodeInvalid,
    PhoneCodeExpired,
    SessionPasswordNeeded,
    PasswordHashInvalid,
)
from pyrogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
)
from pyrogram.enums import ParseMode

from .. import LOGGER, download_dir, api_id, api_hash, app, log
from ..utils.database.access_db import db
from ..utils.helper import check_chat
from ..utils.encoding import get_duration, get_thumbnail, get_width_height
from ..utils.display_progress import progress_for_pyrogram


# ──────────────────────────────────────────────
#  Login state (in-memory)
# ──────────────────────────────────────────────
LOGIN_STATE = {}

cancel_kb = ReplyKeyboardMarkup(
    [[KeyboardButton("❌ Cancel")]],
    resize_keyboard=True,
    one_time_keyboard=True,
)
remove_kb = ReplyKeyboardRemove()

STEPS = {
    "WAITING_PHONE": "🟢 Phone → 🔵 OTP → 🔵 Password",
    "WAITING_CODE":  "✅ Phone → 🟢 OTP → 🔵 Password",
    "WAITING_PASS":  "✅ Phone → ✅ OTP → 🟢 Password",
}


# ──────────────────────────────────────────────
#  DB helpers
# ──────────────────────────────────────────────
async def _get_user_session(user_id: int):
    user = await db._get_user(user_id)
    return user.get("user_session", None)


async def _set_user_session(user_id: int, session_str):
    await db.col.update_one(
        {"id": user_id},
        {"$set": {"user_session": session_str}},
        upsert=True,
    )


# ──────────────────────────────────────────────
#  User Client builder — max speed
# ──────────────────────────────────────────────
def _make_user_client(session_str: str) -> Client:
    return Client(
        "sr_user",
        session_string=session_str,
        api_id=api_id,
        api_hash=api_hash,
        in_memory=True,
        max_concurrent_transmissions=10,
        workers=16,
        sleep_threshold=60,
    )


async def _resolve_log_channel(uc: Client) -> int:
    """
    User client ke liye LOG_CHANNEL resolve karo.
    Pyrogram integer peer bhi tabhi accept karta hai jab uska InputPeer
    session cache mein ho. get_chat() se resolve + cache ho jaata hai.
    Returns resolved chat_id (int) — same as log.
    """
    try:
        chat = await uc.get_chat(log)
        return chat.id
    except Exception:
        # Fallback: shayad already cached ho
        return log


# ──────────────────────────────────────────────
#  /savelogin
# ──────────────────────────────────────────────
@Client.on_message(filters.command("savelogin"))
async def savelogin_start(client: Client, message: Message):
    user_id = message.from_user.id

    existing = await _get_user_session(user_id)
    if existing:
        return await message.reply(
            "✅ **Tum pehle se logged in ho!**\n\n"
            "Account change karne ke liye pehle `/savelogout` karo."
        )

    LOGIN_STATE[user_id] = {"step": "WAITING_PHONE", "data": {}}
    await message.reply(
        f"👋 **Save Restrict Login**\n\n"
        f"_{STEPS['WAITING_PHONE']}_\n\n"
        "📞 Apna **phone number** bhejo (country code ke saath).\n"
        "`Example: +919876543210`\n\n"
        "❌ Cancel karne ke liye button dabaao.",
        reply_markup=cancel_kb,
    )


# ──────────────────────────────────────────────
#  /savelogout
# ──────────────────────────────────────────────
@Client.on_message(filters.command("savelogout"))
async def savelogout(client: Client, message: Message):
    user_id = message.from_user.id

    if user_id in LOGIN_STATE:
        state = LOGIN_STATE.pop(user_id)
        try:
            c = state.get("data", {}).get("client")
            if c:
                await c.disconnect()
        except Exception:
            pass

    await _set_user_session(user_id, None)
    await message.reply(
        "🚪 **Logout ho gaya!**\n\n"
        "Session clear. Dobara login ke liye `/savelogin` karo.",
        reply_markup=remove_kb,
    )


# ──────────────────────────────────────────────
#  Login state filter
# ──────────────────────────────────────────────
async def _in_login(_, __, message):
    return message.from_user and message.from_user.id in LOGIN_STATE

login_filter = filters.create(_in_login)


# ──────────────────────────────────────────────
#  Login flow handler
# ──────────────────────────────────────────────
@Client.on_message(
    filters.private & filters.text & login_filter
    & ~filters.command(["savelogin", "savelogout", "saveget", "cancel"])
)
async def savelogin_handler(client: Client, message: Message):
    user_id = message.from_user.id
    text = message.text.strip()
    state = LOGIN_STATE[user_id]
    step = state["step"]

    if text.lower() in ["❌ cancel", "/cancel"]:
        try:
            c = state.get("data", {}).get("client")
            if c:
                await c.disconnect()
        except Exception:
            pass
        LOGIN_STATE.pop(user_id, None)
        return await message.reply("❌ **Login cancel.**", reply_markup=remove_kb)

    # ── Phone ──
    if step == "WAITING_PHONE":
        phone = text.replace(" ", "")
        tmp = Client(
            name=f"sr_{user_id}",
            api_id=api_id,
            api_hash=api_hash,
            in_memory=True,
        )
        msg = await message.reply(
            f"🔄 Connecting...\n_{STEPS['WAITING_PHONE']}_",
            reply_markup=remove_kb,
        )
        try:
            await tmp.connect()
            code = await tmp.send_code(phone)
        except PhoneNumberInvalid:
            await tmp.disconnect()
            LOGIN_STATE.pop(user_id, None)
            return await msg.edit("❌ **Invalid phone number!** Dobara `/savelogin` karo.")
        except Exception as e:
            await tmp.disconnect()
            LOGIN_STATE.pop(user_id, None)
            return await msg.edit(f"❌ **Error:** `{e}`")

        state["data"] = {"client": tmp, "phone": phone, "hash": code.phone_code_hash}
        state["step"] = "WAITING_CODE"
        await msg.edit(
            f"📩 **OTP bhej diya!**\n_{STEPS['WAITING_CODE']}_\n\n"
            "Code spaces ke saath bhejo:\n`1 2 3 4 5`",
            reply_markup=cancel_kb,
        )

    # ── OTP ──
    elif step == "WAITING_CODE":
        code_val = text.replace(" ", "")
        tmp = state["data"]["client"]
        msg = await message.reply(f"🔍 Verifying...\n_{STEPS['WAITING_CODE']}_")
        try:
            await tmp.sign_in(state["data"]["phone"], state["data"]["hash"], code_val)
            await _finalize_login(msg, tmp, user_id)
        except PhoneCodeInvalid:
            await msg.edit("❌ **Wrong OTP!** Dobara try karo.")
        except PhoneCodeExpired:
            await tmp.disconnect()
            LOGIN_STATE.pop(user_id, None)
            await msg.edit("⏰ **OTP expire.** Dobara `/savelogin` karo.")
        except SessionPasswordNeeded:
            state["step"] = "WAITING_PASS"
            await msg.edit(
                f"🔐 **2-Step Verification!**\n_{STEPS['WAITING_PASS']}_\n\n"
                "Account **password** bhejo.",
                reply_markup=cancel_kb,
            )
        except Exception as e:
            await tmp.disconnect()
            LOGIN_STATE.pop(user_id, None)
            await msg.edit(f"❌ **Error:** `{e}`")

    # ── Password ──
    elif step == "WAITING_PASS":
        tmp = state["data"]["client"]
        msg = await message.reply(f"🔑 Checking...\n_{STEPS['WAITING_PASS']}_")
        try:
            await tmp.check_password(password=text)
            await _finalize_login(msg, tmp, user_id)
        except PasswordHashInvalid:
            await msg.edit("❌ **Wrong password!**")
        except Exception as e:
            await tmp.disconnect()
            LOGIN_STATE.pop(user_id, None)
            await msg.edit(f"❌ **Error:** `{e}`")


async def _finalize_login(msg: Message, tmp_client, user_id: int):
    try:
        session_str = await tmp_client.export_session_string()
        await tmp_client.disconnect()
        await _set_user_session(user_id, session_str)
        LOGIN_STATE.pop(user_id, None)
        await msg.edit(
            "🎉 **Login Successful!**\n\n"
            "✅ Phone → ✅ OTP → ✅ Password\n\n"
            "Ab `/saveget <link>` se restricted content save karo!\n"
            "⚡ **Download + Upload dono user account se — max speed!**",
            reply_markup=remove_kb,
        )
    except Exception as e:
        LOGIN_STATE.pop(user_id, None)
        await msg.edit(f"❌ **Session save nahi hua:** `{e}`")


# ──────────────────────────────────────────────
#  /saveget — Main handler
# ──────────────────────────────────────────────
@Client.on_message(filters.command("saveget"))
async def saveget(client: Client, message: Message):
    c = await check_chat(message, chat="Sudo")
    if not c:
        return

    parts = message.text.split(None, 1)
    if len(parts) < 2 or "t.me/" not in parts[1]:
        return await message.reply(
            "⚠️ **Usage:**\n"
            "`/saveget https://t.me/c/1234567890/100`\n"
            "`/saveget https://t.me/channelname/100`\n\n"
            "Private channel ke liye pehle `/savelogin` karo."
        )

    link = parts[1].strip()
    user_id = message.from_user.id
    is_private = "t.me/c/" in link

    datas = link.split("/")
    try:
        msg_id = int(datas[-1].replace("?single", ""))
    except Exception:
        return await message.reply("❌ **Invalid link!**")

    user_session = await _get_user_session(user_id)
    if not user_session and is_private:
        return await message.reply(
            "🔒 **Login required!**\n\n"
            "Private link ke liye `/savelogin` karo."
        )

    status_msg = await message.reply("⏳ **Fetching...**")

    # ── User client banao ──
    user_client = None
    if user_session:
        try:
            user_client = _make_user_client(user_session)
            await user_client.connect()
        except Exception as e:
            return await status_msg.edit(
                f"❌ **Session invalid/expired.**\n`{e}`\n\nDobara `/savelogin` karo."
            )

    fetch_client = user_client if user_client else client

    # ── Message fetch ──
    try:
        chat_id = int("-100" + datas[4]) if is_private else datas[3]
        msg_obj = await fetch_client.get_messages(chat_id, msg_id)
    except Exception as e:
        await status_msg.edit(f"❌ **Fetch failed:** `{e}`")
        if user_client:
            await user_client.disconnect()
        return

    if msg_obj.empty:
        if user_client:
            await user_client.disconnect()
        return await status_msg.edit("❌ **Message empty/deleted.**")

    if msg_obj.text:
        if user_client:
            await user_client.disconnect()
        return await client.send_message(message.chat.id, msg_obj.text)

    media = (
        msg_obj.document or msg_obj.video or msg_obj.audio
        or msg_obj.photo or msg_obj.voice or msg_obj.video_note
    )
    if not media:
        if user_client:
            await user_client.disconnect()
        return await status_msg.edit("❌ **Unsupported content.**")

    # ── DOWNLOAD via user client ──
    session_id = str(int(time.time()))
    dl_dir = os.path.join(download_dir, f"sr_{session_id}")
    os.makedirs(dl_dir, exist_ok=True)

    c_time = time.time()
    await status_msg.edit("⬇️ **Downloading...**")

    try:
        file_path = await fetch_client.download_media(
            msg_obj,
            file_name=f"{dl_dir}/",
            progress=progress_for_pyrogram,
            progress_args=("⬇️ Downloading...", status_msg, c_time),
        )
    except Exception as e:
        shutil.rmtree(dl_dir, ignore_errors=True)
        if user_client:
            await user_client.disconnect()
        return await status_msg.edit(f"❌ **Download failed:** `{e}`")

    if not file_path or not os.path.exists(file_path):
        shutil.rmtree(dl_dir, ignore_errors=True)
        if user_client:
            await user_client.disconnect()
        return await status_msg.edit("❌ **File nahi mili.**")

    # ── UPLOAD via user client → LOG_CHANNEL ──
    # KEY: pehle LOG_CHANNEL resolve karo user client se
    # get_chat() → InputPeer cache mein store ho jaata hai → phir send works
    fname = os.path.basename(file_path)
    caption = str(msg_obj.caption or fname)
    c_time = time.time()

    await status_msg.edit("📤 **Uploading...**")

    # Upload client decide karo:
    # - user_client available → user se upload (fast!)
    # - nahi → bot se upload (fallback)
    upload_client = user_client if user_client else app
    resolved_log = log  # default

    if user_client:
        try:
            # CRITICAL: LOG_CHANNEL resolve karo user client se
            # Bina iske "unknown peer" error aata hai
            resolved_log = await _resolve_log_channel(user_client)
        except Exception:
            # Resolve fail → bot se upload karo
            upload_client = app
            resolved_log = log

    try:
        saved_msg = None

        if msg_obj.video:
            duration = get_duration(file_path)
            thumb = get_thumbnail(file_path, dl_dir, duration / 4 if duration else 0)
            width, height = get_width_height(file_path)

            saved_msg = await upload_client.send_video(
                chat_id=resolved_log,
                video=file_path,
                caption=f"<b>{caption}</b>",
                duration=duration,
                width=width,
                height=height,
                thumb=thumb,
                supports_streaming=True,
                file_name=fname,
                parse_mode=ParseMode.HTML,
                progress=progress_for_pyrogram,
                progress_args=("📤 Uploading...", status_msg, c_time),
            )

        elif msg_obj.document:
            saved_msg = await upload_client.send_document(
                chat_id=resolved_log,
                document=file_path,
                caption=f"<b>{caption}</b>",
                file_name=fname,
                parse_mode=ParseMode.HTML,
                progress=progress_for_pyrogram,
                progress_args=("📤 Uploading...", status_msg, c_time),
            )

        elif msg_obj.audio:
            saved_msg = await upload_client.send_audio(
                chat_id=resolved_log,
                audio=file_path,
                caption=f"<b>{caption}</b>",
                parse_mode=ParseMode.HTML,
                progress=progress_for_pyrogram,
                progress_args=("📤 Uploading...", status_msg, c_time),
            )

        elif msg_obj.photo:
            saved_msg = await upload_client.send_photo(
                chat_id=resolved_log,
                photo=file_path,
                caption=f"<b>{caption}</b>",
                parse_mode=ParseMode.HTML,
            )

        else:
            saved_msg = await upload_client.send_document(
                chat_id=resolved_log,
                document=file_path,
                caption=f"<b>{caption}</b>",
                parse_mode=ParseMode.HTML,
                progress=progress_for_pyrogram,
                progress_args=("📤 Uploading...", status_msg, c_time),
            )

        # ── Forward: LOG_CHANNEL → target chat ──
        # Bot se forward karo — instant, no re-upload, cover safe
        if saved_msg:
            await app.forward_messages(
                chat_id=message.chat.id,
                from_chat_id=log,
                message_ids=saved_msg.id,
            )
        # LOG_CHANNEL mein already hai — alag log send karne ki zaroorat nahi

    except Exception as e:
        await status_msg.edit(f"❌ **Upload failed:** `{e}`")
    finally:
        if user_client:
            try:
                await user_client.disconnect()
            except Exception:
                pass
        shutil.rmtree(dl_dir, ignore_errors=True)
        try:
            await status_msg.delete()
        except Exception:
            pass
