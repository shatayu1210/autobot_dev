# Use the official Python slim image
FROM python:3.12-slim

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

# Install uv for fast dependency management
RUN pip install --no-cache-dir uv

# Create and set the working directory
WORKDIR /app

# Copy dependency files first (layer-cache friendly)
COPY pyproject.toml uv.lock ./

# Install dependencies into the system Python environment
RUN uv pip install --system -r pyproject.toml

# Copy the rest of the application code
COPY . .

# Expose Cloud Run's default port
EXPOSE 8080

# Use gunicorn for production (1 worker, 8 threads, long timeout)
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "8", "--timeout", "300", "app:flask_app"]
