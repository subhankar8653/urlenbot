"""
Auto Caption Generator
Filename se anime name detect karo, metadata se quality/language.
Format: AnimeName S02E06 in Hindi 1080p [@SBANIME].mp4
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

# Sirf yeh exact words brackets mein milein toh language maano
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


def _is_language_bracket(content):
    """
    Check karo kya bracket ka content sirf language word hai.
    [Hindi]           -> True
    [Rare ToonsIndia] -> False  (group name hai)
    [Dual Audio]      -> True
    [SubsPlease]      -> False
    """
    c = content.strip().lower()
    if c in LANG_WORDS:
        return True
    words = c.split()
    if len(words) == 1 and words[0] in LANG_WORDS:
        return True
    return False


def detect_language_from_filename(filename):
    """
    Filename ke brackets se SIRF language words detect karo.
    [Hindi]           -> ['Hindi']
    [Rare ToonsIndia] -> []   (group name, skip)
    [Dual Audio]      -> ['Dual Audio']
    """
    langs = []
    name = os.path.splitext(os.path.basename(filename))[0]

    bracket_contents = re.findall(r'[\[\(]([^\]\)]+)[\]\)]', name)
    for content in bracket_contents:
        if _is_language_bracket(content):
            c = content.strip().lower()
            lang_name = LANG_WORDS.get(c)
            if not lang_name:
                for word in c.split():
                    if word in LANG_WORDS:
                        lang_name = LANG_WORDS[word]
                        break
            if lang_name and lang_name not in langs:
                langs.append(lang_name)

    return langs


def detect_language_from_metadata(metadata):
    """
    Audio stream se language detect karo.
    SIRF 'language' field use karo - title tag ignore karo
    (title mein 'Visit - RareToonsIndia' jaisi garbage hoti hai)
    """
    streams = metadata.get('streams', [])
    langs = []
    for s in streams:
        if s.get('codec_type') == 'audio':
            # SIRF language code field use karo, title NAHI
            lang_code = s.get('tags', {}).get('language', '').strip().lower()
            if lang_code and lang_code in LANG_MAP:
                lang_name = LANG_MAP[lang_code]
                if lang_name not in langs:
                    langs.append(lang_name)
    return langs


def detect_language(filename, metadata):
    """
    Priority:
    1. Filename brackets [Hindi], (Dual Audio)
    2. Metadata audio stream 'language' field (NOT title)
    """
    # 1. Filename se
    langs = detect_language_from_filename(filename)
    if langs:
        return langs

    # 2. Metadata audio language code se
    langs = detect_language_from_metadata(metadata)
    if langs:
        return langs

    return langs


def detect_quality_from_metadata(metadata):
    """
    ffprobe metadata se actual video quality detect karo.
    Width/Height se - title tag IGNORE karo.
    """
    streams = metadata.get('streams', [])
    for s in streams:
        if s.get('codec_type') == 'video':
            height = str(s.get('height', ''))
            width = str(s.get('width', ''))
            # Height se detect (most reliable)
            if height in ['2160', '1080', '720', '576', '480', '360', '240']:
                return f'{height}p'
            # Width se fallback
            if width in QUALITY_MAP:
                return QUALITY_MAP[width]
    return None


def detect_quality(metadata, current_resolution=None):
    """
    Quality detect karo.
    - OG ya None: ffprobe se actual quality nikalo
    - Encode resolution (720, 1080 etc): use karo as-is
    """
    if current_resolution and current_resolution != 'OG':
        # Encode kiya gaya resolution use karo
        res_map = {
            '2160': '2160p', '1080': '1080p', '720': '720p',
            '576': '576p', '480': '480p', '360': '360p',
        }
        return res_map.get(str(current_resolution), f'{current_resolution}p')

    # OG ya None: metadata se actual quality detect karo
    return detect_quality_from_metadata(metadata)


def extract_anime_info(filename, metadata):
    """
    Anime name HAMESHA filename se lo.
    Metadata title IGNORE - woh group ka promo text ho sakta hai.
    """
    name = os.path.splitext(os.path.basename(filename))[0]

    # Step 1: Starting group tag remove [SubsPlease], [Erai-raws] etc
    name = re.sub(r'^\[[^\]]+\]', '', name).strip()

    # Step 2: [@Channel] tags remove
    name = re.sub(r'\[@[^\]]+\]', '', name).strip()

    # Step 3: Language brackets remove - SIRF wahi jo actual language ho
    def remove_lang_brackets(text):
        text = re.sub(
            r'\[([^\]]+)\]',
            lambda m: '' if _is_language_bracket(m.group(1)) else m.group(0),
            text
        )
        text = re.sub(
            r'\(([^\)]+)\)',
            lambda m: '' if _is_language_bracket(m.group(1)) else m.group(0),
            text
        )
        return text.strip()

    name = remove_lang_brackets(name)

    # Step 4: Season/Episode detect
    season = None
    episode = None

    m = re.search(r'[Ss](\d+)[Ee](\d+)', name)
    if m:
        season = int(m.group(1))
        episode = int(m.group(2))
        anime_name = name[:m.start()].strip(' .-_')
    else:
        # 2-4 digit episode numbers support (e.g. E06, E106, _06_, _106_)
        m = re.search(r'[\s\-_](\d{2,4})[\s\-_\.]', name)
        if m:
            episode = int(m.group(1))
            anime_name = name[:m.start()].strip(' .-_')
        else:
            anime_name = name

    # Step 5: Remaining group brackets remove from end
    anime_name = re.sub(r'\[[^\]]+\]\s*$', '', anime_name).strip()
    anime_name = re.sub(r'\([^\)]+\)\s*$', '', anime_name).strip()

    # Step 6: Quality tags naam se remove
    anime_name = re.sub(
        r'(2160p|1080p|720p|480p|4K|HEVC|x264|x265|WEB-DL|BluRay|HDRip)',
        '', anime_name, flags=re.IGNORECASE
    )
    anime_name = re.sub(r'\s+', ' ', anime_name).strip(' .-_')

    return anime_name, season, episode


def build_auto_caption(filepath, resolution=None, channel='@SBANIME'):
    """
    Format: AnimeName S02E06 in Hindi 1080p [@SBANIME].mp4

    resolution:
      - None / 'OG' : ffprobe se actual quality detect karo
      - '720','1080' : encode ki gai quality use karo
    """
    metadata = get_media_metadata(filepath)
    filename = os.path.basename(filepath)
    # Extension strip karo pehle (double .mp4 problem fix)
    base_filename = os.path.splitext(filename)[0]

    anime_name, season, episode = extract_anime_info(filename, metadata)
    quality = detect_quality(metadata, resolution)
    langs = detect_language(filename, metadata)

    parts = []

    # 1. Anime Name — agar empty toh filename use karo (fallback)
    name_part = anime_name.strip() if anime_name else base_filename
    if name_part:
        parts.append(name_part)

    # 2. S02E06 / E06 — 3+ digit episodes ke liye zero-pad mat karo
    if season and episode:
        ep_str = f'{episode:02d}' if episode < 100 else str(episode)
        parts.append(f'S{season:02d}E{ep_str}')
    elif episode:
        ep_str = f'{episode:02d}' if episode < 100 else str(episode)
        parts.append(f'E{ep_str}')

    # 3. in Hindi
    if langs:
        parts.append(f'in {" + ".join(langs)}')

    # 4. 1080p (bina brackets)
    if quality:
        parts.append(quality)

    # 5. [@SBANIME]
    parts.append(f'[{channel}]')

    caption = ' '.join(parts)
    # Ensure ends with .mp4 (sirf ek baar)
    if not caption.endswith('.mp4'):
        caption += '.mp4'
    return caption


def smart_caption(original_caption, filepath, resolution=None, channel='@SBANIME'):
    """
    Original caption mein quality info hai toh use karo,
    warna auto generate karo.
    """
    if not original_caption:
        return build_auto_caption(filepath, resolution, channel)

    has_quality = bool(re.search(r'\d{3,4}p|4K|FHD', original_caption, re.IGNORECASE))

    if has_quality:
        # Caption already accha hai — sirf ensure karo .mp4 se end ho (double nahi)
        cap = original_caption.strip()
        # Extension normalize karo
        cap = re.sub(r'\.(mkv|avi|mov|flv|wmv|ts|m4v|webm)$', '.mp4', cap, flags=re.IGNORECASE)
        if not cap.lower().endswith('.mp4'):
            cap += '.mp4'
        return cap
    else:
        return build_auto_caption(filepath, resolution, channel)
