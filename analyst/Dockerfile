FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/gunners

COPY agent/requirements.txt agent/requirements.txt
COPY analyst/requirements.txt analyst/requirements.txt
RUN pip install --no-cache-dir -r analyst/requirements.txt

ENV GUNNERS_ROOT=/opt/gunners \
    PYTHONPATH=/opt/gunners \
    PYTHONUNBUFFERED=1

COPY . /opt/gunners

EXPOSE 8100

CMD ["uvicorn", "analyst.api:app", "--host", "0.0.0.0", "--port", "8100"]
