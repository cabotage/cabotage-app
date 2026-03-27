"""Tests for job process type: podspec rendering, CronJob rendering, history limits."""

from unittest.mock import MagicMock, patch

import kubernetes.client
import pytest

import cabotage.celery.tasks.deploy as deploy_module
from cabotage.celery.tasks.deploy import (
    _get_job_schedule,
    _history_limit_for_schedule,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DEPLOY_MODULE = "cabotage.celery.tasks.deploy"
_PATCHES = [
    f"{_DEPLOY_MODULE}.render_cabotage_enroller_container",
    f"{_DEPLOY_MODULE}.render_cabotage_sidecar_container",
    f"{_DEPLOY_MODULE}.render_cabotage_sidecar_tls_container",
    f"{_DEPLOY_MODULE}.render_process_container",
    f"{_DEPLOY_MODULE}.render_datadog_container",
    f"{_DEPLOY_MODULE}.k8s_label_value",
]


def _make_release(job_processes=None):
    """Build a mock release with job_processes support."""
    release = MagicMock()
    release.application.project.organization.slug = "test-org"
    release.application.project.slug = "test-project"
    release.application.slug = "test-app"
    release.version = 1
    release.application.privileged = False

    app_env = MagicMock()
    app_env.k8s_identifier = None
    env_obj = MagicMock()
    env_obj.ephemeral = False
    env_obj.slug = "default"
    app_env.environment = env_obj
    app_env.process_counts = {}
    app_env.process_pod_classes = {}
    release.application_environment = app_env

    release.configuration_objects = {}
    release.job_processes = job_processes or {}

    return release


@pytest.fixture(autouse=True)
def _mock_sub_renderers():
    """Mock all sub-render functions to return simple placeholders."""
    patchers = []
    for target in _PATCHES:
        p = patch(target, return_value=MagicMock(spec=kubernetes.client.V1Container))
        p.start()
        patchers.append(p)
    yield
    for p in patchers:
        p.stop()


@pytest.fixture()
def mock_app():
    mock = MagicMock()
    mock.config = {}
    with patch.object(deploy_module, "current_app", mock):
        yield mock


# ---------------------------------------------------------------------------
# _get_job_schedule
# ---------------------------------------------------------------------------


class TestGetJobSchedule:
    def test_extracts_schedule(self):
        proc = {"cmd": "python cleanup.py", "env": [("SCHEDULE", "0 */6 * * *")]}
        assert _get_job_schedule(proc) == "0 */6 * * *"

    def test_returns_none_when_missing(self):
        proc = {"cmd": "python cleanup.py", "env": [("FOO", "bar")]}
        assert _get_job_schedule(proc) is None

    def test_returns_none_for_empty_env(self):
        proc = {"cmd": "python cleanup.py", "env": []}
        assert _get_job_schedule(proc) is None

    def test_picks_schedule_from_multiple_env(self):
        proc = {
            "cmd": "python cleanup.py",
            "env": [("RETRIES", "3"), ("SCHEDULE", "30 2 * * 1")],
        }
        assert _get_job_schedule(proc) == "30 2 * * 1"


# ---------------------------------------------------------------------------
# _history_limit_for_schedule
# ---------------------------------------------------------------------------


class TestHistoryLimit:
    def test_every_5_minutes(self):
        limit = _history_limit_for_schedule("*/5 * * * *")
        assert limit == 144  # 12 * 60 / 5

    def test_hourly(self):
        limit = _history_limit_for_schedule("0 * * * *")
        assert limit == 12

    def test_daily_floors_to_3(self):
        limit = _history_limit_for_schedule("0 0 * * *")
        assert limit >= 3

    def test_every_minute(self):
        limit = _history_limit_for_schedule("* * * * *")
        assert limit == 720  # 12 * 60

    def test_every_6_hours(self):
        limit = _history_limit_for_schedule("0 */6 * * *")
        # 12h / 6h = 2, but floor is 3
        assert limit >= 3


# ---------------------------------------------------------------------------
# render_podspec for job processes
# ---------------------------------------------------------------------------


class TestRenderPodspecJob:
    def test_job_process_has_on_failure_restart(self, mock_app):
        release = _make_release()
        pod_spec = deploy_module.render_podspec(release, "job-cleanup", "sa-name")
        assert pod_spec.restart_policy == "OnFailure"

    def test_job_process_has_init_containers(self, mock_app):
        release = _make_release()
        pod_spec = deploy_module.render_podspec(release, "job-cleanup", "sa-name")
        # enroller + sidecar
        assert len(pod_spec.init_containers) == 2

    def test_job_process_has_one_container(self, mock_app):
        release = _make_release()
        pod_spec = deploy_module.render_podspec(release, "job-cleanup", "sa-name")
        assert len(pod_spec.containers) == 1

    def test_job_variant_name(self, mock_app):
        """job-reports should also match the job branch."""
        release = _make_release()
        pod_spec = deploy_module.render_podspec(release, "job-reports", "sa-name")
        assert pod_spec.restart_policy == "OnFailure"

    def test_node_selector_applies_to_job(self, mock_app):
        mock_app.config["STANDARD_POOL"] = "standard"
        release = _make_release()
        pod_spec = deploy_module.render_podspec(release, "job-cleanup", "sa-name")
        assert pod_spec.node_selector == {"cabotage.dev/node-pool": "standard"}


# ---------------------------------------------------------------------------
# render_cronjob
# ---------------------------------------------------------------------------


class TestRenderCronjob:
    def test_renders_cronjob_with_schedule(self, mock_app):
        job_procs = {
            "job-cleanup": {
                "cmd": "python cleanup.py",
                "env": [("SCHEDULE", "0 */6 * * *")],
            }
        }
        release = _make_release(job_processes=job_procs)

        with patch(f"{_DEPLOY_MODULE}.k8s_resource_prefix", return_value="proj-app"):
            cj = deploy_module.render_cronjob(
                "test-ns", release, "sa-name", "job-cleanup", "deploy-123"
            )

        assert cj.spec.schedule == "0 */6 * * *"
        assert cj.spec.concurrency_policy == "Forbid"
        assert cj.metadata.name == "proj-app-job-cleanup"
        assert cj.metadata.labels["resident-cronjob.cabotage.io"] == "true"
        assert cj.metadata.labels["process"] == "job-cleanup"
        # Job template gets resident-job label, not resident-pod
        job_tmpl_labels = cj.spec.job_template.metadata.labels
        assert job_tmpl_labels["resident-job.cabotage.io"] == "true"
        assert "resident-pod.cabotage.io" not in job_tmpl_labels
        # Pod template gets resident-pod label, not resident-job
        pod_tmpl_labels = cj.spec.job_template.spec.template.metadata.labels
        assert pod_tmpl_labels["resident-pod.cabotage.io"] == "true"
        assert "resident-job.cabotage.io" not in pod_tmpl_labels

    def test_cronjob_suspended_when_count_zero(self, mock_app):
        job_procs = {
            "job-cleanup": {
                "cmd": "python cleanup.py",
                "env": [("SCHEDULE", "0 * * * *")],
            }
        }
        release = _make_release(job_processes=job_procs)
        release.application_environment.process_counts = {"job-cleanup": 0}

        with patch(f"{_DEPLOY_MODULE}.k8s_resource_prefix", return_value="proj-app"):
            cj = deploy_module.render_cronjob(
                "test-ns", release, "sa-name", "job-cleanup", "deploy-123"
            )

        assert cj.spec.suspend is True

    def test_cronjob_active_when_count_nonzero(self, mock_app):
        job_procs = {
            "job-cleanup": {
                "cmd": "python cleanup.py",
                "env": [("SCHEDULE", "0 * * * *")],
            }
        }
        release = _make_release(job_processes=job_procs)
        release.application_environment.process_counts = {"job-cleanup": 1}

        with patch(f"{_DEPLOY_MODULE}.k8s_resource_prefix", return_value="proj-app"):
            cj = deploy_module.render_cronjob(
                "test-ns", release, "sa-name", "job-cleanup", "deploy-123"
            )

        assert cj.spec.suspend is False

    def test_cronjob_raises_without_schedule(self, mock_app):
        job_procs = {
            "job-cleanup": {"cmd": "python cleanup.py", "env": []},
        }
        release = _make_release(job_processes=job_procs)

        with patch(f"{_DEPLOY_MODULE}.k8s_resource_prefix", return_value="proj-app"):
            with pytest.raises(deploy_module.DeployError, match="SCHEDULE"):
                deploy_module.render_cronjob(
                    "test-ns", release, "sa-name", "job-cleanup", "deploy-123"
                )

    def test_cronjob_history_limit_matches_schedule(self, mock_app):
        job_procs = {
            "job-frequent": {
                "cmd": "python ping.py",
                "env": [("SCHEDULE", "*/5 * * * *")],
            }
        }
        release = _make_release(job_processes=job_procs)

        with patch(f"{_DEPLOY_MODULE}.k8s_resource_prefix", return_value="proj-app"):
            cj = deploy_module.render_cronjob(
                "test-ns", release, "sa-name", "job-frequent", "deploy-123"
            )

        assert cj.spec.successful_jobs_history_limit == 144
        assert cj.spec.failed_jobs_history_limit == 144

    def test_cronjob_job_template_has_backoff_zero(self, mock_app):
        job_procs = {
            "job-cleanup": {
                "cmd": "python cleanup.py",
                "env": [("SCHEDULE", "0 0 * * *")],
            }
        }
        release = _make_release(job_processes=job_procs)

        with patch(f"{_DEPLOY_MODULE}.k8s_resource_prefix", return_value="proj-app"):
            cj = deploy_module.render_cronjob(
                "test-ns", release, "sa-name", "job-cleanup", "deploy-123"
            )

        assert cj.spec.job_template.spec.backoff_limit == 0
        assert cj.spec.job_template.spec.active_deadline_seconds == 3600
