FROM apache/airflow:2.10.4-python3.12

USER root

# Chromium + driver for the Selenium scraper, and the fonts matplotlib needs
# so the rendered PNGs look the same in the container as they do locally.
# xvfb gives the scraper a real (if invisible) display, so Chrome runs with
# its normal renderer rather than the easily-fingerprinted headless one.
RUN apt-get update && apt-get install -y --no-install-recommends \
        chromium chromium-driver \
        xvfb \
        fonts-dejavu-core fonts-liberation \
        libglib2.0-0 libnss3 libx11-6 libxcb1 libxcomposite1 libxdamage1 \
        libxext6 libxfixes3 libxrandr2 libgbm1 libasound2 \
    && rm -rf /var/lib/apt/lists/*

ENV CHROME_BIN=/usr/bin/chromium \
    CHROMEDRIVER_PATH=/usr/bin/chromedriver \
    GUNNERS_ROOT=/opt/gunners \
    PYTHONPATH=/opt/gunners \
    MPLBACKEND=Agg

USER airflow

COPY airflow/requirements.txt /tmp/airflow-requirements.txt
RUN pip install --no-cache-dir -r /tmp/airflow-requirements.txt

WORKDIR /opt/gunners
