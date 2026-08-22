# Build encrusted for the target platform.
FROM --platform=$BUILDPLATFORM rust:1-bookworm AS encrusted-builder
ENV CARGO_BUILD_JOBS=1
RUN cargo install encrusted --bin encrusted --locked

# Build the Go HTTP wrapper after encrusted (serializes BuildKit stages for low-RAM hosts).
FROM --platform=$BUILDPLATFORM golang:1.23-bookworm AS go-builder
COPY --from=encrusted-builder /usr/local/cargo/bin/encrusted /tmp/.encrusted-built
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

COPY --from=encrusted-builder /usr/local/cargo/bin/encrusted /usr/local/bin/encrusted
COPY --from=go-builder /zorkd /usr/local/bin/zorkd

RUN mkdir -p /data /game

ENV ENCRUSTED_PATH=/usr/local/bin/encrusted \
    GAME_FILE=/game/zork1.z3 \
    SAVE_DIR=/data \
    LISTEN_ADDR=:8080

EXPOSE 8080
WORKDIR /data
ENTRYPOINT ["zorkd"]
