"""
job_ctl.py
==========
Ek "Job" = ek poora process (e.g. /rti ya /anime ka season download+upload).

1) SINGLE MESSAGE  : Process ke saare status (link dhundhna, download progress,
                     har quality ka upload progress) ek hi card message mein
                     dikhte hain. Purana code jo `message.reply(...)` / `msg.edit(...)`
                     se alag-alag messages banata tha, ab `Slot` (nakli message)
                     par likhta hai; Job unhe jod kar ek hi message edit karta hai.
2) CANCEL BUTTON   : Card ke neeche "❌ Cancel" button. Dabate hi:
                       - saare asyncio tasks (download/upload) cancel
                       - Chrome / downloader band
                       - temp download folder + cache delete (memory free)
                       - card mein "Cancelled" dikhta hai
"""

import asyncio
import contextvars
import gc
import shutil
import threading
import time
import uuid

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .. import LOGGER

CURRENT = contextvars.ContextVar("suhani_current_job", default=None)
JOBS: dict = {}

EDIT_GAP = 3.0          # card ko itne second se zyada baar edit nahi karte (FloodWait se bachao)
MAX_LINES = 6


def run_in_thread(fn, *args, **kwargs):
    """run_in_executor jaisa, par contextvars (CURRENT job) thread mein bhi milte hain."""
    ctx = contextvars.copy_context()
    loop = asyncio.get_event_loop()
    return loop.run_in_executor(None, lambda: ctx.run(fn, *args, **kwargs))


def register_driver(driver):
    """Selenium driver ko current job se jod do taaki cancel pe band ho sake."""
    job = CURRENT.get()
    if job is not None and driver is not None:
        job.drivers.append(driver)


def track_task(task):
    """Background asyncio task ko current job se jod do (cancel pe ruk jayega)."""
    job = CURRENT.get()
    if job is not None and task is not None:
        job.tasks.add(task)
    return task


def _compact(text: str, drop=()) -> str:
    lines = [l.rstrip() for l in (text or "").splitlines() if l.strip()]
    if drop:
        lines = [l for l in lines if not any(d in l for d in drop)]
    return "\n".join(lines[:MAX_LINES])


class Slot:
    """Nakli message: edit()/delete() card ke ek hisse ko update karta hai."""

    def __init__(self, job, key, order, label=""):
        self.job = job
        self.key = key
        self.order = order
        self.label = label
        self.text = ""
        self.done = False
        self.id = (abs(hash((job.id, key))) % 900000000) + 1

    async def edit(self, text=None, *args, **kwargs):
        if self.job.cancelled or self.job.finished:
            return self
        t = kwargs.get("text", text)
        if t is not None:
            self.text = str(t)
            self.done = False
        self.job.touch()
        return self

    edit_text = edit

    async def delete(self, *args, **kwargs):
        self.done = True
        self.job.touch()
        return True

    def __getattr__(self, name):
        if name.startswith("_") or name in ("job", "key", "order", "label", "text", "done", "id"):
            raise AttributeError(name)
        return getattr(self.job.card, name)


