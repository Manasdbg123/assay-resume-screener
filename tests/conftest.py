import io

import pytest

from resumescreener import create_app
from resumescreener.config import Config

RESUME = """
Priya Raghavan - Senior Backend Engineer

EXPERIENCE
Staff Engineer, Northwind Data (2019-2025)
Led the migration of a monolithic Django application to Kubernetes, cutting p99
latency from 1.8s to 240ms. Owned the PostgreSQL sharding strategy across 12 shards.
Built the observability stack: Filebeat to Logstash to Elasticsearch, with Kibana
dashboards used by four teams.

Backend Engineer, Corvus Systems (2016-2019)
Wrote Python services and REST APIs on AWS. Introduced pytest and raised coverage
from 11% to 78%.

EDUCATION
BSc Computer Science, University of Pune

SKILLS
Python, Django, PostgreSQL, Kubernetes, Docker, AWS, Terraform, Elasticsearch
"""

JOB_DESCRIPTION = """
Senior Backend Engineer

We are looking for a senior backend engineer with at least 5 years of experience
building production Python services. You will own our API platform and its
migration to Kubernetes.

Requirements:
- 5+ years of Python, ideally with Django or FastAPI
- Strong PostgreSQL experience including query optimisation
- Kubernetes and Docker in production
- AWS
- Bachelor's degree in Computer Science or equivalent experience
"""


@pytest.fixture
def config():
    """Config with the LLM disabled, so tests never make a network call."""
    return Config(
        anthropic_api_key="",
        llm_enabled=False,
        json_logs=False,
        log_level="WARNING",
        secret_key="test",
    )


@pytest.fixture
def app(config):
    application = create_app(config)
    application.config["TESTING"] = True
    return application


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def resume_text():
    return RESUME


@pytest.fixture
def job_description():
    return JOB_DESCRIPTION


@pytest.fixture
def resume_upload():
    def _make(content=RESUME, filename="resume.txt"):
        return (io.BytesIO(content.encode("utf-8")), filename)
    return _make
