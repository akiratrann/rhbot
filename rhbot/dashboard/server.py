"""Flask dashboard. Read-only: it observes the engine, it never places orders.

Runs in a background thread bound to localhost. Exposes:
  GET /            -> HTML dashboard (auto-refreshing)
  GET /api/state   -> JSON snapshot of the engine
"""

from __future__ import annotations

import logging
import threading

from flask import Flask, jsonify, render_template

from ..engine import Engine

log = logging.getLogger("rhbot.dashboard")


def make_app(engine: Engine) -> Flask:
    app = Flask(__name__)

    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/api/state")
    def state():
        return jsonify(engine.snapshot())

    return app


def start_dashboard(engine: Engine, host: str, port: int) -> threading.Thread:
    app = make_app(engine)

    def _run():
        # threaded=True so /api/state stays responsive; no reloader in a thread.
        app.run(host=host, port=port, threaded=True,
                use_reloader=False, debug=False)

    t = threading.Thread(target=_run, name="dashboard", daemon=True)
    t.start()
    log.info("Dashboard: http://%s:%s", host, port)
    return t
