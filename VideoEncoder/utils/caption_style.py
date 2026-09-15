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

import logging

LOGGER = logging.getLogger(__name__)

DEFAULT_STYLE_ID = "default"


# ─────────────────────────────────────────────
#  Template renderers
#  data dict fields: anime_name, season (int), episode (int),
#                     quality (str, e.g. "360p, 720p"), audio (str),
#                     genres (str), main_channel (str, e.g. "@SBANIME")
# ─────────────────────────────────────────────
def _t_default(d: dict) -> str:
    return f"Season {d['season']:02d} Episode {d['episode']:02d} {d['audio']}"


def _t_style1(d: dict) -> str:
    return (
        f"‣ {d['anime_name']} (S - {d['season']:02d}) • ✅\n"
        f"╭━━━━━━━━ °°★°° ━━━━━━━━\n"
        f"├ Episode : {d['episode']:02d} (New)\n"
        f"├ Season : {d['season']:02d}\n"
        f"├ Quality : {d['quality']}\n"
        f"├ Audio : {d['audio']} | #Official\n"
        f"╰━━━━━━━━━━━━━━━━━━━━\n"
        f"➳ᴍᴀɪɴ ᴄʜᴀɴɴᴇʟ : {d['main_channel']}"
    )


def _t_style2(d: dict) -> str:
    return (
        f"➲ {d['anime_name']} (S - {d['season']:02d})\n"
        f"╭┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
        f"◈ Episode: {d['episode']:02d} (New)\n"
        f"◈ Audio: {d['audio']} #Official\n"
        f"◈ Quality: {d['quality']}\n"
        f"◈ Genres: {d['genres']}\n"
        f"╰┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
        f"• Main Channel : 『{d['main_channel']}』"
    )


def _t_style3(d: dict) -> str:
    return (
        f"❖ {d['anime_name']}\n"
        f"┏────────────────────⍟\n"
        f"│‣ Season - {d['season']:02d}\n"
        f"│‣ Episode - {d['episode']:02d} (New)\n"
        f"│‣ Audio - {d['audio']} #Official\n"
        f"│‣ Quality - {d['quality']}\n"
        f"│‣ Genres - {d['genres']}\n"
        f"┗────────────────────⍟\n"
        f"• Main Channel : 『{d['main_channel']}』"
    )


def _t_style4(d: dict) -> str:
    return (
        f"╭┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
        f"◈ (S - {d['season']:02d}) Episode: {d['episode']:02d} (New)\n"
        f"◈ Audio: {d['audio']} #Official\n"
        f"◈ Quality: {d['quality']}\n"
        f"◈ Genres: {d['genres']}\n"
        f"╰┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
        f"• Main Channel : 『{d['main_channel']}』"
    )


def _t_style5(d: dict) -> str:
    return (
        f"🎬 {d['anime_name']} 𝗦{d['season']:02d}𝗘{d['episode']:02d}\n"
        f"▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n"
        f"✦ Episode  : {d['episode']:02d} (New)\n"
        f"✦ Season   : {d['season']:02d}\n"
        f"✦ Quality  : {d['quality']}\n"
        f"✦ Audio    : {d['audio']} #Official\n"
        f"✦ Genres   : {d['genres']}\n"
        f"▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n"
        f"✈ ᴍᴀɪɴ ᴄʜᴀɴɴᴇʟ ➻ {d['main_channel']}"
    )


def _t_style6(d: dict) -> str:
    return (
        f"【 {d['anime_name']} 】\n"
        f"S{d['season']:02d} • E{d['episode']:02d} (New)\n"
        f"▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂\n"
        f"▸ Quality : {d['quality']}\n"
        f"▸ Audio   : {d['audio']} #Official\n"
        f"▸ Genres  : {d['genres']}\n"
        f"▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂\n"
        f"📡 Main Channel : {d['main_channel']}"
    )


def _t_style7(d: dict) -> str:
    return (
        f"▌ {d['anime_name']} ▐\n"
        f"「 Season {d['season']:02d} ⋄ Episode {d['episode']:02d} (New) 」\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"◇ Audio   : {d['audio']} #Official\n"
        f"◇ Quality : {d['quality']}\n"
        f"◇ Genres  : {d['genres']}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"↳ ᴍᴀɪɴ ᴄʜᴀɴɴᴇʟ : {d['main_channel']}"
    )


def _t_style8(d: dict) -> str:
    return (
        f"✧･ﾟ: {d['anime_name']} :･ﾟ✧\n"
        f"(Season {d['season']:02d} — Episode {d['episode']:02d}) 🆕\n"
        f"┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈\n"
        f"⌁ Quality : {d['quality']}\n"
        f"⌁ Audio   : {d['audio']} #Official\n"
        f"⌁ Genres  : {d['genres']}\n"
        f"┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈\n"
        f"🔔 Main Channel — {d['main_channel']}"
    )


def _t_style9(d: dict) -> str:
    return (
        f"🎞 {d['anime_name']}\n"
        f"Season {d['season']:02d} | Episode {d['episode']:02d} (New)\n"
        f"╔═══════════════════╗\n"
        f" Quality : {d['quality']}\n"
        f" Audio   : {d['audio']} #Official\n"
        f" Genres  : {d['genres']}\n"
        f"╚═══════════════════╝\n"
        f"➤ Main Channel : {d['main_channel']}"
    )


def _t_style10(d: dict) -> str:
    return (
        f"▶️ {d['anime_name']} [S{d['season']:02d}-E{d['episode']:02d}] (New)\n"
        f"────────────────────\n"
        f"🔹 Quality : {d['quality']}\n"
        f"🔹 Audio   : {d['audio']} #Official\n"
        f"🔹 Genres  : {d['genres']}\n"
        f"────────────────────\n"
        f"🏷 Main Channel : {d['main_channel']}"
    )


STYLES = {
    "default":  {"name": "⚪ Default (Current)", "render": _t_default},
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
    """style_id se render function nikal ke data dict ke saath call karo.
    Unknown style_id ya render error → default plain format pe fallback."""
    style = STYLES.get(style_id) or STYLES[DEFAULT_STYLE_ID]
    try:
        return style["render"](data)
    except Exception as e:
        LOGGER.warning(f"[CaptionStyle] Render failed for '{style_id}': {e}")
        return _t_default(data)


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
