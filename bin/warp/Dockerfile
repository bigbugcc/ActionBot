# ========================================================
# Stage: Builder
# ========================================================
FROM golang:alpine AS builder

WORKDIR /app
RUN apk --no-cache add build-base gcc git tzdata jq

COPY . .

RUN mkdir -p build && \
    unset GOFLAGS && \
    go mod tidy && \
    go build -v -o build/ -trimpath -ldflags="-checklinkname=0 -s -w" ./cmd/warp-plus

RUN mkdir -p /etc/warp/

# ========================================================
# Stage: Final Image
# ========================================================
FROM alpine
ENV TZ=Asia/Shanghai
WORKDIR /app
VOLUME [ "/etc/warp/" ]

RUN apk --no-cache add tzdata jq curl

COPY --from=builder /app/build/warp-plus /usr/bin/
COPY --from=builder /app/DockerEntrypoint.sh /app/
COPY --from=builder /app/example_config.json /app/

RUN chmod +x \
    /usr/bin/warp-plus \
    /app/DockerEntrypoint.sh

ENTRYPOINT [ "./DockerEntrypoint.sh" ]
