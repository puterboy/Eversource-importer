################################################################################
#
# App: Eversource Downloader and Importer
# File: Dockerfile
# Version: 0.9.0
# Copyright Jeff Kosowsky
# Date: August 2026
#
################################################################################

ARG BUILD_FROM=ghcr.io/home-assistant/base:latest
FROM $BUILD_FROM

ARG BUILD_VERSION
ENV APP_VERSION=${BUILD_VERSION}

#===============================================================================

# Force pure plain ASCII rendering environment variables
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8

# Install runtime dependencies matching system constraints
RUN apk add --no-cache \
## Python
    python3 \
    py3-pip \
## Chrome
    chromium \
    chromium-chromedriver \
## Sqlite3
    sqlite \
## Linux utils
    bash

WORKDIR /app

# Install explicit python package stubs
RUN pip3 install --no-cache-dir --break-system-packages selenium

COPY run.sh /app/
COPY eversource_scraper.py /app/
COPY insert_missing_placeholders.py /app
COPY import_electric_usage.py /app
COPY redo_sum_statistics.py /app
COPY add_placeholder_state.sh /app

RUN chmod a+x /app/run.sh /app/add_placeholder_state.sh

CMD [ "/app/run.sh" ]
