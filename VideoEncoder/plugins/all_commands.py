"""
all_commands.py
================
/allcommand  — bot ke SAARE commands + unka use, paginated (◀️ Prev / Next ▶️).

  /allcommand            → page 1
  /allcommand <page>     → seedha us page pe (e.g. /allcommand 3)
  /allcommand <text>     → command / word search (e.g. /allcommand blacklist)

Buttons: ◀️ Prev • 📄 n/N (index) • Next ▶️ • 🏠 Index (section-wise jump) • ❌ Close

Sirf Owner / Sudo. `/help` mein nahi dikhta — secret command.

Naya command aaye aur is CATALOG mein na ho, to bhi wo "🧩 Aur Commands" section mein
apne aap dikh jaata hai (Pyrogram ke registered handlers se auto-discover hota hai).
"""

import html
import time
import uuid

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from .. import LOGGER
from ..utils.helper import check_chat

PAGE_CHAR_LIMIT = 3300       # Telegram message limit 4096 — margin ke saath
SESSION_TIMEOUT = 3600

# ─────────────────────────────────────────────
#  CATALOG: (section title, [(command, usage, description), ...])
#  Command ke aage 👑 = sirf Owner.
# ─────────────────────────────────────────────
CATALOG = [
    ("🎌 Anime Search & Download", [
        ("/anime", "/anime <naam>",
         "6 websites (Raretoon, Haz, Toono, Aniwatch, Animetsu, AniAbyss) mein search → anime chuno → "
         "episode buttons. Har site mein Quality row (+ multi-language wali sites mein Language row)."),
        ("/rti", "/rti <url> [start end]",
         "RareAnimes page: sirf URL = button menu, '5 5' = sirf ep 5, '0 0' = movie mode."),
        ("/toono", "/toono <series_url>", "Toono: season → episode button menu."),
        ("/haz", "/haz <series_url>", "Hindi Anime Zone: episode menu + quality/language buttons."),
        ("/adh", "/adh <series_url>", "animedubhindi.link: language / quality / episode menu."),
        ("/aniwatch", "/aniwatch <naam|link> [3-8] [1080]",
         "Aniwatch / Animetsu se download. Alias: /aniw. Range + quality (360/720/1080/all) seedha de sakte ho."),
        ("/abyss", "/abyss <aniabyss link|slug>", "AniAbyss (Hindi dub, 4K tak) quality + episode menu."),
        ("/Rtic", "/Rtic <url> [start end]",
         "RTI page se episodes seedha matching channel pe auto-upload (anime /add_anime mein hona chahiye)."),
    ]),
    ("📥 Downloaders & Upload", [
        ("/url", "/url <link> [-vt | -e]", "Direct link / m3u8 / zip download → process → upload. -vt = manual options, -e = zip extract."),
        ("/ytdl", "/ytdl <link> [720|audio]", "YouTube + 1000 sites (yt-dlp). Alias: /yt."),
        ("/mega", "/mega <link>", "Mega.nz se download → Telegram upload (original quality)."),
        ("/meganow", "/meganow <link>", "Mega download → upload → encode."),
        ("/swift", "/swift <url> [720p]", "Swift multi-quality page se download + upload. Alias: /swiftdl."),
        ("/swiftencode", "/swiftencode <url> [720p]", "Swift se download + encode."),
        ("/saveget", "/saveget <link>", "Restricted / private channel ka content save karo (pehle /savelogin)."),
        ("/savelogin", "/savelogin", "Apna Telegram account connect karo (save-restricted ke liye)."),
        ("/savelogout", "/savelogout", "Connected account / session hata do."),
        ("/rename", "/rename [naya naam] (file pe reply)", "File ka naam badal ke (metadata + thumb ke saath) upload."),
        ("/vupload", "/vupload", "Video upload (sudo)."),
        ("/dupload", "/dupload", "Document upload (sudo)."),
        ("/gupload", "/gupload", "Google Drive upload (sudo)."),
        ("/dl", "/dl (video/doc pe reply)", "Telegram video/doc ko encode queue mein daalo."),
        ("/af", "/af (video/doc pe reply)", "Telegram video/doc pe audio-features task (queue mein)."),
        ("/ddl", "/ddl <url> | <filename>", "Direct link se download + encode."),
        ("/batch", "/batch <url>", "Batch link se encode."),
    ]),
    ("🤖 Auto Monitor", [
        ("/add_anime", "/add_anime", "Anime + channel + poster + audio + interval — button flow se add karo."),
        ("/cancel_add_anime", "/cancel_add_anime", "Beech mein /add_anime flow cancel."),
        ("/list_anime", "/list_anime", "Saved anime ki list (panel: detail / edit / delete)."),
        ("/del_anime", "/del_anime [number]", "Anime list se remove karo."),
        ("/set_monitor", "/set_monitor <-100id>", "Monitor channel set karo (ya forward post pe reply)."),
        ("/monitor_status", "/monitor_status", "Monitor system health + bot access check."),
        ("/monitor_notify", "/monitor_notify fail|all|off", "Monitor verdict DMs kab aayein."),
        ("/monitor_debug", "/monitor_debug on|off", "Monitor channel ki har post ka verdict DM."),
        ("/check_post", "/check_post (forward post pe reply)", "Dry-run: post detect hoti ya nahi, kyun nahi."),
        ("/monitor_blacklist", "/monitor_blacklist [add w1|w2 | remove w | clear]",
         "🚫 Blacklist panel (alias /mbl). Post ke text mein blacklisted word ho to wo post upload nahi hoti. "
         "➕ Add Word / ➖ Remove Word buttons."),
        ("/set_direct", "/set_direct <-100id|off>", "Direct FILE channel set karo (post nahi, seedha file aati hai)."),
        ("/cancel_season", "/cancel_season [Anime Name]", "Us anime ka chal raha poora auto-upload rok do."),
        ("/cancel_episode", "/cancel_episode [Anime Name] [Ep]", "Sirf ek episode ka process cancel."),
        ("/active_uploads", "/active_uploads", "Abhi chal rahe auto-uploads (anime + episode + kitni der se)."),
    ]),
    ("📢 Update Channel & Posts", [
        ("/update_channel", "/update_channel <id> [N]", "Update channel add karo."),
        ("/update_channel_list", "/update_channel_list", "Saare update channels dekho."),
        ("/delete_update_channel", "/delete_update_channel", "Update channel remove karo."),
        ("/updatechannel", "/updatechannel on|off", "Update channel posting on/off."),
        ("/update_post", "/update_post", "Anime ka update-post entry save karo (5-step)."),
        ("/cancel_update_post", "/cancel_update_post", "Chal rahe update_post flow ko cancel."),
        ("/update_post_list", "/update_post_list", "Saved anime entries — paginated panel."),
        ("/delete_update_post", "/delete_update_post", "Kisi anime ka saved post entry remove."),
        ("/manual_update_post", "/manual_update_post", "Saved anime ka update-post abhi sabhi update channels pe bhejo."),
        ("/update_post_button", "/update_post_button", "'Kaise Dekhein' + 'Join Backup' default button links set."),
        ("/remove_update_post_button", "/remove_update_post_button", "Default update-post buttons hata do."),
        ("/latest_post_delete", "/latest_post_delete", "Update channel ka last bheja post delete."),
        ("/post_delete", "/post_delete <link(s)>", "Link se post delete (jahan bot admin ho)."),
        ("/multi_mode", "/multi_mode on|off", "Multi update-channel mode switch."),
    ]),
    ("🗓 Schedule & End Messages", [
        ("/schedule", "/schedule [days] [total_eps] [Anime Name]", "Anime ka release schedule set (e.g. /schedule 7 12 Witch Hat Atelier)."),
        ("/schedule_list", "/schedule_list", "Saare schedules dekho."),
        ("/schedule_del", "/schedule_del [Anime Name]", "Schedule delete."),
        ("/schedule_on", "/schedule_on", "Har episode ke baad schedule message ('Next episode upload on…') channel pe post ON."),
        ("/schedule_off", "/schedule_off", "Schedule message posting OFF (end messages chalte rahenge)."),
        ("/schedule_status", "/schedule_status", "Schedule posting ON hai ya OFF."),
        ("/end_message", "/end_message", "End message(s) record karo — phir /done."),
        ("/end_message_preview", "/end_message_preview [channel]", "End messages ka preview."),
        ("/end_message_del", "/end_message_del [channel]", "End messages delete."),
        ("/cancel_end", "/cancel_end", "End-message recording cancel."),
        ("/done", "/done", "Recording / sticker setup khatam karke save karo."),
        ("/hindi_schedule", "/hindi_schedule [on|off]", "Hindi anime daily schedule auto-post ka status / on / off."),
        ("/add_hindi_anime_schedule", "/add_hindi_anime_schedule <channel_id>", "Hindi schedule channel set karo."),
        ("/hindi_schedule_time", "/hindi_schedule_time 08:00", "Roz kab post ho (IST)."),
        ("/hindi_schedule_now", "/hindi_schedule_now [mon..sun]", "Abhi schedule post karo."),
        ("/hindi_schedule_debug", "/hindi_schedule_debug", "Hindi schedule scraper ka debug data."),
        ("/anime_news", "/anime_news [on|off]", "Anime news auto-post ka status / on / off."),
        ("/add_anime_news_channel", "/add_anime_news_channel <channel_id>", "Anime news channel set karo."),
        ("/rem_anime_news_channel", "/rem_anime_news_channel", "Anime news channel hatao."),
        ("/anime_news_interval", "/anime_news_interval 10", "Kitne minute mein news check ho (5-360)."),
        ("/anime_news_now", "/anime_news_now", "Abhi news check karke post karo (test)."),
        ("/add_news_rss", "/add_news_rss <url>", "Anime news RSS feed add karo (max 3)."),
        ("/rem_news_rss", "/rem_news_rss <url|number>", "Anime news RSS feed hatao."),
        ("/view_news_rss", "/view_news_rss", "Anime news RSS feeds dekho."),
    ]),
    ("🎨 Customise & Style", [
        ("/Customise", "/Customise", "Saari customization ka ek hub menu (caption, post style, thumbnail, buttons, channels)."),
        ("/caption_style", "/caption_style", "Episode caption ke 10 style presets."),
        ("/update_post_style", "/update_post_style", "Update-channel post ke 10 style presets."),
        ("/setpic_style", "/setpic_style", "Auto thumbnail ke bottom band ka style / colour."),
        ("/how_to_get_link", "/how_to_get_link [url|remove]", "Update-post ke neeche 'How to get link' link."),
        ("/setpic", "/setpic [keyword] (image pe reply)", "Custom thumbnail / keyword pic set."),
        ("/deletepic", "/deletepic <keyword>", "Keyword pic delete. Alias: /delpic."),
        ("/listpic", "/listpic", "Saare pic keywords."),
        ("/previewpic", "/previewpic <keyword>", "Keyword ki saved pic dekho."),
        ("/thumb", "/thumb", "Apna thumbnail set / delete."),
        ("/btncolors", "/btncolors on|off", "Coloured buttons toggle."),
        ("/community", "/community [naam]", "Bot-wide community naam set / dekho."),
        ("/communityclear", "/communityclear", "Community naam default pe reset."),
        ("/setmeta", "/setmeta", "Metadata panel. Alias: /metadata."),
        ("/clearmeta", "/clearmeta", "Saved metadata clear."),
        ("/urlsettings", "/urlsettings", "URL uploader ki current settings."),
        ("/urlpreset", "/urlpreset", "URL auto-processing preset."),
        ("/addswap", "/addswap [old new]", "Text swap rule panel (caption/filename mein)."),
        ("/swap", "/swap old1:new1|old2:new2", "Seedha swap rules set."),
        ("/swaplist", "/swaplist", "Swap rules dekho."),
        ("/clearswap", "/clearswap", "Swap rules clear. (/swapclear bhi)"),
        ("/blacklist", "/blacklist [word1|word2]",
         "CAPTION blacklist — words jo caption / filename se hamesha hata diye jaate hain (monitor blacklist alag hai)."),
        ("/blacklistclear", "/blacklistclear", "Caption blacklist clear."),
        ("/file_mode", "/file_mode", "Upload mode: seedha video file."),
        ("/bot_mode", "/bot_mode", "Upload mode: bot post + quality buttons."),
        ("/upload_mode_status", "/upload_mode_status", "Abhi kaun sa upload mode."),
        ("/border", "/border", "Border sticker setup (phir /done)."),
        ("/season_sticker", "/season_sticker", "Season stickers collect (phir /done)."),
        ("/set_end", "/set_end [template]", "End message template."),
        ("/bot_upload", "/bot_upload <channel_id> <anime> | <season> | <source>", "Poora bot-mode upload pipeline."),
        ("/getemoji", "/getemoji (premium emoji bhejo)", "Premium emoji ka ID nikalo."),
        ("/testemoji", "/testemoji <channel_id> <text>", "Channel mein premium emoji test."),
    ]),
    ("📡 Channels", [
        ("/addchannel", "/addchannel [AnimeName] [Language]", "Upload ke liye channel link karo."),
        ("/seechannel", "/seechannel", "Linked channels dekho."),
        ("/delchannel", "/delchannel", "Channel remove."),
        ("/delete_message", "/delete_message [channel_id]", "Channel pe nayi video aane par purane msgs auto-delete set."),
        ("/delete_message_list", "/delete_message_list", "Auto-delete channels list."),
        ("/delete_message_del", "/delete_message_del", "Auto-delete rule hatao."),
    ]),
    ("⚙️ System & Admin", [
        ("/start", "/start", "Bot start."),
        ("/help", "/help", "Chhota commands list."),
        ("/allcommand", "/allcommand [page|word]", "Yeh list (secret)."),
        ("/stats", "/stats", "CPU / RAM / disk / uptime."),
        ("/status", "/status", "System status + chal rahe tasks."),
        ("/quota", "/quota", "Upload quota live status."),
        ("/queue", "/queue", "Encode queue dekho."),
        ("/clear", "/clear", "Queue clear."),
        ("/clean", "/clean", "Junk files delete."),
        ("/cleanup", "/cleanup", "Manual cleanup."),
        ("/health", "/health", "Disk / RAM health."),
        ("/speedtest", "/speedtest", "Server speed test."),
        ("/restart", "/restart", "Bot restart."),
        ("/update", "/update", "git pull + restart."),
        ("/redeploy", "/redeploy", "Railway redeploy (button)."),
        ("/exec", "/exec <python>", "Python execute."),
        ("/sh", "/sh <cmd>", "Shell command."),
        ("/addchat", "/addchat <id>", "👑 Chat ko authorize karo."),
        ("/addsudo", "/addsudo <id>", "👑 Sudo user add."),
        ("/rmchat", "/rmchat <id>", "👑 Chat authorization hatao."),
        ("/rmsudo", "/rmsudo <id>", "👑 Sudo hatao."),
        ("/recover_data", "/recover_data", "👑 Owner ID badalne ke baad purana data wapas laao."),
        ("/encode_mode", "/encode_mode on|off", "Encode bot mode / auto-upload-only mode."),
        ("/settings", "/settings", "Encode settings menu."),
        ("/vset", "/vset", "Encode settings dekho."),
        ("/reset", "/reset", "Apni encode settings reset."),
        ("/hazapi", "/hazapi", "Haz captcha config + credits status."),
        ("/hazsolvetest", "/hazsolvetest [link]", "Haz captcha solver test."),
    ]),
]

