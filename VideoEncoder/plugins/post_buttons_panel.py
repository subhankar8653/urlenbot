"""
post_buttons_panel.py
=====================
/customise → ✍️ Caption & Post → 🔘 Main Channel Post Buttons

Main Channel Post (update channel post) ke niche ke 3 buttons ko yahin se
edit / delete / restore kar sakte ho:

    🟢 Watch & Download   — naam, URL (default: anime ka invite link), colour, delete
    🔵 Kaise Dekhein      — naam, URL, colour, delete
    🔴 Join Backup        — naam, URL, colour, delete

Multi Mode (/multi_mode on): pehle update channel choose karte ho
("Channel 01", "Channel 02" ...), phir sirf USI channel ke buttons edit hote hain.
"🌐 Default" baaki saare channels ke liye fallback hai. Multi Mode OFF mein
seedha global (purana) panel khulta hai.

Storage:
  • global: update_channel._get_button_cfg()  (+ kaise/backup URL → _get_button_defaults())
  • channel-wise: update_channels record ka  "buttons": {watch/kaise/backup: {text,url,enabled,style}}
"""

from pyrogram import Client, filters, StopPropagation, ContinuePropagation
from pyrogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)

from .. import LOGGER, owner, sudo_users

_SESS: dict = {}     # user_id → {"btn", "scope", "field": "text"|"url"}

# key → (emoji, label, default text, defaults-key for URL (None = anime ka invite link))
_BTNS = {
    "watch":  ("🟢", "Watch & Download", "⎙ ᴡᴀᴛᴄʜ & ᴅᴏᴡɴʟᴏᴀᴅ ⎙", None),
    "kaise":  ("🔵", "Kaise Dekhein",    "• ᴋᴀɪꜱᴇ ᴅᴇᴋʜᴇɪɴ •",     "kaise_dekhein"),
    "backup": ("🔴", "Join Backup",      "• ᴊᴏɪɴ ʙᴀᴄᴋᴜᴘ •",       "join_backup"),
}
_MAX_TEXT = 60
_DEFAULT_STYLE = {"watch": "success", "kaise": "primary", "backup": "danger"}
_COLOURS = {                      # style value → (emoji, naam)
    "success": ("🟢", "Green"),
    "primary": ("🔵", "Blue"),
    "danger":  ("🔴", "Red"),
    "none":    ("⚪", "Off (no colour)"),
}
_PRESET_TEXT = "Episode [E] Added"
_PLACEHOLDER_HELP = (
    "Placeholders: `[E]` = episode (02 ya 02-03), `[S]` = season, `[L]` = language.\n"
    "Example: `Episode [E] Added` → `Episode 02-03 Added`"
)


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


# ── scope helpers: scope None = global, int = us update channel ka id ──
def _tok(btn: str, scope) -> str:
    return btn if scope is None else f"{btn}@{scope}"


def _split(tok: str):
    btn, _, sc = (tok or "").partition("@")
    try:
        scope = int(sc) if sc else None
    except ValueError:
        scope = None
    return btn, scope


def _sc(scope) -> str:
    return "g" if scope is None else str(scope)


async def _channels() -> list:
    from .update_channel import _get_update_channels
    return await _get_update_channels()


async def _channel_info(scope):
    """(position 1-based, record) ya (0, None)."""
    for i, ch in enumerate(await _channels(), 1):
        if ch.get("channel_id") == scope:
            return i, ch
    return 0, None


