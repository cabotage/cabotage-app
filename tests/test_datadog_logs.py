"""Tenant log export is explicit, credential-isolated, and pod-scoped."""

from types import SimpleNamespace
from unittest.mock import patch

import kubernetes.client
import pytest
from flask import Flask

import cabotage.celery.tasks.deploy as deploy_module
from cabotage.server.models.projects import Configuration, EnvironmentConfiguration


def _config(value):
    return SimpleNamespace(secret=False, read_value=lambda reader: value)


def _make_release(configuration=None, organization="tenant", application="app"):
    org = SimpleNamespace(slug=organization, k8s_identifier=organization)
    project = SimpleNamespace(
        slug="project", k8s_identifier="project", organization=org
    )
    app = SimpleNamespace(slug=application, k8s_identifier=application, project=project)
    environment = SimpleNamespace(
        slug="default",
        k8s_identifier="default",
        k8s_namespace=organization,
        uses_environment_namespace=False,
        is_default=True,
        ephemeral=False,
    )
    return SimpleNamespace(
        application=app,
        application_environment=SimpleNamespace(
            environment=environment,
            process_counts={},
            process_pod_classes={},
        ),
        configuration_objects=configuration or {},
        job_processes={"job-cleanup": {"env": [("SCHEDULE", "0 0 * * *")]}},
        repository_name=f"{organization}/project/{application}",
        version=1,
        commit_sha="0123456789abcdef",
        health_check_host=None,
        health_check_path="/",
    )


def _enabled_config(key="tenant-key", site="datadoghq.eu"):
    return {
        "DD_LOGS_ENABLED": _config("true"),
        "DD_API_KEY": _config(key),
        "DD_SITE": _config(site),
    }


@pytest.fixture
def app():
    app = Flask(__name__)
    app.config.update(
        REGISTRY_PULL="registry.example.test",
        SIDECAR_IMAGE="ghcr.io/cabotage/containers/sidecar-rs:1.0",
        DATADOG_IMAGE="datadog/agent:7.55.2",
        DATADOG_LOGS_IMAGE="cr.fluentbit.io/fluent/fluent-bit:4.2.2",
    )
    with app.app_context():
        yield app


@pytest.fixture(
    params=[
        (deploy_module.render_deployment, "web-api"),
        (deploy_module.render_job, "release"),
        (deploy_module.render_cronjob, "job-cleanup"),
    ],
    ids=["deployment", "job", "cronjob"],
)
def workload(request):
    return request.param


def _render(workload, release):
    render, process = workload
    resource = render("tenant", release, "tenant-sa", process, "deployment-1")
    payload = kubernetes.client.ApiClient().sanitize_for_serialization(resource)
    if render is deploy_module.render_cronjob:
        return payload["spec"]["jobTemplate"]["spec"]["template"]
    return payload["spec"]["template"]


def _collector(pod):
    return next(c for c in pod["spec"]["initContainers"] if c["name"] == "datadog-logs")


def _env(container):
    return {e["name"]: e.get("value") for e in container["env"]}


@pytest.mark.parametrize("flag", [None, "false", "", "0"])
def test_key_alone_never_enables_log_export(app, workload, flag):
    configuration = _enabled_config()
    if flag is None:
        configuration.pop("DD_LOGS_ENABLED")
    else:
        configuration["DD_LOGS_ENABLED"] = _config(flag)
    pod = _render(workload, _make_release(configuration))
    assert "datadog-logs" not in {c["name"] for c in pod["spec"]["initContainers"]}
    assert not any("hostPath" in v for v in pod["spec"]["volumes"])
    assert "annotations" not in pod["metadata"]


def test_log_opt_in_preserves_existing_containers(app, workload):
    configuration = _enabled_config()
    configuration["DD_IMAGE"] = _config("datadog/agent:existing-override")
    configuration["DD_LOGS_ENABLED"] = _config("false")
    release = _make_release(configuration)
    before = _render(workload, release)["spec"]

    configuration["DD_LOGS_ENABLED"] = _config("true")
    after = _render(workload, release)["spec"]

    assert after["containers"] == before["containers"]
    assert [
        c for c in after["initContainers"] if c["name"] != "datadog-logs"
    ] == before["initContainers"]


def test_disabled_export_does_not_read_new_destination_secrets(app):
    configuration = {"DD_API_KEY": _config("existing-key")}
    configuration["DD_SITE"] = Configuration(
        name="DD_SITE",
        secret=True,
        buildtime=False,
        key_slug="vault:tenant/project/app/configuration/DD_SITE/1",
    )
    with patch.object(
        deploy_module.config_writer, "read", side_effect=KeyError("unavailable")
    ):
        spec = deploy_module.render_podspec(
            _make_release(configuration), "worker", "sa"
        )
    agent = next(c for c in spec.init_containers if c.name == "dogstatsd-sidecar")
    assert {e.name: e.value for e in agent.env}["DD_API_KEY"] == "existing-key"


