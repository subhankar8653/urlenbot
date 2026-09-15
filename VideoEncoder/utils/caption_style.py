"""
caption_style.py
==================
/bot_upload se jab episode channel pe post hota hai, uska caption ab
tak bilkul plain tha ("Season 01 Episode 01 Hindi"). Yeh module 10
professional, box-drawing/stylized caption TEMPLATES + ek "Default"
(purana plain wala) — total 11 options deta hai.

/caption_style command (plugins/caption_style.py) se style select/
preview/apply hota hai — thumb_style.py / setpic_style.py wale exact
pattern pe (GLOBAL bot-wide setting, DB-backed in-memory cache, turant
effective, restart ki zaroorat nahi).

Yeh module sirf ENGINE hai:
  - STYLE_ORDER / STYLE_NAMES  -> style IDs + display names
  - render_caption()           -> kisi bhi style ID + data dict se
                                   final caption text banata hai
  - PREVIEW_DATA                -> /caption_style ke preview ke liye
                                   sample dummy data
  - load_caption_style_cache() / get_current_style_id() / set_style()
    -> DB-backed, in-memory cache (bot start pe ek baar load hota hai,
       /caption_style se change hone par turant refresh)
"""

import html as _html
import logging

LOGGER = logging.getLogger(__name__)

DEFAULT_STYLE_ID = "default"


def _utf16_len(s: str) -> int:
    """Telegram entity offsets/lengths UTF-16 code units mein hote hain
    (emoji jaise 🎬 do units lete hain) — plain len() galat hoga."""
    return len(s.encode("utf-16-le")) // 2


# ─────────────────────────────────────────────
#  Template renderers
#  data dict fields: anime_name, season (int), episode (int),
#                     quality (str, e.g. "360p, 720p"), audio (str),
#                     genres (str), main_channel (str, e.g. "@SBANIME")
# ─────────────────────────────────────────────
def _t_default(d: dict) -> str:
    return f"Season {d['season']:02d} Episode {d['episode']:02d} {d['audio']}"


def _t_style1(d: dict) -> dict:
    lines = [
        f"‣ {d['anime_name']} (S - {d['season']:02d}) • ✅",
        f"╭━━━━━━━━ °°★°° ━━━━━━━━",
        f"├ Episode : {d['episode']:02d} (New)",
        f"├ Season : {d['season']:02d}",
        f"├ Quality : {d['quality']}",
        f"├ Audio : {d['audio']} | #Official",
        f"╰━━━━━━━━━━━━━━━━━━━━",
        f"➳ᴍᴀɪɴ ᴄʜᴀɴɴᴇʟ : {d['main_channel']}",
    ]
    return {"lines": lines, "quote": {7}, "italic": {7}}


def _t_style2(d: dict) -> dict:
    lines = [
        f"➲ {d['anime_name']} (S - {d['season']:02d})",
        f"╭┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄",
        f"◈ Episode: {d['episode']:02d} (New)",
        f"◈ Audio: {d['audio']} #Official",
        f"◈ Quality: {d['quality']}",
        f"◈ Genres: {d['genres']}",
        f"╰┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄",
        f"• Main Channel : 『{d['main_channel']}』",
    ]
    return {"lines": lines, "quote": {0, 7}, "italic": set()}


def _t_style3(d: dict) -> dict:
    lines = [
        f"❖ {d['anime_name']}",
        f"┏────────────────────⍟",
        f"│‣ Season - {d['season']:02d}",
        f"│‣ Episode - {d['episode']:02d} (New)",
        f"│‣ Audio - {d['audio']} #Official",
        f"│‣ Quality - {d['quality']}",
        f"│‣ Genres - {d['genres']}",
        f"┗────────────────────⍟",
        f"• Main Channel : 『{d['main_channel']}』",
    ]
    return {"lines": lines, "quote": {8}, "italic": set()}


