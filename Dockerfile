FROM docker.io/library/python:3.10-slim-bullseye

RUN apt-get update && apt-get install -y \
    ffmpeg git wget pv jq python3-dev megatools \
    mediainfo gcc libsm6 libxext6 \
    libfontconfig1 libxrender1 libgl1-mesa-glx \
 && rm -rf /var/lib/apt/lists/*

COPY . .

RUN pip3 install --no-cache-dir -r requirements.txt \
    --trusted-host pypi.org \
    --trusted-host files.pythonhosted.org

CMD ["python3", "-m", "VideoEncoder"]
