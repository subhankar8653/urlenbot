"""
update_post_style.py
=====================
/update_post_style se jab episode UPDATE CHANNEL pe post hota hai
(update_channel.py -> send_update_post), uska caption box-layout isse
control hota hai. `/caption_style` jaisa hi 10 professional templates +
"Default" (purana wala, jaisa abhi hai) — GLOBAL (bot-wide), DB-backed,
turant effective.

caption_style.py ke 10 designs hi reuse kiye hain (visual look same),
lekin field-set alag hai (main_channel line nahi hoti update posts mein,
aur 'episode' pehle se hi ek FORMATTED STRING hoti hai — jaise
"01 To 12" ya "05 (New)" ya "12 (Complete)" — season ki tarah plain int
nahi).

  data dict fields: anime_name, season (int), episode (str, already
                     formatted incl. New/Complete tags), audio (str),
                     quality (str, e.g. "360p, 720p, 1080p"),
                     genres (str)

  DEFAULT_STYLE_ID   -> "default" (purana hardcoded layout, jo
                         update_channel.py mein already implement hai —
                         yeh module isko render NAHI karta, send_update_post
                         khud apna purana code chalata hai jab style
                         "default" ho)
  STYLE_ORDER/STYLES -> baaki 10 styles, is module ke render functions
  render_update_caption_entities() -> (text, entities) Bot-API raw
                         entities format mein (send_update_post
                         _bot_api_send_photo() ke saath use hota hai)
"""

import logging

from .caption_style import _utf16_len, _build_entities

LOGGER = logging.getLogger(__name__)

DEFAULT_STYLE_ID = "default"


# ─────────────────────────────────────────────
#  Template renderers — caption_style.py ke 10 designs jaisa hi, bas
#  main_channel line nahi, aur 'episode' already-formatted STRING hai.
# ─────────────────────────────────────────────
def _u_style1(d: dict) -> dict:
    lines = [
        f"‣ {d['anime_name']} (S - {d['season']:02d}) • ✅",
        f"╭━━━━━━━━ °°★°° ━━━━━━━━",
        f"├ Episode : {d['episode']}",
        f"├ Season : {d['season']:02d}",
        f"├ Quality : {d['quality']}",
        f"├ Audio : {d['audio']} | #Official",
        f"╰━━━━━━━━━━━━━━━━━━━━",
        f"➳ ɢᴇɴʀᴇꜱ : {d['genres']}",
    ]
    return {"lines": lines, "quote": {7}, "italic": {7}}


def _u_style2(d: dict) -> dict:
    lines = [
        f"➲ {d['anime_name']} (S - {d['season']:02d})",
        f"╭┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄",
        f"◈ Episode: {d['episode']}",
        f"◈ Audio: {d['audio']} #Official",
        f"◈ Quality: {d['quality']}",
        f"◈ Genres: {d['genres']}",
        f"╰┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄",
    ]
    return {"lines": lines, "quote": {0}, "italic": set()}


def _u_style3(d: dict) -> dict:
    lines = [
        f"❖ {d['anime_name']}",
        f"┏────────────────────⍟",
        f"│‣ Season - {d['season']:02d}",
        f"│‣ Episode - {d['episode']}",
        f"│‣ Audio - {d['audio']} #Official",
        f"│‣ Quality - {d['quality']}",
        f"│‣ Genres - {d['genres']}",
        f"┗────────────────────⍟",
    ]
    return {"lines": lines, "quote": set(), "italic": set()}


def _u_style4(d: dict) -> dict:
    lines = [
        f"╭┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄",
        f"◈ (S - {d['season']:02d}) Episode: {d['episode']}",
        f"◈ Audio: {d['audio']} #Official",
        f"◈ Quality: {d['quality']}",
        f"◈ Genres: {d['genres']}",
        f"╰┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄",
    ]
    return {"lines": lines, "quote": set(), "italic": set()}


def _u_style5(d: dict) -> dict:
    lines = [
        f"🎬 {d['anime_name']} 𝗦{d['season']:02d}",
        f"▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰",
        f"✦ Episode  : {d['episode']}",
        f"✦ Season   : {d['season']:02d}",
        f"✦ Quality  : {d['quality']}",
        f"✦ Audio    : {d['audio']} #Official",
        f"✦ Genres   : {d['genres']}",
        f"▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰",
    ]
    return {"lines": lines, "quote": set(), "italic": set()}


