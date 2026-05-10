"""
save_restrict.py
================
Save Restricted Content feature — Encode Bot ke liye.

Commands:
  /savelogin   — Apna Telegram account connect karo (restricted content save karne ke liye)
  /savelogout  — Session hatao
  /saveget <link> — Kisi bhi restricted/private channel ka content save karo

Flow:
  1. User /savelogin karta hai → phone → OTP → password (agar 2FA ho)
  2. Session string MongoDB mein save hoti hai (existing db use hoga)
  3. /saveget <t.me/c/...> → user ke session se download → bot se upload

NOTE:
  - Bot ka session (BOT_TOKEN) sirf public channels ke liye use hota hai.
  - Restricted content ke liye USER ka string session use hota hai.
  - Isliye /savelogin zaroori hai restricted links ke liye.
  - Apna khud ka session lagane se download/upload speed BADH JAEGI
    kyunki user account ka bandwidth bot account se zyada hota hai.
"""

import asyncio
import os
import shutil
import time

from pyrogram import Client, filters
from pyrogram.errors import (
    ApiIdInvalid,
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

from .. import LOGGER, download_dir, api_id, api_hash, app
from ..utils.database.access_db import db
from ..utils.helper import check_chat
from ..utils.uploads.telegram import upload_video, upload_doc
from ..utils.encoding import get_duration, get_thumbnail, get_width_height
from ..utils.display_progress import progress_for_pyrogram

# ──────────────────────────────────────────────
#  Login state tracker (in-memory)
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
#  DB helpers — user_session field use karenge
# ──────────────────────────────────────────────
async def _get_user_session(user_id: int):
    """MongoDB se user ka saved session string laao."""
    user = await db._get_user(user_id)
    return user.get("user_session", None)


async def _set_user_session(user_id: int, session_str):
    """MongoDB mein user session string save karo."""
    await db.col.update_one(
        {"id": user_id},
        {"$set": {"user_session": session_str}},
        upsert=True,
    )


# ──────────────────────────────────────────────
#  /savelogin — start
# ──────────────────────────────────────────────
@Client.on_message(filters.command("savelogin"))
async def savelogin_start(client: Client, message: Message):
    user_id = message.from_user.id

    existing = await _get_user_session(user_id)
    if existing:
        return await message.reply(
            "✅ **Tum pehle se logged in ho!**\n\n"
            "Agar account change karna hai toh pehle `/savelogout` karo."
        )

    LOGIN_STATE[user_id] = {"step": "WAITING_PHONE", "data": {}}

    await message.reply(
        f"👋 **Save Restrict Login**\n\n"
        f"_{STEPS['WAITING_PHONE']}_\n\n"
        "📞 Apna **Telegram phone number** bhejo (country code ke saath).\n\n"
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
        "Session clear kar diya. Dobara login ke liye `/savelogin` karo."
    )


# ──────────────────────────────────────────────
#  Cancel filter
# ──────────────────────────────────────────────
async def _in_login(_, __, message):
    return message.from_user and message.from_user.id in LOGIN_STATE

login_filter = filters.create(_in_login)


# ──────────────────────────────────────────────
#  Login handler — phone → OTP → password
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

    # Cancel check
    if text.lower() in ["❌ cancel", "/cancel"]:
        try:
            c = state.get("data", {}).get("client")
            if c:
                await c.disconnect()
        except Exception:
            pass
        LOGIN_STATE.pop(user_id, None)
        return await message.reply(
            "❌ **Login cancel kar diya.**",
            reply_markup=remove_kb,
        )

    # ── Step 1: Phone Number ──
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
            return await msg.edit(f"❌ **Error:** `{e}`\nDobara `/savelogin` karo.")

        state["data"] = {
            "client": tmp,
            "phone": phone,
            "hash": code.phone_code_hash,
        }
        state["step"] = "WAITING_CODE"
        await msg.edit(
            f"📩 **OTP bhej diya!**\n_{STEPS['WAITING_CODE']}_\n\n"
            "Telegram app mein code dekho aur yahan bhejo.\n"
            "`Example: 1 2 3 4 5` (spaces ke saath bhejo — auto-delete se bachne ke liye)",
            reply_markup=cancel_kb,
        )

    # ── Step 2: OTP ──
    elif step == "WAITING_CODE":
        code = text.replace(" ", "")
        tmp = state["data"]["client"]
        phone = state["data"]["phone"]
        ph_hash = state["data"]["hash"]

        msg = await message.reply(
            f"🔍 Verifying...\n_{STEPS['WAITING_CODE']}_"
        )
        try:
            await tmp.sign_in(phone, ph_hash, code)
            await _finalize_login(msg, tmp, user_id)
        except PhoneCodeInvalid:
            await msg.edit("❌ **Wrong OTP!** Dobara try karo.")
        except PhoneCodeExpired:
            await tmp.disconnect()
            LOGIN_STATE.pop(user_id, None)
            await msg.edit("⏰ **OTP expire ho gaya.** Dobara `/savelogin` karo.")
        except SessionPasswordNeeded:
            state["step"] = "WAITING_PASS"
            await msg.edit(
                f"🔐 **2-Step Verification on hai!**\n_{STEPS['WAITING_PASS']}_\n\n"
                "Apna **account password** bhejo.",
                reply_markup=cancel_kb,
            )
        except Exception as e:
            await tmp.disconnect()
            LOGIN_STATE.pop(user_id, None)
            await msg.edit(f"❌ **Error:** `{e}`")

    # ── Step 3: Password ──
    elif step == "WAITING_PASS":
        tmp = state["data"]["client"]
        msg = await message.reply(
            f"🔑 Checking password...\n_{STEPS['WAITING_PASS']}_"
        )
        try:
            await tmp.check_password(password=text)
            await _finalize_login(msg, tmp, user_id)
        except PasswordHashInvalid:
            await msg.edit("❌ **Wrong password!** Dobara try karo.")
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
            "Ab tum `/saveget <link>` se restricted content save kar sakte ho! 🚀",
            reply_markup=remove_kb,
        )
    except Exception as e:
        LOGIN_STATE.pop(user_id, None)
        await msg.edit(f"❌ **Session save nahi hua:** `{e}`\nDobara `/savelogin` karo.")


