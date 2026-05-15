FROM python:3.10-slim-bullseye

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ="Asia/Kolkata"

WORKDIR /app

# System packages — chromium removed (Railway pe build fail karta hai)
# Swift downloader chromium use karta hai but core encoding ke liye zaruri nahi
RUN apt-get update && apt-get install -y \
    ffmpeg git wget pv jq python3-dev megatools \
    mediainfo gcc libsm6 libxext6 \
    libfontconfig1 libxrender1 libgl1-mesa-glx \
 && rm -rf /var/lib/apt/lists/*

COPY . .

# Python dependencies
RUN pip3 install --no-cache-dir --upgrade pip && \
    pip3 install --no-cache-dir -r requirements.txt

# Run the bot
CMD ["python3", "-m", "VideoEncoder"]
