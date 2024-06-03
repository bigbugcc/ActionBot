# ========================================================
# Stage: Builder
# ========================================================
FROM golang:1.22-alpine AS builder
WORKDIR /app
ARG TARGETARCH

RUN apk --no-cache --update add \
  build-base \
  gcc \
  wget \
  unzip

COPY . .

RUN go build -v -o build/ -trimpath -ldflags "-s -w" ./cmd/warp-plus
RUN mkdir -p /etc/warp/

# ========================================================
# Stage: Final Image of warp-plus
# ========================================================
FROM alpine
ENV TZ=Asia/Shanghai
WORKDIR /app
VOLUME [ "/etc/warp/" ]
COPY --from=builder /app/build/warp-plus /usr/bin/
COPY --from=builder /app/DockerEntrypoint.sh /app/
COPY --from=builder /app/example_config.json /app/

RUN chmod +x \
  /usr/bin/warp-plus \
  /app/DockerEntrypoint.sh

ENTRYPOINT [ "./DockerEntrypoint.sh" ]