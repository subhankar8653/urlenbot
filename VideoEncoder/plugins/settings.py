

from pyrogram import Client, filters
from pyrogram.types import Message

from .. import all
from ..utils.database.access_db import db
from ..utils.database.add_user import AddUserToDatabase
from ..utils.helper import check_chat, output
from ..utils.settings import OpenSettings


@Client.on_message(filters.command("reset"))
async def reset(bot: Client, update: Message):
    c = await check_chat(update, chat='Both')
    if not c:
        return
    await db.delete_user(update.from_user.id)
    await db.add_user(update.from_user.id)
    await update.reply(text="Settings reset successfully", reply_markup=output)


@Client.on_message(filters.command("settings"))
async def settings_handler(bot: Client, event: Message):
    c = await check_chat(event, chat='Both')
    if not c:
        return
    await AddUserToDatabase(bot, event)
    editable = await event.reply_text("Please Wait ...")
    await OpenSettings(editable, user_id=event.from_user.id)


@Client.on_message(filters.command("vset"))
async def settings_viewer(bot: Client, event: Message):
    c = await check_chat(event, chat='Both')
    if c is None:
        return
    await AddUserToDatabase(bot, event)
    # User ID
    if event.reply_to_message:
        user_id = event.reply_to_message.from_user.id
    elif not event.reply_to_message and len(event.command) == 1:
        user_id = event.from_user.id
    elif not event.reply_to_message and len(event.command) != 1:
        user_id = event.text.split(None, 1)[1]
    else:
        return
    
    # Reframe
    rf = await db.get_reframe(user_id)
    if rf == '4':
        reframe = '4'
    elif rf == '8':
        reframe = '8'
    elif rf == '16':
        reframe = '16'
    else:
        reframe = 'Pass'
    
    # Frame
    fr = await db.get_frame(user_id)
    if fr == 'ntsc':
        frame = 'NTSC'
    elif fr == 'pal':
        frame = 'PAL'
    elif fr == 'film':
        frame = 'FILM'
    elif fr == '23.976':
        frame = '23.976'    
    elif fr == '30':
        frame = '30'
    elif fr == '60':
        frame = '60'
    else:
        frame = 'Source'
    
    # Preset, CRF and Resolution
    p = await db.get_preset(user_id)
    if p == 'uf':
        pre = '𝚄𝚕𝚝𝚛𝚊𝙵𝚊𝚜𝚝'
    elif p == 'sf':
        pre = '𝚂𝚞𝚙𝚎𝚛𝙵𝚊𝚜𝚝'
    elif p == 'vf':
        pre = '𝚅𝚎𝚛𝚢𝙵𝚊𝚜𝚝'
    elif p == 'f':
        pre = '𝙵𝚊𝚜𝚝'
    elif p == 'm':
        pre = '𝙼𝚎𝚍𝚒𝚞𝚖'
    elif p == 's':
        pre = '𝚂𝚕𝚘𝚠'
    elif p == 'sl':
        pre = '𝚂𝚕𝚘𝚠𝚎𝚛'
    elif p == 'vs':
        pre = '𝚅𝚎𝚛𝚢𝚂𝚕𝚘𝚠'
    else:
        pre = 'None'

    crf = await db.get_crf(user_id)

    r = await db.get_resolution(user_id)
    if r == 'OG':
        res = 'Source'
    elif r == '1080':
        res = '𝟷𝟶𝟾𝟶𝙿'
    elif r == '720':
        res = '𝟽𝟸𝟶𝙿'
    elif r == '480':
        res = '𝟺𝟾𝟶𝙿'
    elif r == '540':
        res = '𝟻𝟺𝟶𝙿'
    elif r == '360':
        res = '𝟹𝟼𝟶𝙿'
    elif r == '240':
        res = '𝟸𝟺𝟶𝙿'
    elif r == '1440':
        res = '𝟮𝗞'
    else:
        res = '𝟰𝗞'
    
    # Extension
    ex = await db.get_extensions(user_id)
    if ex == 'MP4':
        extensions = 'MP4'
    elif ex == 'MKV':
        extensions = 'MKV'
    else:
        extensions = 'AVI'
    
    # Audio
    a = await db.get_audio(user_id)
    if a == 'dd':
        audio = 'AC3'
    elif a == 'aac':
        audio = 'AAC'
    elif a == 'vorbis':
        audio = 'VORBIS'
    elif a == 'alac':
        audio = 'ALAC'    
    elif a == 'opus':
        audio = 'OPUS'
    else:
        audio = 'Source'
    
    bit = await db.get_bitrate(user_id)
    if bit == '400':
        bitrate = '400K'
    elif bit == '320':
        bitrate = '320K'
    elif bit == '256':
        bitrate = '256K'
    elif bit == '224':
        bitrate = '224K'
    elif bit == '192':
        bitrate = '192K'
    elif bit == '160':
        bitrate = '160K'
    elif bit == '128':
        bitrate = '128K'
    else:
        bitrate = 'Source'    
    
    sr = await db.get_samplerate(user_id)
    if sr == '44.1K':
        sample = '44.1kHz'
    elif sr == '48K':
        sample = '48kHz'
    else:
        sample = 'Source'    

    c = await db.get_channels(user_id)
    if c == '1.0':
        channels = 'Mono'
    elif c == '2.0':
        channels = 'Stereo'
    elif c == '2.1':
        channels = '2.1'    
    elif c == '5.1':
        channels = '5.1'
    elif c == '7.1':
        channels = '7.1'  
    else:
        channels = 'Source'    
    
    m = await db.get_metadata_w(user_id)
    if m:
        metadata = 'sbanime'
    else:
        metadata = 'change session!'
    
    # Reply Text
    vset = f'''<b>Encode Settings:</b>

<b>📹 Video Settings</b>
Format : {extensions}
Quality: {res}
Codec: {'H265' if ((await db.get_hevc(user_id)) is True) else 'H264'}
Aspect: {'16:9' if ((await db.get_aspect(user_id)) is True) else 'Source'}
Reframe: {reframe} | FPS: {frame}
Tune: {'Animation' if ((await db.get_tune(user_id)) is True) else 'Film'}
Preset: {pre}
Bits: {'10' if ((await db.get_bits(user_id)) is True) else '8'} | CRF: {crf}
CABAC: {'☑️' if ((await db.get_cabac(user_id)) is True) else ''}

<b>📜 Subtitles Settings</b>
Hardsub {'☑️' if ((await db.get_hardsub(user_id)) is True) else ''} | Softsub {'☑️' if ((await db.get_subtitles(user_id)) is True) else ''}

<b>©️ Watermark Settings</b>
Metadata: {metadata}
Video {'☑️' if ((await db.get_watermark(user_id)) is True) else ''}

<b>🔊 Audio Settings</b>
Codec: {audio}
Sample Rate : {sample}
Bit Rate: {bitrate}
Channels: {channels}
'''
    await event.reply_text(vset)
