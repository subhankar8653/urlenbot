import asyncio
import json
import math
import os
import re
import subprocess
import time

from hachoir.metadata import extractMetadata
from hachoir.parser import createParser
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .. import LOGGER, download_dir, encode_dir
from .database.access_db import db
from .display_progress import TimeFormatter


def get_codec(filepath, channel='v:0'):
    try:
        output = subprocess.check_output(['ffprobe', '-v', 'error', '-select_streams', channel,
                                          '-show_entries', 'stream=codec_name,codec_tag_string', '-of',
                                          'default=nokey=1:noprint_wrappers=1', filepath])
        return output.decode('utf-8').split()
    except subprocess.CalledProcessError as e:
        LOGGER.error(f"ffprobe failed for {filepath}: {e}")
        return []
    except Exception as e:
        LOGGER.error(f"ffprobe exception for {filepath}: {e}")
        return []

def get_media_streams(filepath):
    try:
        cmd = ['ffprobe', '-hide_banner', '-print_format', 'json', '-show_streams', filepath]
        output = subprocess.check_output(cmd, stderr=subprocess.DEVNULL)
        return json.loads(output.decode('utf-8')).get('streams', [])
    except Exception as e:
        LOGGER.error(f"Failed to get media streams: {e}")
        return []


async def extract_subs(filepath, msg, user_id):
    path, extension = os.path.splitext(filepath)
    name = os.path.basename(path)
    check = get_codec(filepath, channel='s:0')
    if check == []:
        return None
    elif check == 'pgs':
        return None
    else:
        output = os.path.join(encode_dir, str(msg.id) + '.ass')

    try:
        subprocess.call(['ffmpeg', '-y', '-i', filepath, '-map', 's:0', output])
        try:
            subprocess.call(['mkvextract', 'attachments', filepath, '1', '2', '3', '4', '5', '6', '7', '8', '9', '10', '11', '12', '13', '14', '15', '16',
                            '17', '18', '19', '20', '21', '22', '23', '24', '25', '26', '27', '28', '29', '30', '31', '32', '33', '34', '35', '36', '37', '38', '39', '40'])
        except FileNotFoundError:
            LOGGER.warning("mkvextract not found, skipping attachments extraction.")
        except Exception as e:
            LOGGER.error(f"mkvextract failed: {e}")
        try:
            if os.name != 'nt':
                subprocess.run([f"mv -f *.TTF *.OTF *.ttf *.otf /usr/share/fonts/ && fc-cache -f"], shell=True)
        except Exception as e:
            LOGGER.warning(f"Font moving failed: {e}")
        return output
    except Exception as e:
        LOGGER.error(f"Extract subs failed: {e}")
        return None


async def encode(filepath, message, msg, audio_map=None):
    """
    Entry point. Decides between segmented parallel encoding (splits the
    video into N chunks, encodes them on all cores at once, then merges)
    and the normal single-pass encode — then falls back safely to
    single-pass if parallel mode isn't eligible or fails for any reason.

    Audio and subtitles are ALWAYS taken from the full original file in a
    single pass (never split/re-assembled), so they can never drift out of
    sync — only the video stream is chunked.
    """
    try:
        if await _should_parallel_encode(filepath, message, audio_map):
            result = await parallel_encode(filepath, message, msg, audio_map=audio_map)
            if result:
                return result
            LOGGER.warning("Parallel encode did not complete cleanly — falling back to single-pass encode.")
    except Exception as e:
        LOGGER.error(f"Parallel encode crashed, falling back to single-pass: {e}")

    return await _encode_single(filepath, message, msg, audio_map=audio_map)


async def _should_parallel_encode(filepath, message, audio_map):
    uid = message.from_user.id
    # Multi-audio-track selection and hardsub burn-in both need frame-accurate
    # timing across the whole file — keep those on the safe single-pass path.
    if audio_map:
        LOGGER.info("Parallel encode skipped: custom audio_map in use.")
        return False
    if not await db.get_parallel(uid):
        LOGGER.info("Parallel encode skipped: toggle is OFF in user settings.")
        return False
    if await db.get_hardsub(uid):
        LOGGER.info("Parallel encode skipped: hardsub is ON.")
        return False
    cpu_count = os.cpu_count() or 1
    if cpu_count < 2:
        LOGGER.info("Parallel encode skipped: only 1 CPU core available.")
        return False
    video_i = get_codec(filepath, channel='v:0')
    if video_i == []:
        LOGGER.info("Parallel encode skipped: could not detect a video stream.")
        return False
    duration = get_duration(filepath)
    # Not worth the split/merge overhead on short clips
    if duration < 240:
        LOGGER.info(f"Parallel encode skipped: duration {duration}s < 240s threshold.")
        return False
    return True


