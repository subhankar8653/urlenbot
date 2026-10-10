"""
btn_color.py
============
Bot ke SAARE inline buttons ko unke kaam ke hisab se Telegram ka asli colour deta hai
(Bot API 9.4+ "style": primary=blue, success=green, danger=red) — bilkul update-channel
post ke buttons jaisa.

Kaise:
  Pyrogram (pyrofork) khud "style" nahi bhej sakta. Isliye yahan Client.invoke par ek
  chhota hook hai: jab bhi bot koi message (send/edit) inline keyboard ke saath bhejta
  hai, uske turant baad background mein Bot API `editMessageReplyMarkup` se wahi
  keyboard colour ke saath lagaya jata hai. Baaki plugins mein koi change nahi chahiye —
  naya button jo bhi banao, apne aap colour ho jayega.

Colour rules (button ke text / callback_data se):
  🔴 danger  : Close, Cancel, Delete, Remove, Clear, Stop, Reset, Redeploy, toggle OFF / ❌
  🟢 success : Download, Start, Apply, Save, Confirm, Add, Set, Upload, Update, toggle ON / ✅
  🔵 primary : Back, Next, Prev, Menu, List, Stats, Settings, EP01.., baaki sab
  ⚪ no-colour: page indicator (📄 1/3), section header (── ... ──), noop buttons

Control:
  env  BUTTON_COLORS=0   -> band      (default: on)
  /btncolors on|off      -> runtime toggle (Sudo)
"""

import asyncio
import os
import re

from .. import LOGGER

ENABLED = os.getenv("BUTTON_COLORS", "1").strip().lower() not in ("0", "false", "off", "no")

PRIMARY, SUCCESS, DANGER = "primary", "success", "danger"

# ───────────────────────── colour rules ─────────────────────────
_DANGER_KW = (
    "close", "cancel", "delete", "remove", "clear", "stop", "reset", "redeploy", "restart",
    "blacklist", "disable", "kill", "wipe", "purge", "🗑", "🚫", "⛔", "🛑",
)
_SUCCESS_KW = (
    "download", "start", "apply", "save", "confirm", "continue", "proceed", "accept", "yes",
    "done", "upload", "update", "enable", "run", "add", "set ", "set/", "select all",
    "auto add", "encode", "✅", "⬇", "📥", "📤", "➕",
)
_NOOP_DATA = ("noop",)


def classify(text: str, data: str = "", url: str = "") -> str | None:
    """Button ka colour style return karta hai (ya None = default grey)."""
    t = (text or "").strip()
    low = t.lower()
    d = (data or "").lower()

    if not t:
        return None
    # Anime/episode ke naam wale buttons — naam ke andar "Start/Close/Add" jaisa word ho to
    # colour galat na lage; inhe hamesha blue rakho.
    if d.startswith(("anime_r_", "rti_ep", "toono_ep")) or re.match(r"^\[(s\d+|movie)\]", low):
        return PRIMARY
    if any(n in d for n in _NOOP_DATA):
        return None
    if t.startswith("──") or re.match(r"^📄?\s*\d+\s*/\s*\d+$", t):
        return None

    # toggle state — text ke end mein ON/OFF ya ✅/❌
    if re.search(r"(✅|☑️|🟢|\bon)\s*$", low):
        return SUCCESS
    if re.search(r"(❌|🔴|\boff)\s*$", low):
        return DANGER

    # destructive (red) — "Close", "❌ Cancel", "🗑️ Clear All" ...
    if any(k in low for k in _DANGER_KW) or any(k in d for k in ("close", "cancel", "delete", "clear")):
        return DANGER

    # "Quality: 720p", "CRF: 23" — settings selector (value dikhane wala) -> blue
    if re.match(r"^[^:]{1,28}:\s*\S", t):
        return PRIMARY

    # navigation / info (blue) — success words se pehle check taaki "Back to list" blue rahe
    if any(k in low for k in ("back", "prev", "next", "menu", "list", "stats", "status", "refresh",
                              "help", "info", "view", "open", "◀", "⬅", "➡", "▶")):
        return PRIMARY

    # confirm / action (green)
    if any(k in low for k in _SUCCESS_KW) or any(k in d for k in ("season", "download", "start", "apply", "confirm")):
        return SUCCESS

    return PRIMARY


