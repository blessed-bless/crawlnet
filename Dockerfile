FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /build
COPY pyproject.toml ./
COPY crawlnet ./crawlnet
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN useradd --create-home --uid 10001 crawl
WORKDIR /app

COPY --from=builder /install /usr/local
COPY spiders ./spiders
RUN chown -R crawl:crawl /app
USER crawl

ENTRYPOINT ["crawlnet"]
CMD ["worker"]
