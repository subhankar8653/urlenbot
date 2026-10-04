"""
owner_recover.py
================
Owner ID badalne ke baad purana data wapas laane ke liye.

PROBLEM
  Anime monitor list, schedule, custompics, delete-channels, caption/bot
  styles jaisa data MongoDB ke `users` collection mein OWNER ke apne
  document ({'id': <owner_id>}) mein save hota hai. Owner ID badalne par
  bot naya khaali document banata hai — purana data DB mein safe pada
  rehta hai, bas bot use dekhta nahi. (update_post_map global hota hai,
  isliye wo 35 dikha raha tha, par monitor list sirf 4.)

COMMANDS (sirf OWNER)
  /recover_data  -> DB mein purane owner docs dhoondta hai, summary + button
                    se naye owner mein MERGE karta hai (kuch overwrite nahi hota,
                    merge se pehle backup bhi ban jaata hai).
                    Agar purana doc nahi mila toh 'Rebuild' button deta hai jo
                    update_post_map (35 anime) se monitor list dobara banata hai.
"""

import time

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)

from .. import LOGGER, owner
from ..utils.database.access_db import db

# Owner-doc ki wo keys jinse pata chalta hai ki doc mein real data hai
_DATA_KEYS = (
    'anime_monitor_list', 'episode_schedule_list', 'monitor_channel_id',
    'custompics', 'auto_channels', 'channels', 'caption_blacklist',
    'end_messages_map', 'swap_rules', 'upload_mode', 'bot_intro_template',
    'bot_end_template', 'bot_border', 'bot_season_stickers', 'coverpic',
)
_SKIP_KEYS = {'_id', 'id', 'join_date'}


def _norm(t: str) -> str:
    import re
    return re.sub(r'[^a-z0-9]', '', (t or '').lower())


def _esc(v) -> str:
    import html
    return html.escape(str(v))


def _is_empty(v) -> bool:
    return v is None or v == '' or v == [] or v == {}


def _count(v) -> int:
    return len(v) if isinstance(v, (list, dict)) else (0 if _is_empty(v) else 1)


def _merge_docs(cur: dict, old: dict) -> tuple[dict, list]:
    """Pure function. Returns ($set updates for cur, summary lines). Kuch overwrite nahi karta."""
    updates, summary = {}, []

    for k, ov in old.items():
        if k in _SKIP_KEYS or _is_empty(ov):
            continue
        cv = cur.get(k)

        if k == 'anime_monitor_list' and isinstance(ov, list):
            base = list(cv or [])
            seen = {(e.get('channel_id'), (e.get('anime_name') or '').lower()) for e in base}
            seen_names = {_norm(e.get('anime_name')) for e in base}
            added = 0
            for e in ov:
                key = (e.get('channel_id'), (e.get('anime_name') or '').lower())
                if key in seen or _norm(e.get('anime_name')) in seen_names:
                    continue
                base.append(e)
                seen.add(key)
                added += 1
            if added:
                updates[k] = base
                summary.append(f"📺 anime_monitor_list: +{added} (ab total {len(base)})")

        elif k == 'episode_schedule_list' and isinstance(ov, list):
            base = list(cv or [])
            seen = {_norm(e.get('anime_name')) for e in base}
            added = 0
            for e in ov:
                if _norm(e.get('anime_name')) in seen:
                    continue
                base.append(e)
                seen.add(_norm(e.get('anime_name')))
                added += 1
            if added:
                updates[k] = base
                summary.append(f"📅 episode_schedule_list: +{added} (ab total {len(base)})")

        elif _is_empty(cv):
            updates[k] = ov
            summary.append(f"⚙️ {k}: copy ({_count(ov)})")

        elif isinstance(cv, dict) and isinstance(ov, dict):
            miss = {sk: sv for sk, sv in ov.items() if sk not in cv}
            if miss:
                updates[k] = {**cv, **miss}
                summary.append(f"⚙️ {k}: +{len(miss)} entries")

        elif isinstance(cv, list) and isinstance(ov, list):
            extra = [x for x in ov if x not in cv]
            if extra:
                updates[k] = cv + extra
                summary.append(f"⚙️ {k}: +{len(extra)} items")
        # scalar + dono mein value → naya wala rakho

    return updates, summary


def _cur_owner() -> int | None:
    return owner[0] if owner else None


async def _find_candidates(cur_id: int) -> list:
    out = []
    cursor = db.col.find({'id': {'$nin': list(owner)}})
    async for d in cursor:
        if not isinstance(d.get('id'), int):
            continue
        score = sum(_count(d.get(k)) for k in _DATA_KEYS)
        if score:
            out.append((score, d))
    out.sort(key=lambda x: -x[0])
    return [d for _, d in out]


