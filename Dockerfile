FROM python:3.12-slim

# System libraries needed by lxml, psycopg2, and curl_cffi
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        libxml2-dev \
        libxslt-dev \
        libcurl4-openssl-dev \
        libssl-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Non-root user for security
RUN useradd -m -u 1000 appuser
WORKDIR /app

# Install Python dependencies first (cached layer — only rebuilds when requirements change)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY --chown=appuser:appuser . .

# Remove local secrets file — secrets are injected as env vars by Render
RUN rm -f .streamlit/secrets.toml

USER appuser

# Render injects PORT at runtime (default 8080)
EXPOSE 8080
ENV PORT=8080

CMD ["sh", "-c", \
     "streamlit run app.py \
      --server.port=${PORT} \
      --server.address=0.0.0.0 \
      --server.headless=true \
      --browser.gatherUsageStats=false"]
