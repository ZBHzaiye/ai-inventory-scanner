FROM python:3.11-slim

WORKDIR /app

# Install system dependencies for Pillow
RUN apt-get update && apt-get install -y --no-install-recommends \
    libjpeg-dev \
    libpng-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Ensure data directories exist inside the image (will be overridden by volume)
RUN mkdir -p /app/data/uploads

EXPOSE 5000

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--timeout", "120", "app:app"]
