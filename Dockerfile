FROM python:3.11-slim

WORKDIR /app

# Install ffmpeg for frame extraction and audio processing
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

# Pre-download Whisper tiny model so first video doesn't stall
RUN python -c "import whisper; whisper.load_model('tiny')"

COPY . .
RUN python -c "import app; print('app loaded ok')"

# Persistent data directory (mounted by Contextone)
RUN mkdir -p /app/data

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--timeout", "120", "app:app"]
