"""
mega_py.py — megatools ke bina Mega.nz PUBLIC FILE download (pure Python)
=========================================================================
`megadl` / `megatools` server pe install nahi hota to /mega ye use karta hai.

  * https://mega.nz/file/<id>#<key>   (aur purana  https://mega.nz/#!<id>!<key>)
  * AES-128-CTR decrypt (pycryptodome — requirements.txt mein pehle se hai)
  * Parallel segments (default 4 connection) — ek connection se kaafi tez
  * Folder links yahan support nahi (megatools chahiye) — saaf error aata hai

API:  await download_public_file(url, dest_dir, progress=cb, cancel=event) -> filepath
      progress(done_bytes, total_bytes, speed_bps, name)  — async ya sync callback
"""

import asyncio
import base64
import json
import os
import random
import re
import struct
import time

import aiohttp
from Crypto.Cipher import AES

from .. import LOGGER

API_URL = "https://g.api.mega.co.nz/cs"
PARTS = int(os.getenv("MEGA_PARTS") or 4)               # parallel connections
MIN_PART = 8 * 1024 * 1024                              # isse chhoti file = 1 connection
CHUNK = 1024 * 1024

_ERRORS = {
    -2: "invalid argument", -3: "server busy, retry", -9: "file nahi mili (link delete/expire ho gaya)",
    -11: "access denied", -14: "decryption key galat", -16: "account blocked / link TOS violation",
    -17: "Mega ki bandwidth quota khatam (kuch der baad / VPN ke bina retry karo)",
    -18: "temporarily unavailable, retry", -19: "too many connections",
}


class MegaError(Exception):
    pass


def _b64d(s: str) -> bytes:
    s = s.replace("-", "+").replace("_", "/").replace(",", "")
    return base64.b64decode(s + "=" * (-len(s) % 4))


def parse_link(url: str):
    """-> (file_id, key_bytes) ya MegaError"""
    if re.search(r"mega\.(?:nz|co\.nz)/(?:folder/|#F!)", url):
        raise MegaError("Folder link hai — python fallback sirf single file support karta hai (megatools chahiye)")
    m = re.search(r"mega\.(?:nz|co\.nz)/file/([A-Za-z0-9_-]{8})#([A-Za-z0-9_-]{43})", url) \
        or re.search(r"mega\.(?:nz|co\.nz)/#!([A-Za-z0-9_-]{8})!([A-Za-z0-9_-]{43})", url)
    if not m:
        raise MegaError("Mega file link ka format samajh nahi aaya")
    return m.group(1), _b64d(m.group(2))


def _key_parts(key: bytes):
    if len(key) != 32:
        raise MegaError("key length galat")
    k = struct.unpack(">8I", key)
    aes_key = struct.pack(">4I", k[0] ^ k[4], k[1] ^ k[5], k[2] ^ k[6], k[3] ^ k[7])
    nonce = struct.pack(">2I", k[4], k[5])               # 8 bytes
    return aes_key, nonce


def decrypt_attrs(at_b64: str, aes_key: bytes) -> dict:
    raw = _b64d(at_b64)
    raw = raw + b"\0" * (-len(raw) % 16)
    dec = AES.new(aes_key, AES.MODE_CBC, iv=b"\0" * 16).decrypt(raw).rstrip(b"\0")
    if not dec.startswith(b"MEGA"):
        raise MegaError("attributes decrypt nahi hue (key galat?)")
    return json.loads(dec[4:].decode("utf-8", "replace"))


async def _api(session: aiohttp.ClientSession, payload: list):
    params = {"id": random.randint(0, 0xFFFFFFFF)}
    for attempt in range(4):
        async with session.post(API_URL, params=params, data=json.dumps(payload),
                                timeout=aiohttp.ClientTimeout(total=30)) as r:
            data = await r.json(content_type=None)
        if isinstance(data, int):
            if data in (-3, -18) and attempt < 3:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            raise MegaError(_ERRORS.get(data, f"Mega API error {data}"))
        return data[0]
    raise MegaError("Mega API busy")