def test_collector_can_read_only_its_own_application_logs(app, workload):
    pod = _render(workload, _make_release(_enabled_config()))
    collector = _collector(pod)
    assert pod["metadata"]["annotations"] == {"ad.datadoghq.com/logs_exclude": "true"}
    assert collector["restartPolicy"] == "Always"
    assert collector["volumeMounts"][0] == {
        "name": "datadog-pod-logs",
        "mountPath": "/var/log/cabotage-pod",
        "subPathExpr": "$(POD_NAMESPACE)_$(POD_NAME)_$(POD_UID)",
        "readOnly": True,
    }
    identity = {
        e["name"]: e["valueFrom"]["fieldRef"]["fieldPath"]
        for e in collector["env"]
        if "valueFrom" in e
    }
    assert identity == {
        "POD_NAMESPACE": "metadata.namespace",
        "POD_NAME": "metadata.name",
        "POD_UID": "metadata.uid",
    }
    assert f"path=/var/log/cabotage-pod/{workload[1]}/*.log" in collector["args"]
    assert "multiline.parser=cri" in collector["args"]
    assert "read_from_head=true" in collector["args"]
    assert "tls=on" in collector["args"]
    assert "tls.verify=on" in collector["args"]
    assert "tenant-key" not in " ".join(collector["args"])
    assert collector["securityContext"]["allowPrivilegeEscalation"] is False
    assert collector["securityContext"]["readOnlyRootFilesystem"] is True
    for container in pod["spec"]["containers"] + pod["spec"]["initContainers"]:
        if container["name"] != "datadog-logs":
            assert "datadog-pod-logs" not in {
                v["name"] for v in container.get("volumeMounts", [])
            }


def test_tenant_destinations_and_credentials_never_cross(app, workload):
    app.config["DD_API_KEY"] = "operator-key"
    for tenant, key, site in [
        ("tenant-a", "key-a", "datadoghq.eu"),
        ("tenant-b", "key-b", "us3.datadoghq.com"),
    ]:
        pod = _render(workload, _make_release(_enabled_config(key, site), tenant))
        collector = _collector(pod)
        assert _env(collector)["DD_API_KEY"] == key
        assert f"host=http-intake.logs.{site}" in collector["args"]
        for container in pod["spec"]["initContainers"]:
            if container["name"] == "dogstatsd-sidecar":
                env = _env(container)
                assert env["DD_API_KEY"] == key
                assert env["DD_LOGS_ENABLED"] == "false"


@pytest.mark.parametrize("key", [None, "", "**secret**"])
def test_opt_in_without_tenant_key_fails_without_operator_fallback(app, key):
    app.config["DD_API_KEY"] = "operator-key"
    configuration = _enabled_config()
    if key is None:
        configuration.pop("DD_API_KEY")
    else:
        configuration["DD_API_KEY"] = _config(key)
    with pytest.raises(deploy_module.DeployError, match="application's DD_API_KEY"):
        deploy_module.render_podspec(_make_release(configuration), "worker", "sa")


@pytest.mark.parametrize("site", [None, "", "https://datadoghq.eu", "customer.example"])
def test_opt_in_requires_an_explicit_datadog_site(app, site):
    configuration = _enabled_config()
    if site is None:
        configuration.pop("DD_SITE")
    else:
        configuration["DD_SITE"] = _config(site)
    with pytest.raises(deploy_module.DeployError, match="supported DD_SITE"):
        deploy_module.render_podspec(_make_release(configuration), "worker", "sa")


@pytest.mark.parametrize("model", [Configuration, EnvironmentConfiguration])
def test_runtime_only_api_keys_are_resolved_from_the_tenant_secret(app, model):
    configuration = _enabled_config()
    configuration["DD_API_KEY"] = model(
        name="DD_API_KEY",
        secret=True,
        buildtime=False,
        key_slug="vault:tenant/project/app/configuration/DD_API_KEY/2",
    )
    with patch.object(deploy_module.config_writer, "read") as reader:
        reader.return_value = {"data": {"DD_API_KEY": "runtime-only-tenant-key"}}
        spec = deploy_module.render_podspec(
            _make_release(configuration), "worker", "sa"
        )
    collector = next(c for c in spec.init_containers if c.name == "datadog-logs")
    assert {e.name: e.value for e in collector.env}[
        "DD_API_KEY"
    ] == "runtime-only-tenant-key"
    reader.assert_called_once_with(
        "tenant/project/app/configuration/DD_API_KEY/2",
        secret=True,
    )


def test_unreadable_tenant_secret_aborts_export(app):
    configuration = _enabled_config()
    configuration["DD_API_KEY"] = Configuration(
        name="DD_API_KEY",
        secret=True,
        buildtime=False,
        key_slug="vault:tenant/project/app/configuration/DD_API_KEY/2",
    )
    with patch.object(
        deploy_module.config_writer, "read", side_effect=KeyError("unavailable")
    ):
        with pytest.raises(KeyError):
            deploy_module.render_podspec(_make_release(configuration), "worker", "sa")


def test_disabling_logs_removes_collector_and_host_mounts(app, workload):
    release = _make_release(_enabled_config())
    assert _collector(_render(workload, release))["name"] == "datadog-logs"
    release.configuration_objects["DD_LOGS_ENABLED"] = _config("false")
    pod = _render(workload, release)
    assert "datadog-logs" not in {c["name"] for c in pod["spec"]["initContainers"]}
    assert not any("hostPath" in v for v in pod["spec"]["volumes"])
