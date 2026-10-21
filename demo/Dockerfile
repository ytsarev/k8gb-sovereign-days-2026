FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 unbound bind9-dnsutils ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/demo
COPY core.py observer.py resolver.py ./
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python3", "/opt/demo/resolver.py", "--config", "/config/config.json"]
