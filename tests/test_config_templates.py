"""Tests for _resolve_ingress in config_templates (nginx + tailscale URL resolution)."""

import pytest

from cabotage.utils.config_templates import _resolve_ingress, TemplateResolutionError

# ---------------------------------------------------------------------------
# Fake model objects
# ---------------------------------------------------------------------------


class FakeHost:
    def __init__(self, hostname, tls_enabled=True, is_auto_generated=False):
        self.hostname = hostname
        self.tls_enabled = tls_enabled
        self.is_auto_generated = is_auto_generated


class FakeIngress:
    def __init__(
        self, name="web", enabled=True, ingress_class_name="nginx", hosts=None
    ):
        self.name = name
        self.enabled = enabled
        self.ingress_class_name = ingress_class_name
        self.hosts = hosts or []


class FakeTailscaleIntegration:
    def __init__(self, tailnet=None):
        self.tailnet = tailnet


class FakeOrganization:
    def __init__(self, tailscale_integration=None):
        self.tailscale_integration = tailscale_integration


class FakeProject:
    def __init__(self, organization=None):
        self.organization = organization or FakeOrganization()


class FakeApplication:
    def __init__(self, project=None):
        self.project = project or FakeProject()


class FakeAppEnv:
    def __init__(self, ingresses=None, application=None):
        self.ingresses = ingresses or []
        self.application = application or FakeApplication()


# ---------------------------------------------------------------------------
# nginx ingress resolution
# ---------------------------------------------------------------------------


class TestResolveNginxIngress:
    def test_url_https(self):
        app_env = FakeAppEnv(
            ingresses=[FakeIngress(hosts=[FakeHost("app.example.com")])]
        )
        assert _resolve_ingress(app_env, None, "myapp") == "https://app.example.com"

    def test_url_http_when_tls_disabled(self):
        app_env = FakeAppEnv(
            ingresses=[
                FakeIngress(hosts=[FakeHost("app.example.com", tls_enabled=False)])
            ]
        )
        assert _resolve_ingress(app_env, None, "myapp") == "http://app.example.com"

    def test_host_prop(self):
        app_env = FakeAppEnv(
            ingresses=[FakeIngress(hosts=[FakeHost("app.example.com")])]
        )
        assert (
            _resolve_ingress(app_env, None, "myapp", prop="host") == "app.example.com"
        )

    def test_named_ingress(self):
        app_env = FakeAppEnv(
            ingresses=[
                FakeIngress(name="web", hosts=[FakeHost("web.example.com")]),
                FakeIngress(name="api", hosts=[FakeHost("api.example.com")]),
            ]
        )
        assert _resolve_ingress(app_env, "api", "myapp") == "https://api.example.com"

    def test_prefers_non_auto_generated_host(self):
        app_env = FakeAppEnv(
            ingresses=[
                FakeIngress(
                    hosts=[
                        FakeHost("auto.example.com", is_auto_generated=True),
                        FakeHost("custom.example.com", is_auto_generated=False),
                    ]
                )
            ]
        )
        assert _resolve_ingress(app_env, None, "myapp") == "https://custom.example.com"

    def test_falls_back_to_auto_generated_host(self):
        app_env = FakeAppEnv(
            ingresses=[
                FakeIngress(
                    hosts=[
                        FakeHost("auto.example.com", is_auto_generated=True),
                    ]
                )
            ]
        )
        assert _resolve_ingress(app_env, None, "myapp") == "https://auto.example.com"

    def test_no_ingresses_raises(self):
        app_env = FakeAppEnv(ingresses=[])
        with pytest.raises(TemplateResolutionError, match="no enabled ingresses"):
            _resolve_ingress(app_env, None, "myapp")

    def test_multiple_ingresses_without_name_raises(self):
        app_env = FakeAppEnv(
            ingresses=[
                FakeIngress(name="web", hosts=[FakeHost("web.example.com")]),
                FakeIngress(name="api", hosts=[FakeHost("api.example.com")]),
            ]
        )
        with pytest.raises(TemplateResolutionError, match="multiple ingresses"):
            _resolve_ingress(app_env, None, "myapp")

    def test_named_ingress_not_found_raises(self):
        app_env = FakeAppEnv(
            ingresses=[FakeIngress(name="web", hosts=[FakeHost("web.example.com")])]
        )
        with pytest.raises(TemplateResolutionError, match="not found"):
            _resolve_ingress(app_env, "api", "myapp")

    def test_no_hosts_raises(self):
        app_env = FakeAppEnv(ingresses=[FakeIngress(hosts=[])])
        with pytest.raises(TemplateResolutionError, match="no hosts"):
            _resolve_ingress(app_env, None, "myapp")

    def test_disabled_ingresses_filtered(self):
        app_env = FakeAppEnv(
            ingresses=[FakeIngress(enabled=False, hosts=[FakeHost("app.example.com")])]
        )
        with pytest.raises(TemplateResolutionError, match="no enabled ingresses"):
            _resolve_ingress(app_env, None, "myapp")


# ---------------------------------------------------------------------------
# tailscale ingress resolution
# ---------------------------------------------------------------------------


class TestResolveTailscaleIngress:
    def _make_ts_app_env(self, hostname="my-app", tailnet="my-tailnet.ts.net"):
        ts = FakeTailscaleIntegration(tailnet=tailnet)
        org = FakeOrganization(tailscale_integration=ts)
        project = FakeProject(organization=org)
        app = FakeApplication(project=project)
        ingress = FakeIngress(
            name="ts-web",
            ingress_class_name="tailscale",
            hosts=[FakeHost(hostname)],
        )
        return FakeAppEnv(ingresses=[ingress], application=app)

    def test_url_includes_tailnet(self):
        app_env = self._make_ts_app_env()
        assert (
            _resolve_ingress(app_env, None, "myapp")
            == "https://my-app.my-tailnet.ts.net"
        )

    def test_host_includes_tailnet(self):
        app_env = self._make_ts_app_env()
        assert (
            _resolve_ingress(app_env, None, "myapp", prop="host")
            == "my-app.my-tailnet.ts.net"
        )

    def test_always_https_even_if_tls_disabled(self):
        ts = FakeTailscaleIntegration(tailnet="my-tailnet.ts.net")
        org = FakeOrganization(tailscale_integration=ts)
        project = FakeProject(organization=org)
        app = FakeApplication(project=project)
        ingress = FakeIngress(
            ingress_class_name="tailscale",
            hosts=[FakeHost("my-app", tls_enabled=False)],
        )
        app_env = FakeAppEnv(ingresses=[ingress], application=app)
        url = _resolve_ingress(app_env, None, "myapp")
        assert url.startswith("https://")

    def test_no_tailnet_uses_short_hostname(self):
        app_env = self._make_ts_app_env(tailnet=None)
        assert _resolve_ingress(app_env, None, "myapp") == "https://my-app"

    def test_no_integration_uses_short_hostname(self):
        org = FakeOrganization(tailscale_integration=None)
        project = FakeProject(organization=org)
        app = FakeApplication(project=project)
        ingress = FakeIngress(
            ingress_class_name="tailscale",
            hosts=[FakeHost("my-app")],
        )
        app_env = FakeAppEnv(ingresses=[ingress], application=app)
        assert _resolve_ingress(app_env, None, "myapp") == "https://my-app"

    def test_named_tailscale_ingress(self):
        app_env = self._make_ts_app_env()
        assert (
            _resolve_ingress(app_env, "ts-web", "myapp")
            == "https://my-app.my-tailnet.ts.net"
        )
