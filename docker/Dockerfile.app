FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libgl1 \
    libglib2.0-0 \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir \
        psycopg[binary] \
        fastapi \
        uvicorn[standard] \
        pydantic \
        ezdxf \
        PyMuPDF \
        shapely \
        pytest

COPY . .
RUN pip install --no-cache-dir -e .

EXPOSE 8000
CMD ["uvicorn", "aec_intelligence.operational.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
