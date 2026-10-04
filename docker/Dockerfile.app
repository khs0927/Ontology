FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libgl1 \
    libglib2.0-0 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Dependencies come from pyproject.toml extras (single source of truth, same versions CI tests).
# Copy only what the install needs first so source edits do not invalidate the dependency layer.
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -e ".[operational,cad,pdf]" "pytest>=8"

COPY . .

EXPOSE 8000
CMD ["uvicorn", "aec_intelligence.operational.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
