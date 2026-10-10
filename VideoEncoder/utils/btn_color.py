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
            await asyncio.sleep(0.35)           # tez-tez edits ko collapse karo (latest hi lagega)
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