def _safe(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name or "").strip().strip(".")
    return name[:180] or f"mega_{int(time.time())}"


async def _call(cb, *a):
    if cb is None:
        return
    try:
        r = cb(*a)
        if asyncio.iscoroutine(r):
            await r
    except Exception:
        pass


async def download_public_file(url: str, dest_dir: str, progress=None, cancel=None) -> str:
    os.makedirs(dest_dir, exist_ok=True)
    file_id, key = parse_link(url)
    aes_key, nonce = _key_parts(key)

    async with aiohttp.ClientSession() as session:
        info = await _api(session, [{"a": "g", "g": 1, "p": file_id}])
        if isinstance(info, int):
            raise MegaError(_ERRORS.get(info, f"Mega API error {info}"))
        if "g" not in info:
            raise MegaError("Mega ne download URL nahi diya (link invalid ya quota?)")
        size = int(info["s"])
        name = _safe(decrypt_attrs(info["at"], aes_key).get("n"))
        dl_url = info["g"]
        path = os.path.join(dest_dir, name)
        LOGGER.info(f"[MegaPy] {name} | {size / 1048576:.1f} MB | parts={PARTS}")

        with open(path, "wb") as f:
            f.truncate(size)

        done = [0]
        t0 = time.time()
        stop = asyncio.Event()

        async def _fetch(start: int, end: int):
            """[start, end) — 16-byte aligned start; CTR counter = start // 16"""
            pos = start
            for attempt in range(5):
                if cancel is not None and cancel.is_set():
                    raise MegaError("cancelled")
                try:
                    cipher = AES.new(aes_key, AES.MODE_CTR, nonce=nonce, initial_value=pos // 16)
                    async with session.get(f"{dl_url}/{pos}-{end - 1}",
                                           timeout=aiohttp.ClientTimeout(total=None, sock_read=60)) as r:
                        if r.status == 509:
                            raise MegaError(_ERRORS[-17])
                        if r.status != 200:
                            raise aiohttp.ClientError(f"HTTP {r.status}")
                        with open(path, "r+b") as f:
                            f.seek(pos)
                            async for chunk in r.content.iter_chunked(CHUNK):
                                if cancel is not None and cancel.is_set():
                                    raise MegaError("cancelled")
                                # CTR counter ko partial chunk ke baad bhi sahi rakhne ke liye chunk-by-chunk decrypt
                                f.write(cipher.decrypt(chunk))
                                pos += len(chunk)
                                done[0] += len(chunk)
                    return
                except MegaError:
                    raise
                except Exception as e:
                    LOGGER.warning(f"[MegaPy] part {start}-{end} retry {attempt + 1}: {e!r}")
                    back = pos % 16                  # CTR resume 16-byte boundary se hi ho sakta hai
                    pos -= back
                    done[0] -= back
                    await asyncio.sleep(1.5 * (attempt + 1))
            raise MegaError("download baar-baar fail hua (network)")

        n = 1 if size < MIN_PART * 2 else max(1, min(PARTS, size // MIN_PART))
        seg = (size // n + 15) // 16 * 16                    # 16-byte aligned
        ranges = []
        s = 0
        while s < size:
            e = min(size, s + seg)
            ranges.append((s, e))
            s = e

        async def _reporter():
            while not stop.is_set():
                await asyncio.sleep(3)
                el = max(time.time() - t0, 0.1)
                await _call(progress, done[0], size, done[0] / el, name)

        rep = asyncio.ensure_future(_reporter())
        try:
            await asyncio.gather(*[_fetch(a, b) for a, b in ranges])
        except BaseException:
            try:
                os.remove(path)
            except Exception:
                pass
            raise
        finally:
            stop.set()
            rep.cancel()

    await _call(progress, size, size, size / max(time.time() - t0, 0.1), name)
    return path
