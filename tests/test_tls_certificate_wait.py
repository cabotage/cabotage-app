"""Tests for retrying failed cert-manager issuance while waiting on TLS."""

import copy
from unittest.mock import MagicMock, patch

import pytest
from kubernetes.client.rest import ApiException

from cabotage.celery.tasks.deploy import _wait_for_tls_certificate

NOT_READY = {"type": "Ready", "status": "False", "reason": "DoesNotExist"}

# cert-manager backing off after Let's Encrypt returned a transient 404.
FAILED_CERT = {
    "metadata": {
        "name": "app-web-tls",
        "namespace": "org-pr-1",
        "generation": 1,
        "resourceVersion": "42",
    },
    "status": {
        "conditions": [
            NOT_READY,
            {"type": "Issuing", "status": "False", "reason": "Failed"},
        ],
        "lastFailureTime": "2026-09-28T16:10:03Z",
    },
}
READY_CERT = {"status": {"conditions": [{"type": "Ready", "status": "True"}]}}


@pytest.fixture
def custom_api():
    api = MagicMock()
    with (
        patch("kubernetes.client.CustomObjectsApi", return_value=api),
        patch("cabotage.celery.tasks.deploy.time.sleep"),
    ):
        yield api


def _wait():
    return _wait_for_tls_certificate(None, "org-pr-1", "app-web-tls", timeout=60)


def test_failed_issuance_is_retried_once(custom_api):
    custom_api.get_namespaced_custom_object.side_effect = [FAILED_CERT] * 3 + [
        READY_CERT
    ]

    assert _wait() is True

    custom_api.patch_namespaced_custom_object_status.assert_called_once()
    *target, body = custom_api.patch_namespaced_custom_object_status.call_args.args
    assert target == [
        "cert-manager.io",
        "v1",
        "org-pr-1",
        "certificates",
        "app-web-tls",
    ]
    assert body["metadata"]["resourceVersion"] == "42"
    # Merge patch replaces the list: Ready must survive, Issuing flips to True
    # with a transition newer than the failure so the failed request is replaced.
    ready, issuing = body["status"]["conditions"]
    assert ready == NOT_READY
    assert issuing["type"] == "Issuing" and issuing["status"] == "True"
    assert issuing["lastTransitionTime"] > "2026-09-28T16:10:03Z"


def test_issuance_in_progress_is_not_retried(custom_api):
    in_progress = copy.deepcopy(FAILED_CERT)
    in_progress["status"] = {
        "conditions": [NOT_READY, {"type": "Issuing", "status": "True"}],
        "lastFailureTime": "2026-09-28T16:10:03Z",
    }
    custom_api.get_namespaced_custom_object.side_effect = [in_progress, READY_CERT]

    assert _wait() is True

    custom_api.patch_namespaced_custom_object_status.assert_not_called()


def test_rejected_retry_does_not_fail_the_deploy(custom_api):
    custom_api.get_namespaced_custom_object.side_effect = [
        FAILED_CERT,
        FAILED_CERT,
        READY_CERT,
    ]
    custom_api.patch_namespaced_custom_object_status.side_effect = ApiException(
        status=403
    )

    assert _wait() is True

    custom_api.patch_namespaced_custom_object_status.assert_called_once()