async def _state(btn: str, scope=None) -> dict:
    """Button ka current text / url / enabled / style (post mein jo dikhega)."""
    from .update_channel import _get_button_cfg, _get_button_defaults
    cfg = dict((await _get_button_cfg()).get(btn) or {})
    over = {}
    pos = 0
    if scope is not None:
        pos, ch = await _channel_info(scope)
        over = ((ch or {}).get("buttons") or {}).get(btn) or {}
        cfg.update(over)
    defaults = await _get_button_defaults()
    _, _, default_text, dkey = _BTNS[btn]
    if dkey is None:
        url = cfg.get("url") or ""
        if url:
            url_label = url
        elif scope is not None:
            url_label = "Is channel ka link (anime ke Link N se, auto)"
        else:
            url_label = "Anime ka apna invite link (auto)"
    else:
        url = over.get("url") or defaults.get(dkey) or ""
        url_label = url or "— (set nahi)"
    return {
        "text": cfg.get("text") or default_text,
        "custom_text": bool(cfg.get("text")),
        "url": url,
        "url_label": url_label,
        "enabled": cfg.get("enabled", True),
        "style": cfg.get("style") or _DEFAULT_STYLE[btn],
    }


def _preview(text: str) -> str:
    """Naam ka sample preview ([E]/[S]/[L] bhar ke)."""
    from .update_channel import _fill_button_text
    return _fill_button_text(text, "02-03", 2, "Hindi Dub")


async def _edit(client: Client, cb: CallbackQuery, text: str, kb: InlineKeyboardMarkup):
    from .customise import _safe_edit
    await _safe_edit(client, cb, text, kb)


def _back(to_open: str, label: str) -> list:
    return [
        InlineKeyboardButton(label, callback_data=f"cmz:open:{to_open}"),
        InlineKeyboardButton("🏠 Menu", callback_data="cmz:menu"),
    ]


async def _scope_title(scope) -> str:
    if scope is None:
        return "🌐 Default (baaki sab channels)"
    pos, ch = await _channel_info(scope)
    return f"📢 Channel {pos:02d} — {(ch or {}).get('channel_title', 'Unknown')}"


# ───────────── entry: multi mode → channel list, warna seedha global ─────────────
async def render_main(client: Client, cb: CallbackQuery):
    from .customise import _CUR_SECTION
    from ..utils.multi_mode import is_multi
    _CUR_SECTION.set("update_post_button")
    clear_session(cb.from_user.id)
    if await is_multi():
        await _render_channels(client, cb)
    else:
        await _render_scope(client, cb, None)


async def _render_channels(client: Client, cb: CallbackQuery):
    from .customise import _back_row
    chans = await _channels()
    text = (
        "🔘 **Main Channel Post Buttons**  🔀 _Multi Mode ON_\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Jis update channel ke buttons edit karne hain wo choose karo. "
        "Jis channel ke liye alag setting nahi ki, usme 🌐 Default wale buttons jaate hain."
        + ("" if chans else "\n\n📭 Abhi koi update channel add nahi hai.")
    )
    rows = []
    for i, ch in enumerate(chans, 1):
        mark = " 🎛" if ch.get("buttons") else ""
        rows.append([InlineKeyboardButton(
            f"📢 Channel {i:02d} — {(ch.get('channel_title') or 'Unknown')[:26]}{mark}",
            callback_data=f"cmz:pb:chan:{ch.get('channel_id')}")])
    rows.append([InlineKeyboardButton("🌐 Default (baaki sab ke liye)", callback_data="cmz:pb:chan:g")])
    rows.append(_back_row())
    await _edit(client, cb, text, InlineKeyboardMarkup(rows))


async def _render_scope(client: Client, cb: CallbackQuery, scope):
    """Ek scope (global ya ek channel) ke 3 buttons ki list."""
    from .customise import _back_row
    from ..utils.multi_mode import is_multi
    clear_session(cb.from_user.id)
    lines = []
    for key, (emoji, label, _, _) in _BTNS.items():
        st = await _state(key, scope)
        status = "✅" if (st["enabled"] and (st["url"] or key == "watch")) else "🗑 band"
        col = _COLOURS.get(st["style"], _COLOURS["none"])[0]
        lines.append(f"{emoji} **{label}** — {status}\n    naam: `{st['text']}`\n    link: {st['url_label']}\n    colour: {col}")
    text = (
        f"🔘 **Main Channel Post Buttons**\n"
        + (f"{await _scope_title(scope)}\n" if await is_multi() else "")
        + "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Post ke niche ke 3 buttons. Kisi par tap karke naam / URL badlo ya delete karo.\n\n"
        + "\n\n".join(lines)
    )
    rows = [[InlineKeyboardButton(f"{e} {l}", callback_data=f"cmz:pb:open:{_tok(k, scope)}")]
            for k, (e, l, _, _) in _BTNS.items()]
    if await is_multi():
        if scope is not None:
            rows.append([InlineKeyboardButton("↩️ Is channel ko Default pe", callback_data=f"cmz:pb:chreset:{scope}")])
        rows.append(_back("update_post_button", "⬅️ Channels"))
    else:
        rows.append(_back_row())
    await _edit(client, cb, text, InlineKeyboardMarkup(rows))


