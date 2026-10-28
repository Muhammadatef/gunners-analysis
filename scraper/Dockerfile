FROM python:3.12-slim

# Install Chrome and dependencies
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    unzip \
    curl \
    && wget -q -O - https://dl-ssl.google.com/linux/linux_signing_key.pub | apt-key add - \
    && echo "deb http://dl.google.com/linux/chrome/deb/ stable main" >> /etc/apt/sources.list.d/google.list \
    && apt-get update \
    && apt-get install -y google-chrome-stable \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy scraper code
COPY scraper.py .
COPY healthcheck.py .

# Create data directory
RUN mkdir -p /data/raw

# Healthcheck
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD python healthcheck.py

CMD ["python", "scraper.py", "${MATCH_URL}", "--output", "/data/raw/match.html"]
