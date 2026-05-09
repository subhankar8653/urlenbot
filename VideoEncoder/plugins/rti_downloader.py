"""
╔══════════════════════════════════════════╗
║       RTI DOWNLOADER PLUGIN  (FIXED)     ║
║  For Encode-Bot (VideoEncoder)           ║
║  Place at: VideoEncoder/plugins/         ║
╚══════════════════════════════════════════╝
"""

import asyncio
import glob
import os
import re
import sqlite3
import subprocess
import time
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from pyrogram import Client, filters
from pyrogram.errors import FloodWait
from pyrogram.types import Message

# ── Selenium optional ──────────────────────────────────────────
try:
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait
    SELENIUM_OK = True
except ImportError:
    SELENIUM_OK = False

# ── Hachoir optional ──────────────────────────────────────────
try:
    from hachoir.metadata import extractMetadata
    from hachoir.parser import createParser
    HACHOIR_OK = True
except ImportError:
    HACHOIR_OK = False

# ── Encode Bot imports ───────────────────────────────────────
from .. import app, download_dir
from ..utils.helper import check_chat
from ..utils.database.add_user import AddUserToDatabase

# ══════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════
RTI_DB           = "rti_bot.db"
RTI_DOWNLOAD     = os.path.join(download_dir, "rti")
RTI_SPLIT        = os.path.join(download_dir, "rti_splits")
RTI_DEFAULT_CODE = "0000"
MAX_PYRO_SIZE    = 1.9 * 1024 * 1024 * 1024
SPLIT_SIZE_MB    = 1800
MIN_FILE_SIZE    = 500 * 1024

os.makedirs(RTI_DOWNLOAD, exist_ok=True)
os.makedirs(RTI_SPLIT, exist_ok=True)

_monitor_task = None

# ══════════════════════════════════════════
#  DATABASE
# ══════════════════════════════════════════
def _db():
    return sqlite3.connect(RTI_DB)

