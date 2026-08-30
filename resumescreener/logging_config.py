"""
Structured JSON logging.

Every record is one JSON object on one line, which is what Filebeat harvests from
the container's stdout and hands to Logstash. Keeping the shape stable here is
what makes the Kibana dashboards in `deploy/elk/` work.
"""

import json
import logging
import logging.handlers
import sys
import time
from pathlib import Path
from typing import Any

# LogRecord attributes that are structural rather than caller-supplied context.
_RESERVED = {
    "args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName",
    "levelname", "levelno", "lineno", "module", "msecs", "message", "msg", "name",
    "pathname", "process", "processName", "relativeCreated", "stack_info",
    "thread", "threadName", "taskName",
}


class JsonFormatter(logging.Formatter):
    """Render a LogRecord as a single-line JSON object with ECS-friendly keys."""

    def __init__(self, service: str, environment: str):
        super().__init__()
        self.service = service
        self.environment = environment

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "@timestamp": time.strftime(
                "%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)
            ) + f".{int(record.msecs):03d}Z",
            "log.level": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
            "service.name": self.service,
            "service.environment": self.environment,
        }

        # Anything passed via logger.*(..., extra={...}) becomes a queryable field.
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value

        if record.exc_info:
            payload["error.type"] = record.exc_info[0].__name__
            payload["error.stack_trace"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO", json_logs: bool = True,
                      service: str = "resume-screener",
                      environment: str = "development",
                      log_file: str = "") -> None:
    """
    Install the root log handlers. Safe to call more than once.

    Always logs to stdout. When `log_file` is set, the same JSON lines are also
    written to a rotating file - the path Filebeat harvests when the app is not
    running in a container.
    """
    root = logging.getLogger()
    root.setLevel(level.upper())
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    if json_logs:
        handler.setFormatter(JsonFormatter(service, environment))
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")
        )
    root.addHandler(handler)

    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Rotation is not optional: an unbounded log file on a developer laptop
        # is how you fill a disk overnight.
        file_handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        # The file sink is always JSON even when the console is human-readable,
        # because a log shipper reads the file, not the terminal.
        file_handler.setFormatter(JsonFormatter(service, environment))
        root.addHandler(file_handler)

    # These duplicate our own structured request and screening lines, and would
    # otherwise dominate the Elasticsearch index.
    for noisy in ("werkzeug", "httpx", "httpcore", "google_genai", "google.genai"):
        logging.getLogger(noisy).setLevel("WARNING")
