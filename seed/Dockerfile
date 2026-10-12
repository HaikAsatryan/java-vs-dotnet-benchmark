# seed/Dockerfile: canonical deterministic seeder image.
#
# IMPORTANT: the compose build CONTEXT must be the REPO ROOT (not seed/), so that
# spec/schema.sql can be COPYed into the image. In infra/compose.yaml:
#
#   seed:
#     build:
#       context: ..                     # repo root, relative to infra/
#       dockerfile: seed/Dockerfile
#
# The seeder is a one-shot data tool. It is NOT part of either SUT image and
# never runs in the measured path.

# ---- build stage ----
FROM golang:1.26.5-bookworm@sha256:53eeac89074db483fdf0ab3be1df32bf6e47562263d2d0d6baa7f26acb4957dd AS build
WORKDIR /src

# Module files first for layer caching. Paths are relative to the repo root
# build context, so the seeder module lives under seed/.
COPY seed/go.mod seed/go.sum ./
RUN go mod download

COPY seed/ ./
# Static, reproducible, no cgo (distroless/static runtime).
ENV CGO_ENABLED=0
RUN go build -trimpath -ldflags="-s -w" -o /out/seed ./cmd/seed

# ---- runtime stage ----
FROM gcr.io/distroless/static-debian13:nonroot@sha256:f7f8f729987ad0fdf6b05eeeae94b26e6a0f613bdf46feea7fc40f7bd72953e6

# The DDL ships in the image so --schema can apply it without a host mount. The
# repo-root build context is what makes this COPY valid.
COPY spec/schema.sql /spec/schema.sql

COPY --from=build /out/seed /seed

# Default entrypoint; the runner/compose supplies flags (e.g.
# --mode copy --scale smoke --dsn ... --schema /spec/schema.sql --grants).
ENTRYPOINT ["/seed"]
