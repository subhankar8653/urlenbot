"""
Auto Caption Generator
Metadata se anime info detect karke caption banata hai
Format: AnimeName S01E01 (480p) in Hindi [@SBANIME].mp4
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


def detect_language(metadata):
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
    if not langs:
        # Title tag check karo
        title = metadata.get('format', {}).get('tags', {}).get('title', '')
        for code, name in LANG_MAP.items():
            if name.lower() in title.lower():
                langs.append(name)
    return langs


def detect_quality(metadata, current_resolution=None):
    """Video stream se quality detect karo"""
    if current_resolution and current_resolution != 'OG':
        res_map = {'2160': '2160p', '1080': '1080p', '720': '720p',
                   '576': '576p', '480': '480p'}
        return res_map.get(current_resolution, f'{current_resolution}p')

    streams = metadata.get('streams', [])
    for s in streams:
        if s.get('codec_type') == 'video':
            width = str(s.get('width', ''))
            height = str(s.get('height', ''))
            # Height se quality
            if height in ['2160', '1080', '720', '576', '480', '360', '240']:
                return f'{height}p'
            if width in QUALITY_MAP:
                return QUALITY_MAP[width]
    return None


def extract_anime_info(filename, metadata):
    """Filename aur metadata se anime info extract karo"""
    # Tags se title check karo
    tags = metadata.get('format', {}).get('tags', {})
    meta_title = tags.get('title', '') or tags.get('show', '')

    # Filename clean karo
    name = os.path.splitext(os.path.basename(filename))[0]

    # Common patterns
    # [@SBANIME] AnimeName S01E01 1080p...
    name = re.sub(r'\[@[^\]]+\]', '', name).strip()
    # [GroupName] remove
    name = re.sub(r'^\[[^\]]+\]', '', name).strip()

    # Season/Episode detect
    season = None
    episode = None

    # S01E01 pattern
    m = re.search(r'[Ss](\d+)[Ee](\d+)', name)
    if m:
        season = int(m.group(1))
        episode = int(m.group(2))
        anime_name = name[:m.start()].strip(' .-_')
    else:
        # Episode only - E01 ya 01
        m = re.search(r'[\s\-_](\d{2,3})[\s\-_\.]', name)
        if m:
            episode = int(m.group(1))
            anime_name = name[:m.start()].strip(' .-_')
        else:
            anime_name = name

    # Meta title better hai toh use karo
    if meta_title and len(meta_title) > 3:
        anime_name = meta_title

    # Cleanup anime name
    anime_name = re.sub(r'\s+', ' ', anime_name).strip()
    # Remove quality tags from name
    anime_name = re.sub(r'(1080p|720p|480p|4K|HEVC|x264|x265|WEB-DL|BluRay|HDRip)', '',
                        anime_name, flags=re.IGNORECASE).strip()

    return anime_name, season, episode


def build_auto_caption(filepath, resolution=None, channel='@SBANIME'):
    """
    Metadata se automatic caption banao
    Format: AnimeName S01E01 (480p) in Hindi [@SBANIME].mp4
    """
    metadata = get_media_metadata(filepath)
    filename = os.path.basename(filepath)

    anime_name, season, episode = extract_anime_info(filename, metadata)
    quality = detect_quality(metadata, resolution)
    langs = detect_language(metadata)

    # Caption parts build karo
    parts = []

    # Anime name
    if anime_name:
        parts.append(anime_name)

    # Season + Episode
    if season and episode:
        parts.append(f'S{season:02d}E{episode:02d}')
    elif episode:
        parts.append(f'E{episode:02d}')

    # Quality
    if quality:
        parts.append(f'({quality})')

    # Language
    if langs:
        parts.append(f'in {" + ".join(langs)}')

    # Channel tag
    parts.append(f'[{channel}]')

    caption = ' '.join(parts) + '.mp4'
    return caption


def smart_caption(original_caption, filepath, resolution=None, channel='@SBANIME'):
    """
    Agar original caption informative hai toh use karo,
    warna metadata se auto generate karo
    """
    if not original_caption:
        return build_auto_caption(filepath, resolution, channel)

    # Check karo kya caption mein quality/language info hai
    has_quality = bool(re.search(r'\d{3,4}p|4K|FHD', original_caption, re.IGNORECASE))

    if has_quality:
        # Original caption sahi hai, sirf quality replace karo
        return original_caption
    else:
        # Metadata se banao
        return build_auto_caption(filepath, resolution, channel)
