FROM docker.io/library/python:3.10-slim-bullseye

# ── System packages ────────────────────────────────────────────
RUN apt-get update && apt-get install -y \
    ffmpeg git wget curl pv jq unzip \
    python3-dev gcc \
    megatools aria2 p7zip-full \
    mediainfo \
    libsm6 libxext6 libfontconfig1 libxrender1 libgl1-mesa-glx \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
    libxrandr2 libgbm1 libasound2 \
 && rm -rf /var/lib/apt/lists/*

# ── Google Chrome ──────────────────────────────────────────────
RUN wget -q https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
 && apt-get update -qq \
 && apt-get install -y -qq ./google-chrome-stable_current_amd64.deb \
 && rm google-chrome-stable_current_amd64.deb \
 && rm -rf /var/lib/apt/lists/*

# ── ChromeDriver ───────────────────────────────────────────────
RUN DRIVER_URL=$(curl -s "https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions-with-downloads.json" \
    | python3 -c "import sys,json; d=json.load(sys.stdin); print([x['url'] for x in d['channels']['Stable']['downloads']['chromedriver'] if 'linux64' in x['url']][0])") \
 && wget -q "$DRIVER_URL" -O /tmp/chromedriver.zip \
 && unzip -q /tmp/chromedriver.zip -d /tmp/ \
 && find /tmp -name "chromedriver" -exec mv {} /usr/local/bin/chromedriver \; \
 && chmod +x /usr/local/bin/chromedriver \
 && rm -rf /tmp/chromedriver* \
 && chromedriver --version

COPY . .

# ── Python packages ────────────────────────────────────────────
RUN pip3 install --no-cache-dir -r requirements.txt \
    --trusted-host pypi.org \
    --trusted-host files.pythonhosted.org

CMD ["python3", "-m", "VideoEncoder"]
