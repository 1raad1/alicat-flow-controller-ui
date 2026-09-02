# Build from the repository or extracted relay package root.
# The hosting platform MUST provide HTTPS and keep this backend private.
FROM python:3.13-slim
WORKDIR /app
COPY requirements-relay.txt ./
RUN pip install --no-cache-dir -r requirements-relay.txt
COPY mexa_bridge/__init__.py mexa_bridge/records.py mexa_bridge/transport.py mexa_bridge/relay.py mexa_bridge/relay_server.py ./mexa_bridge/
USER 10001:10001
EXPOSE 8765
CMD ["python", "-m", "mexa_bridge.relay_server", "--host", "0.0.0.0", "--port", "8765", "--behind-tls-proxy"]