@Client.on_message(filters.command("recover_data") & filters.private)
async def cmd_recover_data(client: Client, message: Message):
    if message.from_user.id not in owner:
        return
    cur_id = _cur_owner()
    cur = await db._get_user(cur_id)
    cands = await _find_candidates(cur_id)

    pm = await db.col2.find_one({'id': 'update_post_map'}) or {}
    post_map = pm.get('map', {})

    head = (f"🛟 <b>Data Recovery</b>\n━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 Current owner doc: <code>{cur_id}</code>\n"
            f"📺 Is doc mein anime: <b>{len(cur.get('anime_monitor_list', []))}</b>\n"
            f"🖼 Global update_post_map: <b>{len(post_map)}</b> anime\n"
            f"🗄 DB: <code>{_esc(db.db.name)}</code>\n━━━━━━━━━━━━━━━━━━━━\n\n")

    btns = []
    if cands:
        body = "<b>Purane owner docs mile:</b>\n"
        for d in cands[:8]:
            n_anime = len(d.get('anime_monitor_list', []) or [])
            n_sched = len(d.get('episode_schedule_list', []) or [])
            body += f"• <code>{d['id']}</code> — {n_anime} anime, {n_sched} schedule\n"
            btns.append([InlineKeyboardButton(
                f"♻️ Merge {d['id']} ({n_anime} anime)", callback_data=f"rec_do_{d['id']}")])
        body += "\nMerge se pehle current doc ka backup ban jaata hai. Kuch overwrite/delete nahi hota."
    else:
        body = ("❌ Koi purana owner doc nahi mila (DB mein ya toh delete ho gaya ya alag MONGO_URI/DB hai).\n"
                "Neeche <b>Rebuild</b> se monitor list update_post_map ke saved anime se dobara ban sakti hai.")

    if post_map:
        btns.append([InlineKeyboardButton(
            f"🛠 Rebuild monitor list from {len(post_map)} saved posts", callback_data="rec_rebuild")])
    btns.append([InlineKeyboardButton("❌ Close", callback_data="closeMeh")])
    await message.reply(head + body, parse_mode=ParseMode.HTML,
                        reply_markup=InlineKeyboardMarkup(btns))


async def _backup_current(cur: dict) -> str:
    key = f"recover_backup_{int(time.time())}"
    snap = {k: v for k, v in cur.items() if k != '_id'}
    await db.col2.update_one({'id': key}, {'$set': {'doc': snap}}, upsert=True)
    return key


@Client.on_callback_query(filters.regex(r"^rec_do_(-?\d+)$"))
async def cb_recover_merge(client: Client, cb: CallbackQuery):
    if cb.from_user.id not in owner:
        await cb.answer("Sirf owner.", show_alert=True)
        return
    old_id = int(cb.matches[0].group(1))
    cur_id = _cur_owner()
    cur = await db._get_user(cur_id)
    old = await db.col.find_one({'id': old_id})
    if not old:
        await cb.answer("Wo doc ab nahi mila.", show_alert=True)
        return
    updates, summary = _merge_docs(cur, old)
    if not updates:
        await cb.answer("Merge karne ko kuch naya nahi.", show_alert=True)
        return
    bkey = await _backup_current(cur)
    await db.col.update_one({'id': cur_id}, {'$set': updates}, upsert=True)
    LOGGER.info(f"[Recover] merged {old_id} -> {cur_id}: {summary}")
    await cb.answer("Merge ho gaya ✅", show_alert=True)
    await cb.message.reply(
        f"✅ <b>Merge complete</b>  <code>{old_id}</code> → <code>{cur_id}</code>\n\n"
        + "\n".join(_esc(s) for s in summary)
        + f"\n\n🗂 Backup: <code>{bkey}</code>\n\n"
          f"Ab <code>/list_anime</code> aur <code>/monitor_status</code> check karo. "
          f"Bot ko <b>restart</b> karna zaroori nahi.",
        parse_mode=ParseMode.HTML)


@Client.on_callback_query(filters.regex(r"^rec_rebuild$"))
async def cb_recover_rebuild(client: Client, cb: CallbackQuery):
    if cb.from_user.id not in owner:
        await cb.answer("Sirf owner.", show_alert=True)
        return
    await cb.answer("Rebuild shuru...")
    cur_id = _cur_owner()
    cur = await db._get_user(cur_id)
    alist = list(cur.get('anime_monitor_list', []) or [])
    have = {_norm(e.get('anime_name')) for e in alist}
    pm = (await db.col2.find_one({'id': 'update_post_map'}) or {}).get('map', {})

    added, failed = [], []
    for _, v in pm.items():
        if not isinstance(v, dict):
            continue
        name = v.get('display_name') or ''
        if not name or _norm(name) in have:
            continue
        link = v.get('invite_link') or ''
        chat = None
        if link:
            try:
                chat = await client.get_chat(link)
            except Exception as e:
                LOGGER.info(f"[Recover] get_chat fail {name}: {e}")
        if chat and getattr(chat, 'id', None):
            alist.append({'channel_id': chat.id, 'channel_title': chat.title or '',
                          'anime_name': name, 'hashtag': '', 'channel_link': link})
            have.add(_norm(name))
            added.append(name)
        else:
            failed.append(name)

    if added:
        bkey = await _backup_current(cur)
        await db.col.update_one({'id': cur_id}, {'$set': {'anime_monitor_list': alist}}, upsert=True)
    else:
        bkey = "—"

    txt = (f"🛠 <b>Rebuild done</b>\n✅ Wapas add hue: <b>{len(added)}</b>\n"
           f"⚠️ Channel resolve nahi hua: <b>{len(failed)}</b>\n🗂 Backup: <code>{bkey}</code>\n")
    if failed:
        txt += ("\n<b>Inhe /add_anime se dobara add karo</b> (bot channel mein admin ho, "
                "ya link expire/revoked ho):\n" + "\n".join(f"• {_esc(n)}" for n in failed[:40]))
    await cb.message.reply(txt[:4000], parse_mode=ParseMode.HTML)
