FROM python:3.10-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ="Asia/Kolkata"

WORKDIR /app

# System packages — chromium + chromedriver for Swift downloader
# --fix-missing + retry: guards against transient 404s from the Debian
# security-repo CDN (stale index vs pool mismatch) without needing a rebuild
RUN apt-get update && \
    (apt-get install -y --fix-missing \
    ffmpeg git wget pv jq python3-dev megatools \
    mediainfo gcc libsm6 libxext6 \
    libfontconfig1 libxrender1 libgl1-mesa-glx \
    chromium chromium-driver \
    || (apt-get update && apt-get install -y \
    ffmpeg git wget pv jq python3-dev megatools \
    mediainfo gcc libsm6 libxext6 \
    libfontconfig1 libxrender1 libgl1-mesa-glx \
    chromium chromium-driver)) \
 && rm -rf /var/lib/apt/lists/*

# N_m3u8DL-RE — /aniwatch (Aniwatch/Animetsu) ke HLS download ke liye.
# Fail hua to build nahi rukta; bot mein /aniwatch "binary nahi mili" batayega.
RUN (wget -q https://github.com/nilaoda/N_m3u8DL-RE/releases/download/v0.2.1-beta/N_m3u8DL-RE_Beta_linux-x64_20240828.tar.gz \
    && tar -xzf N_m3u8DL-RE_Beta_linux-x64_20240828.tar.gz \
    && mv N_m3u8DL-RE_Beta_linux-x64/N_m3u8DL-RE /usr/local/bin/ \
    && chmod +x /usr/local/bin/N_m3u8DL-RE) \
    || echo "WARNING: N_m3u8DL-RE install fail hua — /aniwatch kaam nahi karega"; \
    rm -rf N_m3u8DL-RE_Beta_linux-x64*
# .NET binary ko libicu ki zaroorat na pade
ENV DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1

# Selenium ko bata do ke system chromium use karo (selenium-manager mat chalao)
ENV CHROME_BIN=/usr/bin/chromium
ENV CHROMEDRIVER_PATH=/usr/bin/chromedriver

ENV PYTHONUNBUFFERED=1

# Pehle sirf requirements => code badalne par pip layer cache se aati hai (rebuild fast)
COPY requirements.txt .
# Python dependencies (pip upgrade skip — hash mismatch issue)
RUN pip3 install --no-cache-dir -r requirements.txt

COPY . .

# Run the bot
CMD ["python3", "-m", "VideoEncoder"]
