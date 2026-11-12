FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY pyproject.toml ./
COPY crawlnet ./crawlnet
RUN pip install .

COPY spiders ./spiders
RUN useradd -r -u 10001 crawl && chown -R crawl /app
USER crawl

ENTRYPOINT ["crawlnet"]
CMD ["worker"]