class Job:
    def __init__(self, card, owner_id: int, title: str):
        self.id = uuid.uuid4().hex[:8]
        self.card = card
        self.owner = owner_id
        self.title = title
        self.header = ""
        self.done_eps: list = []
        self.slots: dict = {}
        self.cancel_ev = threading.Event()
        self.cancelled = False
        self.finished = False
        self.task = None
        self.tasks: set = set()
        self.drivers: list = []
        self.progresses: list = []
        self.dirs: set = set()
        self.cleanups: list = []
        self._last = 0.0
        self._timer = None
        self._lock = asyncio.Lock()

    # ── slots ──
    def slot(self, key, order=50, label="") -> Slot:
        s = self.slots.get(key)
        if s is None:
            s = Slot(self, key, order, label)
            self.slots[key] = s
        return s

    def reset_slots(self, keep=("stage",)):
        for k in [k for k in self.slots if k not in keep]:
            self.slots.pop(k, None)

    # ── render ──
    def render(self) -> str:
        lines = [f"🎌 **{self.title}**"]
        if self.header:
            lines.append(self.header)
        if self.done_eps:
            shown = self.done_eps[-8:]
            more = f"+{len(self.done_eps) - 8} " if len(self.done_eps) > 8 else ""
            lines.append("✅ Done: " + more + " ".join(shown))
        lines.append("━━━━━━━━━━━━━━")
        for s in sorted(self.slots.values(), key=lambda x: x.order):
            if s.done:
                lines.append(f"✅ `{s.label or s.key}` upload ho gaya")
                continue
            if not s.text:
                continue
            if s.key.startswith("up_"):
                body = _compact(s.text, drop=("Elapsed", "📁"))
                lines.append(f"▸ **{s.label}**\n{body}")
            else:
                lines.append(_compact(s.text))
        return "\n".join(lines)[:3900]

    def markup(self):
        if self.finished or self.cancelled:
            return None
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ Cancel", callback_data=f"jc_{self.id}")]]
        )

    # ── card edit (throttled) ──
    def touch(self, force=False):
        if self.finished:
            return
        now = time.monotonic()
        wait = EDIT_GAP - (now - self._last)
        if force or wait <= 0:
            self._schedule()
        elif self._timer is None:
            try:
                self._timer = asyncio.get_event_loop().call_later(wait, self._fire)
            except Exception:
                pass

    def _fire(self):
        self._timer = None
        self._schedule()

    def _schedule(self):
        self._last = time.monotonic()
        asyncio.ensure_future(self._do_edit())

    async def _do_edit(self):
        async with self._lock:
            if self.finished or self.cancelled:
                return
            await self._safe_edit(self.render(), self.markup())

    async def _safe_edit(self, text, markup):
        try:
            await self.card.edit(text, reply_markup=markup)
        except Exception as e:
            s = str(e)
            if "MESSAGE_NOT_MODIFIED" in s:
                return
            if "FLOOD" in s.upper() or "wait of" in s:
                await asyncio.sleep(5)
                return
            LOGGER.debug(f"[Job] card edit error: {e}")

    # ── lifecycle ──
    async def run(self, coro_fn):
        """coro_fn: bina-argument async callable. Cancel hone par None return."""
        token = CURRENT.set(self)
        JOBS[self.id] = self
        self.touch(force=True)
        self.task = asyncio.ensure_future(coro_fn())
        try:
            return await self.task
        except asyncio.CancelledError:
            if self.cancelled:
                return None
            raise
        finally:
            CURRENT.reset(token)
            JOBS.pop(self.id, None)
            if self._timer:
                try:
                    self._timer.cancel()
                except Exception:
                    pass

    async def finish(self, text: str):
        """Process khatam — final text, cancel button hata do."""
        if self.cancelled:
            return
        self.finished = True
        async with self._lock:
            await self._safe_edit(text[:3900], None)
        self._rm_dirs()

    def _rm_dirs(self):
        for d in list(self.dirs):
            shutil.rmtree(d, ignore_errors=True)

    def _kill_drivers(self):
        try:
            from ..plugins.swift_downloader import _kill_driver_tree
        except Exception:
            _kill_driver_tree = None
        for d in list(self.drivers):
            try:
                if _kill_driver_tree:
                    _kill_driver_tree(d)
                else:
                    d.quit()
            except Exception:
                pass
            pd = getattr(d, "_suhani_profile_dir", None)
            if pd:
                shutil.rmtree(pd, ignore_errors=True)
        self.drivers.clear()

    async def cancel(self):
        if self.cancelled or self.finished:
            return
        self.cancelled = True
        self.cancel_ev.set()
        LOGGER.warning(f"[Job {self.id}] CANCEL requested: {self.title}")

        # 1) fast downloaders band
        for pr in self.progresses:
            for dl in list((pr.get("dls") or {}).values()):
                try:
                    dl.stop()
                except Exception:
                    pass

        # 2) saare asyncio tasks (upload / download / main)
        for t in list(self.tasks):
            try:
                t.cancel()
            except Exception:
                pass
        if self.task and not self.task.done():
            self.task.cancel()

        # 3) Chrome processes
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, self._kill_drivers)
        except Exception:
            pass
        await asyncio.sleep(1.5)

        # 4) uploader clients wagairah
        for fn in list(self.cleanups):
            try:
                await asyncio.wait_for(fn(), timeout=20)
            except Exception:
                pass

        # 5) temp files / cache delete + memory free
        self._rm_dirs()
        gc.collect()
        loop.call_later(5, self._rm_dirs)       # thread ne beech mein kuch likha ho to dobara saaf

        # 6) card
        done = " ".join(self.done_eps[-8:]) if self.done_eps else "—"
        try:
            await self.card.edit(
                f"🛑 **Cancelled** — {self.title}\n\n"
                f"✅ Pehle complete hue: {done}\n"
                f"🧹 Download/upload cache delete kar diya gaya.",
                reply_markup=None,
            )
        except Exception:
            pass
