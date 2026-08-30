import json
import logging

from resumescreener.logging_config import JsonFormatter


def _record(**kwargs):
    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname="x.py", lineno=1,
        msg=kwargs.pop("msg", "hello"), args=(), exc_info=kwargs.pop("exc_info", None),
    )
    for key, value in kwargs.items():
        setattr(record, key, value)
    return record


def test_emits_single_line_json():
    output = JsonFormatter("screener", "test").format(_record())
    assert "\n" not in output
    parsed = json.loads(output)
    assert parsed["message"] == "hello"
    assert parsed["log.level"] == "INFO"
    assert parsed["service.name"] == "screener"


def test_extra_fields_become_queryable_keys():
    """Filebeat lifts these into Elasticsearch fields - they must survive."""
    output = JsonFormatter("screener", "test").format(
        _record(engine="claude", overall_score=82.5, degraded=False)
    )
    parsed = json.loads(output)
    assert parsed["engine"] == "claude"
    assert parsed["overall_score"] == 82.5
    assert parsed["degraded"] is False


def test_exceptions_are_serialised_not_raised():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys
        output = JsonFormatter("screener", "test").format(_record(exc_info=sys.exc_info()))
    parsed = json.loads(output)
    assert parsed["error.type"] == "ValueError"
    assert "boom" in parsed["error.stack_trace"]


def test_unserialisable_values_do_not_break_the_log_line():
    output = JsonFormatter("screener", "test").format(_record(weird=object()))
    assert json.loads(output)["weird"].startswith("<object")