SESSIONS: dict = {}          # sid -> {"user": id, "created": ts}


# ─────────────────────────────────────────────
#  Auto-discover: catalog mein na hone wale commands
# ─────────────────────────────────────────────
def _cmds_from_filter(f) -> set:
    """Filter tree se command names nikalo. `~filters.command(...)` (Not) ko skip karta hai."""
    out = set()
    if f is None:
        return out
    if f.__class__.__name__ == "InvertFilter":    # ~filter
        return out
    cmds = getattr(f, "commands", None)
    if cmds:
        out.update(str(c).lower() for c in cmds)
    for attr in ("base", "other"):
        sub = getattr(f, attr, None)
        if sub is not None and sub is not f:
            out |= _cmds_from_filter(sub)
    return out


def _known_names() -> set:
    known = set()
    for _title, items in CATALOG:
        for cmd, usage, _d in items:
            known.add(cmd.lstrip("/").lower())
            for tok in usage.replace("|", " ").split():
                if tok.startswith("/"):
                    known.add(tok.lstrip("/").lower())
    # alias-only entries jo description mein likhe hain
    known |= {"aniw", "swiftdl", "yt", "delpic", "swapclear", "metadata", "mbl", "rtic", "customise",
              "customize"}
    return known


def _discover_extra() -> list:
    """[(cmd, usage, desc)] — registered handlers mein jo catalog mein nahi hain."""
    extra = {}
    try:
        from .. import app
        known = _known_names()
        for handlers in app.dispatcher.groups.values():
            for h in handlers:
                cb = getattr(h, "callback", None)
                doc = ""
                if cb is not None and cb.__doc__:
                    doc = next((ln.strip() for ln in cb.__doc__.strip().splitlines() if ln.strip()), "")
                for name in _cmds_from_filter(getattr(h, "filters", None)):
                    if name in known or name in extra:
                        continue
                    extra[name] = doc or "(description nahi — code mein dekho)"
    except Exception as e:
        LOGGER.warning(f"[AllCommand] auto-discover fail: {e}")
    return [(f"/{n}", f"/{n}", d[:140]) for n, d in sorted(extra.items())]


