"""Internal loopback relay hosted by the flow app for its Wormhole tunnel.

No standalone deployment entrypoint, serial access, disk logging or commands.
The owning app supplies fresh in-memory role keys and manages the lifecycle.
"""

import asyncio
import hmac
import ipaddress
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
        if not ipaddress.ip_address(host).is_loopback:
            raise ValueError("The built-in Wormhole relay must bind to numeric loopback")
        from websockets.asyncio.server import serve
        return await serve(self.handler, host, port, ssl=ssl_context,
                           process_request=self.process_request, origins=[None],
                           max_size=MAX_LINE, max_queue=2, compression=None,
                           open_timeout=5, close_timeout=1, ping_interval=2,
                           ping_timeout=3, write_limit=MAX_LINE, server_header=None,
                           logger=quiet_logger("mexa.relay.server"))
