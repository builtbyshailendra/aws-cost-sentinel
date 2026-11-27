# syntax=docker/dockerfile:1.7
# Stage 1: build a wheel so the runtime image carries no build tooling.
FROM python:3.12-slim AS build
WORKDIR /src
RUN pip install --no-cache-dir --disable-pip-version-check build==1.2.2
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m build --wheel --outdir /dist

# Stage 2: slim runtime with a non-root user.
FROM python:3.12-slim AS runtime
LABEL org.opencontainers.image.title="aws-cost-sentinel" \
      org.opencontainers.image.description="Find AWS cost waste from Cost Explorer exports or a live account" \
      org.opencontainers.image.licenses="MIT"
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
COPY --from=build /dist/*.whl /tmp/
RUN pip install /tmp/*.whl && rm -f /tmp/*.whl \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin sentinel
USER sentinel
WORKDIR /home/sentinel
# A CLI that exits after each run has no long-lived process to health-check.
ENTRYPOINT ["cost-sentinel"]
CMD ["--help"]
