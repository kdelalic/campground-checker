# Use an official Python runtime as a parent image
FROM python:3.14-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Set work directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project
COPY . .

# Explicitly install boto3 if not already in requirements.txt (it is, but for safety)
# and ensure camply is ready
RUN pip install --no-cache-dir boto3

# Add execute permissions to the script
RUN chmod +x check_campsites.py

# Run the script with --forever by default as requested for a persistent worker
CMD ["python", "check_campsites.py", "--forever"]
