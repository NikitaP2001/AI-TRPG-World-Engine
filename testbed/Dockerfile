FROM python:3.9-slim

WORKDIR /app

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Copy project files
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Ensure scripts are executable
RUN chmod +x testbed/runner.py

WORKDIR /app
CMD ["python", "testbed/runner.py", "--help"]