# ──────────────────────────────────────────────
#  /saveget <link> — restricted content save
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

    # Link parse karo
    datas = link.split("/")
    is_private = "t.me/c/" in link

    try:
        msg_id = int(datas[-1].replace("?single", ""))
    except Exception:
        return await message.reply("❌ **Invalid link format!**")

    # Session check
    user_session = await _get_user_session(user_id)
    if user_session is None and is_private:
        return await message.reply(
            "🔒 **Login required!**\n\n"
            "Private/restricted channel ke liye pehle `/savelogin` karo."
        )

    status_msg = await message.reply("⏳ **Fetching content...**")

    # User client banao (user session) ya bot client use karo
    if user_session:
        try:
            user_client = Client(
                "sr_download",
                session_string=user_session,
                api_id=api_id,
                api_hash=api_hash,
                in_memory=True,
                max_concurrent_transmissions=10,
            )
            await user_client.connect()
        except Exception as e:
            return await status_msg.edit(
                f"❌ **Session expired ya invalid hai.**\n"
                f"`{e}`\n\nDobara `/savelogin` karo."
            )
        fetch_client = user_client
    else:
        fetch_client = client  # public link — bot client use karo

    try:
        if is_private:
            chat_id = int("-100" + datas[4])
        else:
            chat_id = datas[3]  # username

        msg_obj = await fetch_client.get_messages(chat_id, msg_id)
    except Exception as e:
        await status_msg.edit(f"❌ **Message fetch nahi hua:** `{e}`")
        if user_session:
            await user_client.disconnect()
        return

    if msg_obj.empty:
        if user_session:
            await user_client.disconnect()
        return await status_msg.edit("❌ **Message empty ya deleted hai.**")

    # File type detect
    media = (
        msg_obj.document or msg_obj.video or msg_obj.audio
        or msg_obj.photo or msg_obj.voice or msg_obj.video_note
    )
    if not media and not msg_obj.text:
        if user_session:
            await user_client.disconnect()
        return await status_msg.edit("❌ **Unsupported content type.**")

    if msg_obj.text:
        if user_session:
            await user_client.disconnect()
        return await client.send_message(message.chat.id, msg_obj.text)

    # Download
    session_id = str(int(time.time()))
    dl_dir = os.path.join(download_dir, f"saverestrict_{session_id}")
    os.makedirs(dl_dir, exist_ok=True)

    await status_msg.edit("⬇️ **Downloading...**")
    c_time = time.time()

    try:
        file_path = await fetch_client.download_media(
            msg_obj,
            file_name=f"{dl_dir}/",
            progress=progress_for_pyrogram,
            progress_args=("⬇️ Downloading...", status_msg, c_time),
        )
    except Exception as e:
        shutil.rmtree(dl_dir, ignore_errors=True)
        if user_session:
            await user_client.disconnect()
        return await status_msg.edit(f"❌ **Download failed:** `{e}`")

    if user_session:
        try:
            await user_client.disconnect()
        except Exception:
            pass

    if not file_path or not os.path.exists(file_path):
        shutil.rmtree(dl_dir, ignore_errors=True)
        return await status_msg.edit("❌ **File download nahi hua.**")

    # Upload
    await status_msg.edit("📤 **Uploading...**")
    fname = os.path.basename(file_path)
    c_time = time.time()

    try:
        if msg_obj.video:
            duration = get_duration(file_path)
            thumb = get_thumbnail(file_path, dl_dir, duration / 4 if duration else 0)
            width, height = get_width_height(file_path)
            caption = msg_obj.caption or fname
            await upload_video(
                message, status_msg, file_path, caption,
                c_time, thumb, duration, width, height,
                file_name=fname,
            )
        elif msg_obj.document or msg_obj.audio:
            caption = msg_obj.caption or fname
            await upload_doc(message, status_msg, c_time, fname, file_path)
        elif msg_obj.photo:
            caption = msg_obj.caption or ""
            await client.send_photo(
                message.chat.id,
                photo=file_path,
                caption=caption,
                reply_to_message_id=message.id,
            )
        else:
            await client.send_document(
                message.chat.id,
                document=file_path,
                caption=msg_obj.caption or fname,
                reply_to_message_id=message.id,
            )
    except Exception as e:
        await status_msg.edit(f"❌ **Upload failed:** `{e}`")
    finally:
        shutil.rmtree(dl_dir, ignore_errors=True)
        try:
            await status_msg.delete()
        except Exception:
            pass
