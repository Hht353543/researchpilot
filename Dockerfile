FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    RESEARCHPILOT_PROVIDER=mock \
    RESEARCHPILOT_KB_PATH=/app/data/knowledge_base \
    RESEARCHPILOT_RUNS_PATH=/app/runs

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml constraints.txt README.md LICENSE ./
COPY researchpilot ./researchpilot
COPY configs ./configs
COPY scripts ./scripts

# Install the build backend explicitly, then the project itself. Keeping this as
# two steps makes the image build fail loudly on packaging problems instead of
# silently resolving an older setuptools from the base image.
RUN pip install --no-cache-dir --upgrade "setuptools>=77" "wheel>=0.43,<1" \
    && pip install --no-cache-dir -c constraints.txt .

COPY data ./data
COPY eval ./eval

RUN mkdir -p /app/runs

EXPOSE 8000 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status==200 else 1)"

CMD ["uvicorn", "researchpilot.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
