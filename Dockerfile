FROM python:3.12-slim
RUN apt-get update && apt-get install -y ffmpeg yt-dlp curl && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir fastapi uvicorn groq openai-whisper httpx pydantic
WORKDIR /app
RUN curl -fsSL -o worker.py https://raw.githubusercontent.com/olatonrain/metrohyp-clip-worker/main/worker.py
EXPOSE 8000
CMD ["uvicorn", "worker:app", "--host", "0.0.0.0", "--port", "8000"]
