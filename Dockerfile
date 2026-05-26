FROM python:3.11-slim AS base

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install -r requirements.txt

# Pre-download embedding model to avoid cold-start latency
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5')" || true

# Copy application code
COPY . /app

# Create data directories and copy sample data if present
RUN mkdir -p /data/prompts /data/few_shots && \
    if [ -d "data/prompts" ]; then cp -r data/prompts/* /data/prompts/ 2>/dev/null || true; fi && \
    if [ -d "data/few_shots" ]; then cp -r data/few_shots/* /data/few_shots/ 2>/dev/null || true; fi

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# Run the application
CMD ["uvicorn", "layercache.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