# ───────────── single button screen ─────────────
async def _render_btn(client: Client, cb: CallbackQuery, btn: str, scope=None, note: str = ""):
    clear_session(cb.from_user.id)
    emoji, label, _, dkey = _BTNS[btn]
    st = await _state(btn, scope)
    shown = st["enabled"] and (st["url"] or btn == "watch")
    cur_style = _COLOURS.get(st['style'], _COLOURS['none'])
    head = f"{await _scope_title(scope)}\n" if scope is not None else ""
    text = (
        f"{emoji} **{label} Button**\n{head}"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Status: **{'✅ Post mein dikhega' if shown else '🗑 Delete / band'}**\n"
        f"Naam: `{st['text']}`\n"
        + (f"Post mein dikhega: `{_preview(st['text'])}`\n" if "[" in st['text'] else "")
        + f"Link: {st['url_label']}\n"
        f"Colour: {cur_style[0]} {cur_style[1]}"
        + (f"\n\n{note}" if note else "")
    )
    cur = st["style"]
    tok = _tok(btn, scope)
    colour_row = [
        InlineKeyboardButton(("✅" if cur == key else emo), callback_data=f"cmz:pb:col:{tok}:{key}")
        for key, (emo, _n) in _COLOURS.items()
    ]
    rows = [[
        InlineKeyboardButton("✏️ Edit Name", callback_data=f"cmz:pb:name:{tok}"),
        InlineKeyboardButton("🔗 Edit URL", callback_data=f"cmz:pb:url:{tok}"),
    ],
        [InlineKeyboardButton("📌 Preset: Episode [E] Added", callback_data=f"cmz:pb:preset:{tok}")],
        [InlineKeyboardButton("🎨", callback_data="cmz:noop")] + colour_row,
    ]
    if st["enabled"]:
        rows.append([InlineKeyboardButton("🗑 Delete Button", callback_data=f"cmz:pb:del:{tok}")])
    else:
        rows.append([InlineKeyboardButton("♻️ Restore Button", callback_data=f"cmz:pb:restore:{tok}")])
    rows.append([InlineKeyboardButton("↩️ Naam / URL / Colour Default pe", callback_data=f"cmz:pb:reset:{tok}")])
    from ..utils.multi_mode import is_multi
    if await is_multi():
        rows.append([
            InlineKeyboardButton("⬅️ Buttons", callback_data=f"cmz:pb:chan:{_sc(scope)}"),
            InlineKeyboardButton("🏠 Menu", callback_data="cmz:menu"),
        ])
    else:
        rows.append(_back("update_post_button", "⬅️ Buttons"))
    await _edit(client, cb, text, InlineKeyboardMarkup(rows))


async def _patch_cfg(btn: str, scope=None, **changes):
    """changes[k]=None → us key ko hata do. scope None = global, int = us channel ka record."""
    if scope is None:
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
        return

    from .update_channel import _get_update_channels, _save_update_channels
    chans = await _get_update_channels()
    for ch in chans:
        if ch.get("channel_id") == scope:
            buttons = dict(ch.get("buttons") or {})
            entry = dict(buttons.get(btn) or {})
            for k, v in changes.items():
                if v is None:
                    entry.pop(k, None)
                else:
                    entry[k] = v
            if entry:
                buttons[btn] = entry
            else:
                buttons.pop(btn, None)
            if buttons:
                ch["buttons"] = buttons
            else:
                ch.pop("buttons", None)
            break
    await _save_update_channels(chans)


