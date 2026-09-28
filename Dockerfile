# Two stages: build the wheel, then install it into a clean runtime image.
#
# Docker is included for this project specifically because it runs *two long-lived processes*
# (the bot and the outbox worker) plus an HTTP API, and compose is genuinely the simplest way to
# start all three with one command. The sibling projects in this portfolio deliberately do not
# ship a Dockerfile, because for them `pip install -e .` is simpler and Docker would be cargo
# cult.

FROM python:3.13-slim AS build
WORKDIR /build
RUN pip install --no-cache-dir build
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m build --wheel --outdir /dist

FROM python:3.13-slim AS runtime

# Fail fast and log immediately: buffered output in a container means losing the last lines of
# a crash, which are the ones that matter.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# A non-root user, created before the install so nothing in the image is owned by root that
# does not need to be.
RUN useradd --create-home --uid 10001 talabflow
WORKDIR /app

COPY --from=build /dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm -f /tmp/*.whl

# Migrations and their config are needed at runtime to bring a fresh volume up to date.
COPY alembic.ini ./
COPY migrations ./migrations

RUN mkdir -p /app/data && chown -R talabflow:talabflow /app
USER talabflow

# SQLite lives on a volume so orders survive a container rebuild.
VOLUME ["/app/data"]
ENV TALABFLOW_DATABASE_URL=sqlite:////app/data/talabflow.sqlite3

EXPOSE 8000

# Inside a container the API must listen on all interfaces; the process boundary is the
# container, and compose decides what is actually published to the host.
CMD ["python", "-m", "talabflow.cli", "serve", "--host", "0.0.0.0", "--port", "8000"]
