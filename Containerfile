# AegisOS - Fedora Hummingbird + Hermes Agent combined bootc image
#
# Build (plain container build):
#   sudo podman build -t localhost/aegisos:latest .
#   (or: python3 deploy/aegisctl.py build)
#
# Build with browser support (playwright/chromium):
#   sudo podman build --build-arg INSTALL_BROWSER=true -t localhost/aegisos:latest .

# --- Base image ---
# Official Fedora Hummingbird bootc image.
# Verified the hummingbird-community/bootc-os repo and its latest tag (multi-arch) via the quay.io API.
#   - https://quay.io/api/v1/repository/hummingbird-community/bootc-os/tag/
#   - https://fedoramagazine.org/fedora-hummingbird-linux-taking-the-hummingbird-model-to-the-full-os/
#   - https://discussion.fedoraproject.org/t/fedora-hummingbird-taking-the-hummingbird-model-to-the-full-operating-system/191184
#     (the maintainer corrected hummingbird-ci -> hummingbird-community)
# Hummingbird is a rolling release, so use :latest.
FROM quay.io/hummingbird-community/bootc-os:latest

# --- Build arguments ---
# Toggle for installing the browser (playwright/chromium). Off by default to keep the image small.
ARG INSTALL_BROWSER=false

# Hermes data directory. Placed under /var, the bootc persistent writable area.
ARG HERMES_HOME=/var/lib/hermes

# --- Install system dependencies ---
# In bootc (image-mode), run dnf install and dnf clean all in the same RUN layer to drop the cache.
# Reference: bootc guide "RUN $pkgsystem install somepackage && $pkgsystem clean all"
#   - https://bootc.dev/bootc/building/guidance.html
#
# Why preinstall nodejs at build time: if the system node is insufficient, install.sh installs a
# Hermes-managed Node under HERMES_HOME(=/var), which puts a runtime dependency in the data area.
# Preinstalling the system node avoids that trap.
# Hermes node requirement: ^20.19 || >=22.12 (install.sh node_satisfies_build()).
# Fedora's nodejs package provides 22 or newer.
RUN dnf install -y \
        nodejs \
        npm \
        git \
        ripgrep \
        ffmpeg-free \
        python3 \
    && node --version \
    && dnf clean all

# --- Install Hermes Agent ---
# Run install.sh non-interactively.
#   - HERMES_HOME=/var/lib/hermes  -> data (config.yaml, .env, sessions, logs, ...) goes under /var
#   - Code follows the root + Linux FHS layout: /usr/local/lib/hermes-agent (immutable /usr)
#   - The command is symlinked at /usr/local/bin/hermes
#   - uv-managed Python goes to /usr/local/share/uv
#   - --skip-setup avoids baking config/.env (secrets are injected after boot)
#   - --skip-browser skips playwright/chromium (installed when INSTALL_BROWSER=true)
# Verified by reading install.sh: the flags (--non-interactive/--skip-setup/--skip-browser/--hermes-home)
#       and the gateway subcommand exist.
#   - https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh
#
# Note: content written to /var/lib/hermes at build time is treated by bootc only as a "first-boot seed"
#       and is kept separate from the runtime /var afterward (VOLUME /var semantics, "unpacked only from the
#       initial image"). That is why keeping code in /usr/local rather than /var matters.
#   - https://bootc.dev/bootc/filesystem.html
ENV HERMES_HOME=${HERMES_HOME}
RUN set -eux; \
    EXTRA_FLAGS="--skip-browser"; \
    if [ "${INSTALL_BROWSER}" = "true" ]; then EXTRA_FLAGS=""; fi; \
    curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh \
        -o /tmp/hermes-install.sh; \
    HERMES_HOME="${HERMES_HOME}" bash /tmp/hermes-install.sh \
        --non-interactive \
        --skip-setup \
        ${EXTRA_FLAGS} \
        --hermes-home "${HERMES_HOME}"; \
    rm -f /tmp/hermes-install.sh; \
    /usr/local/bin/hermes --version || true

# --- Place systemd units / tmpfiles.d ---
# Use tmpfiles.d to guarantee /var/lib/hermes is created on first boot.
# Reference: the bootc guide recommends tmpfiles.d for pre-creating directories under /var
#   ("recommended to use systemd tmpfiles.d for this").
#   - https://bootc.dev/bootc/filesystem.html
COPY tmpfiles.d/hermes.conf /usr/lib/tmpfiles.d/hermes.conf
COPY systemd/hermes-gateway.service /usr/lib/systemd/system/hermes-gateway.service
COPY systemd/hermes-gateway.path /usr/lib/systemd/system/hermes-gateway.path

# Enable the .path unit so it watches for config.yaml creation at boot.
# The moment config.yaml appears, hermes-gateway.service starts automatically.
RUN systemctl enable hermes-gateway.path

# bootc container lint (checks for missing tmpfiles.d, etc., when available). Does not block the build on failure.
RUN bootc container lint || true
