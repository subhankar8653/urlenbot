"""
pyrogram_patch.py
=================
Pyrogram ke upload internals ko patch karta hai max speed ke liye.

Fixes:
  1. Part size: 512KB (Telegram default, safe for all clients)
  2. Workers: 4 parallel (20 se deadlock hota tha — queue full → main loop block)
  3. Queue size: unlimited (maxsize=workers se deadlock hota tha)
"""

import asyncio
import functools
import inspect
import io
import logging
import math
import os
from hashlib import md5
from pathlib import PurePath
from typing import Union, BinaryIO, Callable

import pyrogram
from pyrogram import StopTransmission
from pyrogram import raw
from pyrogram.session import Session
from pyrogram.methods.advanced.save_file import SaveFile

log = logging.getLogger(__name__)

# ── Tunable constants ──────────────────────────────────────────────────────────
_PART_SIZE = 512 * 1024   # 512 KB — Telegram default, safe for all clients

# Parallel workers per upload:
# - 20 workers + 20-maxsize queue = deadlock (main loop blocks on queue.put when full)
# - 4 workers = safe, fast enough for most connections, no deadlock
_WORKERS_COUNT = 4


async def save_file(
    self: "pyrogram.Client",
    path: Union[str, BinaryIO],
    file_id: int = None,
    file_part: int = 0,
    progress: Callable = None,
    progress_args: tuple = ()
):
    async with self.save_file_semaphore:
        if path is None:
            return None

        async def worker(session):
            while True:
                data = await queue.get()
                if data is None:
                    queue.task_done()
                    return
                try:
                    await session.invoke(data)
                except Exception as e:
                    log.exception(e)
                finally:
                    queue.task_done()

        part_size = _PART_SIZE

        if isinstance(path, (str, PurePath)):
            fp = open(path, "rb")
        elif isinstance(path, io.IOBase):
            fp = path
        else:
            raise ValueError("Invalid file. Expected a file path or binary file pointer")

        file_name = getattr(fp, "name", "file.jpg")

        fp.seek(0, io.SEEK_END)
        file_size = fp.tell()
        fp.seek(0)

        if file_size == 0:
            raise ValueError("File size equals to 0 B")

        file_size_limit_mib = 4000 if self.me.is_premium else 2000

        if file_size > file_size_limit_mib * 1024 * 1024:
            raise ValueError(f"Can't upload files bigger than {file_size_limit_mib} MiB")

        file_total_parts = int(math.ceil(file_size / part_size))
        is_big = file_size > 10 * 1024 * 1024

        # FIX: workers = 4 (was max_concurrent_transmissions = 20 → deadlock)
        # FIX: queue maxsize = 0 (unlimited) — was workers_count → main loop blocked on put()
        workers_count = _WORKERS_COUNT if is_big else 1

        is_missing_part = file_id is not None
        file_id = file_id or self.rnd_id()
        md5_sum = md5() if not is_big and not is_missing_part else None

        # FIX: keyword args use kiye (positional nahi) — kuch kurigram versions
        # mein Session.__init__ ke parameters ka order badal gaya hai (naye
        # features ke saath), jisse positional call galat slot mein values
        # daal deta tha aur "missing required positional arguments" crash
        # deta tha. Keyword args se chahe order kuch bhi ho, sahi jagah par
        # hi value jaayegi — kabhi bhi crash nahi hoga.
        session = Session(
            client=self,
            dc_id=await self.storage.dc_id(),
            auth_key=await self.storage.auth_key(),
            test_mode=await self.storage.test_mode(),
            is_media=True,
        )

        # maxsize=0 = unlimited queue — main loop kabhi block nahi hoga queue.put() pe
        queue = asyncio.Queue(0)
        workers = [self.loop.create_task(worker(session)) for _ in range(workers_count)]

        try:
            await session.start()
            fp.seek(part_size * file_part)

            while True:
                chunk = fp.read(part_size)

                if not chunk:
                    if not is_big and not is_missing_part:
                        md5_sum = "".join([hex(i)[2:].zfill(2) for i in md5_sum.digest()])
                    break

                if is_big:
                    rpc = raw.functions.upload.SaveBigFilePart(
                        file_id=file_id,
                        file_part=file_part,
                        file_total_parts=file_total_parts,
                        bytes=chunk
                    )
                else:
                    rpc = raw.functions.upload.SaveFilePart(
                        file_id=file_id,
                        file_part=file_part,
                        bytes=chunk
                    )

                await queue.put(rpc)

                if is_missing_part:
                    return

                if not is_big and not is_missing_part:
                    md5_sum.update(chunk)

                file_part += 1

                if progress:
                    func = functools.partial(
                        progress,
                        min(file_part * part_size, file_size),
                        file_size,
                        *progress_args
                    )
                    if inspect.iscoroutinefunction(progress):
                        await func()
                    else:
                        await self.loop.run_in_executor(self.executor, func)

        except StopTransmission:
            raise
        except Exception as e:
            log.exception(e)
        else:
            if is_big:
                return raw.types.InputFileBig(
                    id=file_id,
                    parts=file_total_parts,
                    name=file_name,
                )
            else:
                return raw.types.InputFile(
                    id=file_id,
                    parts=file_total_parts,
                    name=file_name,
                    md5_checksum=md5_sum
                )
        finally:
            for _ in workers:
                await queue.put(None)
            await asyncio.gather(*workers)
            await session.stop()
            if isinstance(path, (str, PurePath)):
                fp.close()


# Apply patch
SaveFile.save_file = save_file
pyrogram.Client.save_file = save_file
