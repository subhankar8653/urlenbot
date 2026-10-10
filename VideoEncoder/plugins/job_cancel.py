"""❌ Cancel button ka handler (job_ctl.Job ke card ke neeche wala)."""
import asyncio

from pyrogram import Client, filters
from pyrogram.types import CallbackQuery

from .. import LOGGER
from ..utils import job_ctl


@Client.on_callback_query(filters.regex(r"^jc_"))
async def job_cancel_cb(client: Client, cb: CallbackQuery):
    try:
        jid = cb.data[3:]
        job = job_ctl.JOBS.get(jid)
        if not job:
            await cb.answer("Ye process pehle hi khatam / cancel ho chuka hai.", show_alert=True)
            return
        if cb.from_user.id != job.owner:
            await cb.answer("❌ Ye tumhara process nahi hai.", show_alert=True)
            return
        await cb.answer("🛑 Cancel ho raha hai... cache saaf kar raha hoon")
        asyncio.ensure_future(job.cancel())
    except Exception as e:
        LOGGER.error(f"[Job] cancel cb error: {e}")
        try:
            await cb.answer("❌ Cancel error", show_alert=True)
        except Exception:
            pass