def _t_style4(d: dict) -> dict:
    lines = [
        f"╭┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄",
        f"◈ (S - {d['season']:02d}) Episode: {d['episode']:02d} (New)",
        f"◈ Audio: {d['audio']} #Official",
        f"◈ Quality: {d['quality']}",
        f"◈ Genres: {d['genres']}",
        f"╰┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄",
        f"• Main Channel : 『{d['main_channel']}』",
    ]
    return {"lines": lines, "quote": {6}, "italic": set()}


def _t_style5(d: dict) -> dict:
    lines = [
        f"🎬 {d['anime_name']} 𝗦{d['season']:02d}𝗘{d['episode']:02d}",
        f"▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰",
        f"✦ Episode  : {d['episode']:02d} (New)",
        f"✦ Season   : {d['season']:02d}",
        f"✦ Quality  : {d['quality']}",
        f"✦ Audio    : {d['audio']} #Official",
        f"✦ Genres   : {d['genres']}",
        f"▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰",
        f"✈ ᴍᴀɪɴ ᴄʜᴀɴɴᴇʟ ➻ {d['main_channel']}",
    ]
    return {"lines": lines, "quote": {8}, "italic": {8}}


def _t_style6(d: dict) -> dict:
    lines = [
        f"【 {d['anime_name']} 】",
        f"S{d['season']:02d} • E{d['episode']:02d} (New)",
        f"▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂",
        f"▸ Quality : {d['quality']}",
        f"▸ Audio   : {d['audio']} #Official",
        f"▸ Genres  : {d['genres']}",
        f"▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂",
        f"📡 Main Channel : {d['main_channel']}",
    ]
    return {"lines": lines, "quote": {7}, "italic": set()}


def _t_style7(d: dict) -> dict:
    lines = [
        f"▌ {d['anime_name']} ▐",
        f"「 Season {d['season']:02d} ⋄ Episode {d['episode']:02d} (New) 」",
        f"━━━━━━━━━━━━━━━━━━━",
        f"◇ Audio   : {d['audio']} #Official",
        f"◇ Quality : {d['quality']}",
        f"◇ Genres  : {d['genres']}",
        f"━━━━━━━━━━━━━━━━━━━",
        f"↳ ᴍᴀɪɴ ᴄʜᴀɴɴᴇʟ : {d['main_channel']}",
    ]
    return {"lines": lines, "quote": {7}, "italic": {7}}


def _t_style8(d: dict) -> dict:
    lines = [
        f"✧･ﾟ: {d['anime_name']} :･ﾟ✧",
        f"(Season {d['season']:02d} — Episode {d['episode']:02d}) 🆕",
        f"┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈",
        f"⌁ Quality : {d['quality']}",
        f"⌁ Audio   : {d['audio']} #Official",
        f"⌁ Genres  : {d['genres']}",
        f"┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈",
        f"🔔 Main Channel — {d['main_channel']}",
    ]
    return {"lines": lines, "quote": {7}, "italic": set()}


def _t_style9(d: dict) -> dict:
    lines = [
        f"🎞 {d['anime_name']}",
        f"Season {d['season']:02d} | Episode {d['episode']:02d} (New)",
        f"╔═══════════════════╗",
        f" Quality : {d['quality']}",
        f" Audio   : {d['audio']} #Official",
        f" Genres  : {d['genres']}",
        f"╚═══════════════════╝",
        f"➤ Main Channel : {d['main_channel']}",
    ]
    return {"lines": lines, "quote": {7}, "italic": set()}


def _t_style10(d: dict) -> dict:
    lines = [
        f"▶️ {d['anime_name']} [S{d['season']:02d}-E{d['episode']:02d}] (New)",
        f"────────────────────",
        f"🔹 Quality : {d['quality']}",
        f"🔹 Audio   : {d['audio']} #Official",
        f"🔹 Genres  : {d['genres']}",
        f"────────────────────",
        f"🏷 Main Channel : {d['main_channel']}",
    ]
    return {"lines": lines, "quote": {6}, "italic": set()}