def _u_style6(d: dict) -> dict:
    lines = [
        f"【 {d['anime_name']} 】",
        f"S{d['season']:02d} • E{d['episode']}",
        f"▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂",
        f"▸ Quality : {d['quality']}",
        f"▸ Audio   : {d['audio']} #Official",
        f"▸ Genres  : {d['genres']}",
        f"▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂",
    ]
    return {"lines": lines, "quote": set(), "italic": set()}


def _u_style7(d: dict) -> dict:
    lines = [
        f"▌ {d['anime_name']} ▐",
        f"「 Season {d['season']:02d} ⋄ Episode {d['episode']} 」",
        f"━━━━━━━━━━━━━━━━━━━",
        f"◇ Audio   : {d['audio']} #Official",
        f"◇ Quality : {d['quality']}",
        f"◇ Genres  : {d['genres']}",
        f"━━━━━━━━━━━━━━━━━━━",
    ]
    return {"lines": lines, "quote": set(), "italic": set()}


def _u_style8(d: dict) -> dict:
    lines = [
        f"✧･ﾟ: {d['anime_name']} :･ﾟ✧",
        f"(Season {d['season']:02d} — Episode {d['episode']}) 🆕",
        f"┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈",
        f"⌁ Quality : {d['quality']}",
        f"⌁ Audio   : {d['audio']} #Official",
        f"⌁ Genres  : {d['genres']}",
        f"┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈",
    ]
    return {"lines": lines, "quote": set(), "italic": set()}


def _u_style9(d: dict) -> dict:
    lines = [
        f"🎞 {d['anime_name']}",
        f"Season {d['season']:02d} | Episode {d['episode']}",
        f"╔═══════════════════╗",
        f" Quality : {d['quality']}",
        f" Audio   : {d['audio']} #Official",
        f" Genres  : {d['genres']}",
        f"╚═══════════════════╝",
    ]
    return {"lines": lines, "quote": set(), "italic": set()}


def _u_style10(d: dict) -> dict:
    lines = [
        f"▶️ {d['anime_name']} [S{d['season']:02d}-E{d['episode']}]",
        f"────────────────────",
        f"🔹 Quality : {d['quality']}",
        f"🔹 Audio   : {d['audio']} #Official",
        f"🔹 Genres  : {d['genres']}",
        f"────────────────────",
    ]
    return {"lines": lines, "quote": set(), "italic": set()}


STYLES = {
    "default":  {"name": "⚪ Default"},
    "style1":   {"name": "1️⃣ Star Box",          "render": _u_style1},
    "style2":   {"name": "2️⃣ Dotted Frame",      "render": _u_style2},
    "style3":   {"name": "3️⃣ Bracket Card",      "render": _u_style3},
    "style4":   {"name": "4️⃣ Compact Dotted",    "render": _u_style4},
    "style5":   {"name": "5️⃣ Bold Block",        "render": _u_style5},
    "style6":   {"name": "6️⃣ Bracket Title",     "render": _u_style6},
    "style7":   {"name": "7️⃣ Line Frame",        "render": _u_style7},
    "style8":   {"name": "8️⃣ Sparkle Dotted",    "render": _u_style8},
    "style9":   {"name": "9️⃣ Double Border",     "render": _u_style9},
    "style10":  {"name": "🔟 Minimal Rule",       "render": _u_style10},
}

STYLE_ORDER = ["style1", "style2", "style3", "style4", "style5",
               "style6", "style7", "style8", "style9", "style10"]

PREVIEW_DATA = {
    "anime_name": "Grand Blue",
    "season": 1,
    "episode": "01 To 12",
    "quality": "360p, 720p, 1080p",
    "audio": "Hindi",
    "genres": "Animation, Comedy",
}


