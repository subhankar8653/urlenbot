"""
❌ Cancel + 🗑 Delete uploads — job card ke buttons ka handler.

Kaun dabा sakta hai:
  • job ka owner, bot owner / sudo users
  • Jis chat (monitor channel) ka job hai, uske ADMINS — auto-monitor card pe koi bhi admin
    Cancel ya Delete kar sakta hai (sirf bot owner hona zaroori nahi).

🗑 Delete uploads (auto-monitor card pe):
  Is job ne jo bhi channel pe post kiya (episodes ki saari qualities, season intro / border /
  sticker, update-channel post, bot-mode post, schedule "Next episode" msgs) sab delete.
  Pehle confirm poochta hai (✅ Haan / ↩️ Nahi). Job abhi chal raha ho to pehle cancel hota hai.
"""
import asyncio

from pyrogram import Client, filters
from pyrogram.enums import ChatMemberStatus
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from .. import LOGGER, owner, sudo_users
from ..utils import job_ctl


async def _is_allowed(client: Client, uid: int, job_owner, admin_chat) -> bool:
    if uid == job_owner or uid in (set(sudo_users or []) | set(owner or [])):
        return True
    if admin_chat:
        try:
            m = await client.get_chat_member(admin_chat, uid)
            return m.status in (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR)
        except Exception as e:  # noqa: BLE001
            LOGGER.debug(f"[Job] admin check fail ({admin_chat}, {uid}): {e}")
    return False


async def _load_ledger(jid: str):
    """(ledger, admin_chat, owner, title, meta, job_obj|None) — live job > finished job > DB."""
    job = job_ctl.JOBS.get(jid) or job_ctl.FINISHED.get(jid)
    if job is not None:
        return list(job.ledger), job.admin_chat, job.owner, job.title, dict(job.meta), job
    try:
        from ..utils.database.access_db import db
        doc = await db.col2.find_one({"id": f"jledger_{jid}"})
    except Exception:
        doc = None
    if not doc:
        return None
    return ([tuple(x) for x in doc.get("ledger", [])], doc.get("admin_chat"), doc.get("owner"),
            doc.get("title", ""), doc.get("meta") or {}, None)


@Client.on_callback_query(filters.regex(r"^jc_"))
async def job_cancel_cb(client: Client, cb: CallbackQuery):
    try:
        jid = cb.data[3:]
        job = job_ctl.JOBS.get(jid)
        if not job:
            await cb.answer("Ye process pehle hi khatam / cancel ho chuka hai.", show_alert=True)
            return
        if not await _is_allowed(client, cb.from_user.id, job.owner, job.admin_chat):
            await cb.answer("❌ Sirf bot owner / is channel ke admins hi cancel kar sakte hain.", show_alert=True)
            return
        await cb.answer("🛑 Cancel ho raha hai... cache saaf kar raha hoon")
        asyncio.ensure_future(job.cancel())
    except Exception as e:
        LOGGER.error(f"[Job] cancel cb error: {e}")
        try:
            await cb.answer("❌ Cancel error", show_alert=True)
        except Exception:
            pass


# ───────────────────────── 🔄 Re-check ─────────────────────────
@Client.on_callback_query(filters.regex(r"^jr_"))
async def job_recheck_cb(client: Client, cb: CallbackQuery):
    """
    Auto-monitor card ka "🔄 Re-check" button.
      • Job abhi chal raha hai  -> poller ko turant jagao (agle interval ka wait nahi) /
                                   agar abhi check chal hi raha hai to uske turant baad dobara.
      • Job timeout se khatam   -> baaki qualities ke liye naya poll-cycle shuru.
    """
    try:
        jid = cb.data[3:]
        job = job_ctl.JOBS.get(jid)
        if job is not None and not job.finished and not job.cancelled:
            if not await _is_allowed(client, cb.from_user.id, job.owner, job.admin_chat):
                await cb.answer("❌ Sirf bot owner / is channel ke admins hi Re-check kar sakte hain.", show_alert=True)
                return
            ev = job.recheck_ev
            if ev is None or not job.can_recheck:
                await cb.answer("Abhi re-check ka time nahi hai — thodi der baad try karo.", show_alert=True)
                return
            ev.set()
            await cb.answer("🔄 Dobara check ho raha hai...")
            return

        rj = job_ctl.RECHECKS.get(jid)
        if rj is None or not rj.recheck_items:
            await cb.answer("Is process ka re-check ab available nahi (restart / pehle hi chal chuka).", show_alert=True)
            return
        if not await _is_allowed(client, cb.from_user.id, rj.owner, rj.admin_chat):
            await cb.answer("❌ Sirf bot owner / is channel ke admins hi Re-check kar sakte hain.", show_alert=True)
            return

        items = list(rj.recheck_items)
        rj.recheck_items.clear()
        job_ctl.RECHECKS.pop(jid, None)
        await cb.answer("🔄 Naya check shuru ho raha hai...")
        try:   # purane card se Re-check button hata do (Delete button rehne do)
            await cb.message.edit_reply_markup(rj.final_markup())
        except Exception:
            pass

        async def _run_all():
            for fn in items:
                await fn()

        asyncio.ensure_future(job_ctl.run_with_card(
            cb.message, "AutoMonitor", _run_all,
            owner_id=rj.owner or 0, admin_chat=rj.admin_chat, deletable=rj.deletable,
        ))
    except Exception as e:
        LOGGER.error(f"[Job] recheck cb error: {e}", exc_info=True)
        try:
            await cb.answer("❌ Re-check error", show_alert=True)
        except Exception:
            pass


