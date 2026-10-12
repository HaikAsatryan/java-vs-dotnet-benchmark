# infra/vegeta/Dockerfile: load-generator image: vegeta, built from source so
# the version is pinned to the locked v12.13.0, not a moving release artifact.
#
# Image digests pinned 2026-08-13.

# ---- stage 1: build vegeta v12.13.0 from source (locked Go toolchain) --------
FROM golang:1.26.5-bookworm@sha256:53eeac89074db483fdf0ab3be1df32bf6e47562263d2d0d6baa7f26acb4957dd AS vegeta-build
ENV CGO_ENABLED=0 GOFLAGS=-trimpath
# `go install <module>@<version>` builds the pinned tag reproducibly into
# /go/bin. v12 is a /v12-suffixed module path.
RUN go install github.com/tsenart/vegeta/v12@v12.13.0 \
    && /go/bin/vegeta -version

# ---- stage 2: runtime ---------------------------------------------------------
FROM debian:bookworm-slim@sha256:abd67ffcfa541b485a3dff59865ab629aa048a6c613e639d36e7456b0b229241
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates; \
    rm -rf /var/lib/apt/lists/*
COPY --from=vegeta-build /go/bin/vegeta /usr/local/bin/vegeta
# Sanity: the tool resolves and reports its pinned version at build time.
RUN vegeta -version
# No-op holder: the runner docker-execs attack invocations so every param
# (-rate/-duration/-targets/...) is runner-controlled, never baked into the image.
CMD ["sleep", "infinity"]
