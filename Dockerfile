ARG PYTHON_IMAGE=python:3.12-alpine@sha256:c5ba3deef00c7758561a0f03c26f44f10589263d40e732b8748e0d1f38f6dd89

FROM scratch AS release-amd64
ADD https://github.com/pocketbase/pocketbase/releases/download/v0.40.6/pocketbase_0.40.6_linux_amd64.zip /pocketbase.zip

FROM scratch AS release-arm64
ADD https://github.com/pocketbase/pocketbase/releases/download/v0.40.6/pocketbase_0.40.6_linux_arm64.zip /pocketbase.zip

FROM release-${TARGETARCH} AS release

FROM ${PYTHON_IMAGE} AS extract
ARG TARGETARCH
COPY --from=release /pocketbase.zip /tmp/pocketbase.zip
RUN case "$TARGETARCH" in \
      amd64) sha=0e1f609158ed945f809f8a02ae012aa0427129c66ebb63c1aee76f2c0309373f ;; \
      arm64) sha=d22c66ceb67ca92b7d767f36d826e848a276bcfa3c197ead20bf87c42208984f ;; \
    esac && echo "$sha  /tmp/pocketbase.zip" | sha256sum -c - && \
    unzip -p /tmp/pocketbase.zip pocketbase > /usr/local/bin/pocketbase && chmod 755 /usr/local/bin/pocketbase

FROM ${PYTHON_IMAGE}
COPY --from=extract /usr/local/bin/pocketbase /usr/local/bin/pocketbase
COPY LICENSE /usr/share/licenses/agents-mb/LICENSE
COPY licenses/PocketBase-MIT.txt /usr/share/licenses/pocketbase/LICENSE
WORKDIR /opt/mb
COPY mb server.py sync_server.py sync_client.py payload.py ./
COPY pb_migrations/ ./pb_migrations/
EXPOSE 8090
ENTRYPOINT ["python3", "-u", "/opt/mb/server.py"]
