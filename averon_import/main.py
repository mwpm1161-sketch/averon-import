from __future__ import annotations

import argparse
import threading
import webbrowser

import uvicorn

from averon_import.application import create_app

app = create_app()


def cli() -> None:
    parser = argparse.ArgumentParser(description="Запуск Averon Import")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    if not args.no_browser:
        threading.Timer(
            1.2, lambda: webbrowser.open(f"http://{args.host}:{args.port}")
        ).start()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    cli()
