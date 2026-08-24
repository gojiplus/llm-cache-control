"""A cache-control gateway you can point an OpenAI client at.

Implements binding-openai-chat.md in front of an upstream inference server.
It authenticates the caller, applies the cache intent on each fragment,
lowers the effective policy onto the upstream cache partition, strips every
gateway-facing member, forwards, and returns the response with cache_status
attached.

    VLLM_BASE_URL=http://127.0.0.1:8000 python -m gateway.proxy

Then point a client at http://127.0.0.1:8100 with one of the demo tokens.
This is a reference, not a production server: the token table is a dict and
there is no TLS.
"""

import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .auth import AuthContext
from .binding_openai import OpenAIChatBinding
from .gateway import Gateway
from .vllm_backend import VLLMBackend

# Stands in for real credential resolution. The point of the table is that
# the tenant comes from the credential and never from the request body.
TOKENS = {
    "acme-key": AuthContext(tenant="acme", principal="alice", session="s-acme"),
    "other-key": AuthContext(tenant="other-corp", principal="bob", session="s-other"),
}


class Handler(BaseHTTPRequestHandler):
    binding: OpenAIChatBinding = None
    upstream: str = None

    def log_message(self, fmt, *args):
        sys.stderr.write("proxy: " + fmt % args + "\n")

    def _send(self, status, payload):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth(self):
        header = self.headers.get("Authorization", "")
        token = header[7:] if header.startswith("Bearer ") else None
        return TOKENS.get(token)

    def do_GET(self):
        if self.path == "/v1/cache_intent_capabilities":
            self._send(200, self.binding.capabilities_document())
            return
        self._send(404, {"error": {"code": "not_found", "message": self.path}})

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self._send(404, {"error": {"code": "not_found", "message": self.path}})
            return

        auth = self._auth()
        if auth is None:
            # Scope cannot be resolved without a credential, and the body is
            # not allowed to supply one.
            self._send(
                401,
                {"error": {"code": "unauthorized", "message": "unknown or missing bearer token"}},
            )
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8")

        request_id = f"req-{uuid.uuid4().hex}"
        upstream_request, result, error = self.binding.prepare(body, auth, request_id)
        if error is not None:
            self._send(400, error.encode("utf-8"))
            return

        try:
            request = urllib.request.Request(
                f"{self.upstream}/v1/chat/completions",
                data=json.dumps(upstream_request).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=300) as response:
                payload = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            self._send(exc.code, exc.read())
            return
        except urllib.error.URLError as exc:
            self._send(502, {"error": {"code": "upstream_unreachable", "message": str(exc)}})
            return

        self._send(200, self.binding.attach_status(payload, result))


def main() -> int:
    upstream = os.environ.get("VLLM_BASE_URL")
    if not upstream:
        print("VLLM_BASE_URL is not set.")
        return 1
    model = os.environ.get("VLLM_MODEL", "Qwen/Qwen2.5-0.5B-Instruct")
    port = int(os.environ.get("PROXY_PORT", "8100"))

    Handler.binding = OpenAIChatBinding(Gateway(backend=VLLMBackend(upstream, model)))
    Handler.upstream = upstream.rstrip("/")

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"cache control gateway on http://127.0.0.1:{port} -> {upstream}")
    print(f"demo tokens: {', '.join(sorted(TOKENS))}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
