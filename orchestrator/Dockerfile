FROM python:3.12-slim

# Install Chrome and dependencies for Selenium (needed for scraper subprocess)
RUN apt-get update && apt-get install -y \
    wget \
    gnupg2 \
    ca-certificates \
    fonts-liberation \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libatspi2.0-0 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libgbm1 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libwayland-client0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxkbcommon0 \
    libxrandr2 \
    xdg-utils \
    libu2f-udev \
    libvulkan1 \
    xvfb \
    && wget -q https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
    && apt-get install -y ./google-chrome-stable_current_amd64.deb \
    && rm google-chrome-stable_current_amd64.deb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/gunners

# Build context is the repo root so the whole project can be imported as a
# package tree (orchestrator.pipeline, db.loader, visualizer.match_report, ...).
COPY orchestrator/requirements.txt orchestrator/requirements.txt
RUN pip install --no-cache-dir -r orchestrator/requirements.txt

ENV GUNNERS_ROOT=/opt/gunners \
    PYTHONPATH=/opt/gunners \
    MPLBACKEND=Agg

# Code is bind-mounted in compose; this copy keeps the image runnable alone.
COPY . /opt/gunners

RUN mkdir -p data/raw data/processed data/quality data/visualizations logs

EXPOSE 8000

CMD ["uvicorn", "orchestrator.api:app", "--host", "0.0.0.0", "--port", "8000"]