async def parallel_encode(filepath, message, msg, audio_map=None):
    uid = message.from_user.id
    ex = await db.get_extensions(uid)
    path, extension = os.path.splitext(filepath)
    name = os.path.basename(path)

    if ex == 'MP4':
        output_filepath = os.path.join(encode_dir, name + '.mp4')
    elif ex == 'AVI':
        output_filepath = os.path.join(encode_dir, name + '.avi')
    else:
        output_filepath = os.path.join(encode_dir, name + '.mkv')

    assert(output_filepath != filepath)

    duration = get_duration(filepath)
    cpu_count = os.cpu_count() or 2
    # Segments run all-at-once, in parallel — so segment count is bounded by
    # available CPU cores, not by video length. Capped at 8 so the host
    # doesn't split into pointlessly tiny chunks (split/merge overhead).
    n_segments = min(cpu_count, 8)
    if n_segments < 2 or duration < 1:
        return None

    seg_len = duration / n_segments
    starts = [round(i * seg_len, 2) for i in range(n_segments)]
    lengths = []
    for i in range(n_segments):
        if i == n_segments - 1:
            lengths.append(round(duration - starts[i], 2))
        else:
            lengths.append(round(starts[i + 1] - starts[i], 2))

    # ── Video options (same knobs as single-pass encode, video-only) ──────
    x265 = await db.get_hevc(uid)
    codec_flag = '-c:v libx265' if x265 else '-c:v libx264'
    tune = await db.get_tune(uid)
    tunevideo = '-tune animation' if tune else '-tune film'
    cbb = await db.get_cabac(uid)
    cabac = '-coder 1' if cbb else '-coder 0'
    rf = await db.get_reframe(uid)
    reframe = {'4': '-refs 4', '8': '-refs 8', '16': '-refs 16'}.get(rf, '')
    b = await db.get_bits(uid)
    codec_flag += ' -pix_fmt yuv420p10le' if b else ' -pix_fmt yuv420p'

    crf = await db.get_crf(uid)
    if not crf:
        await db.set_crf(uid, crf=26)
        crf = 26
    Crf = f'-crf {crf}'

    fr = await db.get_frame(uid)
    frame = {
        'ntsc': '-r ntsc', 'pal': '-r pal', 'film': '-r film',
        '23.976': '-r 24000/1001', '30': '-r 30', '60': '-r 60'
    }.get(fr, '')

    ap = await db.get_aspect(uid)
    aspect = '-aspect 16:9' if ap else ''

    p = await db.get_preset(uid)
    preset = {
        'uf': '-preset ultrafast', 'sf': '-preset superfast',
        'vf': '-preset veryfast',  'f':  '-preset fast',
        'm':  '-preset medium'
    }.get(p, '-preset slow')

    video_opts = '-profile:v main' if x265 else f'{cabac} {reframe} -profile:v main'

    r = await db.get_resolution(uid)
    watermark = {'1080': '-vf scale=1920:1080', '720': '-vf scale=1280:720',
                 '576': '-vf scale=768:576', '480': '-vf scale=852:480'}.get(r, '')
    w = await db.get_watermark(uid)
    if w:
        watermark += (',subtitles=VideoEncoder/utils/extras/watermark.ass'
                      if watermark else '-vf subtitles=VideoEncoder/utils/extras/watermark.ass')

    # Split CPU budget fairly across the segments running at the same time
    threads_per_seg = max(1, cpu_count // n_segments)
    x265_speed_params = []
    if x265:
        params = (
            f"pools={threads_per_seg}:frame-threads={min(threads_per_seg, 4)}:"
            f"wpp=1:pmode=1:pme=1:rc-lookahead=10:b-adapt=0:ref=1"
        )
        x265_speed_params = ['-x265-params', params]

    seg_paths = [os.path.join(encode_dir, f"{msg.id}_seg{i}.mkv") for i in range(n_segments)]
    progress_paths = [os.path.join(download_dir, f"process_{msg.id}_{i}.txt") for i in range(n_segments)]
    cleanup_paths = list(seg_paths) + list(progress_paths)
    for pp in progress_paths:
        with open(pp, 'w'):
            pass

    def _cleanup(extra=None):
        for pth in cleanup_paths + (extra or []):
            try:
                os.remove(pth)
            except Exception:
                pass

    seg_cmds = []
    for i in range(n_segments):
        cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-progress', progress_paths[i],
               '-y', '-ss', str(starts[i]), '-i', filepath, '-t', str(lengths[i]), '-map', '0:v:0']
        cmd += codec_flag.split() + preset.split()
        if x265_speed_params:
            cmd += x265_speed_params
        cmd += (frame.split() + tunevideo.split() + aspect.split() + video_opts.split() +
                Crf.split() + watermark.split() + ['-threads', str(threads_per_seg), seg_paths[i]])
        seg_cmds.append(cmd)

    LOGGER.info(f"Parallel encode: {n_segments} segments x {threads_per_seg} threads each")

    # Confirmation line so it's obvious in chat that parallel mode kicked in
    # (this fires immediately, before the first 5s progress tick)
    try:
        await msg.edit(
            f"⚡ <b>Parallel Encode:</b> splitting into {n_segments} segments "
            f"({threads_per_seg} threads each)...\nAudio/Subtitles: full-length, single pass."
        )
    except Exception:
        pass

    procs = []
    for cmd in seg_cmds:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        procs.append(proc)

    # ── Audio: always full-length, single pass, run alongside the video
    #    segments so it costs no extra wall time and can never desync ──────
    sr = await db.get_samplerate(uid)
    sample = {'44.1K': '-ar 44100', '48K': '-ar 48000'}.get(sr, '')
    bit = await db.get_bitrate(uid)
    bitrate = {'400': '-b:a 400k', '320': '-b:a 320k', '256': '-b:a 256k',
               '224': '-b:a 224k', '192': '-b:a 192k', '160': '-b:a 160k',
               '128': '-b:a 128k'}.get(bit, '')
    a = await db.get_audio(uid)
    a_i = get_codec(filepath, channel='a:0')
    audio_proc = None
    audio_temp = None
    use_copy_audio = True
    if a_i != []:
        audio_codec_opts = {
            'dd':     f'-c:a ac3 {sample} {bitrate}',
            'aac':    f'-c:a aac {sample} {bitrate}',
            'vorbis': f'-c:a libvorbis {sample} {bitrate}',
            'alac':   f'-c:a alac {sample} {bitrate}',
            'opus':   f'-c:a libopus -vbr on {sample} {bitrate}',
        }.get(a, '-c:a copy')
        c = await db.get_channels(uid)
        channels = ''
        if '-c:a copy' not in audio_codec_opts:
            channels = {
                '1.0': '-rematrix_maxval 1.0 -ac 1', '2.0': '-rematrix_maxval 1.0 -ac 2',
                '2.1': '-rematrix_maxval 1.0 -ac 3', '5.1': '-rematrix_maxval 1.0 -ac 6',
                '7.1': '-rematrix_maxval 1.0 -ac 8',
            }.get(c, '')
        if audio_codec_opts.strip() != '-c:a copy':
            use_copy_audio = False
            audio_temp = os.path.join(encode_dir, f"{msg.id}_audio.mka")
            cleanup_paths.append(audio_temp)
            audio_cmd = (['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-i', filepath,
                         '-vn', '-sn'] + audio_codec_opts.split() + channels.split() +
                         ['-map', '0:a?', audio_temp])
            audio_proc = await asyncio.create_subprocess_exec(
                *audio_cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
            )
    else:
        use_copy_audio = False  # nothing to map

    ok = await _monitor_parallel_progress(procs, progress_paths, lengths, msg, message)
    if not ok:
        if audio_proc:
            try:
                audio_proc.kill()
            except Exception:
                pass
        _cleanup()
        return None

    if audio_proc:
        _, aerr = await audio_proc.communicate()
        if audio_proc.returncode != 0 or not os.path.isfile(audio_temp) or os.path.getsize(audio_temp) == 0:
            LOGGER.error(f"Parallel audio pass failed, falling back: {aerr.decode(errors='ignore')[:500]}")
            _cleanup()
            return None

    for sp in seg_paths:
        if not os.path.isfile(sp) or os.path.getsize(sp) == 0:
            LOGGER.error(f"Segment missing/empty: {sp}, falling back to single-pass.")
            _cleanup()
            return None

    # ── Concat segments — lossless stream copy since every segment shares
    #    identical encoder settings/pix_fmt/resolution ─────────────────────
    concat_list = os.path.join(encode_dir, f"{msg.id}_concat.txt")
    cleanup_paths.append(concat_list)
    with open(concat_list, 'w') as f:
        for sp in seg_paths:
            f.write(f"file '{os.path.abspath(sp)}'\n")

    merged_video = os.path.join(encode_dir, f"{msg.id}_merged.mkv")
    cleanup_paths.append(merged_video)
    concat_cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-f', 'concat',
                  '-safe', '0', '-i', concat_list, '-c', 'copy', merged_video]
    proc = await asyncio.create_subprocess_exec(
        *concat_cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    _, cerr = await proc.communicate()
    if proc.returncode != 0 or not os.path.isfile(merged_video) or os.path.getsize(merged_video) == 0:
        LOGGER.error(f"Segment concat failed, falling back: {cerr.decode(errors='ignore')[:500]}")
        _cleanup()
        return None

    # ── Final mux: merged video + full-length audio + full-length subs ────
    if a_i == []:
        mux_inputs = ['-i', filepath]
        orig_idx = 1
    elif use_copy_audio:
        mux_inputs = ['-i', filepath]
        orig_idx = 1
    else:
        mux_inputs = ['-i', audio_temp, '-i', filepath]
        orig_idx = 2

    h = await db.get_hardsub(uid)  # guaranteed False here, kept for parity/safety
    s = await db.get_subtitles(uid)
    subs_i = get_codec(filepath, channel='s:0')
    subtitles = ''
    if subs_i != [] and s and not h:
        if ex == 'MP4':
            subtitles = f'-c:s mov_text -c:t copy -map {orig_idx}:t? -map {orig_idx}:s?'
        elif ex != 'AVI':
            subtitles = f'-c:s copy -c:t copy -map {orig_idx}:t? -map {orig_idx}:s?'

    full_meta = await db.get_full_metadata(uid)
    metadata_args = ['-map_chapters', str(orig_idx), '-map_metadata', str(orig_idx)]
    if full_meta.get('enabled'):
        if full_meta.get('clear_metadata'):
            metadata_args += ['-map_metadata', '-1']
        if full_meta.get('comment'):
            metadata_args += ['-metadata', f'comment={full_meta["comment"]}']
        vtitle = full_meta.get('video_title', '')
        if vtitle:
            metadata_args += ['-metadata:s:v', f'title={vtitle}']
        atitle = full_meta.get('audio_title', '')
        if atitle:
            metadata_args += ['-metadata:s:a', f'title={atitle}']
        stitle = full_meta.get('subtitle_title', '')
        if stitle:
            metadata_args += ['-metadata:s:s', f'title={stitle}']
        if full_meta.get('strip_attachments'):
            metadata_args += ['-map', f'-{orig_idx}:t']
    else:
        m = await db.get_metadata_w(uid)
        if m:
            metadata_args += [
                '-metadata', 'title=SuhaniBots',
                '-metadata:s:v', 'title=SuhaniBots',
                '-metadata:s:a', 'title=SuhaniBots',
            ]

    mux_cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-i', merged_video] + mux_inputs
    mux_cmd += ['-map', '0:v:0']
    if a_i != []:
        if use_copy_audio:
            mux_cmd += ['-map', f'{orig_idx}:a?', '-c:a', 'copy']
        else:
            mux_cmd += ['-map', '1:a?', '-c:a', 'copy']
    mux_cmd += ['-c:v', 'copy'] + subtitles.split() + metadata_args + [output_filepath]

    proc = await asyncio.create_subprocess_exec(
        *mux_cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    _, merr = await proc.communicate()
    _cleanup()

    if proc.returncode != 0 or not os.path.isfile(output_filepath) or os.path.getsize(output_filepath) == 0:
        LOGGER.error(f"Final mux failed: {merr.decode(errors='ignore')[:500]}")
        if os.path.isfile(output_filepath):
            try:
                os.remove(output_filepath)
            except Exception:
                pass
        return None

    return output_filepath


async def _monitor_parallel_progress(procs, progress_paths, lengths, msg, message):
    status = download_dir + "status.json"
    with open(status, 'w') as f:
        json.dump({'running': True, 'message': msg.id, 'user': message.from_user.id,
                   'pids': [p.pid for p in procs]}, f)

    total_len = sum(lengths) or 1
    wait_tasks = [asyncio.create_task(p.wait()) for p in procs]

    while not all(t.done() for t in wait_tasks):
        await asyncio.sleep(5)
        elapsed_sum = 0.0
        speeds = []
        for i, pp in enumerate(progress_paths):
            try:
                with open(pp, 'r') as f:
                    text = f.read()
            except Exception:
                continue
            time_in_us = re.findall(r"out_time_ms=(\d+)", text)
            speed = re.findall(r"speed=(\d+\.?\d*)", text)
            prog_tag = re.findall(r"progress=(\w+)", text)
            seg_elapsed = int(time_in_us[-1]) / 1000000 if time_in_us else 0
            if prog_tag and prog_tag[-1] == "end":
                seg_elapsed = lengths[i]
            elapsed_sum += min(seg_elapsed, lengths[i])
            if speed:
                speeds.append(float(speed[-1]))

        pct = min(math.floor(elapsed_sum * 100 / total_len), 100)
        avg_speed = sum(speeds) / len(speeds) if speeds else 0.5
        remaining = max(total_len - elapsed_sum, 0)
        eta_secs = math.floor(remaining / avg_speed) if avg_speed > 0 else 0
        ETA = TimeFormatter(eta_secs) if eta_secs > 0 else "Almost done!"

        bar = '█' * math.floor(pct / 10) + '░' * (10 - math.floor(pct / 10))
        done_segments = sum(1 for t in wait_tasks if t.done())
        stats = (
            f"<b>⚡ Parallel Encoding:</b> {pct}%\n"
            f"{bar}\n"
            f"• Segments done: {done_segments}/{len(procs)}\n"
            f"• ETA: {ETA}  • Speed: {avg_speed:.2f}x"
        )
        try:
            await msg.edit(
                text=stats,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton('Cancel', callback_data='cancel'),
                    InlineKeyboardButton('Stats',  callback_data='stats')
                ]])
            )
        except Exception:
            pass

        try:
            with open(status, 'r') as sf:
                if not json.load(sf).get('running', True):
                    for p in procs:
                        try:
                            p.kill()
                        except Exception:
                            pass
                    await msg.edit("🚦 Encoding Cancelled!")
                    return False
        except Exception:
            pass

    returncodes = [t.result() for t in wait_tasks]
    fail = any(rc != 0 for rc in returncodes)
    if fail:
        for i, p in enumerate(procs):
            if returncodes[i] != 0:
                try:
                    _, err = await p.communicate()
                    LOGGER.error(f"Segment {i} ffmpeg failed (code {returncodes[i]}): {err.decode(errors='ignore')[:500]}")
                except Exception:
                    pass
    return not fail


