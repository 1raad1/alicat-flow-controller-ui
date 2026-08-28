# Build from the repository or extracted relay package root.
# The hosting platform MUST provide HTTPS and keep this backend private.
FROM python:3.13-slim
WORKDIR /app
COPY requirements-relay.txt ./
RUN pip install --no-cache-dir -r requirements-relay.txt
COPY flow_controller/__init__.py ./flow_controller/__init__.py
COPY flow_controller/mexa/__init__.py flow_controller/mexa/records.py flow_controller/mexa/transport.py flow_controller/mexa/relay.py flow_controller/mexa/relay_server.py ./flow_controller/mexa/
USER 10001:10001
EXPOSE 8765
CMD ["python", "-m", "flow_controller.mexa.relay_server", "--host", "0.0.0.0", "--port", "8765", "--behind-tls-proxy"]
