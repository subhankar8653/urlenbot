FROM python:3.10-slim-bullseye

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ="Asia/Kolkata"

# Install system packages + Chromium (apt se direct, koi alag download nahi)
RUN apt-get update && apt-get install -y \
    ffmpeg git wget pv jq python3-dev megatools \
    mediainfo gcc libsm6 libxext6 \
    libfontconfig1 libxrender1 libgl1-mesa-glx \
    chromium chromium-driver \
 && rm -rf /var/lib/apt/lists/*

COPY . .

# Install Python dependencies
RUN pip3 install --no-cache-dir --upgrade pip --trusted-host pypi.org --trusted-host files.pythonhosted.org && \
    pip3 install --no-cache-dir -r requirements.txt --trusted-host pypi.org --trusted-host files.pythonhosted.org

# Run the bot
CMD ["python3", "-m", "VideoEncoder"]