# ───────────────────────── raw -> Bot API markup ─────────────────────────
class _Unsupported(Exception):
    pass


def _button_to_dict(b):
    n = type(b).__name__
    text = getattr(b, "text", "") or ""
    if n == "KeyboardButtonCallback":
        try:
            data = b.data.decode("utf-8")
        except Exception:
            raise _Unsupported("non-utf8 callback")
        return {"text": text, "callback_data": data}, data, ""
    if n == "KeyboardButtonUrl":
        return {"text": text, "url": b.url}, "", b.url
    if n == "KeyboardButtonSwitchInline":
        key = "switch_inline_query_current_chat" if getattr(b, "same_peer", False) else "switch_inline_query"
        return {"text": text, key: b.query}, "", ""
    if n in ("KeyboardButtonWebView", "KeyboardButtonSimpleWebView"):
        return {"text": text, "web_app": {"url": b.url}}, "", b.url
    if n == "KeyboardButtonCopy":
        return {"text": text, "copy_text": {"text": b.copy_text}}, "", ""
    raise _Unsupported(n)


def markup_to_botapi(raw_markup):
    """raw ReplyInlineMarkup -> (Bot API reply_markup dict | None). None = kuch colour karne layak nahi."""
    rows, any_style = [], False
    for row in raw_markup.rows:
        out_row = []
        for b in row.buttons:
            d, data, url = _button_to_dict(b)
            st = classify(d["text"], data, url)
            if st:
                d["style"] = st
                any_style = True
            out_row.append(d)
        rows.append(out_row)
    if not any_style:
        return None
    return {"inline_keyboard": rows}


# ───────────────────────── chat id helpers ─────────────────────────
def _input_peer_chat_id(p):
    n = type(p).__name__
    if n == "InputPeerUser":
        return p.user_id
    if n == "InputPeerChat":
        return -p.chat_id
    if n == "InputPeerChannel":
        return -(1000000000000 + p.channel_id)
    return None


def _peer_chat_id(p):
    n = type(p).__name__
    if n == "PeerUser":
        return p.user_id
    if n == "PeerChat":
        return -p.chat_id
    if n == "PeerChannel":
        return -(1000000000000 + p.channel_id)
    return None


def _targets(query, result):
    """Jis message(s) ko abhi bheja/edit kiya gaya unke [(chat_id, message_id)]."""
    qn = type(query).__name__
    out = []
    if qn == "EditMessage":
        cid = _input_peer_chat_id(query.peer)
        if cid is not None and getattr(query, "id", None):
            out.append((cid, query.id))
        return out
    ups = getattr(result, "updates", None)
    if ups is not None:
        for u in ups:
            m = getattr(u, "message", None)
            if m is not None and type(u).__name__ in ("UpdateNewMessage", "UpdateNewChannelMessage") \
                    and type(m).__name__ == "Message":
                cid = _peer_chat_id(m.peer_id)
                if cid is not None:
                    out.append((cid, m.id))
    elif type(result).__name__ == "UpdateShortSentMessage":
        cid = _input_peer_chat_id(query.peer)
        if cid is not None:
            out.append((cid, result.id))
    return out


# ───────────────────────── Bot API worker ─────────────────────────
_pending: dict = {}
_running: set = set()
_http = None
_warned = set()


async def _post(token: str, method: str, payload: dict):
    global _http
    import httpx
    if _http is None:
        _http = httpx.AsyncClient(timeout=15)
    r = await _http.post(f"https://api.telegram.org/bot{token}/{method}", json=payload)
    return r.status_code, (r.json() if r.content else {})