def format_episode_display(episode: int | None, episode_start: int | None,
                            episode_end: int | None, total_eps: int | None) -> str:
    """Episode number(s) ko display string mein convert karo:
      - Single:  05          -> "05"
      - Range:   01, 12      -> "01 To 12"
      - Ep 1 hai              -> " (New)" tag
      - Season ka last ep hai (total_eps maloom ho aur end_ep >= total_eps)
                               -> " (Complete)" tag
    """
    if episode_start and episode_end and episode_start != episode_end:
        s = episode_start if episode_start < 100 else episode_start
        e = episode_end if episode_end < 100 else episode_end
        s_str = f"{episode_start:02d}" if episode_start < 100 else str(episode_start)
        e_str = f"{episode_end:02d}" if episode_end < 100 else str(episode_end)
        ep_str = f"{s_str} To {e_str}"
        start_ep, end_ep = episode_start, episode_end
    elif episode_start:
        ep_str = f"{episode_start:02d}" if episode_start < 100 else str(episode_start)
        start_ep = end_ep = episode_start
    elif episode:
        ep_str = f"{episode:02d}" if episode < 100 else str(episode)
        start_ep = end_ep = episode
    else:
        return "—"

    tags = []
    if start_ep == 1:
        tags.append("New")
    if total_eps and end_ep and end_ep >= total_eps:
        tags.append("Complete")
    if tags:
        ep_str += f" ({' / '.join(tags)})"
    return ep_str


def render_update_caption_entities(style_id: str, data: dict) -> tuple:
    """(text, entities) — poora caption bold, header-line blockquote
    (jis template mein hai) ke saath. 'default' ke liye yahan call mat
    karo — woh send_update_post ke purane hardcoded code se banta hai.

    Agar data['how_to_get_link_url'] set hai (/how_to_get_link se), toh
    box ke neeche ek clickable "➥ ʜᴏᴡ ᴛᴏ ɢᴇᴛ ʟɪɴᴋ" line add hoti hai —
    khali/removed ho toh yeh line simply nahi aati."""
    style = STYLES.get(style_id)
    if not style or style_id == DEFAULT_STYLE_ID or "render" not in style:
        # Safe fallback — kabhi bhi crash nahi, bas plain bold text
        text = f"{data.get('anime_name', '')}"
        return text, [{"type": "bold", "offset": 0, "length": _utf16_len(text)}]
    try:
        result = style["render"](data)
        text, entities = _build_entities(result["lines"], result["quote"], result["italic"])

        link_url = (data.get("how_to_get_link_url") or "").strip()
        if link_url:
            link_line = "➥ ʜᴏᴡ ᴛᴏ ɢᴇᴛ ʟɪɴᴋ"
            new_text = f"{text}\n\n{link_line}"
            link_offset = _utf16_len(text) + 2  # do '\n' skip karo
            link_len = _utf16_len(link_line)
            for e in entities:
                if e["type"] == "bold" and e["offset"] == 0:
                    e["length"] = _utf16_len(new_text)  # bold pura naya text cover kare
                    break
            entities.append({"type": "text_link", "offset": link_offset, "length": link_len, "url": link_url})
            text = new_text

        return text, entities
    except Exception as e:
        LOGGER.warning(f"[UpdatePostStyle] Entity render failed for '{style_id}': {e}")
        text = f"{data.get('anime_name', '')}"
        return text, [{"type": "bold", "offset": 0, "length": _utf16_len(text)}]


# ─────────────────────────────────────────────
#  "How to get link" — /how_to_get_link se set/remove hota hai. Set ho
#  toh styled update-post templates (style1..style10, DEFAULT mein nahi)
#  ke neeche "➥ ʜᴏᴡ ᴛᴏ ɢᴇᴛ ʟɪɴᴋ" clickable line add ho jaati hai.
# ─────────────────────────────────────────────
async def get_how_to_get_link() -> str:
    from .database.access_db import db
    doc = await db.col2.find_one({'id': 'how_to_get_link'})
    if not doc:
        return ""
    return doc.get('url', '') or ""


async def set_how_to_get_link(url: str):
    from .database.access_db import db
    await db.col2.update_one({'id': 'how_to_get_link'}, {'$set': {'url': url}}, upsert=True)


# ─────────────────────────────────────────────
#  In-memory cache (DB-backed)
# ─────────────────────────────────────────────
_cache_style_id = DEFAULT_STYLE_ID


async def load_update_post_style_cache():
    """Bot startup pe ek baar call karo — DB se current style load karta hai."""
    global _cache_style_id
    try:
        from .database.access_db import db
        style_id = await db.get_update_post_style()
        _cache_style_id = style_id or DEFAULT_STYLE_ID
        LOGGER.info(f"[UpdatePostStyle] Loaded: style={_cache_style_id}")
    except Exception as e:
        LOGGER.warning(f"[UpdatePostStyle] Cache load failed, using default: {e}")


async def set_style(style_id: str):
    global _cache_style_id
    from .database.access_db import db
    await db.set_update_post_style(style_id)
    _cache_style_id = style_id


def get_current_style_id() -> str:
    return _cache_style_id
