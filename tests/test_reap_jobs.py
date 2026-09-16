"""Tests for the job reaper: helper functions and the reap_finished_jobs task."""

import datetime
import os
from unittest.mock import MagicMock, patch

from cabotage.celery.tasks.reap_jobs import (
    _is_finished,
    _is_succeeded,
    _parse_datetime,
    _extract_resources,
    _reap_limit,
    DEFAULT_REAP_LIMIT,
)

# ---------------------------------------------------------------------------
# Fake K8s objects
# ---------------------------------------------------------------------------


def _make_condition(type_, status="True"):
    cond = MagicMock()
    cond.type = type_
    cond.status = status
    return cond


def _make_job(
    name="test-job",
    namespace="test-ns",
    conditions=None,
    labels=None,
    annotations=None,
    start_time=None,
    completion_time=None,
    active=0,
    succeeded=0,
    failed=0,
    containers=None,
):
    job = MagicMock()
    job.metadata.name = name
    job.metadata.namespace = namespace
    job.metadata.labels = labels or {}
    job.metadata.annotations = annotations or {}
    job.status.conditions = conditions
    job.status.start_time = start_time
    job.status.completion_time = completion_time
    job.status.active = active
    job.status.succeeded = succeeded
    job.status.failed = failed
    if containers is not None:
        job.spec.template.spec.containers = containers
    else:
        job.spec.template.spec.containers = []
    return job


def _make_container(name, requests=None, limits=None):
    container = MagicMock()
    container.name = name
    if requests or limits:
        container.resources = MagicMock()
        container.resources.requests = requests
        container.resources.limits = limits
    else:
        container.resources = None
    return container


# ---------------------------------------------------------------------------
# _is_finished
# ---------------------------------------------------------------------------


class TestIsFinished:
    def test_no_conditions(self):
        job = _make_job(conditions=None)
        assert _is_finished(job) is False

    def test_empty_conditions(self):
        job = _make_job(conditions=[])
        assert _is_finished(job) is False

    def test_complete(self):
        job = _make_job(conditions=[_make_condition("Complete")])
        assert _is_finished(job) is True

    def test_failed(self):
        job = _make_job(conditions=[_make_condition("Failed")])
        assert _is_finished(job) is True

    def test_complete_false_status(self):
        job = _make_job(conditions=[_make_condition("Complete", "False")])
        assert _is_finished(job) is False

    def test_running_not_finished(self):
        job = _make_job(conditions=[_make_condition("Running")])
        assert _is_finished(job) is False


# ---------------------------------------------------------------------------
# _is_succeeded
# ---------------------------------------------------------------------------


class TestIsSucceeded:
    def test_complete(self):
        job = _make_job(conditions=[_make_condition("Complete")])
        assert _is_succeeded(job) is True

    def test_failed(self):
        job = _make_job(conditions=[_make_condition("Failed")])
        assert _is_succeeded(job) is False

    def test_no_conditions(self):
        job = _make_job(conditions=None)
        assert _is_succeeded(job) is False


# ---------------------------------------------------------------------------
# _parse_datetime
# ---------------------------------------------------------------------------


class TestParseDatetime:
    def test_none(self):
        assert _parse_datetime(None) is None

    def test_datetime_passthrough(self):
        dt = datetime.datetime(2026, 3, 27, 14, 2, 0)
        assert _parse_datetime(dt) is dt

    def test_iso_string_with_z(self):
        result = _parse_datetime("2026-03-27T18:02:00Z")
        assert result.year == 2026
        assert result.month == 3
        assert result.hour == 18

    def test_iso_string_with_offset(self):
        result = _parse_datetime("2026-03-27T14:02:00+00:00")
        assert result.hour == 14


# ---------------------------------------------------------------------------
# _extract_resources
# ---------------------------------------------------------------------------


class TestExtractResources:
    def test_extracts_matching_container(self):
        container = _make_container(
            "job-cleanup",
            requests={"cpu": "500m", "memory": "1Gi"},
            limits={"cpu": "1", "memory": "1536Mi"},
        )
        job = _make_job(
            labels={"process": "job-cleanup"},
            containers=[container],
        )
        result = _extract_resources(job)
        assert result == {
            "requests": {"cpu": "500m", "memory": "1Gi"},
            "limits": {"cpu": "1", "memory": "1536Mi"},
        }

    def test_skips_non_matching_containers(self):
        init = _make_container("cabotage-sidecar")
        process = _make_container(
            "job-cleanup",
            requests={"cpu": "250m", "memory": "512Mi"},
            limits={"cpu": "500m", "memory": "768Mi"},
        )
        job = _make_job(
            labels={"process": "job-cleanup"},
            containers=[init, process],
        )
        result = _extract_resources(job)
        assert result["requests"]["cpu"] == "250m"

    def test_no_matching_container(self):
        container = _make_container("something-else")
        job = _make_job(
            labels={"process": "job-cleanup"},
            containers=[container],
        )
        assert _extract_resources(job) is None

    def test_no_resources_on_container(self):
        container = _make_container("job-cleanup", requests=None, limits=None)
        job = _make_job(
            labels={"process": "job-cleanup"},
            containers=[container],
        )
        assert _extract_resources(job) is None

    def test_requests_only(self):
        container = _make_container(
            "job-cleanup",
            requests={"cpu": "100m"},
            limits=None,
        )
        job = _make_job(
            labels={"process": "job-cleanup"},
            containers=[container],
        )
        result = _extract_resources(job)
        assert result == {"requests": {"cpu": "100m"}}

    def test_empty_containers(self):
        job = _make_job(
            labels={"process": "job-cleanup"},
            containers=[],
        )
        assert _extract_resources(job) is None

    def test_no_process_label(self):
        container = _make_container(
            "job-cleanup",
            requests={"cpu": "100m"},
            limits=None,
        )
        job = _make_job(labels={}, containers=[container])
        assert _extract_resources(job) is None


# ---------------------------------------------------------------------------
# _reap_limit
# ---------------------------------------------------------------------------


class TestReapLimit:
    def test_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CABOTAGE_JOBS_REAPED_PER_RUN", None)
            assert _reap_limit() == DEFAULT_REAP_LIMIT

    def test_from_env(self):
        with patch.dict(os.environ, {"CABOTAGE_JOBS_REAPED_PER_RUN": "25"}):
            assert _reap_limit() == 25

    def test_invalid_env_falls_back(self):
        with patch.dict(os.environ, {"CABOTAGE_JOBS_REAPED_PER_RUN": "nope"}):
            assert _reap_limit() == DEFAULT_REAP_LIMIT
