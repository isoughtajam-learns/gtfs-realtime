FROM python:3.11-slim

WORKDIR /app
ENV PYTHONPATH=/app

# Install system dependencies for psycopg2 and other build tools
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy pyproject.toml and uv.lock (if using uv) or just install from pyproject
COPY pyproject.toml uv.lock ./

# Install pip and dependencies
RUN pip install --no-cache-dir -e .

# Copy the rest of the application
COPY . .

EXPOSE 8000

# Start FastAPI server
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