# ───────────────────────── 🗑 Delete uploads ─────────────────────────
@Client.on_callback_query(filters.regex(r"^jd_"))
async def job_delete_ask_cb(client: Client, cb: CallbackQuery):
    try:
        jid = cb.data[3:]
        info = await _load_ledger(jid)
        if not info or not info[0]:
            await cb.answer("Delete karne ke liye kuch nahi mila (ya pehle hi delete ho chuka).", show_alert=True)
            return
        ledger, admin_chat, jowner, title, meta, job = info
        if not await _is_allowed(client, cb.from_user.id, jowner, admin_chat):
            await cb.answer("❌ Sirf bot owner / is channel ke admins hi delete kar sakte hain.", show_alert=True)
            return
        n = len(ledger)
        running = " Process abhi chal raha hai — woh bhi ruk jaayega." if (job and not job.finished and not job.cancelled) else ""
        await cb.answer(f"⚠️ {n} posts delete honge (episodes + update post + extras).{running} Neeche confirm karo.",
                        show_alert=True)
        await cb.message.edit_reply_markup(InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Haan, sab delete karo", callback_data=f"jdy_{jid}"),
            InlineKeyboardButton("↩️ Nahi", callback_data=f"jdn_{jid}"),
        ]]))
    except Exception as e:
        LOGGER.error(f"[Job] delete-ask error: {e}")
        try:
            await cb.answer("❌ Error", show_alert=True)
        except Exception:
            pass


@Client.on_callback_query(filters.regex(r"^jdn_"))
async def job_delete_no_cb(client: Client, cb: CallbackQuery):
    try:
        jid = cb.data[4:]
        info = await _load_ledger(jid)
        if info:
            ledger, admin_chat, jowner, title, meta, job = info
            if not await _is_allowed(client, cb.from_user.id, jowner, admin_chat):
                await cb.answer("❌ Allowed nahi.", show_alert=True)
                return
        await cb.answer("Theek hai, delete nahi kiya.")
        live = job_ctl.JOBS.get(jid)
        if live is not None:
            await cb.message.edit_reply_markup(live.markup())
        else:
            fin = job_ctl.FINISHED.get(jid)
            if fin is not None:
                await cb.message.edit_reply_markup(fin.final_markup())
            else:
                await cb.message.edit_reply_markup(InlineKeyboardMarkup([[
                    InlineKeyboardButton("🗑 Delete uploads", callback_data=f"jd_{jid}")]]))
    except Exception as e:
        LOGGER.error(f"[Job] delete-no error: {e}")


