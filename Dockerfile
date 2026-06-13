FROM python:3.10-slim-bullseye

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ="Asia/Kolkata"

WORKDIR /app

# System packages — chromium + chromedriver for Swift downloader
RUN apt-get update && apt-get install -y \
    ffmpeg git wget pv jq python3-dev megatools \
    mediainfo gcc libsm6 libxext6 \
    libfontconfig1 libxrender1 libgl1-mesa-glx \
    chromium chromium-driver \
 && rm -rf /var/lib/apt/lists/*

# Selenium ko bata do ke system chromium use karo (selenium-manager mat chalao)
ENV CHROME_BIN=/usr/bin/chromium
ENV CHROMEDRIVER_PATH=/usr/bin/chromedriver

COPY . .

# Python dependencies
RUN pip3 install --no-cache-dir --upgrade pip --isolated && \
    pip3 install --no-cache-dir -r requirements.txt

# Run the bot
CMD ["python3", "-m", "VideoEncoder"]