# ─────────────────────────────────────────────
#  Pages
# ─────────────────────────────────────────────
def _entry_html(cmd: str, usage: str, desc: str) -> str:
    return f"<b>{html.escape(cmd)}</b>\n└ <code>{html.escape(usage)}</code>\n   {html.escape(desc)}\n"


def build_pages() -> tuple[list, list]:
    """Returns (pages: list[str html], section_first_page: list[(title, page_idx)])."""
    sections = list(CATALOG)
    extra = _discover_extra()
    if extra:
        sections.append(("🧩 Aur Commands", extra))

    pages: list = []
    index: list = []
    for title, items in sections:
        head = f"<b>{html.escape(title)}</b>\n"
        cur = ""
        first = True
        for cmd, usage, desc in items:
            block = _entry_html(cmd, usage, desc)
            prefix = head if first else f"<b>{html.escape(title)}</b> <i>(contd.)</i>\n"
            if cur and len(cur) + len(block) > PAGE_CHAR_LIMIT:
                pages.append(cur)
                cur = ""
                first = False
                prefix = f"<b>{html.escape(title)}</b> <i>(contd.)</i>\n"
            if not cur:
                cur = prefix + "━━━━━━━━━━━━━━━━━━\n"
                if first:
                    index.append((title, len(pages)))
                    first = False
            cur += block + "\n"
        if cur:
            pages.append(cur)
    return pages, index


