FROM python:3.12-slim@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a

WORKDIR /app

# Systemowe zależności
RUN apt-get update && \
    apt-get install -y --no-install-recommends git patch && \
    rm -rf /var/lib/apt/lists/*

# Zależności Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Kod aplikacji
COPY pyproject.toml ./
COPY redsl/ ./redsl/
COPY config/ ./config/

# Install redsl as a real package so `python -m redsl` works regardless of
# the runtime WORKDIR (e.g. when docker-compose overrides working_dir to a
# bind-mounted project root). Without this, /app drops out of sys.path and
# you get `ModuleNotFoundError: No module named redsl` despite the source
# being present in the image.
RUN pip install --no-cache-dir --no-deps -e .

# Build-time sanity check — fail the build if the module cannot be imported
# from anywhere (catches regressions like a missing __init__.py or a bad
# packaging entry in pyproject.toml early, instead of at `compose run` time).
RUN python -c "import redsl; print('redsl OK at', redsl.__file__)"

# Katalog na wyniki refaktoryzacji
RUN mkdir -p /app/redsl_output /tmp/redsl_memory

ENV PYTHONUNBUFFERED=1
ENV REFACTOR_DRY_RUN=true
# Belt-and-suspenders: even without `pip install -e .`, /app stays on
# sys.path so submodule discovery works regardless of WORKDIR override.
ENV PYTHONPATH=/app

# API mode
CMD ["uvicorn", "redsl.api:app", "--host", "0.0.0.0", "--port", "8000"]
