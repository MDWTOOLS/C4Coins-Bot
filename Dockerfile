FROM python:3.12-slim

WORKDIR /app

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    libgl1 \
    libglx-mesa0 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir --break-system-packages -r requirements.txt

# Copy app
COPY app.py ./

# Data directory
ENV DATA_DIR=/app/data
ENV PORT=8080
RUN mkdir -p /app/data

EXPOSE 8080

CMD ["python3", "app.py"]
