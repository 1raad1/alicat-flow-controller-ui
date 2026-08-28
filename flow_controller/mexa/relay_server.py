"""Single-instrument relay service. No serial access, disk logging or commands.

Run separately from both lab PCs, behind an approved HTTPS endpoint or with
TLS certificates. Role keys come from environment variables, never CLI flags.
"""

import argparse
import asyncio
import hmac
import ipaddress
import os
import secrets
import ssl
import time
from http import HTTPStatus

from .records import MAX_LINE
from .relay import RelayError, decode, is_digest, quiet_logger, receive, send, validate_key


class RelayService:
    def __init__(self, publisher_key, receiver_key):
        validate_key(publisher_key)
        validate_key(receiver_key)
        if publisher_key == receiver_key:
            raise ValueError("Publisher and receiver access keys must be different")
        self.keys = {"publisher": publisher_key, "receiver": receiver_key}
        self.peers = {}
        self.lock = asyncio.Lock()
        self.active = 0

    def process_request(self, connection, request):
        if request.path == "/healthz":
            return connection.respond(HTTPStatus.OK, "MEXA relay running\n")
        if request.path != "/mexa":
            return connection.respond(HTTPStatus.NOT_FOUND, "Not found\n")
        if self.active >= 8:
            return connection.respond(HTTPStatus.SERVICE_UNAVAILABLE, "Relay busy\n")

    async def handler(self, ws):
        from websockets.exceptions import ConnectionClosed
        self.active += 1
        role = None
        peer = None
        try:
            if self.active > 8:
                await ws.close(1013, "Relay busy")
                return
            hello = await receive(ws)
            requested_role, key = hello.get("role"), hello.get("key")
            if (set(hello) != {"type", "version", "role", "key"} or hello["type"] != "join"
                    or type(hello["version"]) is not int or hello["version"] != 1
                    or not isinstance(requested_role, str) or requested_role not in self.keys
                    or not isinstance(key, str) or not key.isascii()
                    or not hmac.compare_digest(key, self.keys[requested_role])):
                await ws.close(4001, "Access denied")
                return
            async with self.lock:
                if requested_role in self.peers:
                    await ws.close(4009, "Role occupied")
                    return
                role = requested_role
                peer = {"ws": ws, "other": None, "session": None}
                self.peers[role] = peer
                await send(ws, {"type": "accepted"})
                other = self.peers.get("receiver" if role == "publisher" else "publisher")
                if other:
                    peer["other"], other["other"] = other, peer
                    peer["session"] = other["session"] = {"stage": "challenge"}
                    await send(other["ws"], {"type": "paired"})
                    await send(ws, {"type": "paired"})
            bucket_at, messages = time.monotonic(), 0
            async for raw in ws:
                value = decode(raw)
                if time.monotonic() - bucket_at >= 1:
                    bucket_at, messages = time.monotonic(), 0
                messages += 1
                if messages > 8:
                    raise RelayError("Rate exceeded")
                other, session = peer["other"], peer["session"]
                if not other or not session:
                    raise RelayError("No peer")
                stage = session["stage"]
                if role == "receiver" and stage == "challenge":
                    if (set(value) != {"type", "nonce", "proof"} or value["type"] != "challenge"
                            or not is_digest(value["nonce"]) or not is_digest(value["proof"])):
                        raise RelayError("Bad challenge")
                    session["stage"] = "proof"
                elif role == "publisher" and stage == "proof":
                    if set(value) != {"type", "signature"} or value["type"] != "proof" or not is_digest(value["signature"]):
                        raise RelayError("Bad proof")
                    session["stage"] = "stream"
                elif role == "publisher" and stage == "stream":
                    if (set(value) != {"type", "payload", "signature"} or value["type"] != "sample"
                            or not isinstance(value["payload"], str) or not is_digest(value["signature"])):
                        raise RelayError("Bad sample")
                else:
                    raise RelayError("Messages in this direction are not allowed")
                await send(other["ws"], value)
        except ConnectionClosed:
            pass
        except (ValueError, TypeError, KeyError, TimeoutError):
            await ws.close(1008, "Session rejected")
        finally:
            async with self.lock:
                # Remove both identities before awaiting close. An old handler
                # must never evict a new connection that reused its role.
                if role and self.peers.get(role) is peer:
                    del self.peers[role]
                    other_role = "receiver" if role == "publisher" else "publisher"
                    other = self.peers.pop(other_role, None) if peer["other"] else None
                else:
                    other = None
            try:
                if other:
                    await other["ws"].close(1012, "Peer disconnected")
            finally:
                self.active -= 1

    async def start(self, host="127.0.0.1", port=8765, *, ssl_context=None):
        from websockets.asyncio.server import serve
        return await serve(self.handler, host, port, ssl=ssl_context,
                           process_request=self.process_request, origins=[None],
                           max_size=MAX_LINE, max_queue=2, compression=None,
                           open_timeout=5, close_timeout=1, ping_interval=2,
                           ping_timeout=3, write_limit=MAX_LINE, server_header=None,
                           logger=quiet_logger("mexa.relay.server"))


def server_tls(host, cert, key, behind_tls_proxy=False):
    if bool(cert) != bool(key):
        raise ValueError("Supply both --cert and --key")
    if cert:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(cert, key)
        return context
    if not behind_tls_proxy and not ipaddress.ip_address(host).is_loopback:
        raise ValueError("Non-loopback binding requires TLS certificates or --behind-tls-proxy on a private backend")
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--cert", help="TLS certificate chain file")
    parser.add_argument("--key", help="TLS private-key file, not a relay access key")
    parser.add_argument("--behind-tls-proxy", action="store_true", help="Private backend only; the proxy MUST provide HTTPS")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--generate-keys", action="store_true", help="Print new role keys once; store them securely")
    mode.add_argument("--local-test", action="store_true", help="Same-PC test only: loopback with fresh keys printed to this console")
    args = parser.parse_args(argv)
    if args.generate_keys:
        print("MEXA_RELAY_PUBLISH_KEY=" + secrets.token_hex(32))
        print("MEXA_RELAY_RECEIVE_KEY=" + secrets.token_hex(32))
        return 0
    if args.local_test and (args.host != "127.0.0.1" or args.cert or args.key or args.behind_tls_proxy):
        parser.error("--local-test only supports 127.0.0.1, with no TLS/proxy options")
    try:
        if not 1 <= args.port <= 65535:
            raise ValueError("Port must be 1–65535")
        context = server_tls(args.host, args.cert, args.key, args.behind_tls_proxy)
        publisher_key = secrets.token_hex(32) if args.local_test else os.environ.get("MEXA_RELAY_PUBLISH_KEY", "")
        receiver_key = secrets.token_hex(32) if args.local_test else os.environ.get("MEXA_RELAY_RECEIVE_KEY", "")
        service = RelayService(publisher_key, receiver_key)
    except (ValueError, OSError):
        parser.error("Invalid relay configuration. Check TLS/binding, port and two different 32–128-character role keys in the environment.")

    async def run():
        async with await service.start(args.host, args.port, ssl_context=context) as server:
            print("MEXA relay running; one analyser and one receiver. No measurement files are saved.", flush=True)
            if args.local_test:
                print(f"LOCAL TEST ONLY: ws://127.0.0.1:{args.port}/mexa\n"
                      f"Publisher key: {publisher_key}\nReceiver key: {receiver_key}\n"
                      "Use Simulation only in the bridge. These keys expire when this process stops.\n"
                      "This is not reachable from another PC. Ctrl+C stops the server.", flush=True)
            await server.serve_forever()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
