ARG GO_VERSION=1.26.3
ARG ALPINE_VERSION=3.21
ARG K6_TAG=v2.0.0
ARG PROM_CLIENT_TAG=v0.5.0
ARG FAKER_TAG=v0.5.1

FROM docker.io/library/golang:${GO_VERSION}-alpine AS builder
ARG K6_TAG
ARG PROM_CLIENT_TAG
ARG FAKER_TAG

# xk6 builds k6 with extensions: k6/x/remotewrite (Prometheus remote write), k6/x/faker
RUN apk add --no-cache git && go install go.k6.io/xk6/cmd/xk6@latest

RUN CGO_ENABLED=0 xk6 build --k6-version ${K6_TAG} \
    --with github.com/grafana/xk6-client-prometheus-remote@${PROM_CLIENT_TAG} \
    --with github.com/grafana/xk6-faker@${FAKER_TAG} \
    --output /k6

FROM docker.io/library/alpine:${ALPINE_VERSION}

RUN adduser -D -u 12345 k6

COPY --from=builder /k6 /usr/local/bin/k6

USER k6
WORKDIR /home/k6
ENTRYPOINT ["k6"]
