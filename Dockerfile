FROM python:3.13-slim

# tmux for Linux terminal backend
RUN apt-get update \
    && apt-get install -y --no-install-recommends tmux \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir onecmd

# Persist data (database, config, aliases)
VOLUME /data
ENV ONECMD_DB=/data/mybot.sqlite

WORKDIR /data

ENTRYPOINT ["onecmd"]