def init_rti_db():
    conn = _db(); c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS rti_users
                 (user_id INTEGER PRIMARY KEY, username TEXT,
                  recovery_code TEXT DEFAULT '9826',
                  check_interval INTEGER DEFAULT 120)""")
    c.execute("""CREATE TABLE IF NOT EXISTS rti_sites
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER, url TEXT, anime_title TEXT,
                  last_episode INTEGER DEFAULT 0,
                  active INTEGER DEFAULT 1, added_date TEXT)""")
    conn.commit(); conn.close()

init_rti_db()

def db_add_user(uid, uname):
    conn = _db(); c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO rti_users (user_id, username) VALUES (?, ?)", (uid, uname))
    conn.commit(); conn.close()

def db_get_code(uid):
    conn = _db(); c = conn.cursor()
    c.execute("SELECT recovery_code FROM rti_users WHERE user_id=?", (uid,))
    r = c.fetchone(); conn.close()
    return r[0] if r else RTI_DEFAULT_CODE

def db_set_code(uid, code):
    conn = _db(); c = conn.cursor()
    c.execute("UPDATE rti_users SET recovery_code=? WHERE user_id=?", (code, uid))
    conn.commit(); conn.close()

def db_get_interval(uid):
    conn = _db(); c = conn.cursor()
    c.execute("SELECT check_interval FROM rti_users WHERE user_id=?", (uid,))
    r = c.fetchone(); conn.close()
    return r[0] if r and r[0] else 120

def db_set_interval(uid, sec):
    conn = _db(); c = conn.cursor()
    c.execute("UPDATE rti_users SET check_interval=? WHERE user_id=?", (sec, uid))
    conn.commit(); conn.close()

def db_add_site(uid, url, title, ep):
    conn = _db(); c = conn.cursor()
    date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c.execute("INSERT INTO rti_sites (user_id, url, anime_title, last_episode, added_date) VALUES (?,?,?,?,?)",
              (uid, url, title, ep, date))
    conn.commit(); conn.close()

def db_get_user_sites(uid):
    conn = _db(); c = conn.cursor()
    c.execute("SELECT * FROM rti_sites WHERE user_id=? AND active=1", (uid,))
    rows = c.fetchall(); conn.close(); return rows

def db_get_all_sites():
    conn = _db(); c = conn.cursor()
    c.execute("SELECT * FROM rti_sites WHERE active=1")
    rows = c.fetchall(); conn.close(); return rows

def db_update_ep(site_id, ep):
    conn = _db(); c = conn.cursor()
    c.execute("UPDATE rti_sites SET last_episode=? WHERE id=?", (ep, site_id))
    conn.commit(); conn.close()

def db_remove_site(uid, site_id):
    conn = _db(); c = conn.cursor()
    c.execute("UPDATE rti_sites SET active=0 WHERE id=? AND user_id=?", (site_id, uid))
    conn.commit(); conn.close()

# ══════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════
def fmt_bytes(size):
    for u in ['B','KB','MB','GB']:
        if size < 1024.0: return f"{size:.2f} {u}"
        size /= 1024.0
    return f"{size:.2f} TB"

def fmt_dur(sec):
    if sec <= 0: return "Unknown"
    return f"{int(sec//60):02d}:{int(sec%60):02d}"

def pbar(pct, ln=10):
    f = int(ln * pct / 100)
    return f"[{'█'*f}{'░'*(ln-f)}] {pct:.1f}%"

def parse_title(full):
    t = full.strip()
    sm = re.search(r'Season\s*(\d+)', t, re.IGNORECASE)
    season = sm.group(1) if sm else "1"
    if 'hindi' in t.lower(): audio = "Hindi"
    elif 'english' in t.lower(): audio = "English"
    elif 'japanese' in t.lower(): audio = "Japanese"
    else: audio = "Multi"
    name = re.sub(r'Season\s*\d+', '', t, flags=re.IGNORECASE)
    name = re.sub(r'Hindi\s*Dubbed?', '', name, flags=re.IGNORECASE)
    name = re.sub(r'Episodes?\s*Download\s*HD?', '', name, flags=re.IGNORECASE)
    name = re.sub(r'\s+', ' ', name).strip()
    return name, season, audio

def get_video_attrs(fp):
    w = h = dur = 0
    if HACHOIR_OK:
        try:
            p = createParser(str(fp))
            if p:
                m = extractMetadata(p)
                if m:
                    if m.has("duration"): dur = int(m.get("duration").seconds)
                    if m.has("width"):    w = m.get("width")
                    if m.has("height"):   h = m.get("height")
                try: p.stream._input.close()
                except: pass
        except: pass
    if dur == 0:
        try:
            r = subprocess.run(f'ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "{fp}"',
                               shell=True, capture_output=True, text=True, timeout=30)
            if r.stdout.strip(): dur = int(float(r.stdout.strip()))
        except: pass
    if w == 0 or h == 0:
        try:
            r = subprocess.run(f'ffprobe -v error -select_streams v:0 -show_entries stream=width,height -of csv=p=0 "{fp}"',
                               shell=True, capture_output=True, text=True, timeout=30)
            if r.stdout.strip():
                pts = r.stdout.strip().split(',')
                if len(pts) >= 2:
                    w = int(pts[0]) if pts[0].strip().isdigit() else 0
                    h = int(pts[1]) if pts[1].strip().isdigit() else 0
        except: pass
    return w, h, dur

def take_thumb(video, out):
    try:
        subprocess.run(f'ffmpeg -y -i "{video}" -ss 00:00:05 -vframes 1 -q:v 2 "{out}" 2>/dev/null',
                       shell=True, timeout=60)
        if os.path.exists(out) and os.path.getsize(out) > 100: return out
    except: pass
    return None

def finfo(fp):
    try:
        sz = os.path.getsize(fp)
        w, h, dur = get_video_attrs(fp)
        return {'size': sz, 'size_fmt': fmt_bytes(sz), 'dur': dur,
                'dur_fmt': fmt_dur(dur), 'fn': os.path.basename(fp), 'w': w, 'h': h}
    except: return None

def split_video(fp, out_dir, max_mb=SPLIT_SIZE_MB):
    os.makedirs(out_dir, exist_ok=True)
    sz_mb = os.path.getsize(fp) / (1024*1024)
    if sz_mb <= max_mb: return [fp]
    _, _, dur = get_video_attrs(fp)
    if dur <= 0: return [fp]
    parts = int(sz_mb / max_mb) + 1
    pd = dur / parts
    name, ext = os.path.splitext(os.path.basename(fp))
    outs = []
    for i in range(parts):
        out = os.path.join(out_dir, f"{name}_part{i+1}{ext}")
        subprocess.run(f'ffmpeg -y -i "{fp}" -ss {i*pd} -t {pd} -c copy "{out}" 2>/dev/null', shell=True)
        if os.path.exists(out) and os.path.getsize(out) > 1024: outs.append(out)
    return outs if outs else [fp]

# ══════════════════════════════════════════
#  STATUS MESSAGE  — FIX: Pyrogram Client use karo
# ══════════════════════════════════════════
class StatusMsg:
    def __init__(self, client, chat_id):
        self.client = client   # FIX: 'bot' nahi, Pyrogram client
        self.chat_id = chat_id
        self.msg = None; self.last = ""; self.t = 0

    async def send(self, text):
        try:
            # FIX: Pyrogram ka send_message
            self.msg = await self.client.send_message(chat_id=self.chat_id, text=text, parse_mode="html")
            self.last = text; self.t = time.time()
        except Exception as e: print(f"[SM.send] {e}")

    async def edit(self, text, force=False):
        if not force and time.time() - self.t < 2: return
        if text == self.last: return
        try:
            if self.msg:
                await self.msg.edit_text(text, parse_mode="html")
                self.last = text; self.t = time.time()
            else: await self.send(text)
        except Exception as e:
            if "not modified" not in str(e).lower() and "MESSAGE_NOT_MODIFIED" not in str(e):
                try: await self.send(text)
                except: pass

    async def done(self, text): await self.edit(text, force=True)

    async def new(self, text):
        try: await self.client.send_message(chat_id=self.chat_id, text=text, parse_mode="html")
        except Exception as e: print(f"[SM.new] {e}")

# ══════════════════════════════════════════
#  ANIME MONITOR
# ══════════════════════════════════════════
class AnimeMonitor:
    H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    PRI = ['hindi','dual','multi','english','japanese','sub','unknown']

    def get_latest(self, url):
        try:
            r = requests.get(url, headers=self.H, timeout=15); r.raise_for_status()
            soup = BeautifulSoup(r.content, "html.parser")
            t = soup.find("h1", class_="entry-title")
            title = t.text.strip() if t else "Unknown Anime"
            for p in reversed(soup.find_all("p")):
                em = re.search(r'Episode\s*(\d+)', p.get_text(" ", strip=True), re.IGNORECASE)
                if em:
                    ep = int(em.group(1))
                    links = self._collect(p, ep)
                    if links: return ep, self._best(links)['href'], title
            return None, None, None
        except Exception as e:
            print(f"[Monitor] {e}"); return None, None, None

    def get_specific(self, url, ep_num):
        try:
            r = requests.get(url, headers=self.H, timeout=15); r.raise_for_status()
            soup = BeautifulSoup(r.content, "html.parser")
            t = soup.find("h1", class_="entry-title")
            title = t.text.strip() if t else "Unknown Anime"
            for p in soup.find_all("p"):
                em = re.search(r'Episode\s*(\d+)', p.get_text(" ", strip=True), re.IGNORECASE)
                if em and int(em.group(1)) == ep_num:
                    links = self._collect(p, ep_num)
                    if links: return self._best(links)['href'], title
            return None, None
        except: return None, None

    def _collect(self, p, ep):
        links = self._wmq(p)
        for i, s in enumerate(p.find_next_siblings()):
            if i > 8: break
            em = re.search(r'Episode\s*(\d+)', s.get_text(" ", strip=True), re.IGNORECASE)
            if em and int(em.group(1)) != ep: break
            links.extend(self._wmq(s))
        return links

    def _wmq(self, el):
        out = []
        for a in el.find_all('a', href=True):
            txt = a.get_text(strip=True).lower()
            href = a.get('href','')
            if 'watchmultquality' in txt or 'watchmultquality' in href.lower() or 'multiquality' in txt:
                out.append({'href': href, 'audio': self._audio(a, el.get_text(" ", strip=True))})
        return out

    def _audio(self, a, ctx):
        ps = a.previous_sibling
        if ps:
            d = self._xaudio(str(ps).lower())
            if d != 'unknown': return d
        par = a.find_parent()
        if par:
            pt = par.get_text(" ", strip=True).lower()
            at = a.get_text(strip=True).lower()
            if at in pt:
                d = self._xaudio(pt.split(at)[0][-50:])
                if d != 'unknown': return d
        for pat, aud in [(r'hindi\s*[-–—]\s*\[?watch','hindi'),
                         (r'english\s*[-–—]\s*\[?watch','english'),
                         (r'dual\s*audio\s*[-–—]\s*\[?watch','dual')]:
            if re.search(pat, ctx.lower()): return aud
        return self._xaudio(ctx)

    def _xaudio(self, t):
        t = t.lower()
        if 'hindi' in t: return 'hindi'
        if 'dual' in t: return 'dual'
        if 'multi' in t: return 'multi'
        if 'english' in t or 'eng' in t: return 'english'
        if 'japanese' in t or 'jap' in t: return 'japanese'
        if 'sub' in t: return 'sub'
        return 'unknown'

    def _best(self, links):
        for p in self.PRI:
            for l in links:
                if l['audio'] == p: return l
        return links[0]

# ══════════════════════════════════════════
#  LINK EXTRACTOR
# ══════════════════════════════════════════
class LinkExtractor:
    def __init__(self, code): self.code = code

    def _driver(self, dl_dir=None):
        if not SELENIUM_OK: raise RuntimeError("Selenium nahi hai!")
        opt = webdriver.ChromeOptions()
        for a in ["--headless","--no-sandbox","--disable-dev-shm-usage","--disable-gpu",
                  "--blink-settings=imagesEnabled=false",
                  "user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"]:
            opt.add_argument(a)
        prefs = {"profile.managed_default_content_settings.images": 2}
        if dl_dir:
            prefs.update({"download.default_directory": dl_dir,
                          "download.prompt_for_download": False,
                          "profile.default_content_setting_values.automatic_downloads": 1})
            opt.binary_location = "/usr/bin/google-chrome"
        opt.add_experimental_option("prefs", prefs)
        opt.page_load_strategy = 'eager'
        d = webdriver.Chrome(options=opt); d.set_page_load_timeout(40)
        return d

    def _get_dl(self, driver, url):
        try:
            driver.get(url)
            WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
            time.sleep(4); driver.execute_script("window.stop();")
            soup = BeautifulSoup(driver.page_source, "html.parser")
            for a in soup.find_all("a"):
                t = (a.get_text(strip=True) or "").upper()
                if "GET DOWNLOAD" in t or "DOWNLOAD" in t:
                    h = a.get("href")
                    if h: return urljoin(driver.current_url, h)
        except: pass
        return None

    def _swift(self, html, base):
        try:
            soup = BeautifulSoup(html, "html.parser")
            for a in soup.find_all("a", href=True):
                if "swift.multiquality.click/downlead" in a["href"]:
                    return urljoin(base, a["href"])
            m = re.findall(r'https?://swift\.multiquality\.click/downlead/[^\s\'"<>]+', html)
            return m[0] if m else None
        except: return None

    def _enter_code(self, driver):
        try:
            wait = WebDriverWait(driver, 12); time.sleep(2)
            try:
                wait.until(EC.element_to_be_clickable((By.PARTIAL_LINK_TEXT, "Click to recover"))).click()
                time.sleep(2)
            except: pass
            inp = None
            for sel in [(By.XPATH,"//input[@placeholder='Enter 4-Digit Code']"),
                        (By.XPATH,"//input[contains(@placeholder,'Code')]"),
                        (By.XPATH,"//input[@type='text']")]:
                try: inp = wait.until(EC.presence_of_element_located(sel)); break
                except: continue
            if not inp: return None
            inp.clear(); inp.send_keys(self.code); time.sleep(1)
            for sel in [(By.XPATH,"//button[contains(text(),'Recover')]"),
                        (By.XPATH,"//button[@type='submit']")]:
                try: wait.until(EC.element_to_be_clickable(sel)).click(); break
                except: continue
            time.sleep(8); driver.execute_script("window.stop();")
            return self._swift(driver.page_source, driver.current_url)
        except: return None

    def process(self, wmq_url):
        d = None
        try:
            d = self._driver()
            dl = self._get_dl(d, wmq_url)
            if not dl: return None
            d.get(dl); time.sleep(3)
            return self._enter_code(d)
        except Exception as e:
            print(f"[LinkExtractor] {e}"); return None
        finally:
            if d:
                try: d.quit()
                except: pass

# ══════════════════════════════════════════
#  DOWNLOADER
# ══════════════════════════════════════════
class RTIDownloader:
    def _clean(self, d):
        for f in os.listdir(d):
            fp = os.path.join(d, f)
            if os.path.isfile(fp):
                try: os.remove(fp)
                except: pass

    def _driver(self, dl_dir):
        self._clean(dl_dir)
        if not SELENIUM_OK: raise RuntimeError("Selenium nahi hai!")
        opt = webdriver.ChromeOptions()
        for a in ['--headless','--no-sandbox','--disable-dev-shm-usage','--disable-gpu']:
            opt.add_argument(a)
        opt.add_experimental_option("prefs", {
            "download.default_directory": dl_dir,
            "download.prompt_for_download": False,
            "safebrowsing.enabled": True,
            "profile.default_content_setting_values.automatic_downloads": 1
        })
        opt.binary_location = "/usr/bin/google-chrome"
        return webdriver.Chrome(options=opt)

    def _close_popups(self, d, main):
        try:
            for h in list(d.window_handles):
                if h != main: d.switch_to.window(h); d.close()
            d.switch_to.window(main)
        except: pass

    def _qualities(self, d):
        try:
            txt = d.find_element(By.TAG_NAME, "body").text.lower()
            return [q for q in ["360p","480p","720p","1080p"] if q in txt]
        except: return []

    async def _wait_q(self, d, main, sm):
        for i in range(1, 201):
            qs = self._qualities(d)
            if len(qs) >= 3: return qs
            if i >= 5 and len(qs) >= 1: return qs
            await sm.edit(f"╔══════════════════╗\n║  🔄 QUALITY CHECK  ║\n╚══════════════════╝\n\n"
                          f"⏳ Attempt: {i}/200\n📊 Found: {len(qs)} ({', '.join(qs) if qs else 'None'})\n\n⏱️ 40s baad refresh...")
            await asyncio.sleep(40)
            try: d.refresh()
            except: pass
            await asyncio.sleep(5); self._close_popups(d, main)
        return self._qualities(d)

    def _click_q(self, d, q):
        try:
            for btn in d.find_elements(By.TAG_NAME, "a"):
                if q in btn.text.lower():
                    d.execute_script("arguments[0].click();", btn); return True
        except: pass
        return False

    async def _monitor_dl(self, sm, dl_dir, expected=3):
        # FIX: dl_dir parameter — hardcoded nahi
        start = time.time(); last_upd = 0; stable = 0; prev_cnt = 0
        while True:
            temp = glob.glob(f"{dl_dir}/*.crdownload") + glob.glob(f"{dl_dir}/*.part")
            done = []
            for f in glob.glob(f"{dl_dir}/*"):
                if not f.endswith(('.crdownload','.part')):
                    try:
                        if os.path.exists(f) and os.path.getsize(f) > 1024: done.append(f)
                    except: pass
            now = time.time(); elapsed = int(now - start)
            if len(done) == prev_cnt and not temp: stable += 1
            else: stable = 0
            prev_cnt = len(done)
            if now - last_upd > 8:
                total_sz = sum(os.path.getsize(f) for f in glob.glob(f"{dl_dir}/*") if os.path.exists(f))
                pct = min(len(done)/max(expected,1)*100, 100)
                await sm.edit(f"╔══════════════════╗\n║  ⬇️ DOWNLOADING  ║\n╚══════════════════╝\n\n"
                              f"{pbar(pct)}\n\n📁 Done: {len(done)}/{expected}\n"
                              f"💾 Size: {fmt_bytes(total_sz)}\n⏱️ Time: {elapsed}s\n📥 In Progress: {len(temp)}")
                last_upd = now
            if len(done) >= expected and not temp: return done
            if stable >= 15 and done: return done
            if elapsed > 1200: return done
            await asyncio.sleep(2)

    async def download(self, swift_url, sm):
        d = None; dl_dir = RTI_DOWNLOAD
        try:
            d = self._driver(dl_dir)
            await sm.edit("🔗 <b>Download page khul raha hai...</b>")
            d.get(swift_url); main = d.current_window_handle
            await asyncio.sleep(10); self._close_popups(d, main)
            qs = await self._wait_q(d, main, sm)
            if not qs:
                await sm.edit("⚠️ <b>Qualities nahi mili!</b> Default try kar raha hun...")
                qs = ["720p","480p","360p"]
            await sm.edit(f"╔═════════════════════╗\n║  ✅ QUALITIES MILI  ║\n╚═════════════════════╝\n\n"
                          f"📊 Available: {', '.join(qs)}\n\n⬇️ Downloads shuru...")
            clicked = 0
            for q in qs[:3]:
                if self._click_q(d, q): clicked += 1
                await asyncio.sleep(2); self._close_popups(d, main); await asyncio.sleep(8)
            if clicked == 0: await sm.edit("⚠️ <b>Click fail</b>, downloads ka wait...")
            return await self._monitor_dl(sm, dl_dir, min(clicked,3) if clicked > 0 else 3)
        except Exception as e:
            print(f"[RTIDownloader] {e}")
            await sm.edit(f"❌ <b>Download Error:</b> {str(e)[:100]}"); return None
        finally:
            if d:
                try: d.quit()
                except: pass

# ══════════════════════════════════════════
#  UPLOADER
# ══════════════════════════════════════════
class RTIUploader:
    def __init__(self): self._utime = 0; self._lupd = 0

    async def upload(self, fp, user_id, sm, ep, name, season, audio):
        if not os.path.exists(fp):
            await sm.new(f"❌ File nahi mila: {os.path.basename(fp)}"); return False
        info = finfo(fp)
        if not info:
            await sm.new(f"❌ File read nahi hua: {os.path.basename(fp)}"); return False
        sz = info['size']
        if sz < MIN_FILE_SIZE:
            await sm.new(f"⚠️ File bahut choti: {info['size_fmt']} (min 500KB)"); return False

        files = split_video(fp, RTI_SPLIT) if sz > MAX_PYRO_SIZE else [fp]
        total = len(files); uploaded = 0

        for idx, uf in enumerate(files):
            try:
                ps = os.path.getsize(uf)
            except: continue
            try:
                w, h, dur = get_video_attrs(uf)
                thumb = None
                try:
                    tn = os.path.join(os.path.dirname(uf),
                                     os.path.splitext(os.path.basename(uf))[0] + "_th.jpg")
                    thumb = take_thumb(uf, tn)
                    if thumb and os.path.exists(thumb) and os.path.getsize(thumb) < 100:
                        os.remove(thumb); thumb = None
                except: thumb = None

                caption = (
                    f"╔════════════════════╗\n║  🎬 ANIME VIDEO  ║\n╚════════════════════╝\n\n"
                    f"┌────────────────────┐\n│ 📺 {name}\n├────────────────────┤\n"
                    f"│ 🏝️ Season: {season}\n│ 🎞️ Episode: {ep}\n│ 🔊 Audio: {audio}\n"
                    f"│ ⏱️ Duration: {fmt_dur(dur)}\n│ 💾 Size: {fmt_bytes(ps)}\n"
                    f"└─────────────────────┘\n\n"
                    f"༺═━═━━ {{ ⚜ }} ━━═━═༻\n     👑 Developed by RJ\n༺═━═━━ {{ ⚜ }} ━━═━═༻"
                )
                if total > 1: caption += f"\n📂 Part: {idx+1}/{total}"

                self._utime = time.time(); self._lupd = 0

                # FIX: closure variable capture — default arg use karo
                async def progress(cur, tot, _i=idx):
                    if time.time() - self._lupd < 3: return
                    self._lupd = time.time()
                    pct = cur/tot*100; spd = cur/max(time.time()-self._utime, 0.01)
                    pt = f" (Part {_i+1}/{total})" if total > 1 else ""
                    await sm.edit(f"╔═══════════════════════╗\n║  📤 UPLOADING{pt}  ║\n╚═══════════════════════╝\n\n"
                                  f"{pbar(pct)}\n\n📊 {fmt_bytes(cur)} / {fmt_bytes(tot)}\n"
                                  f"🚀 Speed: {fmt_bytes(spd)}/s")

                kw = {"chat_id": user_id, "video": uf, "caption": caption,
                      "supports_streaming": True, "progress": progress}
                if dur > 0: kw["duration"] = int(dur)
                if w > 0:   kw["width"] = int(w)
                if h > 0:   kw["height"] = int(h)
                if thumb and os.path.exists(thumb): kw["thumb"] = thumb

                ok = False
                try:
                    await app.send_video(**kw); ok = True
                except Exception as e1:
                    print(f"[Upload] Attempt1 fail: {e1}")
                    try:
                        await app.send_video(chat_id=user_id, video=uf,
                                             caption=caption, supports_streaming=True); ok = True
                    except Exception as e2:
                        print(f"[Upload] Attempt2 fail: {e2}")
                        try:
                            await app.send_document(chat_id=user_id, document=uf, caption=caption); ok = True
                        except Exception as e3:
                            raise e3

                if ok: uploaded += 1
                for cp in [uf, thumb]:
                    if cp and os.path.exists(cp):
                        try: os.remove(cp)
                        except: pass
                await asyncio.sleep(2)

            except FloodWait as e:
                await sm.edit(f"⏳ Rate limit! {e.value}s wait...")
                await asyncio.sleep(e.value + 5)
                try:
                    await app.send_video(chat_id=user_id, video=uf,
                                         caption=caption, supports_streaming=True)
                    uploaded += 1
                except: pass
            except Exception as e:
                print(f"[RTIUploader] {e}")
                await sm.new(f"❌ Upload Error: {str(e)[:80]}\nFile: <code>{os.path.basename(uf)}</code>")
        return uploaded > 0

# ══════════════════════════════════════════
#  EPISODE PROCESSOR
# ══════════════════════════════════════════
async def process_episode(ep, wmq_link, full_title, user_id, client, code):
    """FIX: 'client' parameter — Pyrogram Client object"""
    name, season, audio = parse_title(full_title)
    sm = StatusMsg(client, user_id)   # FIX: client, not bot

    await sm.send(f"╔══════════════════════╗\n║  🎬 EPISODE {ep} PROCESS  ║\n╚══════════════════════╝\n\n"
                  f"📺 {name}\n🏷️ Season: {season}\n🔊 Audio: {audio}\n\n⏳ Link extract ho raha hai...")

    # FIX: Selenium blocking call ko executor mein run karo
    try:
        loop = asyncio.get_event_loop()
        swift = await loop.run_in_executor(None, LinkExtractor(code).process, wmq_link)
    except Exception as e:
        swift = None; print(f"[process_ep] LinkExtractor: {e}")

    if not swift:
        await sm.done(f"❌ <b>Link extract fail!</b>\n\n📺 {name} - Ep {ep}\n\nRecovery code check: /rticode")
        return False

    await sm.edit(f"✅ <b>Link mila!</b>\n\n📺 {name}\n🎬 Episode: {ep}\n\n⬇️ Download shuru...")

    files = await RTIDownloader().download(swift, sm)
    if not files:
        await sm.done(f"❌ <b>Download fail!</b>\n\n📺 {name} - Ep {ep}"); return False

    valid = []
    for f in files:
        info = finfo(f)
        try: sz = os.path.getsize(f)
        except: sz = 0
        if info and sz >= MIN_FILE_SIZE: valid.append((f, info))

    if not valid:
        for f in files:
            info = finfo(f)
            try: sz = os.path.getsize(f)
            except: sz = 0
            if info and sz > 100*1024: valid.append((f, info))

    if not valid:
        await sm.done(f"❌ <b>Koi valid file nahi!</b>\n\n📺 {name} - Ep {ep}"); return False

    valid.sort(key=lambda x: x[1]['size'])
    await sm.edit(f"╔═══════════════════════╗\n║  📤 UPLOAD SHURU  ║\n╚═══════════════════════╝\n\n"
                  f"📺 {name}\n🎬 Episode: {ep}\n📁 Files: {len(valid)}")

    uploader = RTIUploader(); done_c = 0
    for fp_v, _ in valid:
        if await uploader.upload(fp_v, user_id, sm, ep, name, season, audio): done_c += 1
        await asyncio.sleep(2)

    await sm.done(f"╔════════════════════════╗\n║  ✅ EPISODE COMPLETE!  ║\n╚════════════════════════╝\n\n"
                  f"📺 <b>{name}</b>\n🏷️ Season: {season}\n🎬 Episode: {ep}\n🔊 Audio: {audio}\n\n"
                  f"📤 Uploaded: {done_c}/{len(valid)}\n\n"
                  f"༺═━═━━ {{ ⚜ }} ━━═━═༻\n     👑 Developed by RJ\n༺═━═━━ {{ ⚜ }} ━━═━═༻")
    return done_c > 0

# ══════════════════════════════════════════
#  BACKGROUND MONITOR
# ══════════════════════════════════════════
async def rti_background_monitor():
    monitor = AnimeMonitor()
    print("🔄 [RTI] Background monitoring chalu...")
    while True:
        try:
            sites = db_get_all_sites()
            if not sites:
                await asyncio.sleep(120); continue

            min_iv = 120
            for site in sites:
                iv = db_get_interval(site[1])
                if iv < min_iv: min_iv = iv

            for site in sites:
                try:
                    sid, uid, url, title, last_ep = site[0], site[1], site[2], site[3], site[4]
                    ep, link, t = monitor.get_latest(url)
                    if ep and ep > last_ep:
                        code = db_get_code(uid)
                        ok = await process_episode(ep, link, t, uid, app, code)
                        if ok: db_update_ep(sid, ep)
                except Exception as se:
                    print(f"[RTI Monitor] Site error: {se}"); continue

            print(f"[RTI Monitor] {min_iv}s so raha hun...")
            await asyncio.sleep(min_iv)

        except asyncio.CancelledError:
            print("[RTI Monitor] Band hua."); break
        except Exception as e:
            print(f"[RTI] BG error: {e}"); await asyncio.sleep(60)

def _ensure_monitor():
    """FIX: Safe monitor start"""
    global _monitor_task
    try:
        loop = asyncio.get_event_loop()
        if _monitor_task is None or _monitor_task.done():
            _monitor_task = loop.create_task(rti_background_monitor())
    except Exception as e:
        print(f"[RTI] Monitor start fail: {e}")

# ══════════════════════════════════════════
#  COMMANDS
# ══════════════════════════════════════════

@Client.on_message(filters.command("rti"))
async def rti_set(client, message: Message):
    c = await check_chat(message, chat='Both')
    if not c: return
    await AddUserToDatabase(client, message)
    uid = message.from_user.id
    db_add_user(uid, message.from_user.username or "")

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply("❌ URL dena hoga!\n\n<code>/rti https://site.com/anime-url/</code>", parse_mode="html")
        return
    url = args[1].strip()
    if not url.startswith("http"):
        await message.reply("❌ Valid URL nahi hai!"); return
    if not SELENIUM_OK:
        await message.reply("❌ <b>Selenium install nahi hai!</b>\n\n<code>pip install selenium==4.18.1 beautifulsoup4 lxml hachoir</code>", parse_mode="html")
        return

    sm = StatusMsg(client, message.chat.id)
    await sm.send("🔍 <b>Anime detect ho raha hai (Hindi Priority)...</b>")
    monitor = AnimeMonitor()
    ep, link, full_title = monitor.get_latest(url)
    if not ep:
        await sm.done("❌ <b>Anime detect nahi hua!</b>\n\nSite URL check karo."); return

    name, season, audio = parse_title(full_title)
    await sm.done(f"╔═════════════════════╗\n║  ✅ ANIME MILA!  ║\n╚═════════════════════╝\n\n"
                  f"📺 <b>{name}</b>\n🏷️ Season: {season}\n🔊 Audio: {audio}\n"
                  f"🎬 Last Episode: {ep}\n\n⏳ Ep {ep} process ho raha hai...")

    code = db_get_code(uid)
    await process_episode(ep, link, full_title, uid, client, code)
    db_add_site(uid, url, full_title, ep)

    iv_min = db_get_interval(uid) / 60
    await client.send_message(message.chat.id,
        f"╔══════════════╗\n║  ✅ MONITOR ON  ║\n╚══════════════╝\n\n"
        f"📺 <b>{name}</b>\n🎯 Hindi Priority: ON\n⏱️ Check: {iv_min:.1f} min\n\n"
        f"👀 Ep {ep+1}+ auto-download hoga!\n\n"
        f"༺═━═━━ {{ ⚜ }} ━━═━═༻\n     👑 Developed by RJ\n༺═━═━━ {{ ⚜ }} ━━═━═༻",
        parse_mode="html")
    _ensure_monitor()


@Client.on_message(filters.command("rtibatch"))
async def rti_batch(client, message: Message):
    c = await check_chat(message, chat='Both')
    if not c: return
    await AddUserToDatabase(client, message)
    uid = message.from_user.id
    db_add_user(uid, message.from_user.username or "")

    parts = message.text.split()
    if len(parts) < 3:
        await message.reply("❌ Usage:\n<code>/rtibatch https://url.com {2-5}</code>", parse_mode="html"); return
    url = parts[1]; rng = parts[2]
    m = re.match(r'\{(\d+)-(\d+)\}', rng)
    if not m:
        await message.reply("❌ Range: <code>{start-end}</code>", parse_mode="html"); return
    start, end = int(m.group(1)), int(m.group(2))
    if start > end: await message.reply("❌ Start ≤ End!"); return
    if end - start > 15: await message.reply("❌ Max 15 episodes!"); return

    total = end - start + 1
    await message.reply(f"╔═══════════════════╗\n║  🚀 BATCH SHURU  ║\n╚═══════════════════╝\n\n"
                        f"📺 Episodes: {start} → {end}\n📊 Total: {total}\n🎯 Hindi Priority: ON", parse_mode="html")

    monitor = AnimeMonitor(); code = db_get_code(uid); ok = 0
    for ep_num in range(start, end+1):
        cur = ep_num - start + 1
        await message.reply(f"📥 <b>[{cur}/{total}]</b> Episode {ep_num}", parse_mode="html")
        ep_link, title = monitor.get_specific(url, ep_num)
        if not ep_link: await message.reply(f"❌ Episode {ep_num} nahi mila!"); continue
        if await process_episode(ep_num, ep_link, title, uid, client, code): ok += 1
        await asyncio.sleep(5)

    await message.reply(f"╔══════════════════════╗\n║  ✅ BATCH COMPLETE  ║\n╚══════════════════════╝\n\n"
                        f"📊 Success: {ok}/{total}\n\n"
                        f"༺═━═━━ {{ ⚜ }} ━━═━═༻\n     👑 Developed by RJ\n༺═━═━━ {{ ⚜ }} ━━═━═༻",
                        parse_mode="html")


@Client.on_message(filters.command("rtisee"))
async def rti_see(client, message: Message):
    c = await check_chat(message, chat='Both')
    if not c: return
    uid = message.from_user.id; sites = db_get_user_sites(uid)
    if not sites:
        await message.reply("📭 Koi anime monitor nahi!\n\n/rti &lt;url&gt; se add karo.", parse_mode="html"); return
    text = f"╔═══════════════════════╗\n║  📺 MONITORED ({len(sites)})  ║\n╚═══════════════════════╝\n\n"
    for i, s in enumerate(sites, 1):
        n, sea, aud = parse_title(s[3])
        text += f"<b>{i}. {n}</b>\n   🏷️ S{sea} | 🎬 Ep {s[4]} | 🔊 {aud}\n\n"
    text += "💡 Remove: /rtidel &lt;number&gt;"
    await message.reply(text, parse_mode="html")


@Client.on_message(filters.command("rtidel"))
async def rti_del(client, message: Message):
    c = await check_chat(message, chat='Both')
    if not c: return
    uid = message.from_user.id; args = message.text.split()
    if len(args) < 2: await message.reply("❌ Usage: <code>/rtidel 1</code>", parse_mode="html"); return
    try: num = int(args[1])
    except: await message.reply("❌ Number dena hai!"); return
    sites = db_get_user_sites(uid)
    if num < 1 or num > len(sites): await message.reply("❌ Invalid number!"); return
    s = sites[num-1]; db_remove_site(uid, s[0])
    n, _, _ = parse_title(s[3])
    await message.reply(f"🗑️ Removed: <b>{n}</b>", parse_mode="html")


@Client.on_message(filters.command("rticode"))
async def rti_code(client, message: Message):
    c = await check_chat(message, chat='Both')
    if not c: return
    uid = message.from_user.id; db_add_user(uid, message.from_user.username or "")
    args = message.text.split()
    if len(args) < 2:
        code = db_get_code(uid)
        await message.reply(f"🔐 Current Code: <code>{code}</code>\n\nChange: <code>/rticode 1234</code>", parse_mode="html"); return
    code = args[1].strip()
    if not re.match(r'^\d{4}$', code): await message.reply("❌ 4 digit number!"); return
    db_set_code(uid, code)
    await message.reply(f"✅ Code set: <code>{code}</code>", parse_mode="html")


@Client.on_message(filters.command("rtitime"))
async def rti_time(client, message: Message):
    c = await check_chat(message, chat='Both')
    if not c: return
    uid = message.from_user.id; db_add_user(uid, message.from_user.username or "")
    args = message.text.split()
    if len(args) < 2:
        iv = db_get_interval(uid)/60
        await message.reply(f"⏱️ Current: <b>{iv:.1f} min</b>\n\nChange: <code>/rtitime 2</code>", parse_mode="html"); return
    try: mins = float(args[1])
    except: await message.reply("❌ Number dena hai!"); return
    if mins < 0.5: await message.reply("❌ Min 0.5 min!"); return
    if mins > 60: await message.reply("❌ Max 60 min!"); return
    db_set_interval(uid, int(mins * 60))
    await message.reply(f"✅ Interval: <b>{mins:.1f} minutes</b>", parse_mode="html")


@Client.on_message(filters.command("rtistop"))
async def rti_stop(client, message: Message):
    c = await check_chat(message, chat='Both')
    if not c: return
    global _monitor_task
    if _monitor_task and not _monitor_task.done():
        _monitor_task.cancel(); _monitor_task = None
        await message.reply("⛔ RTI monitoring band kar diya!")
    else:
        await message.reply("ℹ️ Monitoring already band tha.")


@Client.on_message(filters.command("rtistatus"))
async def rti_status(client, message: Message):
    c = await check_chat(message, chat='Both')
    if not c: return
    uid = message.from_user.id; db_add_user(uid, message.from_user.username or "")
    sites = db_get_user_sites(uid); code = db_get_code(uid)
    iv = db_get_interval(uid)/60
    mon_ok = _monitor_task is not None and not _monitor_task.done()
    await message.reply(
        f"╔══════════════════╗\n║  📊 RTI STATUS  ║\n╚══════════════════╝\n\n"
        f"📺 Monitored: {len(sites)}\n🔐 Code: <code>{code}</code>\n⏱️ Interval: {iv:.1f} min\n"
        f"🔄 Monitor: {'✅ Running' if mon_ok else '⛔ Stopped'}\n\n"
        f"🛠️ Selenium: {'✅' if SELENIUM_OK else '❌ Install karo'}\n"
        f"🛠️ Hachoir: {'✅' if HACHOIR_OK else '⚠️ ffprobe fallback'}\n\n"
        f"Commands:\n/rti /rtibatch /rtisee /rtidel\n/rticode /rtitime /rtistop",
        parse_mode="html")