STYLES = {
    "default":  {"name": "⚪ Default", "render": _t_default},
    "style1":   {"name": "1️⃣ Star Box",          "render": _t_style1},
    "style2":   {"name": "2️⃣ Dotted Frame",      "render": _t_style2},
    "style3":   {"name": "3️⃣ Bracket Card",      "render": _t_style3},
    "style4":   {"name": "4️⃣ Compact Dotted",    "render": _t_style4},
    "style5":   {"name": "5️⃣ Bold Block",        "render": _t_style5},
    "style6":   {"name": "6️⃣ Bracket Title",     "render": _t_style6},
    "style7":   {"name": "7️⃣ Line Frame",        "render": _t_style7},
    "style8":   {"name": "8️⃣ Sparkle Dotted",    "render": _t_style8},
    "style9":   {"name": "9️⃣ Double Border",     "render": _t_style9},
    "style10":  {"name": "🔟 Minimal Rule",       "render": _t_style10},
}

STYLE_ORDER = ["style1", "style2", "style3", "style4", "style5",
               "style6", "style7", "style8", "style9", "style10"]

PREVIEW_DATA = {
    "anime_name": "I Became a Legend after My 10 Year-Long Last Stand",
    "season": 1,
    "episode": 1,
    "quality": "720p",
    "audio": "Hindi",
    "genres": "Action, Fantasy, Adventure",
    "main_channel": "@SBANIME",
}


def render_caption(style_id: str, data: dict) -> str:
    """Plain joined text (koi Telegram entity nahi) — sirf /caption_style
    ke preview screen ke liye. Asli posting ke liye render_caption_entities()
    use karo (woh bold/blockquote/italic Telegram entities bhi deta hai)."""
    style = STYLES.get(style_id) or STYLES[DEFAULT_STYLE_ID]
    try:
        if style_id == DEFAULT_STYLE_ID:
            return _t_default(data)
        result = style["render"](data)
        return "\n".join(result["lines"])
    except Exception as e:
        LOGGER.warning(f"[CaptionStyle] Render failed for '{style_id}': {e}")
        return _t_default(data)


def _build_entities(lines: list, quote_idxs: set, italic_idxs: set) -> tuple:
    """lines ko '\\n' se jod ke text banao, aur bold (poora text) +
    blockquote/italic (jo line-indices di gayi hain unpe) entities compute
    karo. Offsets UTF-16 code units mein hote hain (Bot API requirement)."""
    text = "\n".join(lines)
    entities = [{"type": "bold", "offset": 0, "length": _utf16_len(text)}]
    offset = 0
    for i, line in enumerate(lines):
        line_len = _utf16_len(line)
        if line_len:
            if i in quote_idxs:
                entities.append({"type": "blockquote", "offset": offset, "length": line_len})
            if i in italic_idxs:
                entities.append({"type": "italic", "offset": offset, "length": line_len})
        offset += line_len + 1  # +1 for the '\n' separator
    return text, entities


def render_caption_entities(style_id: str, data: dict) -> tuple:
    """Asli posting ke liye — (text, entities) deta hai jisme poora caption
    bold hota hai aur "Main Channel" (kabhi-kabhi title bhi) line native
    Telegram blockquote/italic ke saath highlight hoti hai — jaisa reference
    screenshots mein hai. 'default' style ke liye yahan call mat karo (uska
    entity-building bot_upload_engine.py mein alag se, custom-emoji ➲ ke
    saath, hoti hai)."""
    style = STYLES.get(style_id)
    if not style or style_id == DEFAULT_STYLE_ID:
        text = _t_default(data)
        return text, [{"type": "bold", "offset": 0, "length": _utf16_len(text)}]
    try:
        result = style["render"](data)
        return _build_entities(result["lines"], result["quote"], result["italic"])
    except Exception as e:
        LOGGER.warning(f"[CaptionStyle] Entity render failed for '{style_id}': {e}")
        text = _t_default(data)
        return text, [{"type": "bold", "offset": 0, "length": _utf16_len(text)}]