async def _set_default_url(dkey: str, url):
    from .update_channel import _get_button_defaults, _save_button_defaults
    d = await _get_button_defaults()
    d[dkey] = url
    await _save_button_defaults(d)


# ───────────── callbacks:  cmz:pb:<sub>:<btn[@channel]>[:<val>] ─────────────
async def handle(client: Client, cb: CallbackQuery, args: list):
    sub = args[0] if args else ""

    # channel choose (multi mode): cmz:pb:chan:<channel_id | g>
    if sub == "chan":
        tok = args[1] if len(args) > 1 else "g"
        await cb.answer()
        if tok == "g":
            await _render_scope(client, cb, None)
        else:
            try:
                cid = int(tok)
            except ValueError:
                await _render_channels(client, cb)
                return
            pos, ch = await _channel_info(cid)
            if not ch:
                await cb.answer("Channel list mein nahi mila.", show_alert=True)
                await _render_channels(client, cb)
                return
            await _render_scope(client, cb, cid)
        return

    if sub == "chreset":
        try:
            cid = int(args[1])
        except (ValueError, IndexError):
            await cb.answer()
            return
        from .update_channel import _get_update_channels, _save_update_channels
        chans = await _get_update_channels()
        for ch in chans:
            if ch.get("channel_id") == cid:
                ch.pop("buttons", None)
        await _save_update_channels(chans)
        await cb.answer("↩️ Channel Default pe aa gaya")
        await _render_scope(client, cb, cid)
        return

    btn, scope = _split(args[1] if len(args) > 1 else "")
    if btn not in _BTNS:
        await cb.answer()
        await render_main(client, cb)
        return
    emoji, label, default_text, dkey = _BTNS[btn]

    if sub == "open":
        await cb.answer()
        await _render_btn(client, cb, btn, scope)
        return

    if sub == "col":
        val = args[2] if len(args) > 2 else ""
        if val not in _COLOURS:
            await cb.answer()
            return
        await _patch_cfg(btn, scope, style=val)
        await cb.answer(f"{_COLOURS[val][0]} {_COLOURS[val][1]}")
        await _render_btn(client, cb, btn, scope)
        return

    if sub == "preset":
        await _patch_cfg(btn, scope, text=_PRESET_TEXT, enabled=None)
        await cb.answer("📌 Preset lag gaya")
        await _render_btn(client, cb, btn, scope)
        return

    if sub in ("name", "url"):
        _SESS[cb.from_user.id] = {"btn": btn, "scope": scope, "field": "text" if sub == "name" else "url"}
        await cb.answer()
        where = f"\n{await _scope_title(scope)}" if scope is not None else ""
        if sub == "name":
            prompt = (
                f"**✏️ {label} — naya naam bhejo**{where}\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                f"Button par ye text dikhega (max {_MAX_TEXT} akshar).\n"
                f"Default: `{default_text}`\n\n"
                f"{_PLACEHOLDER_HELP}"
            )
        else:
            if dkey is None:
                extra = ("Is channel ka link anime ke Link N se wapas lagana ho to `auto` bhejo."
                         if scope is not None else
                         "Anime ka apna invite link wapas lagana ho to `auto` bhejo.")
            else:
                extra = "Button hatana ho to `skip` bhejo."
            prompt = (
                f"**🔗 {label} — naya URL bhejo**{where}\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "Example: `https://t.me/+xxxxxxxx` ya `@channel`\n"
                f"{extra}"
            )
        back_cb = f"cmz:pb:open:{_tok(btn, scope)}"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=back_cb)]])
        await _edit(client, cb, prompt, kb)
        return

    if sub == "del":
        await _patch_cfg(btn, scope, enabled=False)
        await cb.answer("🗑 Button delete ho gaya")
        await _render_btn(client, cb, btn, scope,
                          "_Ab ye button post mein nahi aayega. Wapas lane ke liye Restore dabao._")
        return

    if sub == "restore":
        # scoped: override hata do (global ke hisaab se wapas); global: enabled hata do
        await _patch_cfg(btn, scope, enabled=None)
        await cb.answer("♻️ Restore ho gaya")
        await _render_btn(client, cb, btn, scope)
        return

    if sub == "reset":
        await _patch_cfg(btn, scope, text=None, enabled=None, style=None,
                         **({"url": None} if (dkey is None or scope is not None) else {}))
        await cb.answer("↩️ Default pe aa gaya")
        await _render_btn(client, cb, btn, scope)
        return

    await cb.answer()


