FROM python:3.12-slim
RUN apt-get update && apt-get install -y ffmpeg curl && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir --upgrade "yt-dlp[default]" fastapi uvicorn groq openai-whisper httpx pydantic
# deno: JS runtime for yt-dlp EJS challenge solving (YouTube n-challenge)
ADD https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip /tmp/deno.zip
RUN cd /tmp && python3 -c "import zipfile; zipfile.ZipFile('deno.zip').extractall('/usr/local/bin/')" && chmod +x /usr/local/bin/deno && rm /tmp/deno.zip
WORKDIR /app
RUN curl -fsSL -o worker.py https://raw.githubusercontent.com/olatonrain/metrohyp-clip-worker/main/worker.py
ENV YT_COOKIES_FILE=/data/cookies.txt
EXPOSE 8000
CMD ["uvicorn", "worker:app", "--host", "0.0.0.0", "--port", "8000"]
