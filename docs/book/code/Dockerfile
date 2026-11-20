# The book's container, installing featherweight from PyPI. The build context is this folder (see README.md for how to use it).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN pip install 'featherweight[mcp,anthropic]'

# A user of its own, so that nothing runs as root. The data (the sqlite file) lives in /data, and the code you edit in /code.
RUN useradd --create-home --uid 1000 reader && mkdir /code /data && chown reader /code /data
USER reader
WORKDIR /code