async def _encode_single(filepath, message, msg, audio_map=None):

    ex = await db.get_extensions(message.from_user.id)
    path, extension = os.path.splitext(filepath)
    name = os.path.basename(path)

    if ex == 'MP4':
        output_filepath = os.path.join(encode_dir, name + '.mp4')
    elif ex == 'AVI':
        output_filepath = os.path.join(encode_dir, name + '.avi')
    else:
        output_filepath = os.path.join(encode_dir, name + '.mkv')

    subtitles_path = os.path.join(encode_dir, str(msg.id) + '.ass')
    progress = os.path.join(download_dir, "process.txt")
    with open(progress, 'w') as f:
        pass

    assert(output_filepath != filepath)

    if os.path.isfile(output_filepath):
        LOGGER.warning(f'"{output_filepath}": already exists')
    else:
        LOGGER.info(filepath)

    # Railway pe kitne vCPU milte hain
    cpu_count = os.cpu_count() or 2

    # HEVC / H264
    x265 = await db.get_hevc(message.from_user.id)
    video_i = get_codec(filepath, channel='v:0')
    if video_i == []:
        codec_flag = ''
    else:
        codec_flag = '-c:v libx265' if x265 else '-c:v libx264'

    # Tune
    tune = await db.get_tune(message.from_user.id)
    tunevideo = '-tune animation' if tune else '-tune film'

    # CABAC
    cbb = await db.get_cabac(message.from_user.id)
    cabac = '-coder 1' if cbb else '-coder 0'

    # Reframe
    rf = await db.get_reframe(message.from_user.id)
    reframe = {'4': '-refs 4', '8': '-refs 8', '16': '-refs 16'}.get(rf, '')

    # Bits
    b = await db.get_bits(message.from_user.id)
    codec_flag += ' -pix_fmt yuv420p10le' if b else ' -pix_fmt yuv420p'

    # CRF
    crf = await db.get_crf(message.from_user.id)
    if crf:
        Crf = f'-crf {crf}'
    else:
        await db.set_crf(message.from_user.id, crf=26)
        Crf = '-crf 26'

    # Frame rate
    fr = await db.get_frame(message.from_user.id)
    frame = {
        'ntsc': '-r ntsc', 'pal': '-r pal', 'film': '-r film',
        '23.976': '-r 24000/1001', '30': '-r 30', '60': '-r 60'
    }.get(fr, '')

    # Aspect
    ap = await db.get_aspect(message.from_user.id)
    aspect = '-aspect 16:9' if ap else ''

    # Preset
    p = await db.get_preset(message.from_user.id)
    preset = {
        'uf': '-preset ultrafast', 'sf': '-preset superfast',
        'vf': '-preset veryfast',  'f':  '-preset fast',
        'm':  '-preset medium'
    }.get(p, '-preset slow')

    # Video opts
    if x265:
        video_opts = '-profile:v main -map 0:v? -map_chapters 0 -map_metadata 0'
    else:
        video_opts = f'{cabac} {reframe} -profile:v main -map 0:v? -map_chapters 0 -map_metadata 0'

    # Metadata — Mirror Leech style (list banao, string nahi — spaces safe rahein)
    full_meta = await db.get_full_metadata(message.from_user.id)
    metadata_args = []
    if full_meta.get('enabled'):
        if full_meta.get('clear_metadata'):
            metadata_args += ['-map_metadata', '-1']
        if full_meta.get('comment'):
            metadata_args += ['-metadata', f'comment={full_meta["comment"]}']
        vtitle = full_meta.get('video_title', '')
        if vtitle:
            metadata_args += ['-metadata:s:v', f'title={vtitle}']
        atitle = full_meta.get('audio_title', '')
        if atitle:
            metadata_args += ['-metadata:s:a', f'title={atitle}']
        stitle = full_meta.get('subtitle_title', '')
        if stitle:
            metadata_args += ['-metadata:s:s', f'title={stitle}']
        if full_meta.get('strip_attachments'):
            metadata_args += ['-map', '-0:t']
    else:
        # Fallback: purani simple metadata setting
        m = await db.get_metadata_w(message.from_user.id)
        if m:
            metadata_args = [
                '-metadata', 'title=SuhaniBots',
                '-metadata:s:v', 'title=SuhaniBots',
                '-metadata:s:a', 'title=SuhaniBots',
            ]
    # Subtitles
    h = await db.get_hardsub(message.from_user.id)
    s = await db.get_subtitles(message.from_user.id)
    subs_i = get_codec(filepath, channel='s:0')
    subtitles = ''
    if subs_i != [] and s and not h:
        if ex == 'MP4':
            subtitles = '-c:s mov_text -c:t copy -map 0:t? -map 0:s?'
        elif ex != 'AVI':
            subtitles = '-c:s copy -c:t copy -map 0:t? -map 0:s?'

    # Resolution + Watermark
    r = await db.get_resolution(message.from_user.id)
    w = await db.get_watermark(message.from_user.id)
    watermark = {'1080': '-vf scale=1920:1080', '720': '-vf scale=1280:720',
                 '576': '-vf scale=768:576', '480': '-vf scale=852:480'}.get(r, '')
    if w:
        watermark += (',subtitles=VideoEncoder/utils/extras/watermark.ass'
                      if watermark else '-vf subtitles=VideoEncoder/utils/extras/watermark.ass')
    if h:
        watermark += (f',subtitles={subtitles_path}'
                      if watermark else f'-vf subtitles={subtitles_path}')

    # Audio
    sr = await db.get_samplerate(message.from_user.id)
    sample = {'44.1K': '-ar 44100', '48K': '-ar 48000'}.get(sr, '')
    bit = await db.get_bitrate(message.from_user.id)
    bitrate = {'400': '-b:a 400k', '320': '-b:a 320k', '256': '-b:a 256k',
               '224': '-b:a 224k', '192': '-b:a 192k', '160': '-b:a 160k',
               '128': '-b:a 128k'}.get(bit, '')
    a = await db.get_audio(message.from_user.id)
    a_i = get_codec(filepath, channel='a:0')
    audio_opts = ''
    if a_i != []:
        audio_opts = {
            'dd':     f'-c:a ac3 {sample} {bitrate}',
            'aac':    f'-c:a aac {sample} {bitrate}',
            'vorbis': f'-c:a libvorbis {sample} {bitrate}',
            'alac':   f'-c:a alac {sample} {bitrate}',
            'opus':   f'-c:a libopus -vbr on {sample} {bitrate}',
        }.get(a, '-c:a copy')
        if audio_map:
            map_opts = ''.join(f' -map 0:{idx}' for idx in audio_map)
            audio_opts = f'{audio_opts}{map_opts} -disposition:a:0 default'
        else:
            audio_opts += ' -map 0:a?'

    c = await db.get_channels(message.from_user.id)
    channels = ''
    if '-c:a copy' not in audio_opts:
        channels = {
            '1.0': '-rematrix_maxval 1.0 -ac 1', '2.0': '-rematrix_maxval 1.0 -ac 2',
            '2.1': '-rematrix_maxval 1.0 -ac 3', '5.1': '-rematrix_maxval 1.0 -ac 6',
            '7.1': '-rematrix_maxval 1.0 -ac 8',
        }.get(c, '')

    # ── x265 CPU speed params ─────────────────────────────────────────────────
    # Railway free = shared CPU, isliye jo bhi cores milein sab use karo
    # x265-params se encoder ke andar parallel processing on karo
    x265_speed_params = []
    if x265:
        params = (
            f"pools={cpu_count}:"       # worker thread pools
            f"frame-threads={min(cpu_count, 4)}:"  # parallel frame encoding
            f"wpp=1:"                   # Wavefront Parallel Processing
            f"pmode=1:"                 # parallel mode decision
            f"pme=1:"                   # parallel motion estimation
            f"rc-lookahead=10:"         # default 40 → 10, speed +25%
            f"b-adapt=0:"               # B-frame adapt off, speed boost
            f"ref=1"                    # 1 reference frame (superfast pe waise bhi 1 hota)
        )
        x265_speed_params = ['-x265-params', params]

    # ── Final command ─────────────────────────────────────────────────────────
    command = [
        'ffmpeg', '-hide_banner', '-loglevel', 'error',
        '-progress', progress,
        '-y', '-i', filepath,
    ]
    command.extend(codec_flag.split())
    command.extend(preset.split())
    if x265_speed_params:
        command.extend(x265_speed_params)
    command.extend(
        frame.split() + tunevideo.split() + aspect.split() +
        video_opts.split() + Crf.split() + watermark.split() +
        metadata_args + subtitles.split() +
        audio_opts.split() + channels.split() +
        ['-threads', str(cpu_count)]
    )
    command.append(output_filepath)

    LOGGER.info(f"FFmpeg: {' '.join(command)}")

    proc = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    await handle_progress(proc, msg, message, filepath)
    stdout, stderr = await proc.communicate()

    e_response = stderr.decode().strip()
    if e_response:
        LOGGER.error(f"FFmpeg stderr: {e_response}")

    if not os.path.isfile(output_filepath) or os.path.getsize(output_filepath) == 0:
        LOGGER.error(f"Encoding failed: output not created or 0 bytes.")
        if os.path.isfile(output_filepath):
            os.remove(output_filepath)
        return None

    return output_filepath


