"""Synthetic helper lifecycle output. No network or instrument access."""
import sys
import time

mode = sys.argv[1]
if mode == "blocked":
    print("x509: certificate valid for blocked-due-to-malware.ucl.ac.uk secret-value", flush=True)
    raise SystemExit(1)
if mode == "exit":
    print("arbitrary error secret-value", flush=True)
    raise SystemExit(1)
print("6:49PM INF status changed status=connecting", flush=True)
if mode not in ("silent", "retry"):
    print("6:49PM INF status changed status=online", flush=True)
    print("6:49PM INF tunnel established url=https://test-one.wormhole.bar", flush=True)
if mode in ("recover", "change", "stale"):
    time.sleep(.4)
    print("6:49PM INF status changed status=reconnecting", flush=True)
    time.sleep(.4)
    print("6:49PM INF status changed status=online", flush=True)
    if mode == "stale":
        print("Forwarding: https://test-one.wormhole.bar -> http://localhost:1234", flush=True)
    else:
        name = "test-two" if mode == "change" else "test-one"
        print(f"6:49PM INF reconnected url=https://{name}.wormhole.bar", flush=True)
while True:
    if mode == "retry":
        print("6:49PM INF status changed status=reconnecting", flush=True)
    time.sleep(.1)
