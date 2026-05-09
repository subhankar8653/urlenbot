"""
Auto Caption Generator
Filename se anime info detect karke caption banata hai
Format: AnimeName S02E03 in Hindi 1080p [@SBANIME].mp4
"""
import re
import subprocess
import json
import os


LANG_MAP = {
    'hin': 'Hindi', 'jpn': 'Japanese', 'eng': 'English',
    'tel': 'Telugu', 'tam': 'Tamil', 'ben': 'Bengali',
    'mal': 'Malayalam', 'kan': 'Kannada', 'mar': 'Marathi',
    'por': 'Portuguese', 'spa': 'Spanish', 'fre': 'French',
    'ger': 'German', 'kor': 'Korean', 'chi': 'Chinese',
    'ara': 'Arabic', 'rus': 'Russian', 'dut': 'Dutch',
    'ita': 'Italian', 'zho': 'Chinese', 'ind': 'Indonesian',
}

# Filename brackets ke andar language words
LANG_WORDS = {
    'hindi': 'Hindi', 'japanese': 'Japanese', 'english': 'English',
    'telugu': 'Telugu', 'tamil': 'Tamil', 'bengali': 'Bengali',
    'malayalam': 'Malayalam', 'kannada': 'Kannada', 'marathi': 'Marathi',
    'portuguese': 'Portuguese', 'spanish': 'Spanish', 'french': 'French',
    'german': 'German', 'korean': 'Korean', 'chinese': 'Chinese',
    'arabic': 'Arabic', 'russian': 'Russian', 'dutch': 'Dutch',
    'italian': 'Italian', 'indonesian': 'Indonesian',
    'dual audio': 'Dual Audio', 'dual': 'Dual Audio',
    'multi audio': 'Multi Audio', 'multi': 'Multi Audio',
    'hin': 'Hindi', 'jpn': 'Japanese', 'eng': 'English',
}

QUALITY_MAP = {
    '3840': '2160p', '1920': '1080p', '1280': '720p',
    '854': '480p', '848': '480p', '640': '360p',
}


