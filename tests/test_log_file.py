"""The file sink is how logs reach Filebeat when the app is not containerised."""

import json

from resumescreener.logging_config import configure_logging


def test_writes_json_lines_to_the_configured_file(tmp_path):
    import logging

    log_path = tmp_path / "nested" / "app.jsonl"
    configure_logging(json_logs=False, log_file=str(log_path))   # console stays human-readable
    logging.getLogger("test").info("screening completed", extra={"engine": "gemini"})
    logging.shutdown()

    assert log_path.exists(), "the parent directory should be created automatically"
    record = json.loads(log_path.read_text(encoding="utf-8").strip().splitlines()[0])
    # The file sink is JSON even when the console formatter is not.
    assert record["message"] == "screening completed"
    assert record["engine"] == "gemini"

    configure_logging(json_logs=False)   # detach the file handler for later tests