def get_thumbnail(in_filename, path, ttl):
    out_filename = os.path.join(path, str(time.time()) + ".jpg")
    try:
        subprocess.run([
            'ffmpeg', '-hide_banner', '-loglevel', 'error',
            '-ss', str(ttl), '-i', in_filename, '-vframes', '1', '-y', out_filename
        ], check=True, capture_output=True)
        return out_filename if os.path.isfile(out_filename) else None
    except Exception as e:
        LOGGER.warning(f"Thumbnail failed: {e}")
        return None


def get_duration(filepath):
    try:
        output = subprocess.check_output([
            'ffprobe', '-v', 'error', '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1', filepath
        ]).decode('utf-8').strip()
        return int(float(output))
    except Exception:
        try:
            metadata = extractMetadata(createParser(filepath))
            if metadata and metadata.has("duration"):
                return metadata.get('duration').seconds
        except Exception:
            pass
    return 0


def get_width_height(filepath):
    try:
        output = subprocess.check_output([
            'ffprobe', '-v', 'error', '-select_streams', 'v:0',
            '-show_entries', 'stream=width,height', '-of', 'csv=s=x:p=0', filepath
        ]).decode('utf-8').strip()
        width, height = map(int, output.split('x'))
        return width, height
    except Exception:
        try:
            metadata = extractMetadata(createParser(filepath))
            if metadata and metadata.has("width") and metadata.has("height"):
                return metadata.get("width"), metadata.get("height")
        except Exception:
            pass
    return (1280, 720)


