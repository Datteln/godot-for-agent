FROM ubuntu:24.04

ARG GODOT_VERSION=4.3-stable

RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin codeact \
    && apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install --yes --no-install-recommends ca-certificates curl git python3 unzip \
    && curl --fail --location --retry 3 \
       "https://github.com/godotengine/godot/releases/download/${GODOT_VERSION}/Godot_v${GODOT_VERSION}_linux.x86_64.zip" \
       --output /tmp/godot.zip \
    && unzip /tmp/godot.zip -d /tmp/godot \
    && install -m 0755 /tmp/godot/Godot_v${GODOT_VERSION}_linux.x86_64 /usr/local/bin/godot \
    && rm -rf /tmp/godot /tmp/godot.zip /var/lib/apt/lists/*

USER 10001:10001
WORKDIR /workspace
