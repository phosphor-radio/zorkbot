# encrusted is built separately and referenced here as a pinned image, because
# compiling it takes an hour or more on a Pi Zero and it changes far less often
# than zorkd does. A `FROM <local tag>` reference resolves from the local image
# store and never rebuilds, so it survives `docker save`/`load` (which discards
# build cache) and base-image tag movement.
#
# Bootstrap it once — see game/Dockerfile.encrusted and the README:
#
#   docker build -t zorkbot-encrusted:1.1.0 -f Dockerfile.encrusted .
#
# Override with --build-arg ENCRUSTED_IMAGE=... to test a different build.
ARG ENCRUSTED_IMAGE=zorkbot-encrusted:1.1.0

FROM ${ENCRUSTED_IMAGE} AS encrusted

# The Go wrapper cross-compiles via GOOS/GOARCH, so it always builds natively on
# $BUILDPLATFORM regardless of the target arch.
FROM --platform=$BUILDPLATFORM golang:1.23-bookworm AS go-builder
WORKDIR /src
COPY go.mod ./
RUN go mod download
COPY . .
ARG TARGETOS TARGETARCH
ENV GOMAXPROCS=1
RUN CGO_ENABLED=0 GOOS=${TARGETOS} GOARCH=${TARGETARCH} go build -o /zorkd ./cmd/zorkd

FROM debian:bookworm-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates wget \
    && rm -rf /var/lib/apt/lists/*

COPY --from=encrusted /encrusted /usr/local/bin/encrusted
COPY --from=go-builder /zorkd /usr/local/bin/zorkd

RUN mkdir -p /data /game

ENV ENCRUSTED_PATH=/usr/local/bin/encrusted \
    GAME_FILE=/game/zork1.z3 \
    SAVE_DIR=/data \
    LISTEN_ADDR=:8080

EXPOSE 8080
WORKDIR /data
ENTRYPOINT ["zorkd"]