@Client.on_callback_query(filters.regex(r"^jdy_"))
async def job_delete_do_cb(client: Client, cb: CallbackQuery):
    try:
        jid = cb.data[4:]
        info = await _load_ledger(jid)
        if not info or not info[0]:
            await cb.answer("Pehle hi delete ho chuka / kuch nahi mila.", show_alert=True)
            return
        ledger, admin_chat, jowner, title, meta, job = info
        if not await _is_allowed(client, cb.from_user.id, jowner, admin_chat):
            await cb.answer("❌ Allowed nahi.", show_alert=True)
            return
        await cb.answer("🗑 Delete ho raha hai...")
        who = cb.from_user.first_name or str(cb.from_user.id)
        try:
            await cb.message.edit("🗑 **Delete ho raha hai...**\nThodi der ruko.", reply_markup=None)
        except Exception:
            pass

        # 1) job chal raha hai to pehle rokho (naye uploads aur na aayein)
        if job is not None and not job.finished and not job.cancelled:
            try:
                await asyncio.wait_for(job.cancel(), timeout=60)
            except Exception:
                pass
            ledger = list(job.ledger)

        deleted, failed = await _delete_ledger(client, ledger, meta, jowner)

        # 2) DB ledger + memory saaf
        try:
            from ..utils.database.access_db import db
            await db.col2.delete_one({"id": f"jledger_{jid}"})
        except Exception:
            pass
        job_ctl.FINISHED.pop(jid, None)
        fin = job_ctl.JOBS.get(jid)
        if fin is not None:
            fin.ledger.clear()

        text = (f"🗑 **Delete ho gaya** — {title}\n\n"
                f"✅ `{deleted}` posts delete\n"
                + (f"⚠️ `{failed}` delete nahi ho paye (bot ko 'Delete messages' right do / pehle hi delete)\n" if failed else "")
                + f"👤 By: {who}")
        try:
            await cb.message.edit(text, reply_markup=None)
        except Exception:
            pass
    except Exception as e:
        LOGGER.error(f"[Job] delete-do error: {e}", exc_info=True)
        try:
            await cb.answer("❌ Delete error", show_alert=True)
        except Exception:
            pass


async def _delete_ledger(client: Client, ledger: list, meta: dict, owner_id) -> tuple:
    """ledger [(chat, msg)] + schedule msgs + update-post record delete. Returns (deleted, failed)."""
    by_chat: dict = {}
    for chat_id, mid in ledger:
        by_chat.setdefault(int(chat_id), set()).add(int(mid))

    # schedule_notify ke "Next episode on ..." / END msgs (is job ke channel(s) ke)
    for ch in (meta.get("channels") or []):
        try:
            from .schedule_notify import get_last_posted_msg_ids, clear_last_posted_msg_ids
            ids = await get_last_posted_msg_ids(int(ch))
            if ids:
                by_chat.setdefault(int(ch), set()).update(int(i) for i in ids)
                await clear_last_posted_msg_ids(int(ch))
        except Exception as e:  # noqa: BLE001
            LOGGER.warning(f"[Job] schedule ids delete skip: {e}")

    uc = None
    deleted = failed = 0
    try:
        for chat_id, ids in by_chat.items():
            ids = sorted(ids)
            for i in range(0, len(ids), 100):
                chunk = ids[i:i + 100]
                ok = False
                try:
                    await client.delete_messages(chat_id, chunk)
                    ok = True
                except Exception as e:  # noqa: BLE001
                    LOGGER.warning(f"[Job] bot delete fail ({chat_id}): {e!r} — user session try")
                    if uc is None and owner_id:
                        try:
                            from ..utils.uploads.telegram import _make_uploader_client
                            uc = await asyncio.wait_for(_make_uploader_client(owner_id, max_transmissions=2), timeout=30)
                        except Exception:
                            uc = None
                    if uc is not None:
                        try:
                            await uc.delete_messages(chat_id, chunk)
                            ok = True
                        except Exception as e2:  # noqa: BLE001
                            LOGGER.warning(f"[Job] user delete fail ({chat_id}): {e2!r}")
                if ok:
                    deleted += len(chunk)
                else:
                    failed += len(chunk)
                await asyncio.sleep(0.3)
            try:
                from ..utils import episode_guard
                episode_guard.forget_channel(chat_id)     # dobara upload ho sake
            except Exception:
                pass
    finally:
        if uc is not None:
            try:
                await uc.disconnect()
            except Exception:
                pass

    # update-channel ka "latest post" record agar hamare delete kiye hue posts ka tha to saaf
    try:
        from .update_channel import _get_latest_post_ids, _save_latest_post_ids
        latest = await _get_latest_post_ids()
        gone = {(int(c), int(m)) for c, ids in by_chat.items() for m in ids}
        keep = [p for p in latest if (int(p.get("channel_id", 0)), int(p.get("message_id", 0))) not in gone]
        if len(keep) != len(latest):
            await _save_latest_post_ids(keep)
    except Exception as e:  # noqa: BLE001
        LOGGER.debug(f"[Job] latest_post cleanup skip: {e}")
    return deleted, failed
