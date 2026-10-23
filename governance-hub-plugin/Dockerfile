# Multi-stage build for the @mcombi/backstage-plugin-governance-hub dynamic plugin.
#
# Stage 1: Build the plugin using Node.js and @red-hat-developer-hub/cli.
#   - Installs dependencies
#   - Runs tsc to generate declaration files (dist-types/)
#   - Runs backstage-cli package build  →  dist/
#   - Runs rhdh-cli plugin package --export-to dist-dynamic  →  dist-dynamic/
#     (replaces @janus-idp/cli export-dynamic-plugin)
#
# Stage 2: Minimal OCI image from scratch.
#   RHDH 1.10's install-dynamic-plugins init container reads the OCI image
#   MANIFEST annotation `io.backstage.dynamic-packages` (a JSON array) to
#   discover plugin packages.  Without it the container raises:
#     "No plugins found in OCI image … The image might not contain the
#      'io.backstage.dynamic-packages' annotation."
#
#   IMPORTANT: This annotation MUST be added as an OCI manifest annotation via
#     podman build --annotation 'io.backstage.dynamic-packages=[{"packagePath":"."}]'
#   A Dockerfile LABEL writes to the image config, NOT the OCI manifest, and is
#   NOT read by RHDH's init container.  See build-and-push.sh for the full command.
#
FROM node:20-slim AS build

WORKDIR /app

# Install all deps (including peers) for better layer caching.
COPY package.json ./
RUN npm install --legacy-peer-deps --no-audit --include=peer

# Copy source and TypeScript config.
COPY tsconfig.json ./
COPY src/ ./src/

# Generate TypeScript declaration files required by backstage-cli build.
RUN npm run tsc

# Build CommonJS bundle (dist/) then export for RHDH dynamic loading.
# npm run export-dynamic -> janus-cli package export-dynamic-plugin --clean
# which creates dist-dynamic/ with scalprum-format bundle (plugin-manifest.json
# + bundled JS in dist-scalprum/) — required by RHDH's scalprum backend.
# The io.backstage.dynamic-packages OCI annotation is injected by podman at
# build time via --annotation (see build-and-push.sh); cannot use LABEL here.
RUN npm run build && npm run export-dynamic

# ── Final stage: scratch OCI image ──────────────────────────────────────────
# janus-cli export-dynamic-plugin writes the annotation:
#   io.backstage.dynamic-packages=[{"@mcombi/backstage-plugin-governance-hub":{}}]
# RHDH's install-dynamic-plugins init container reads the package name from this
# OLD-format annotation and extracts the OCI image layer, then installs the plugin
# from the directory matching the package name inside the image.
# Plugin files must be at /@mcombi/backstage-plugin-governance-hub/ in the image.
# The annotation is injected at build time via podman build --annotation (see
# build-and-push.sh); it cannot be set here with LABEL.
FROM scratch
COPY --from=build /app/dist-dynamic /backstage-plugin-governance-hub/
