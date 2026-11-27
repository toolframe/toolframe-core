# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.13.10@sha256:a3ea1357342c1a4bf9a0c53a3deb1b55379aefa037338d8113d5771efa01774f AS uv

FROM debian:trixie-slim@sha256:8aa5a7bf5a1955bddbe7d74fcdd0b91ab30e74362a8b73af672b0481b35b44a0 AS runtime
ARG TARGETARCH
ARG TOOLFRAME_VERSION=0.8.4
ARG TOOLFRAME_REVISION
LABEL org.opencontainers.image.source="https://github.com/toolframe/toolframe-core" \
      org.opencontainers.image.title="Toolframe" \
      org.opencontainers.image.description="Persistent control host for Python plugins" \
      org.opencontainers.image.licenses="Apache-2.0 AND MIT" \
      org.opencontainers.image.version="${TOOLFRAME_VERSION}" \
      org.opencontainers.image.revision="${TOOLFRAME_REVISION}"
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends ca-certificates git libstdc++6 procps tini \
    && rm -rf /var/lib/apt/lists/* \
    && find /usr -xdev -type f -perm /6000 -exec chmod a-s '{}' + \
    && groupadd --gid 65532 nonroot \
    && useradd --uid 65532 --gid 65532 --create-home --home-dir /home/nonroot nonroot \
    && install -d -o 65532 -g 65532 /data /opt/toolframe
COPY --from=uv /uv /usr/local/bin/uv
# prepare-container.py verifies the release checksums before staging these files.
COPY build/container/${TARGETARCH}/toolframe /usr/local/bin/toolframe
COPY build/container/${TARGETARCH}/LICENSE build/container/${TARGETARCH}/NOTICE /usr/share/doc/toolframe/
COPY --chown=65532:65532 build/container/wheels/ /opt/toolframe/wheels/
COPY scripts/container-healthcheck.py /usr/local/lib/toolframe/healthcheck.py
COPY scripts/harden-container.py /usr/local/lib/toolframe/harden.py
ENV HOME=/home/nonroot \
    TOOLFRAME_UV=/usr/local/bin/uv \
    TOOLFRAME_RUNTIME_ROOT=/opt/toolframe/runtime \
    TOOLFRAME_WHEELHOUSE=/opt/toolframe/wheels \
    UV_PYTHON_INSTALL_DIR=/opt/toolframe/python \
    UV_CACHE_DIR=/data/.toolframe/uv-cache \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
USER 65532:65532
WORKDIR /data
# --help prepares Core without creating an application, keys or management tokens.
# Keep the same absolute Python and venv paths in both final images.
RUN test "$(toolframe --version)" = "toolframe ${TOOLFRAME_VERSION}" \
    && UV_CACHE_DIR=/tmp/uv-build-cache toolframe --help > /dev/null \
    && ln -s "$(uv python find --managed-python 3.14.7)" /opt/toolframe/python3 \
    && UV_CACHE_DIR=/tmp/uv-build-cache uv cache clean \
    && /opt/toolframe/python3 /usr/local/lib/toolframe/harden.py
VOLUME ["/data"]
EXPOSE 9876
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
    CMD ["/opt/toolframe/python3", "/usr/local/lib/toolframe/healthcheck.py"]
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/toolframe", "--home", "/data"]
CMD ["serve", "--host", "0.0.0.0"]

FROM runtime AS ps-dependencies
USER root
# Core uses ps to reconcile worker ownership. Retain the final base's libc and
# loader, and copy only ps's additional shared libraries under their SONAMEs.
# Include dpkg status fragments so scanners can identify these copied components.
RUN mkdir -p /opt/ps-libs/var/lib/dpkg/status.d \
    && ldd /usr/bin/ps | awk '/=> \// { print $3 }' | while read -r library; do \
         case "$library" in */libc.so.*|*/libm.so.*) continue ;; esac; \
         directory=$(dirname "$(readlink -f "$library")"); \
         install -D "$library" "/opt/ps-libs$directory/$(basename "$library")"; \
         package=$(dpkg-query -S "$(readlink -f "$library")") || exit 1; \
         package=${package%%:*}; \
         dpkg-query -s "$package" > "/opt/ps-libs/var/lib/dpkg/status.d/$package" || exit 1; \
       done \
    && dpkg-query -s procps > /opt/ps-libs/var/lib/dpkg/status.d/procps \
    && dpkg-query -s tini > /opt/ps-libs/var/lib/dpkg/status.d/tini

FROM gcr.io/distroless/cc-debian13:nonroot@sha256:29ee70762214fe8190cea04ef1c0a84a8365879e047ce8f911ae5a5ab2355fa2 AS distroless
ARG TOOLFRAME_VERSION=0.8.4
ARG TOOLFRAME_REVISION
LABEL org.opencontainers.image.source="https://github.com/toolframe/toolframe-core" \
      org.opencontainers.image.title="Toolframe (Distroless)" \
      org.opencontainers.image.description="Persistent control host for Python plugins, without a system shell" \
      org.opencontainers.image.licenses="Apache-2.0 AND MIT" \
      org.opencontainers.image.version="${TOOLFRAME_VERSION}" \
      org.opencontainers.image.revision="${TOOLFRAME_REVISION}"
COPY --from=runtime /usr/local/bin/toolframe /usr/local/bin/uv /usr/local/bin/
COPY --from=runtime /usr/bin/tini /usr/bin/tini
COPY --from=runtime /usr/bin/ps /usr/bin/ps
COPY --from=ps-dependencies /opt/ps-libs/ /
COPY --from=runtime /usr/share/doc/ /usr/share/doc/
COPY --from=runtime /usr/local/lib/toolframe/ /usr/local/lib/toolframe/
COPY --from=runtime --chown=65532:65532 /opt/toolframe/ /opt/toolframe/
COPY --from=runtime --chown=65532:65532 /home/nonroot/ /home/nonroot/
COPY --from=runtime --chown=65532:65532 /data/ /data/
ENV HOME=/home/nonroot \
    TOOLFRAME_UV=/usr/local/bin/uv \
    TOOLFRAME_RUNTIME_ROOT=/opt/toolframe/runtime \
    TOOLFRAME_WHEELHOUSE=/opt/toolframe/wheels \
    UV_PYTHON_INSTALL_DIR=/opt/toolframe/python \
    UV_CACHE_DIR=/data/.toolframe/uv-cache \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
USER 65532:65532
WORKDIR /data
VOLUME ["/data"]
EXPOSE 9876
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
    CMD ["/opt/toolframe/python3", "/usr/local/lib/toolframe/healthcheck.py"]
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/toolframe", "--home", "/data"]
CMD ["serve", "--host", "0.0.0.0"]

# The default build target is the Debian image.
FROM runtime AS standard
