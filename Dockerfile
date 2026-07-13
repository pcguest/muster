# Muster container image.
#
# Multi-stage: the build stage compiles the wheel, the runtime stage
# installs it (with the postgres extra) and nothing else — no compilers,
# no build tooling, no packages beyond what the slim base already carries.
# The process runs as a non-root user and treats /project as the mounted
# Muster project (muster.yaml, sources/, runs/).

FROM python:3.12-slim AS build

WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir build && python -m build --wheel --outdir /wheels

FROM python:3.12-slim

# A fixed non-root identity: orchestrator policies can pin uid 10001, and
# nothing in the image is writable by it except its home and /project.
RUN groupadd --gid 10001 muster \
    && useradd --uid 10001 --gid muster --create-home --shell /usr/sbin/nologin muster

COPY --from=build /wheels /wheels
# psycopg[binary] ships manylinux wheels, so the postgres extra installs
# without a compiler in the image.
RUN pip install --no-cache-dir "$(echo /wheels/*.whl)[postgres]" && rm -rf /wheels

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# The mounted project directory: muster.yaml, sources/, runs/. It must be
# writable by uid 10001 — runs/, output/ and the dashboard token live here.
WORKDIR /project
RUN chown muster:muster /project
USER 10001:10001

# Liveness for `muster serve` on its default port. Probed with the stdlib
# so the image needs no curl or wget. Workloads that run the daemon
# instead should override this (see deploy/docker-compose.yaml).
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8600/healthz', timeout=2).status == 200 else 1)"]

ENTRYPOINT ["muster"]
CMD ["--help"]
