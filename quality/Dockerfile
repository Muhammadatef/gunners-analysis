FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy validation code
COPY validate.py .

# Create data directories
RUN mkdir -p /data/processed /data/quality

CMD ["python", "validate.py", "--events", "/data/processed/EventData.csv", "--players", "/data/processed/PlayerData.csv", "--output", "/data/quality/validation_report.json"]
