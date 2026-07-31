"""Standalone entry point for the Network Maestro control panel."""

from __future__ import annotations

import os

from flask import Flask

from pikaraoke.routes.network_maestro_standalone import network_maestro_standalone_bp


def create_app() -> Flask:
    """Create a minimal Flask app dedicated to Network Maestro."""
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.secret_key = os.environ.get("NETWORK_MAESTRO_SECRET", "network-maestro-local")
    app.config["JSON_SORT_KEYS"] = False
    app.register_blueprint(network_maestro_standalone_bp)
    return app


app = create_app()


if __name__ == "__main__":
    host = os.environ.get("NETWORK_MAESTRO_HOST", "0.0.0.0")
    port = int(os.environ.get("NETWORK_MAESTRO_PORT", "5560"))
    try:
        from gevent.pywsgi import WSGIServer

        WSGIServer((host, port), app).serve_forever()
    except Exception:
        app.run(host=host, port=port)