async def _apply(token, chat_id, msg_id, markup):
    payload = {"chat_id": chat_id, "message_id": msg_id, "reply_markup": markup}
    for attempt in (1, 2):
        try:
            code, js = await _post(token, "editMessageReplyMarkup", payload)
        except Exception as e:
            if "net" not in _warned:
                _warned.add("net")
                LOGGER.warning(f"[BtnColor] Bot API call fail: {e!r}")
            return
        if code == 200:
            return
        desc = str(js.get("description", ""))
        if code == 429 and attempt == 1:
            await asyncio.sleep(min(int(js.get("parameters", {}).get("retry_after", 2)), 10))
            continue
        if "not modified" in desc or "message to edit not found" in desc or "can't be edited" in desc:
            return
        key = desc[:40]
        if key not in _warned:                  # har naye error type ko sirf ek baar log karo
            _warned.add(key)
            LOGGER.warning(f"[BtnColor] editMessageReplyMarkup {code}: {desc}")
        return


async def _worker(key):
    try:
        while key in _pending:
            await asyncio.sleep(0.08)           # tez-tez sends ko collapse karo (latest hi lagega)
            token, markup = _pending.pop(key)
            await _apply(token, key[0], key[1], markup)
    finally:
        _running.discard(key)
        if key in _pending:                     # worker ke jaate-jaate naya aa gaya
            _running.add(key)
            asyncio.ensure_future(_worker(key))


def _enqueue(token, chat_id, msg_id, markup):
    key = (chat_id, msg_id)
    _pending[key] = (token, markup)
    if key not in _running:
        _running.add(key)
        asyncio.ensure_future(_worker(key))


# ───────────────────────── FLICKER-FREE EDIT (single Bot API call) ─────────────────────────
# Do-step tareeka (pehle Pyrogram edit -> colour hat jaata hai -> phir colour wapas) blink karta
# tha. Isliye EDIT ko hi seedha Bot API se, colour ke saath, ek hi call mein bhejte hain; phir
# Pyrogram ko chahiye hone wala result (edited Message) MTProto GetMessages se bana dete hain.
# Kuch bhi gadbad ho to purane do-step tareeke par fallback (kabhi edit fail nahi hota).
_ENT_SIMPLE = {
    "MessageEntityBold": "bold", "MessageEntityItalic": "italic", "MessageEntityUnderline": "underline",
    "MessageEntityStrike": "strikethrough", "MessageEntitySpoiler": "spoiler", "MessageEntityCode": "code",
    "MessageEntityMention": "mention", "MessageEntityHashtag": "hashtag", "MessageEntityCashtag": "cashtag",
    "MessageEntityBotCommand": "bot_command", "MessageEntityUrl": "url", "MessageEntityEmail": "email",
    "MessageEntityPhone": "phone_number", "MessageEntityBankCard": "bank_card_number",
}


def entities_to_botapi(ents):
    out = []
    for e in ents or []:
        n = type(e).__name__
        d = {"offset": e.offset, "length": e.length}
        if n in _ENT_SIMPLE:
            d["type"] = _ENT_SIMPLE[n]
        elif n == "MessageEntityPre":
            d["type"] = "pre"
            if getattr(e, "language", None):
                d["language"] = e.language
        elif n == "MessageEntityTextUrl":
            d["type"] = "text_link"
            d["url"] = e.url
        elif n == "MessageEntityMentionName":
            d["type"] = "text_mention"
            d["user"] = {"id": e.user_id}
        elif n == "MessageEntityBlockquote":
            d["type"] = "expandable_blockquote" if getattr(e, "collapsed", False) else "blockquote"
        elif n == "MessageEntityCustomEmoji":
            d["type"] = "custom_emoji"
            d["custom_emoji_id"] = str(e.document_id)
        elif n == "MessageEntityUnknown":
            continue
        else:
            raise _Unsupported(n)
        out.append(d)
    return out


async def _fetch_edited_updates(client, query, orig_invoke):
    """Bot API se edit ho chuke message ko MTProto se uthakar Pyrogram ke expected 'Updates' mein lapeto."""
    from pyrogram import raw
    peer = query.peer
    is_channel = type(peer).__name__ == "InputPeerChannel"
    ids = [raw.types.InputMessageID(id=query.id)]
    if is_channel:
        r = await orig_invoke(client, raw.functions.channels.GetMessages(
            channel=raw.types.InputChannel(channel_id=peer.channel_id, access_hash=peer.access_hash), id=ids))
    else:
        r = await orig_invoke(client, raw.functions.messages.GetMessages(id=ids))
    msgs = [m for m in getattr(r, "messages", []) if type(m).__name__ == "Message" and m.id == query.id]
    if not msgs:
        return None
    upd_cls = raw.types.UpdateEditChannelMessage if is_channel else raw.types.UpdateEditMessage
    upd = upd_cls(message=msgs[0], pts=0, pts_count=0)
    return raw.types.Updates(updates=[upd], users=r.users, chats=r.chats, date=0, seq=0)