_cache: dict = {"pages": None, "index": None, "t": 0.0}


def _get_pages():
    # discover bot chalu hone ke baad poora hota hai — thoda cache, par stale na ho
    if _cache["pages"] is None or time.time() - _cache["t"] > 300:
        _cache["pages"], _cache["index"] = build_pages()
        _cache["t"] = time.time()
    return _cache["pages"], _cache["index"]


def _page_text(pages: list, page: int) -> str:
    total = len(pages)
    return (f"📚 <b>All Commands</b> — Page {page + 1}/{total}\n"
            f"<i>👑 = sirf Owner • &lt;&gt; = zaroori • [] = optional</i>\n\n"
            f"{pages[page]}")


def _nav_kb(sid: str, page: int, total: int) -> InlineKeyboardMarkup:
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"acmd_p_{sid}_{page - 1}"))
    nav.append(InlineKeyboardButton(f"📄 {page + 1}/{total}", callback_data=f"acmd_i_{sid}"))
    if page < total - 1:
        nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"acmd_p_{sid}_{page + 1}"))
    rows = [nav,
            [InlineKeyboardButton("🏠 Index", callback_data=f"acmd_i_{sid}"),
             InlineKeyboardButton("❌ Close", callback_data=f"acmd_x_{sid}")]]
    return InlineKeyboardMarkup(rows)


