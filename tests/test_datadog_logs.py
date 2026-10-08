"""Tenant log export is explicit, credential-isolated, and needs no pod access."""

from base64 import b64decode
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import kubernetes.client
import pytest
import yaml
from flask import Flask
from kubernetes.client.exceptions import ApiException

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
        LOG_EXPORT_ENABLED=True,
        LOG_EXPORT_NAMESPACE="cabotage",
        LOG_EXPORT_SECRET="tenant-log-export",
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


def _output(release):
    document = yaml.safe_load(deploy_module.render_log_export_output(release))
    (output,) = document["pipeline"]["outputs"]
    return output


class _Secrets:
    """In-memory stand-in for the exporter Secret's strategic-merge patches."""

    def __init__(self, data=None, exists=True):
        self.data = dict(data or {})
        self.exists = exists
        self.api = MagicMock()
        self.api.read_namespaced_secret.side_effect = self._read
        self.api.patch_namespaced_secret.side_effect = self._patch

    def _read(self, name, namespace):
        assert (name, namespace) == ("tenant-log-export", "cabotage")
        if not self.exists:
            raise ApiException(status=404)
        return SimpleNamespace(data=dict(self.data))

    def _patch(self, name, namespace, body):
        assert (name, namespace) == ("tenant-log-export", "cabotage")
        if not self.exists:
            raise ApiException(status=404)
        for key, value in body["data"].items():
            if value is None:
                self.data.pop(key, None)
            else:
                self.data[key] = value


@pytest.mark.parametrize("flag", [None, "false", "", "0"])
def test_key_alone_never_enables_log_export(app, workload, flag):
    configuration = _enabled_config()
    if flag is None:
        configuration.pop("DD_LOGS_ENABLED")
    else:
        configuration["DD_LOGS_ENABLED"] = _config(flag)
    pod = _render(workload, _make_release(configuration))
    assert pod["metadata"]["labels"][deploy_module.LOG_EXPORT_LABEL] == "false"
    assert pod["metadata"]["annotations"] == {"ad.datadoghq.com/logs_exclude": "false"}


def test_platform_without_exporter_ignores_tenant_opt_in(app, workload):
    app.config["LOG_EXPORT_ENABLED"] = False
    release = _make_release(_enabled_config())
    secrets = _Secrets(exists=False)

    pod = _render(workload, release)
    assert deploy_module.sync_log_export(secrets.api, release) is False
    deploy_module.remove_log_export_outputs(secrets.api, "tenant.")
    with patch.object(deploy_module, "ApplicationEnvironment") as model:
        model.query.filter.return_value = [_live_app_env("tenant", _enabled_config())]
        deploy_module._reconcile_log_export(secrets.api)

    assert pod["metadata"]["labels"][deploy_module.LOG_EXPORT_LABEL] == "false"
    assert pod["metadata"]["annotations"] == {"ad.datadoghq.com/logs_exclude": "false"}
    secrets.api.read_namespaced_secret.assert_not_called()
    secrets.api.patch_namespaced_secret.assert_not_called()


def test_opt_in_only_labels_pods_and_never_grants_host_access(app, workload):
    release = _make_release(_enabled_config())
    release.configuration_objects["DD_LOGS_ENABLED"] = _config("false")
    before = _render(workload, release)
    release.configuration_objects["DD_LOGS_ENABLED"] = _config("true")
    after = _render(workload, release)

    assert after["spec"] == before["spec"]
    assert not any("hostPath" in v for v in after["spec"]["volumes"])
    assert after["metadata"]["labels"][deploy_module.LOG_EXPORT_LABEL] == "true"
    assert after["metadata"]["annotations"] == {"ad.datadoghq.com/logs_exclude": "true"}


def test_disabled_export_does_not_read_new_destination_secrets(app):
    configuration = {"DD_API_KEY": _config("existing-key")}
    configuration["DD_SITE"] = Configuration(
        name="DD_SITE",
        secret=True,
        buildtime=False,
        key_slug="vault:tenant/project/app/configuration/DD_SITE/1",
    )
    secrets = _Secrets()
    with patch.object(
        deploy_module.config_writer, "read", side_effect=KeyError("unavailable")
    ):
        spec = deploy_module.render_podspec(
            _make_release(configuration), "worker", "sa"
        )
        assert (
            deploy_module.sync_log_export(secrets.api, _make_release(configuration))
            is False
        )
    agent = next(c for c in spec.init_containers if c.name == "dogstatsd-sidecar")
    assert {e.name: e.value for e in agent.env}["DD_API_KEY"] == "existing-key"


def test_tenant_destinations_and_credentials_never_cross(app):
    app.config["DD_API_KEY"] = "operator-key"
    outputs = {}
    for tenant, key, site in [
        ("tenant-a", "key-a", "datadoghq.eu"),
        ("tenant-b", "key-b", "us3.datadoghq.com"),
    ]:
        outputs[tenant] = _output(_make_release(_enabled_config(key, site), tenant))

    assert outputs["tenant-a"]["match"] == "tenant.tenant-a.tenant-a-project-app"
    assert outputs["tenant-a"]["apikey"] == "key-a"
    assert outputs["tenant-a"]["host"] == "http-intake.logs.datadoghq.eu"
    assert outputs["tenant-b"]["match"] == "tenant.tenant-b.tenant-b-project-app"
    assert outputs["tenant-b"]["apikey"] == "key-b"
    assert outputs["tenant-b"]["host"] == "http-intake.logs.us3.datadoghq.com"
    for output in outputs.values():
        assert output["tls.verify"] == "on"
        assert output["dd_message_key"] == "_entry"
        assert output["retry_limit"] != "no_limits"


