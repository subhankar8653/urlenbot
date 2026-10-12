"""
post_buttons_panel.py
=====================
/customise → ✍️ Caption & Post → 🔘 Main Channel Post Buttons

Main Channel Post (update channel post) ke niche ke 3 buttons ko yahin se
edit / delete / restore kar sakte ho:

    🟢 Watch & Download   — naam, URL (default: har anime ka apna invite link), delete
    🔵 Kaise Dekhein      — naam, URL, delete
    🔴 Join Backup        — naam, URL, delete

Storage:
  • naam / delete(hide) / watch-URL override -> update_channel._get_button_cfg()
  • Kaise Dekhein / Join Backup ka URL        -> update_channel._get_button_defaults()
    (wahi jo purana /update_post_button use karta hai — ek hi source of truth)
"""

from pyrogram import Client, filters, StopPropagation
from pyrogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)

from .. import LOGGER, owner, sudo_users

_SESS: dict = {}     # user_id → {"btn": "watch"|"kaise"|"backup", "field": "text"|"url"}

# key → (emoji, label, default text, defaults-key for URL (None = per-anime invite))
_BTNS = {
    "watch":  ("🟢", "Watch & Download", "⎙ ᴡᴀᴛᴄʜ & ᴅᴏᴡɴʟᴏᴀᴅ ⎙", None),
    "kaise":  ("🔵", "Kaise Dekhein",    "• ᴋᴀɪꜱᴇ ᴅᴇᴋʜᴇɪɴ •",     "kaise_dekhein"),
    "backup": ("🔴", "Join Backup",      "• ᴊᴏɪɴ ʙᴀᴄᴋᴜᴘ •",       "join_backup"),
}
_MAX_TEXT = 60


def clear_session(user_id: int):
    _SESS.pop(user_id, None)


def _is_auth(user_id: int) -> bool:
    return user_id in owner or user_id in sudo_users


def _norm_url(raw: str):
    """Valid URL banao ya None. @user / t.me/... / http(s):// / tg:// accept."""
    u = (raw or "").strip()
    if not u or " " in u:
        return None
    if u.startswith("@") and len(u) > 1:
        return f"https://t.me/{u[1:]}"
    if u.lower().startswith(("http://", "https://", "tg://")):
        return u
    if u.lower().startswith(("t.me/", "telegram.me/")):
        return f"https://{u}"
    return None


async def _state(btn: str) -> dict:
    """Button ka current text / url / enabled (post mein jo dikhega)."""
    from .update_channel import _get_button_cfg, _get_button_defaults
    cfg = (await _get_button_cfg()).get(btn) or {}
    defaults = await _get_button_defaults()
    _, _, default_text, dkey = _BTNS[btn]
    if dkey is None:
        url = cfg.get("url") or ""
        url_label = url or "Anime ka apna invite link (auto)"
    else:
        url = defaults.get(dkey) or ""
        url_label = url or "— (set nahi)"
    return {
        "text": cfg.get("text") or default_text,
        "custom_text": bool(cfg.get("text")),
        "url": url,
        "url_label": url_label,
        "enabled": cfg.get("enabled", True),
    }


async def _edit(client: Client, cb: CallbackQuery, text: str, kb: InlineKeyboardMarkup):
    from .customise import _safe_edit
    await _safe_edit(client, cb, text, kb)


def _back(to_open: str, label: str) -> list:
    return [
        InlineKeyboardButton(label, callback_data=f"cmz:open:{to_open}"),
        InlineKeyboardButton("🏠 Menu", callback_data="cmz:menu"),
    ]


# ───────────── main panel ─────────────
async def render_main(client: Client, cb: CallbackQuery):
    from .customise import _CUR_SECTION, _back_row
    _CUR_SECTION.set("update_post_button")
    clear_session(cb.from_user.id)
    lines = []
    for key, (emoji, label, _, _) in _BTNS.items():
        st = await _state(key)
        status = "✅" if (st["enabled"] and (st["url"] or key == "watch")) else "🗑 band"
        lines.append(f"{emoji} **{label}** — {status}\n    naam: `{st['text']}`\n    link: {st['url_label']}")
    text = (
        "🔘 **Main Channel Post Buttons**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Post ke niche ke 3 buttons. Kisi par tap karke naam / URL badlo ya delete karo.\n\n"
        + "\n\n".join(lines)
    )
    rows = [[InlineKeyboardButton(f"{e} {l}", callback_data=f"cmz:pb:open:{k}")]
            for k, (e, l, _, _) in _BTNS.items()]
    rows.append(_back_row())
    await _edit(client, cb, text, InlineKeyboardMarkup(rows))


# ───────────── single button screen ─────────────
async def _render_btn(client: Client, cb: CallbackQuery, btn: str, note: str = ""):
    clear_session(cb.from_user.id)
    emoji, label, _, dkey = _BTNS[btn]
    st = await _state(btn)
    shown = st["enabled"] and (st["url"] or btn == "watch")
    text = (
        f"{emoji} **{label} Button**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Status: **{'✅ Post mein dikhega' if shown else '🗑 Delete / band'}**\n"
        f"Naam: `{st['text']}`\n"
        f"Link: {st['url_label']}"
        + (f"\n\n{note}" if note else "")
    )
    rows = [[
        InlineKeyboardButton("✏️ Edit Name", callback_data=f"cmz:pb:name:{btn}"),
        InlineKeyboardButton("🔗 Edit URL", callback_data=f"cmz:pb:url:{btn}"),
    ]]
    if st["enabled"]:
        rows.append([InlineKeyboardButton("🗑 Delete Button", callback_data=f"cmz:pb:del:{btn}")])
    else:
        rows.append([InlineKeyboardButton("♻️ Restore Button", callback_data=f"cmz:pb:restore:{btn}")])
    rows.append([InlineKeyboardButton("↩️ Naam / URL Default pe", callback_data=f"cmz:pb:reset:{btn}")])
    rows.append(_back("update_post_button", "⬅️ Buttons"))
    await _edit(client, cb, text, InlineKeyboardMarkup(rows))


