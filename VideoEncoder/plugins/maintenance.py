"""/cleanup — manual cleanup, /health — disk/RAM status (owner & sudo)."""
import asyncio

from pyrogram import Client, filters

from .. import owner, sudo_users
from ..utils import janitor
from .. import profile as _prof


def _hr(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}TB"


_auth = filters.user(list(set(owner + sudo_users)))


@Client.on_message(filters.command(["cleanup"]) & _auth)
async def cleanup_cmd(_, message):
    if janitor.is_busy():
        await message.reply_text("Task chal raha hai — sirf idle/purani files hatayi jayengi.")
    res = await asyncio.get_running_loop().run_in_executor(None, janitor.run_cleanup, True)
    cache_now = janitor.mem_breakdown()["cache"]
    await message.reply_text(
        f"<b>🧹 Cleanup done</b>\nFreed: {_hr(res['freed'])}\n"
        f"Chrome killed: {res['killed']}\nFile cache ab: {_hr(cache_now)}\nDisk free: {_hr(res['free'])} (used {res['pct']:.0f}%)")


@Client.on_message(filters.command(["health"]) & _auth)
async def health_cmd(_, message):
    used, limit = janitor.container_mem()
    free, pct = janitor.disk_stats()
    b = janitor.mem_breakdown()
    await message.reply_text(
        f"<b>🩺 Health</b>\nProfile: {_prof.summary()}\nRAM: {_hr(used)} / {_hr(limit)} ({used/limit*100:.0f}%)\n"
        f"├ Bot (python): {_hr(b['python'])}\n"
        f"├ Chrome: {_hr(b['chrome'])} ({b['chrome_procs']} proc)\n"
        f"├ ffmpeg: {_hr(b['ffmpeg'])}\n"
        f"└ File cache (ginti se bahar): {_hr(b['cache'])}\n"
        f"Downloads folder: {_hr(janitor.dir_size(janitor.download_dir))}\n"
        f"Encode folder: {_hr(janitor.dir_size(janitor.encode_dir))}\n"
        f"Disk free: {_hr(free)} (used {pct:.0f}%)\nBusy: {janitor.is_busy()}")
