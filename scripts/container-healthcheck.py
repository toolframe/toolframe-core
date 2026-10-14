"""Probe Core liveness without requiring a shell or management credentials."""

import json
import urllib.request
from pathlib import Path


def main():
    endpoint = json.loads(Path("/data/.toolframe/endpoint.json").read_text())
    host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(endpoint["host"], endpoint["host"])
    if ":" in host:
        host = f"[{host}]"
    # A configured outbound proxy must not intercept the container's local probe.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(
        f"http://{host}:{int(endpoint['port'])}/healthz", timeout=3
    ) as reply:
        if reply.status != 200:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