async def _try_botapi_edit(client, query, token, orig_invoke):
    """
    Returns raw Updates (Pyrogram ke liye) agar Bot API edit success, warna None (-> purana tareeka).
    """
    rm = getattr(query, "reply_markup", None)
    if rm is None or type(rm).__name__ != "ReplyInlineMarkup":
        return None
    if getattr(query, "media", None) is not None or getattr(query, "schedule_date", None):
        return None
    chat_id = _input_peer_chat_id(query.peer)
    if chat_id is None or not getattr(query, "id", None):
        return None
    try:
        markup = markup_to_botapi(rm)
        ents = entities_to_botapi(getattr(query, "entities", None))
    except _Unsupported:
        return None
    if markup is None:
        return None

    text = getattr(query, "message", None)
    base = {"chat_id": chat_id, "message_id": query.id, "reply_markup": markup}
    if text is None:
        method, payload = "editMessageReplyMarkup", base
    else:
        method = "editMessageText"
        payload = dict(base, text=text)
        if ents:
            payload["entities"] = ents
        if getattr(query, "no_webpage", False):
            payload["link_preview_options"] = {"is_disabled": True}

    try:
        code, js = await _post(token, method, payload)
        if code != 200 and "no text in the message" in str(js.get("description", "")) and text is not None:
            payload = dict(base, caption=text)
            if ents:
                payload["caption_entities"] = ents
            code, js = await _post(token, "editMessageCaption", payload)
    except Exception as e:
        LOGGER.debug(f"[BtnColor] botapi edit net error: {e!r}")
        return None
    if code != 200:
        return None                             # not modified / koi bhi error -> purana raasta (sahi exception wahi dega)

    try:
        return await _fetch_edited_updates(client, query, orig_invoke)
    except Exception as e:
        LOGGER.debug(f"[BtnColor] fetch edited msg fail: {e!r}")
        return None


# ───────────────────────── install hook ─────────────────────────
_installed = False


def _after_invoke(client, query, result):
    if not ENABLED:
        return
    rm = getattr(query, "reply_markup", None)
    if rm is None or type(rm).__name__ != "ReplyInlineMarkup":
        return
    if type(query).__name__ not in ("SendMessage", "SendMedia", "EditMessage"):
        return
    token = getattr(client, "bot_token", None) or os.getenv("BOT_TOKEN", "")
    if not token:
        return
    try:
        markup = markup_to_botapi(rm)
    except _Unsupported:
        return
    if markup is None:
        return
    for chat_id, msg_id in _targets(query, result):
        _enqueue(token, chat_id, msg_id, markup)


def install():
    global _installed
    if _installed:
        return
    try:
        from pyrogram import Client
        _orig = Client.invoke

        async def invoke(self, query, *args, **kwargs):
            if ENABLED and type(query).__name__ == "EditMessage":
                token = getattr(self, "bot_token", None) or os.getenv("BOT_TOKEN", "")
                if token:
                    try:
                        res = await _try_botapi_edit(self, query, token, _orig)
                    except Exception as e:
                        LOGGER.debug(f"[BtnColor] flicker-free edit error: {e!r}")
                        res = None
                    if res is not None:
                        return res              # ek hi call, colour kabhi gaya hi nahi
            result = await _orig(self, query, *args, **kwargs)
            try:
                _after_invoke(self, query, result)
            except Exception as e:
                LOGGER.debug(f"[BtnColor] hook error: {e!r}")
            return result

        Client.invoke = invoke
        _installed = True
        LOGGER.info(f"[BtnColor] Coloured buttons hook installed (enabled={ENABLED})")
    except Exception as e:
        LOGGER.warning(f"[BtnColor] install fail: {e!r}")


install()