def get_media_metadata(filepath):
    """FFprobe se file ka metadata nikalo"""
    try:
        cmd = [
            'ffprobe', '-v', 'quiet',
            '-print_format', 'json',
            '-show_streams', '-show_format',
            filepath
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return json.loads(result.stdout)
    except Exception:
        return {}


def detect_language_from_filename(filename):
    """
    Filename ke brackets se language detect karo.
    Example: 'Naruto S01E01 [Hindi].mkv'  -> ['Hindi']
    Example: 'One Piece (Dual Audio).mkv' -> ['Dual Audio']
    """
    langs = []
    name = os.path.splitext(os.path.basename(filename))[0]

    # [] aur () ke andar content nikalo
    bracket_contents = re.findall(r'[\[\(]([^\]\)]+)[\]\)]', name)
    for content in bracket_contents:
        content_lower = content.strip().lower()
        # Full phrase match pehle (e.g. "dual audio")
        if content_lower in LANG_WORDS:
            lang_name = LANG_WORDS[content_lower]
            if lang_name not in langs:
                langs.append(lang_name)
        else:
            # Word by word check
            for word, lang_name in LANG_WORDS.items():
                if word in content_lower.split():
                    if lang_name not in langs:
                        langs.append(lang_name)

    return langs


def detect_language_from_metadata(metadata):
    """Audio stream se language detect karo"""
    streams = metadata.get('streams', [])
    langs = []
    for s in streams:
        if s.get('codec_type') == 'audio':
            lang = s.get('tags', {}).get('language', '')
            if lang and lang in LANG_MAP:
                name = LANG_MAP[lang]
                if name not in langs:
                    langs.append(name)
    return langs


def detect_language(filename, metadata):
    """
    Priority order:
    1. Filename brackets [Hindi], (Dual Audio) etc
    2. Metadata audio stream language tag
    3. Metadata title tag
    """
    # 1. Filename se (sabse reliable for anime)
    langs = detect_language_from_filename(filename)
    if langs:
        return langs

    # 2. Metadata audio stream se
    langs = detect_language_from_metadata(metadata)
    if langs:
        return langs

    # 3. Metadata title tag se
    title = metadata.get('format', {}).get('tags', {}).get('title', '')
    for word, lang_name in LANG_WORDS.items():
        if word in title.lower():
            if lang_name not in langs:
                langs.append(lang_name)

    return langs


def detect_quality(metadata, current_resolution=None):
    """
    Quality detect karo - bina brackets ke return karo (1080p, 720p)
    """
    if current_resolution and current_resolution != 'OG':
        res_map = {
            '2160': '2160p', '1080': '1080p', '720': '720p',
            '576': '576p', '480': '480p', '360': '360p',
        }
        return res_map.get(str(current_resolution), f'{current_resolution}p')

    streams = metadata.get('streams', [])
    for s in streams:
        if s.get('codec_type') == 'video':
            width = str(s.get('width', ''))
            height = str(s.get('height', ''))
            if height in ['2160', '1080', '720', '576', '480', '360', '240']:
                return f'{height}p'
            if width in QUALITY_MAP:
                return QUALITY_MAP[width]
    return None


def extract_anime_info(filename, metadata):
    """Filename aur metadata se anime name, season, episode extract karo"""
    tags = metadata.get('format', {}).get('tags', {})

    # Filename clean karo
    name = os.path.splitext(os.path.basename(filename))[0]

    # [@SBANIME] type channel tags remove karo
    name = re.sub(r'\[@[^\]]+\]', '', name).strip()
    # Starting mein [GroupName] remove karo
    name = re.sub(r'^\[[^\]]+\]', '', name).strip()
    # Language brackets remove karo [Hindi], (Dual Audio) etc
    name = re.sub(
        r'[\[\(](Hindi|Japanese|English|Telugu|Tamil|Bengali|Malayalam|Kannada|'
        r'Dual Audio|Multi Audio|Dual|Multi|Eng|Hin|Jpn|Korean|Chinese|Arabic)[\]\)]',
        '', name, flags=re.IGNORECASE
    ).strip()

    # Season/Episode detect
    season = None
    episode = None

    # S01E01 pattern (most common)
    m = re.search(r'[Ss](\d+)[Ee](\d+)', name)
    if m:
        season = int(m.group(1))
        episode = int(m.group(2))
        anime_name = name[:m.start()].strip(' .-_')
    else:
        # Standalone episode number - E01 ya just 01
        m = re.search(r'[\s\-_](\d{2,3})[\s\-_\.]', name)
        if m:
            episode = int(m.group(1))
            anime_name = name[:m.start()].strip(' .-_')
        else:
            anime_name = name

    # Metadata title better ho toh use karo
    meta_title = tags.get('title', '') or tags.get('show', '')
    if meta_title and len(meta_title) > 3:
        meta_clean = re.sub(
            r'(1080p|720p|480p|4K|HEVC|x264|x265|WEB-DL|BluRay|HDRip)',
            '', meta_title, flags=re.IGNORECASE
        ).strip()
        if meta_clean:
            anime_name = meta_clean

    # Final cleanup
    anime_name = re.sub(r'(1080p|720p|480p|4K|HEVC|x264|x265|WEB-DL|BluRay|HDRip)',
                        '', anime_name, flags=re.IGNORECASE)
    anime_name = re.sub(r'\s+', ' ', anime_name).strip(' .-_')

    return anime_name, season, episode


def build_auto_caption(filepath, resolution=None, channel='@SBANIME'):
    """
    Final caption banao.
    Format: AnimeName S02E03 in Hindi 1080p [@SBANIME].mp4
    """
    metadata = get_media_metadata(filepath)
    filename = os.path.basename(filepath)

    anime_name, season, episode = extract_anime_info(filename, metadata)
    quality = detect_quality(metadata, resolution)
    langs = detect_language(filename, metadata)

    parts = []

    # 1. Anime Name
    if anime_name:
        parts.append(anime_name)

    # 2. S02E03
    if season and episode:
        parts.append(f'S{season:02d}E{episode:02d}')
    elif episode:
        parts.append(f'E{episode:02d}')

    # 3. in Hindi / in Japanese etc
    if langs:
        parts.append(f'in {" + ".join(langs)}')

    # 4. Quality - bina brackets (1080p, not (1080p))
    if quality:
        parts.append(quality)

    # 5. Channel tag
    parts.append(f'[{channel}]')

    return ' '.join(parts) + '.mp4'


def smart_caption(original_caption, filepath, resolution=None, channel='@SBANIME'):
    """
    Agar original caption mein quality info hai toh wahi use karo,
    warna filename/metadata se auto generate karo.
    """
    if not original_caption:
        return build_auto_caption(filepath, resolution, channel)

    has_quality = bool(re.search(r'\d{3,4}p|4K|FHD', original_caption, re.IGNORECASE))

    if has_quality:
        return original_caption
    else:
        return build_auto_caption(filepath, resolution, channel)