async def _patch_cfg(btn: str, **changes):
    from .update_channel import _get_button_cfg, _save_button_cfg
    cfg = await _get_button_cfg()
    entry = dict(cfg.get(btn) or {})
    for k, v in changes.items():
        if v is None:
            entry.pop(k, None)
        else:
            entry[k] = v
    if entry:
        cfg[btn] = entry
    else:
        cfg.pop(btn, None)
    await _save_button_cfg(cfg)


async def _set_default_url(dkey: str, url):
    from .update_channel import _get_button_defaults, _save_button_defaults
    d = await _get_button_defaults()
    d[dkey] = url
    await _save_button_defaults(d)


# ───────────── callbacks:  cmz:pb:<sub>:<btn> ─────────────
async def handle(client: Client, cb: CallbackQuery, args: list):
    sub = args[0] if args else ""
    btn = args[1] if len(args) > 1 else ""
    if btn not in _BTNS:
        await cb.answer()
        await render_main(client, cb)
        return
    emoji, label, default_text, dkey = _BTNS[btn]

    if sub == "open":
        await cb.answer()
        await _render_btn(client, cb, btn)
        return

    if sub in ("name", "url"):
        _SESS[cb.from_user.id] = {"btn": btn, "field": "text" if sub == "name" else "url"}
        await cb.answer()
        if sub == "name":
            prompt = (
                f"**✏️ {label} — naya naam bhejo**\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                f"Button par ye text dikhega (max {_MAX_TEXT} akshar).\n"
                f"Default: `{default_text}`"
            )
        else:
            extra = (
                "Anime ka apna invite link wapas lagana ho to `auto` bhejo."
                if dkey is None else
                "Button hatana ho to `skip` bhejo."
            )
            prompt = (
                f"**🔗 {label} — naya URL bhejo**\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "Example: `https://t.me/+xxxxxxxx` ya `@channel`\n"
                f"{extra}"
            )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cmz:pb:open:{btn}")]])
        await _edit(client, cb, prompt, kb)
        return

    if sub == "del":
        await _patch_cfg(btn, enabled=False)
        await cb.answer("🗑 Button delete ho gaya")
        await _render_btn(client, cb, btn, "_Ab ye button post mein nahi aayega. Wapas lane ke liye Restore dabao._")
        return

    if sub == "restore":
        await _patch_cfg(btn, enabled=None)
        await cb.answer("♻️ Restore ho gaya")
        await _render_btn(client, cb, btn)
        return

    if sub == "reset":
        await _patch_cfg(btn, text=None, enabled=None, **({"url": None} if dkey is None else {}))
        await cb.answer("↩️ Default pe aa gaya")
        await _render_btn(client, cb, btn)
        return

    await cb.answer()


# ───────────── text input ─────────────
# group=-3: update_channel.py ke purane button-session text handler se pehle
# chalta hai; sirf tab consume karta hai jab user is panel ke input mode mein ho.
@Client.on_message(filters.text & filters.private, group=-3)
async def pb_text_input(client: Client, message: Message):
    if not message.from_user:
        return
    uid = message.from_user.id
    sess = _SESS.get(uid)
    if not sess or not _is_auth(uid):
        return
    raw = (message.text or "").strip()
    if raw.startswith("/"):          # koi command aaya — input mode cancel, command normal chale
        _SESS.pop(uid, None)
        return

    btn, field = sess["btn"], sess["field"]
    emoji, label, default_text, dkey = _BTNS[btn]
    back_kb = InlineKeyboardMarkup([_back("update_post_button", "⬅️ Buttons")])
    try:
        if field == "text":
            if not raw or len(raw) > _MAX_TEXT:
                await message.reply(f"⚠️ Naam 1 se {_MAX_TEXT} akshar ke beech hona chahiye. Dobara bhejo.")
                raise StopPropagation
            await _patch_cfg(btn, text=raw, enabled=None)
            _SESS.pop(uid, None)
            await message.reply(f"✅ **{label}** ka naam ab: `{raw}`", reply_markup=back_kb)
        else:
            low = raw.lower()
            if dkey is None and low == "auto":
                await _patch_cfg(btn, url=None)
                _SESS.pop(uid, None)
                await message.reply(f"✅ **{label}** ab har anime ka apna invite link use karega.", reply_markup=back_kb)
            elif dkey is not None and low in ("skip", "none", "remove"):
                await _set_default_url(dkey, None)
                _SESS.pop(uid, None)
                await message.reply(f"🗑 **{label}** ka URL hata diya — button post mein nahi aayega.", reply_markup=back_kb)
            else:
                url = _norm_url(raw)
                if not url:
                    await message.reply("⚠️ Sahi URL bhejo (`https://...` ya `@channel`). Dobara bhejo.")
                    raise StopPropagation
                if dkey is None:
                    await _patch_cfg(btn, url=url, enabled=None)
                else:
                    await _set_default_url(dkey, url)
                    await _patch_cfg(btn, enabled=None)
                _SESS.pop(uid, None)
                await message.reply(f"✅ **{label}** ka URL set ho gaya:\n{url}", reply_markup=back_kb)
    except StopPropagation:
        raise
    except Exception as e:
        LOGGER.error(f"[PostButtons] save failed: {e}")
        await message.reply("❌ Save nahi ho paya, dobara try karo.")
    raise StopPropagation