@pytest.mark.parametrize("key", ["a: b", "x\n  - name: stdout", 'it\'s #"q"'])
def test_api_key_cannot_inject_fluent_bit_configuration(app, key):
    output = _output(_make_release(_enabled_config(key)))
    assert output["apikey"] == key
    assert output["name"] == "datadog"


def test_api_key_cannot_expand_exporter_environment(app):
    with pytest.raises(deploy_module.DeployError, match=r"\$\{"):
        _output(_make_release(_enabled_config("${HOSTNAME}")))


@pytest.mark.parametrize("key", [None, "", "**secret**"])
def test_opt_in_without_tenant_key_fails_without_operator_fallback(app, key):
    app.config["DD_API_KEY"] = "operator-key"
    configuration = _enabled_config()
    if key is None:
        configuration.pop("DD_API_KEY")
    else:
        configuration["DD_API_KEY"] = _config(key)
    with pytest.raises(deploy_module.DeployError, match="application's DD_API_KEY"):
        deploy_module.render_log_export_output(_make_release(configuration))


@pytest.mark.parametrize("site", [None, "", "https://datadoghq.eu", "customer.example"])
def test_opt_in_requires_an_explicit_datadog_site(app, site):
    configuration = _enabled_config()
    if site is None:
        configuration.pop("DD_SITE")
    else:
        configuration["DD_SITE"] = _config(site)
    with pytest.raises(deploy_module.DeployError, match="supported DD_SITE"):
        deploy_module.render_log_export_output(_make_release(configuration))


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
        output = _output(_make_release(configuration))
    assert output["apikey"] == "runtime-only-tenant-key"
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
            deploy_module.sync_log_export(_Secrets().api, _make_release(configuration))


def test_sync_writes_and_then_removes_only_its_own_output(app):
    neighbour = "tenant.tenant-other-app.yaml"
    secrets = _Secrets({"00-placeholder.yaml": "", neighbour: "bmVpZ2hib3Vy"})
    release = _make_release(_enabled_config())
    key = "tenant.tenant-project-app.yaml"

    assert deploy_module.sync_log_export(secrets.api, release) is True
    assert yaml.safe_load(b64decode(secrets.data[key])) == yaml.safe_load(
        deploy_module.render_log_export_output(release)
    )

    release.configuration_objects["DD_LOGS_ENABLED"] = _config("false")
    assert deploy_module.sync_log_export(secrets.api, release) is False
    assert secrets.data == {"00-placeholder.yaml": "", neighbour: "bmVpZ2hib3Vy"}


def test_opt_in_fails_when_exporter_is_not_installed(app):
    secrets = _Secrets(exists=False)
    with pytest.raises(deploy_module.DeployError, match="not installed"):
        deploy_module.sync_log_export(secrets.api, _make_release(_enabled_config()))
    assert deploy_module.sync_log_export(secrets.api, _make_release()) is False


def test_namespace_teardown_removes_only_that_namespace(app):
    secrets = _Secrets(
        {
            "00-placeholder.yaml": "",
            "pr-12.a.yaml": "YQ==",
            "pr-12.b.yaml": "Yg==",
            "pr-123.a.yaml": "YQ==",
        }
    )
    deploy_module.remove_log_export_outputs(secrets.api, "pr-12.")
    assert secrets.data == {"00-placeholder.yaml": "", "pr-123.a.yaml": "YQ=="}


def _live_app_env(organization, configuration, *, running=False):
    release = _make_release(configuration, organization)
    app_env = release.application_environment
    app_env.application = release.application
    app_env.latest_deployment_running = object() if running else None
    app_env.latest_deployment_completed = SimpleNamespace(release_object=release)
    return app_env


def test_reconcile_rebuilds_outputs_from_live_releases(app):
    disabled = _enabled_config()
    disabled["DD_LOGS_ENABLED"] = _config("false")
    app_envs = [
        _live_app_env("tenant-a", _enabled_config()),
        _live_app_env("tenant-b", disabled),
        _live_app_env("tenant-c", disabled, running=True),
        _live_app_env("tenant-d", _enabled_config(site="customer.example")),
    ]
    secrets = _Secrets(
        {
            "00-placeholder.yaml": "",
            "tenant-b.tenant-b-project-app.yaml": "b2xk",
            "tenant-c.tenant-c-project-app.yaml": "aW5mbGlnaHQ=",
            "tenant-d.tenant-d-project-app.yaml": "bGFzdC1nb29k",
            "deleted.deleted-project-app.yaml": "b3JwaGFu",
        }
    )
    with patch.object(deploy_module, "ApplicationEnvironment") as model:
        model.query.filter.return_value = app_envs
        deploy_module._reconcile_log_export(secrets.api)

    restored = secrets.data.pop("tenant-a.tenant-a-project-app.yaml")
    assert yaml.safe_load(b64decode(restored)) == yaml.safe_load(
        deploy_module.render_log_export_output(
            app_envs[0].latest_deployment_completed.release_object
        )
    )
    assert secrets.data == {
        "00-placeholder.yaml": "",
        "tenant-c.tenant-c-project-app.yaml": "aW5mbGlnaHQ=",
        "tenant-d.tenant-d-project-app.yaml": "bGFzdC1nb29k",
    }


def test_reconcile_without_exporter_changes_nothing(app):
    secrets = _Secrets(exists=False)
    with patch.object(deploy_module, "ApplicationEnvironment") as model:
        model.query.filter.return_value = [_live_app_env("tenant", _enabled_config())]
        deploy_module._reconcile_log_export(secrets.api)
    secrets.api.patch_namespaced_secret.assert_not_called()
