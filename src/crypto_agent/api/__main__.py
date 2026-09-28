"""python -m crypto_agent.api --demo (explicit demo; otherwise Paper only)."""

import argparse
from pathlib import Path

import uvicorn

from crypto_agent.api.app import create_app


def main():
    parser = argparse.ArgumentParser(description="Local read-only Alpaca Paper / explicit demo monitor")
    parser.add_argument(
        "--demo", action="store_true", help="Explicit fixed synthetic demo; no network or database"
    )
    parser.add_argument(
        "--config", type=Path, default=Path("config"), help="Existing project configuration directory"
    )
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--external-origin", help="Exact HTTPS .ts.net origin served by Tailscale Serve")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    uvicorn.run(
        create_app(demo_mode=args.demo, config_dir=args.config, external_origin=args.external_origin),
        host="127.0.0.1",
        port=args.port,
        log_level="warning",
        access_log=False,
    )


if __name__ == "__main__":
    main()