async def media_info(saved_file_path):
    process = subprocess.Popen(
        ['ffmpeg', '-hide_banner', '-i', saved_file_path],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    stdout, _ = process.communicate()
    output = stdout.decode().strip()
    duration = re.search(r"Duration:\s*(\d*):(\d*):(\d+\.?\d*)[\s\w*$]", output)
    bitrates = re.search(r"bitrate:\s*(\d+)[\s\w*$]", output)
    total_seconds = None
    if duration:
        total_seconds = (int(duration.group(1)) * 3600 +
                         int(duration.group(2)) * 60 +
                         math.floor(float(duration.group(3))))
    bitrate = bitrates.group(1) if bitrates else None
    return total_seconds, bitrate


async def handle_progress(proc, msg, message, filepath):
    COMPRESSION_START_TIME = time.time()
    LOGGER.info("ffmpeg_process: " + str(proc.pid))
    status = download_dir + "status.json"
    with open(status, 'w') as f:
        json.dump({'running': True, 'message': msg.id, 'user': message.from_user.id}, f)
    with open(status, 'r+') as f:
        st = json.load(f)
        st['pid'] = proc.pid
        f.seek(0)
        json.dump(st, f, indent=2)

    while proc.returncode is None:
        await asyncio.sleep(5)
        try:
            with open(download_dir + 'process.txt', 'r') as file:
                text = file.read()
        except Exception:
            continue

        frame      = re.findall(r"frame=(\d+)", text)
        time_in_us = re.findall(r"out_time_ms=(\d+)", text)
        prog_tag   = re.findall(r"progress=(\w+)", text)
        speed      = re.findall(r"speed=(\d+\.?\d*)", text)

        if prog_tag and prog_tag[-1] == "end":
            break

        speed_val    = float(speed[-1]) if speed else 0.5
        elapsed_time = int(time_in_us[-1]) / 1000000 if time_in_us else 0
        total_time, _ = await media_info(filepath)

        if total_time and total_time > 0 and speed_val > 0:
            diff = math.floor((total_time - elapsed_time) / speed_val)
            ETA  = TimeFormatter(diff) if diff > 0 else "Almost done!"
            pct  = min(math.floor(elapsed_time * 100 / total_time), 100)
        else:
            ETA, pct = "-", 0

        bar = '█' * math.floor(pct / 10) + '░' * (10 - math.floor(pct / 10))
        stats = (
            f"<b>Encoding:</b> {pct}%\n"
            f"{bar}\n"
            f"• ETA: {ETA}  • Speed: {speed_val:.2f}x"
        )

        try:
            await msg.edit(
                text=stats,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton('Cancel', callback_data='cancel'),
                    InlineKeyboardButton('Stats',  callback_data='stats')
                ]])
            )
        except Exception:
            pass

        # Cancel check
        try:
            with open(status, 'r') as sf:
                if not json.load(sf).get('running', True):
                    proc.kill()
                    await msg.edit("🚦 Encoding Cancelled!")
                    return
        except Exception:
            pass
