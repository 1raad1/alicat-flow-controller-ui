"""Test child process: emits helper lifecycle events, never uses the network."""
import sys
import time

mode = sys.argv[1]
if mode == "blocked":
    print('failed to request quick Tunnel: certificate is valid for blocked-due-to-malware.ucl.ac.uk, not api.trycloudflare.com', flush=True)
    raise SystemExit(1)
if mode == "exit":
    print("Private diagnostic that must not reach UI: secret-value", flush=True)
    raise SystemExit(1)
if mode != "silent":
    print("| https://synthetic-tunnel.trycloudflare.com |", flush=True)
    print("INF Registered tunnel connection connIndex=0 protocol=http2", flush=True)
if mode == "recover":
    time.sleep(.4)
    print("ERR Unregistered tunnel connection connIndex=0", flush=True)
    time.sleep(.4)
    print("INF Registered tunnel connection connIndex=0 protocol=http2", flush=True)
while True:
    time.sleep(.1)
