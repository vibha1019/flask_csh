FROM python:3.12-slim

WORKDIR /app

# Install Node.js
RUN apt-get update \
  && apt-get install -y --no-install-recommends nodejs \
  && rm -rf /var/lib/apt/lists/*

# Install Python deps first for better layer caching
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
  && pip install --no-cache-dir -r /app/requirements.txt

# Copy service code
COPY . /app

# Create unprivileged user
RUN useradd --create-home --shell /usr/sbin/nologin runner

USER runner

EXPOSE 8591

# Run Flask with Gunicorn
CMD ["gunicorn", "--bind", "0.0.0.0:8591", "--workers", "2", "runner:runner"]