def _index_view(sid: str, index: list, total: int):
    text = ("📑 <b>Index</b>\n━━━━━━━━━━━━━━━━━━\n"
            "Section pe tap karo — seedha us page pe jaoge.\n"
            f"Total pages: <b>{total}</b>")
    rows, row = [], []
    for title, pg in index:
        row.append(InlineKeyboardButton(f"{title} • {pg + 1}", callback_data=f"acmd_p_{sid}_{pg}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("❌ Close", callback_data=f"acmd_x_{sid}")])
    return text, InlineKeyboardMarkup(rows)


def _prune():
    now = time.time()
    for k in [k for k, v in SESSIONS.items() if now - v["created"] > SESSION_TIMEOUT]:
        SESSIONS.pop(k, None)


# ─────────────────────────────────────────────
#  Handlers
# ─────────────────────────────────────────────
@Client.on_message(filters.command("allcommand"))
async def allcommand_cmd(client: Client, message: Message):
    c = await check_chat(message, chat="Sudo")
    if not c:
        return
    _prune()
    pages, _index = _get_pages()
    arg = " ".join((message.text or "").split()[1:]).strip()

    # /allcommand <word> — search
    if arg and not arg.isdigit():
        q = arg.lower().lstrip("/")
        hits = []
        for title, items in CATALOG + [("🧩 Aur Commands", _discover_extra())]:
            for cmd, usage, desc in items:
                if q in cmd.lower() or q in usage.lower() or q in desc.lower():
                    hits.append(_entry_html(cmd, usage, desc))
        if not hits:
            await message.reply(f"❌ `{arg}` se kuch nahi mila. /allcommand se poori list dekho.")
            return
        body = f"🔎 <b>Search:</b> <code>{html.escape(arg)}</code> — {len(hits)} mile\n\n" + "\n".join(hits)
        await message.reply(body[:4000], parse_mode=ParseMode.HTML)
        return

    page = 0
    if arg.isdigit():
        page = max(0, min(int(arg) - 1, len(pages) - 1))
    sid = uuid.uuid4().hex[:8]
    SESSIONS[sid] = {"user": message.from_user.id, "created": time.time()}
    await message.reply(_page_text(pages, page), reply_markup=_nav_kb(sid, page, len(pages)),
                        parse_mode=ParseMode.HTML, disable_web_page_preview=True)


@Client.on_callback_query(filters.regex(r"^acmd_"))
async def allcommand_cb(client: Client, cb: CallbackQuery):
    try:
        parts = cb.data.split("_")
        action = parts[1] if len(parts) > 1 else ""
        sid = parts[2] if len(parts) > 2 else ""
        s = SESSIONS.get(sid)
        if not s:
            await cb.answer("⌛ Session expire — /allcommand dubara bhejo.", show_alert=True)
            return
        if cb.from_user.id != s["user"]:
            await cb.answer("❌ Yeh tumhara menu nahi hai.", show_alert=True)
            return

        if action == "x":
            SESSIONS.pop(sid, None)
            await cb.answer()
            try:
                await cb.message.delete()
            except Exception:
                pass
            return

        pages, index = _get_pages()
        if action == "i":
            text, kb = _index_view(sid, index, len(pages))
            await cb.answer()
            await cb.message.edit(text, reply_markup=kb, parse_mode=ParseMode.HTML)
            return

        if action == "p":
            page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
            page = max(0, min(page, len(pages) - 1))
            await cb.answer()
            await cb.message.edit(_page_text(pages, page), reply_markup=_nav_kb(sid, page, len(pages)),
                                  parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            return

        await cb.answer()
    except Exception as e:
        if "MESSAGE_NOT_MODIFIED" in str(e):
            return
        LOGGER.error(f"[AllCommand] callback error: {e}", exc_info=True)
        try:
            await cb.answer("❌ Error, dubara try karo.", show_alert=True)
        except Exception:
            pass
