"""Flask application factory."""

import logging
import time
import uuid

from flask import Flask, g, jsonify, render_template, request
from werkzeug.exceptions import RequestEntityTooLarge

from .config import Config, load_config
from .logging_config import configure_logging
from .parsing import ParsingError
from .routes import api
from .screener import ScreeningError

logger = logging.getLogger(__name__)


def create_app(config: Config = None) -> Flask:
    config = config or load_config()

    configure_logging(
        level=config.log_level,
        json_logs=config.json_logs,
        service=config.service_name,
        environment=config.environment,
        log_file=config.log_file,
    )

    app = Flask(__name__, template_folder="../templates", static_folder="../static")
    app.config["SECRET_KEY"] = config.secret_key
    app.config["MAX_CONTENT_LENGTH"] = config.max_content_length
    app.config["APP_CONFIG"] = config

    app.register_blueprint(api)

    @app.before_request
    def _start_timer():
        # A correlation id ties every log line for one request together in Kibana.
        g.request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
        g.started = time.perf_counter()

    @app.after_request
    def _log_request(response):
        duration_ms = int((time.perf_counter() - getattr(g, "started", time.perf_counter())) * 1000)
        # Health probes fire constantly and would drown the index.
        if request.path not in ("/healthz", "/readyz"):
            logger.info(
                "request completed",
                extra={
                    "http.request.method": request.method,
                    "url.path": request.path,
                    "http.response.status_code": response.status_code,
                    "event.duration_ms": duration_ms,
                    "trace.id": getattr(g, "request_id", None),
                },
            )
        response.headers["X-Request-ID"] = getattr(g, "request_id", "")
        return response

    @app.route("/")
    def index():
        return render_template("index.html")

    @app.errorhandler(RequestEntityTooLarge)
    def _too_large(_):
        limit_mb = config.max_content_length // (1024 * 1024)
        return jsonify({"error": f"File is too large. Maximum size is {limit_mb}MB."}), 413

    @app.errorhandler(ParsingError)
    def _parse_error(exc):
        return jsonify({"error": str(exc)}), 400

    @app.errorhandler(ScreeningError)
    def _screening_error(exc):
        logger.error("screening failed", extra={"reason": str(exc)})
        return jsonify({"error": "Screening failed. Please try again."}), 503

    @app.errorhandler(Exception)
    def _unhandled(exc):
        # Log the detail; return an opaque message. Leaking str(exc) to the client
        # can expose file paths, API errors, and internal structure.
        logger.exception("unhandled error", extra={"trace.id": getattr(g, "request_id", None)})
        return jsonify({"error": "An unexpected error occurred."}), 500

    return app