# ───────────── text input ─────────────
# group=-3: update_channel.py ke purane button-session text handler se pehle
# chalta hai; sirf tab consume karta hai jab user is panel ke input mode mein ho.
@Client.on_message(filters.text & filters.private, group=-4)
async def pb_text_input(client: Client, message: Message):
    if not message.from_user:
        raise ContinuePropagation
    uid = message.from_user.id
    sess = _SESS.get(uid)
    if not sess or not _is_auth(uid):
        raise ContinuePropagation
    raw = (message.text or "").strip()
    if raw.startswith("/"):          # koi command aaya — input mode cancel, command normal chale
        _SESS.pop(uid, None)
        raise ContinuePropagation

    btn, field, scope = sess["btn"], sess["field"], sess.get("scope")
    emoji, label, default_text, dkey = _BTNS[btn]
    back_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("⬅️ Button", callback_data=f"cmz:pb:open:{_tok(btn, scope)}"),
        InlineKeyboardButton("🏠 Menu", callback_data="cmz:menu"),
    ]])
    try:
        if field == "text":
            if not raw or len(raw) > _MAX_TEXT:
                await message.reply(f"⚠️ Naam 1 se {_MAX_TEXT} akshar ke beech hona chahiye. Dobara bhejo.")
                raise StopPropagation
            await _patch_cfg(btn, scope, text=raw, enabled=None)
            _SESS.pop(uid, None)
            await message.reply(
                f"✅ **{label}** ka naam ab: `{raw}`"
                + (f"\nPost mein dikhega: `{_preview(raw)}`" if "[" in raw else ""),
                reply_markup=back_kb,
            )
        else:
            low = raw.lower()
            if dkey is None and low == "auto":
                await _patch_cfg(btn, scope, url=None)
                _SESS.pop(uid, None)
                await message.reply(f"✅ **{label}** ab anime ka apna link use karega.", reply_markup=back_kb)
            elif dkey is not None and low in ("skip", "none", "remove"):
                if scope is None:
                    await _set_default_url(dkey, None)
                else:
                    await _patch_cfg(btn, scope, enabled=False)      # channel-wise: button hide
                _SESS.pop(uid, None)
                await message.reply(f"🗑 **{label}** ka URL hata diya — button post mein nahi aayega.", reply_markup=back_kb)
            else:
                url = _norm_url(raw)
                if not url:
                    await message.reply("⚠️ Sahi URL bhejo (`https://...` ya `@channel`). Dobara bhejo.")
                    raise StopPropagation
                if dkey is None or scope is not None:
                    await _patch_cfg(btn, scope, url=url, enabled=None)
                else:
                    await _set_default_url(dkey, url)
                    await _patch_cfg(btn, scope, enabled=None)
                _SESS.pop(uid, None)
                await message.reply(f"✅ **{label}** ka URL set ho gaya:\n{url}", reply_markup=back_kb)
    except StopPropagation:
        raise
    except Exception as e:
        LOGGER.error(f"[PostButtons] save failed: {e}")
        await message.reply("❌ Save nahi ho paya, dobara try karo.")
    raise StopPropagation
