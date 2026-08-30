"""End-to-end HTTP tests. The LLM is disabled, so the baseline engine serves."""

import io


def _post(client, resume_upload, jd, **overrides):
    data = {"resume": resume_upload(), "job_description": jd}
    data.update(overrides)
    return client.post("/analyze", data=data, content_type="multipart/form-data")


def test_index_renders(client):
    assert client.get("/").status_code == 200


def test_healthz_is_dependency_free(client):
    assert client.get("/healthz").get_json() == {"status": "ok"}


def test_readyz_reports_the_active_engine(client):
    body = client.get("/readyz").get_json()
    assert body["status"] == "ready"
    assert body["engine"] == "baseline"


def test_successful_analysis(client, resume_upload, job_description):
    response = _post(client, resume_upload, job_description)
    assert response.status_code == 200

    body = response.get_json()
    assert body["engine"] == "baseline"
    assert body["filename"] == "resume.txt"
    assert 0 <= body["result"]["overall_score"] <= 100
    assert body["result"]["verdict"]
    assert isinstance(body["result"]["suggestions"], list)


def test_missing_resume_is_rejected(client, job_description):
    response = client.post("/analyze", data={"job_description": job_description})
    assert response.status_code == 400
    assert "resume" in response.get_json()["error"].lower()


def test_short_job_description_is_rejected(client, resume_upload):
    response = _post(client, resume_upload, "hiring")
    assert response.status_code == 400
    assert "too short" in response.get_json()["error"]


def test_unsupported_file_type_is_rejected(client, resume_upload, job_description):
    response = _post(client, resume_upload, job_description,
                     resume=(io.BytesIO(b"binary"), "resume.exe"))
    assert response.status_code == 400
    assert "Unsupported file format" in response.get_json()["error"]


def test_near_empty_resume_is_rejected(client, resume_upload, job_description):
    response = _post(client, resume_upload, job_description,
                     resume=(io.BytesIO(b"hi"), "resume.txt"))
    assert response.status_code == 400
    assert "scanned or" in response.get_json()["error"]


def test_oversized_upload_is_rejected(app, resume_upload, job_description):
    app.config["MAX_CONTENT_LENGTH"] = 1024
    client = app.test_client()
    response = _post(client, resume_upload, job_description,
                     resume=(io.BytesIO(b"x" * 5000), "resume.txt"))
    assert response.status_code == 413
    assert "too large" in response.get_json()["error"].lower()


def test_every_response_carries_a_correlation_id(client, resume_upload, job_description):
    response = _post(client, resume_upload, job_description)
    assert response.headers["X-Request-ID"]


def test_supplied_correlation_id_is_echoed(client):
    response = client.get("/healthz", headers={"X-Request-ID": "trace-abc-123"})
    assert response.headers["X-Request-ID"] == "trace-abc-123"


def test_internal_errors_do_not_leak_details(app, monkeypatch, resume_upload, job_description):
    """A crash must not return exception text - it can carry paths and internals."""
    def _boom(*args, **kwargs):
        raise RuntimeError("/secret/path/to/internals.py exploded")

    monkeypatch.setattr("resumescreener.routes.screen_resume", _boom)
    app.config["TESTING"] = False  # let the error handler run, as in production
    client = app.test_client()

    response = _post(client, resume_upload, job_description)
    assert response.status_code == 500
    assert "secret" not in response.get_data(as_text=True)
    assert response.get_json()["error"] == "An unexpected error occurred."
