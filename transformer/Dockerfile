FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy transformer code
COPY transform.py .

# Copy contracts
COPY ../contracts /app/contracts

# Create data directories
RUN mkdir -p /data/raw /data/processed

CMD ["python", "transform.py", "/data/raw/match.html", "--output-dir", "/data/processed"]