def render_caption_html(style_id: str, data: dict) -> str:
    """render_caption_entities() jaisa hi output, lekin raw Bot-API entities
    ki jagah HTML markup (<b>/<blockquote>/<i>) deta hai — un jagahon ke liye
    jo pyrogram ke parse_mode=ParseMode.HTML se caption bhejte hain
    (e.g. utils/uploads/telegram.py -> upload_to_tg, jo /upload, /url,
    /bot_upload sabhi manual+auto video uploads ke peeche common hai).
    Isse GLOBAL caption style /bot_upload ke alawa har jagah lagta hai."""
    style = STYLES.get(style_id)
    if not style or style_id == DEFAULT_STYLE_ID:
        return f"<b>{_html.escape(_t_default(data))}</b>"
    try:
        result = style["render"](data)
        lines = result["lines"]
        quote_idxs = result["quote"]
        italic_idxs = result["italic"]
        html_lines = []
        for i, line in enumerate(lines):
            esc = _html.escape(line)
            if i in italic_idxs:
                esc = f"<i>{esc}</i>"
            if i in quote_idxs:
                esc = f"<blockquote>{esc}</blockquote>"
            html_lines.append(esc)
        body = "\n".join(html_lines)
        return f"<b>{body}</b>"
    except Exception as e:
        LOGGER.warning(f"[CaptionStyle] HTML render failed for '{style_id}': {e}")
        return f"<b>{_html.escape(_t_default(data))}</b>"


async def lookup_genres(anime_name: str) -> str:
    """Genres nikalne ke 2 tareeke, order mein try karte hain:
      1. anime_monitor_list mein saved genres (/add_anime ke time TMDB se
         already fetch ho chuke hote hain — fastest, DB read only).
      2. Agar wahan nahi mile (anime monitor list mein add hi nahi hai,
         ya genres field khali hai) — TMDB API se LIVE fetch karo
         (utils/anime_api.py, wahi jo /add_anime use karta hai).
    Dono fail ho jaayein (TMDB_API_KEY set nahi hai, ya match nahi mila)
    toh "—" fallback.
    """
    if not anime_name:
        return "—"

    # 1) Saved monitor-list entry
    try:
        from .database.access_db import db
        from .. import owner
        if owner:
            user = await db._get_user(owner[0])
            for entry in (user.get('anime_monitor_list') or []):
                if (entry.get('anime_name') or '').strip().lower() == anime_name.strip().lower():
                    saved = (entry.get('genres') or '').strip()
                    if saved and saved != "—":
                        return saved
                    break
    except Exception as e:
        LOGGER.warning(f"[CaptionStyle] Genres lookup (DB) failed: {e}")

    # 2) Live TMDB fetch (fallback — anime monitor list mein nahi hai ya
    #    genres khali the)
    try:
        from .anime_api import fetch_anime_details
        details = await fetch_anime_details(anime_name)
        if details:
            live_genres = (details.get('genres') or '').strip()
            if live_genres:
                return live_genres
    except Exception as e:
        LOGGER.warning(f"[CaptionStyle] Genres lookup (TMDB) failed: {e}")

    return "—"


# ─────────────────────────────────────────────
#  In-memory cache (DB-backed) — episode post banate waqt hot-path,
#  isliye har caption pe DB call nahi karte.
# ─────────────────────────────────────────────
_cache_style_id = DEFAULT_STYLE_ID


async def load_caption_style_cache():
    """Bot startup pe ek baar call karo — DB se current style load karta hai."""
    global _cache_style_id
    try:
        from .database.access_db import db
        style_id = await db.get_caption_style()
        _cache_style_id = style_id or DEFAULT_STYLE_ID
        LOGGER.info(f"[CaptionStyle] Loaded: style={_cache_style_id}")
    except Exception as e:
        LOGGER.warning(f"[CaptionStyle] Cache load failed, using default: {e}")


async def set_style(style_id: str):
    global _cache_style_id
    from .database.access_db import db
    await db.set_caption_style(style_id)
    _cache_style_id = style_id


def get_current_style_id() -> str:
    return _cache_style_id
