FROM python:3.10-slim-bullseye

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ="Asia/Kolkata"

# Install system packages + Chrome
RUN apt-get update && apt-get install -y \
    ffmpeg git wget pv jq python3-dev megatools \
    mediainfo gcc libsm6 libxext6 \
    libfontconfig1 libxrender1 libgl1-mesa-glx \
    curl unzip gnupg \
 && rm -rf /var/lib/apt/lists/*

# Install Google Chrome
RUN curl -sSL https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb -o /tmp/chrome.deb \
 && apt-get update && apt-get install -y /tmp/chrome.deb \
 && rm /tmp/chrome.deb \
 && rm -rf /var/lib/apt/lists/*

# Install ChromeDriver (Chrome ke version se match karta hai automatically)
RUN CHROME_VERSION=$(google-chrome --version | grep -oP '\d+\.\d+\.\d+') \
 && CHROMEDRIVER_VERSION=$(curl -s "https://chromedriver.storage.googleapis.com/LATEST_RELEASE_${CHROME_VERSION%%.*}") \
 && curl -sSL "https://chromedriver.storage.googleapis.com/${CHROMEDRIVER_VERSION}/chromedriver_linux64.zip" -o /tmp/chromedriver.zip \
 && unzip /tmp/chromedriver.zip -d /usr/local/bin/ \
 && rm /tmp/chromedriver.zip \
 && chmod +x /usr/local/bin/chromedriver

COPY . .

# Install Python dependencies
RUN pip3 install --no-cache-dir --upgrade pip --trusted-host pypi.org --trusted-host files.pythonhosted.org && \
    pip3 install --no-cache-dir -r requirements.txt --trusted-host pypi.org --trusted-host files.pythonhosted.org

# Run the bot
CMD ["python3", "-m", "VideoEncoder"]
