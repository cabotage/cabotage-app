"""Tests for ingress rendering (nginx + tailscale)."""

import hashlib

import pytest

from cabotage.celery.tasks.deploy import (
    _build_ingress_paths,
    _repair_ingress_hostnames,
    render_ingress_object,
    render_service,
)
from cabotage.server.models.projects import Ingress, IngressHost, IngressSnapshot
from cabotage.server.models.utils import (
    readable_k8s_hostname,
    repair_ingress_hostname,
)

# ---------------------------------------------------------------------------
# Fake model objects that mimic Ingress/IngressSnapshot, Host, Path
# ---------------------------------------------------------------------------


class FakeHost:
    def __init__(self, hostname, tls_enabled=True, is_auto_generated=False):
        self.hostname = hostname
        self.tls_enabled = tls_enabled
        self.is_auto_generated = is_auto_generated


class FakePath:
    def __init__(self, path="/", path_type="Prefix", target_process_name="web"):
        self.path = path
        self.path_type = path_type
        self.target_process_name = target_process_name


class FakeIngress:
    def __init__(self, **kwargs):
        self.name = kwargs.get("name", "web")
        self.enabled = kwargs.get("enabled", True)
        self.ingress_class_name = kwargs.get("ingress_class_name", "nginx")
        self.backend_protocol = kwargs.get("backend_protocol", "HTTPS")
        self.proxy_connect_timeout = kwargs.get("proxy_connect_timeout", "10s")
        self.proxy_read_timeout = kwargs.get("proxy_read_timeout", "10s")
        self.proxy_send_timeout = kwargs.get("proxy_send_timeout", "10s")
        self.proxy_body_size = kwargs.get("proxy_body_size", "10M")
        self.client_body_buffer_size = kwargs.get("client_body_buffer_size", "1M")
        self.proxy_request_buffering = kwargs.get("proxy_request_buffering", "on")
        self.session_affinity = kwargs.get("session_affinity", False)
        self.use_regex = kwargs.get("use_regex", False)
        self.allow_annotations = kwargs.get("allow_annotations", False)
        self.extra_annotations = kwargs.get("extra_annotations", {})
        self.cluster_issuer = kwargs.get("cluster_issuer", "letsencrypt")
        self.force_ssl_redirect = kwargs.get("force_ssl_redirect", True)
        self.service_upstream = kwargs.get("service_upstream", True)
        self.tailscale_hostname = kwargs.get("tailscale_hostname", None)
        self.tailscale_funnel = kwargs.get("tailscale_funnel", False)
        self.tailscale_tags = kwargs.get("tailscale_tags", None)
        self.hosts = kwargs.get("hosts", [])
        self.paths = kwargs.get("paths", [])


class FakeRelease:
    """Minimal Release-like object for render_service."""

    class App:
        class Project:
            class Org:
                slug = "myorg"
                k8s_identifier = "myorg-abc123"

            organization = Org()
            slug = "myproject"
            k8s_identifier = "myproject-def456"

        project = Project()
        slug = "myapp"
        k8s_identifier = "myapp-ghi789"

    class AppEnv:
        class Env:
            slug = "production"
            k8s_identifier = "production-xyz"
            uses_environment_namespace = True

        k8s_identifier = "appenv-123"
        environment = Env()

    application = App()
    application_environment = AppEnv()
    processes = {"web": "gunicorn app:app", "worker": "celery -A app worker"}


LABELS = {
    "organization": "myorg",
    "project": "myproject",
    "application": "myapp",
    "app": "test-label",
}
RESOURCE_PREFIX = "proj-abc-app-def"


# ---------------------------------------------------------------------------
# _build_ingress_paths
# ---------------------------------------------------------------------------


class TestBuildIngressPaths:
    def test_explicit_paths(self):
        ing = FakeIngress(
            paths=[
                FakePath(path="/", target_process_name="web"),
                FakePath(path="/api", path_type="Prefix", target_process_name="web"),
            ]
        )
        paths = _build_ingress_paths(ing, RESOURCE_PREFIX, [])
        assert len(paths) == 2
        assert paths[0].path == "/"
        assert paths[1].path == "/api"
        # All paths use port name "https"
        assert paths[0].backend.service.port.name == "https"
        assert paths[1].backend.service.port.name == "https"

    def test_default_path_when_no_explicit(self):
        ing = FakeIngress(paths=[])
        paths = _build_ingress_paths(ing, RESOURCE_PREFIX, ["web", "worker"])
        assert len(paths) == 1
        assert paths[0].path == "/"
        assert paths[0].path_type == "Prefix"
        # Should target first web process
        assert paths[0].backend.service.name == f"{RESOURCE_PREFIX}-web"

    def test_no_paths_no_web_processes(self):
        ing = FakeIngress(paths=[])
        paths = _build_ingress_paths(ing, RESOURCE_PREFIX, ["worker", "beat"])
        assert paths is None

    def test_no_paths_no_processes(self):
        ing = FakeIngress(paths=[])
        paths = _build_ingress_paths(ing, RESOURCE_PREFIX, [])
        assert paths is None

    def test_backend_service_name(self):
        ing = FakeIngress(paths=[FakePath(target_process_name="web2")])
        paths = _build_ingress_paths(ing, RESOURCE_PREFIX)
        assert paths[0].backend.service.name == f"{RESOURCE_PREFIX}-web2"

    def test_port_always_named_https(self):
        ing = FakeIngress(paths=[FakePath()])
        paths = _build_ingress_paths(ing, RESOURCE_PREFIX)
        assert paths[0].backend.service.port.name == "https"
        assert paths[0].backend.service.port.number is None


# ---------------------------------------------------------------------------
# render_ingress_object — nginx
# ---------------------------------------------------------------------------


class TestRenderNginxIngress:
    def _make_nginx_ingress(self, **overrides):
        defaults = dict(
            ingress_class_name="nginx",
            hosts=[FakeHost("app.example.com")],
            paths=[FakePath()],
        )
        defaults.update(overrides)
        return FakeIngress(**defaults)

    def test_basic_nginx_ingress(self):
        ing = self._make_nginx_ingress()
        obj = render_ingress_object(ing, RESOURCE_PREFIX, LABELS, process_names=["web"])
        assert obj is not None
        assert obj.spec.ingress_class_name == "nginx"
        assert obj.metadata.name == f"{RESOURCE_PREFIX}-web"

    def test_disabled_returns_none(self):
        ing = self._make_nginx_ingress(enabled=False)
        obj = render_ingress_object(ing, RESOURCE_PREFIX, LABELS)
        assert obj is None

    def test_nginx_annotations(self):
        ing = self._make_nginx_ingress()
        obj = render_ingress_object(ing, RESOURCE_PREFIX, LABELS)
        ann = obj.metadata.annotations
        assert "nginx.ingress.kubernetes.io/backend-protocol" in ann
        assert ann["nginx.ingress.kubernetes.io/backend-protocol"] == "HTTPS"
        assert "cert-manager.io/cluster-issuer" in ann
        assert ann["cert-manager.io/cluster-issuer"] == "letsencrypt"
        assert "nginx.ingress.kubernetes.io/force-ssl-redirect" in ann

    def test_nginx_no_tailscale_annotations(self):
        ing = self._make_nginx_ingress()
        obj = render_ingress_object(ing, RESOURCE_PREFIX, LABELS)
        ann = obj.metadata.annotations
        assert "tailscale.com/proxy-group" not in ann
        assert "tailscale.com/tags" not in ann

    def test_proxy_timeouts(self):
        ing = self._make_nginx_ingress(
            proxy_connect_timeout="30s",
            proxy_read_timeout="60s",
        )
        obj = render_ingress_object(ing, RESOURCE_PREFIX, LABELS)
        ann = obj.metadata.annotations
        assert ann["nginx.ingress.kubernetes.io/proxy-connect-timeout"] == "30s"
        assert ann["nginx.ingress.kubernetes.io/proxy-read-timeout"] == "60s"

    def test_session_affinity(self):
        ing = self._make_nginx_ingress(session_affinity=True)
        obj = render_ingress_object(ing, RESOURCE_PREFIX, LABELS)
        assert (
            obj.metadata.annotations["nginx.ingress.kubernetes.io/affinity"] == "cookie"
        )

    def test_no_session_affinity_by_default(self):
        ing = self._make_nginx_ingress()
        obj = render_ingress_object(ing, RESOURCE_PREFIX, LABELS)
        assert "nginx.ingress.kubernetes.io/affinity" not in obj.metadata.annotations

    def test_use_regex(self):
        ing = self._make_nginx_ingress(use_regex=True)
        obj = render_ingress_object(ing, RESOURCE_PREFIX, LABELS)
        assert (
            obj.metadata.annotations["nginx.ingress.kubernetes.io/use-regex"] == "true"
        )

    def test_extra_annotations_when_allowed(self):
        ing = self._make_nginx_ingress(
            allow_annotations=True,
            extra_annotations={"custom.io/foo": "bar"},
        )
        obj = render_ingress_object(ing, RESOURCE_PREFIX, LABELS)
        assert obj.metadata.annotations["custom.io/foo"] == "bar"

    def test_extra_annotations_ignored_when_not_allowed(self):
        ing = self._make_nginx_ingress(
            allow_annotations=False,
            extra_annotations={"custom.io/foo": "bar"},
        )
        obj = render_ingress_object(ing, RESOURCE_PREFIX, LABELS)
        assert "custom.io/foo" not in obj.metadata.annotations

    def test_tls_with_secret_name(self):
        ing = self._make_nginx_ingress(
            hosts=[FakeHost("app.example.com", tls_enabled=True)],
        )
        obj = render_ingress_object(ing, RESOURCE_PREFIX, LABELS)
        assert len(obj.spec.tls) == 1
        assert obj.spec.tls[0].secret_name == f"{RESOURCE_PREFIX}-web-tls"
        assert "app.example.com" in obj.spec.tls[0].hosts

    def test_no_tls_when_disabled(self):
        ing = self._make_nginx_ingress(
            hosts=[FakeHost("app.example.com", tls_enabled=False)],
        )
        obj = render_ingress_object(ing, RESOURCE_PREFIX, LABELS)
        assert obj.spec.tls is None

    def test_host_in_rules(self):
        ing = self._make_nginx_ingress(
            hosts=[FakeHost("app.example.com")],
        )
        obj = render_ingress_object(ing, RESOURCE_PREFIX, LABELS)
        assert len(obj.spec.rules) == 1
        assert obj.spec.rules[0].host == "app.example.com"

    def test_multiple_hosts(self):
        ing = self._make_nginx_ingress(
            hosts=[
                FakeHost("app.example.com"),
                FakeHost("www.example.com"),
            ],
        )
        obj = render_ingress_object(ing, RESOURCE_PREFIX, LABELS)
        assert len(obj.spec.rules) == 2
        hosts = {r.host for r in obj.spec.rules}
        assert hosts == {"app.example.com", "www.example.com"}

    def test_labels_applied(self):
        ing = self._make_nginx_ingress()
        obj = render_ingress_object(ing, RESOURCE_PREFIX, LABELS)
        assert obj.metadata.labels["resident-ingress.cabotage.io"] == "true"
        assert obj.metadata.labels["ingress"] == "web"
        assert obj.metadata.labels["organization"] == "myorg"


# ---------------------------------------------------------------------------
# render_ingress_object — tailscale
# ---------------------------------------------------------------------------


class TestRenderTailscaleIngress:
    def _make_ts_ingress(self, **overrides):
        defaults = dict(
            name="ts-web",
            ingress_class_name="tailscale",
            hosts=[FakeHost("my-app")],
            paths=[FakePath()],
        )
        defaults.update(overrides)
        return FakeIngress(**defaults)

    def test_basic_tailscale_ingress(self):
        ing = self._make_ts_ingress()
        obj = render_ingress_object(
            ing,
            RESOURCE_PREFIX,
            LABELS,
            org_k8s_identifier="myorg-abc123",
        )
        assert obj is not None
        assert obj.spec.ingress_class_name == "tailscale"

    def test_disabled_returns_none(self):
        ing = self._make_ts_ingress(enabled=False)
        obj = render_ingress_object(ing, RESOURCE_PREFIX, LABELS)
        assert obj is None

    def test_no_nginx_annotations(self):
        ing = self._make_ts_ingress()
        obj = render_ingress_object(
            ing,
            RESOURCE_PREFIX,
            LABELS,
            org_k8s_identifier="myorg-abc123",
        )
        ann = obj.metadata.annotations
        assert "nginx.ingress.kubernetes.io/backend-protocol" not in ann
        assert "cert-manager.io/cluster-issuer" not in ann

    def test_proxy_group_annotation(self):
        ing = self._make_ts_ingress()
        obj = render_ingress_object(
            ing,
            RESOURCE_PREFIX,
            LABELS,
            org_k8s_identifier="myorg-abc123",
        )
        ann = obj.metadata.annotations
        assert ann["tailscale.com/proxy-group"] == "ingress-myorg-abc123"

    def test_no_proxy_group_without_org_identifier(self):
        ing = self._make_ts_ingress()
        obj = render_ingress_object(ing, RESOURCE_PREFIX, LABELS)
        ann = obj.metadata.annotations
        assert "tailscale.com/proxy-group" not in ann

    def test_tailscale_tags_annotation(self):
        ing = self._make_ts_ingress(tailscale_tags="tag:web,tag:prod")
        obj = render_ingress_object(ing, RESOURCE_PREFIX, LABELS)
        assert obj.metadata.annotations["tailscale.com/tags"] == "tag:web,tag:prod"

    def test_no_tags_when_not_set(self):
        ing = self._make_ts_ingress(tailscale_tags=None)
        obj = render_ingress_object(ing, RESOURCE_PREFIX, LABELS)
        assert "tailscale.com/tags" not in obj.metadata.annotations

    def test_org_default_tags_fallback(self):
        ing = self._make_ts_ingress(tailscale_tags=None)
        obj = render_ingress_object(
            ing, RESOURCE_PREFIX, LABELS, org_default_tags="tag:cabotage"
        )
        assert obj.metadata.annotations["tailscale.com/tags"] == "tag:cabotage"

    def test_ingress_tags_override_org_default(self):
        ing = self._make_ts_ingress(tailscale_tags="tag:custom")
        obj = render_ingress_object(
            ing, RESOURCE_PREFIX, LABELS, org_default_tags="tag:cabotage"
        )
        assert obj.metadata.annotations["tailscale.com/tags"] == "tag:custom"

    def test_tls_always_on(self):
        """Tailscale ingresses always include all hosts in TLS, regardless of tls_enabled."""
        ing = self._make_ts_ingress(
            hosts=[FakeHost("my-app", tls_enabled=False)],
        )
        obj = render_ingress_object(ing, RESOURCE_PREFIX, LABELS)
        assert len(obj.spec.tls) == 1
        assert "my-app" in obj.spec.tls[0].hosts

    def test_tls_no_secret_name(self):
        """Tailscale handles TLS automatically — no secret_name needed."""
        ing = self._make_ts_ingress()
        obj = render_ingress_object(ing, RESOURCE_PREFIX, LABELS)
        assert obj.spec.tls[0].secret_name is None

    def test_host_not_in_rules(self):
        """Tailscale ingress rules should have host=None to avoid FQDN mismatch."""
        ing = self._make_ts_ingress(
            hosts=[FakeHost("my-app")],
        )
        obj = render_ingress_object(ing, RESOURCE_PREFIX, LABELS)
        assert len(obj.spec.rules) == 1
        assert obj.spec.rules[0].host is None

    def test_host_in_tls(self):
        """The hostname goes into tls.hosts for MagicDNS name."""
        ing = self._make_ts_ingress(
            hosts=[FakeHost("my-app")],
        )
        obj = render_ingress_object(ing, RESOURCE_PREFIX, LABELS)
        assert obj.spec.tls[0].hosts == ["my-app"]

    def test_labels_applied(self):
        ing = self._make_ts_ingress()
        obj = render_ingress_object(ing, RESOURCE_PREFIX, LABELS)
        assert obj.metadata.labels["resident-ingress.cabotage.io"] == "true"
        assert obj.metadata.labels["ingress"] == "ts-web"

    def test_backend_port_uses_named_https(self):
        """Tailscale backends use port name 'https' for HTTPS detection."""
        ing = self._make_ts_ingress()
        obj = render_ingress_object(ing, RESOURCE_PREFIX, LABELS)
        path = obj.spec.rules[0].http.paths[0]
        assert path.backend.service.port.name == "https"


# ---------------------------------------------------------------------------
# render_service
# ---------------------------------------------------------------------------


class TestRenderService:
    def test_service_name(self):
        svc = render_service(FakeRelease(), "web")
        # resource_prefix is safe_k8s_name(project.k8s_identifier, app.k8s_identifier)
        assert "web" in svc.metadata.name

    def test_port_named_https(self):
        svc = render_service(FakeRelease(), "web")
        assert len(svc.spec.ports) == 1
        assert svc.spec.ports[0].name == "https"
        assert svc.spec.ports[0].port == 8000
        assert svc.spec.ports[0].target_port == 8000

    def test_labels(self):
        svc = render_service(FakeRelease(), "web")
        assert svc.metadata.labels["resident-service.cabotage.io"] == "true"
        assert svc.metadata.labels["process"] == "web"

    def test_selector(self):
        svc = render_service(FakeRelease(), "web")
        assert "process" in svc.spec.selector
        assert svc.spec.selector["process"] == "web"


DOMAIN = "psfhosted.net"
BROKEN_HOST = (
    "pyladies-production-pyladiescon-portal-pyladiescon-por-16e47b77-web." + DOMAIN
)
REPAIRED_HOST = (
    "pyladies-production-pyladiescon-portal-pyladiescon-ad1f5ad7-web." + DOMAIN
)
VALID_HOSTS = [
    "portal.pyladies.com",
    "pyladies-pyladiescon-portal-pyladiescon-portal-d0dfbbdd-web." + DOMAIN,
    "pyladiescon-portal.ingress.us-east-2.psfhosted.computer",
    "pyladiescon-portal.us-east-2.psfhosted.computer",
]


class TestIngressHostnameGeneration:
    @pytest.mark.parametrize(
        ("suffix", "expected"),
        [
            ("web", "python-production-litestar-litestar-is-so-cool-sup-7955f573-web"),
            ("api", "python-production-litestar-litestar-is-so-cool-sup-d05e77b2-api"),
        ],
    )
    def test_new_app_with_long_name_gets_shortened_hostname(
        self, suffix: str, expected: str
    ) -> None:
        pairs = (
            ("python", "python-12345678"),
            ("production", "production-23456789"),
            ("litestar", "litestar-34567890"),
            (
                "litestar-is-so-cool-super-silly-extra-long-application-name",
                "litestar-45678901",
            ),
        )
        # Previously the first label was 67 bytes:
        # python-production-litestar-litestar-is-so-cool-super-s-6077e272-web
        hostname = f"{readable_k8s_hostname(*pairs, suffix=suffix)}.{DOMAIN}"
        assert hostname == f"{expected}.{DOMAIN}"
        assert all(len(label.encode("ascii")) <= 63 for label in hostname.split("."))

    @pytest.mark.parametrize("base_length", [1, 49, 50])
    def test_preserves_valid_names_through_63_byte_boundary(
        self, base_length: int
    ) -> None:
        slug = "a" * base_length
        digest = hashlib.sha256(b"stable-identity").hexdigest()[:8]
        expected = f"{slug}-{digest}-web"
        assert (
            readable_k8s_hostname((slug, "stable-identity"), suffix="web") == expected
        )
        assert len(expected) <= 63

    @pytest.mark.parametrize(
        ("slug", "suffix"),
        [("a" * 51, "web"), ("a" * 100, "web"), ("app", "web-" + "x" * 60)],
    )
    def test_bounds_complete_label_and_keeps_ingress_names_distinct(
        self, slug: str, suffix: str
    ) -> None:
        first = readable_k8s_hostname((slug, "stable-identity"), suffix=suffix)
        second = readable_k8s_hostname(
            (slug, "stable-identity"), suffix=suffix + "-other"
        )
        assert len(first.encode()) <= 63
        assert len(second.encode()) <= 63
        assert first != second
        if len(suffix) <= 52:
            assert first.endswith(f"-{suffix}")
        assert "." not in first
        # Repairs of old names must converge on the same name as fresh generation.
        digest = hashlib.sha256(b"stable-identity").hexdigest()[:8]
        prefix = f"{slug}-{digest}"
        if len(prefix) > 63:
            prefix = f"{slug[:54]}-{digest}"
        assert repair_ingress_hostname(f"{prefix}-{suffix}.{DOMAIN}", suffix) == (
            f"{first}.{DOMAIN}"
        )

    def test_repairs_observed_production_name_without_changing_domain(self) -> None:
        assert repair_ingress_hostname(BROKEN_HOST, "web") == REPAIRED_HOST
        assert len(REPAIRED_HOST.split(".")[0]) == 63
        assert repair_ingress_hostname(REPAIRED_HOST, "web") == REPAIRED_HOST

    def test_preserves_valid_and_unrelated_custom_hostnames(self) -> None:
        unrelated = "a" * 64 + ".customer.example"
        for hostname in [*VALID_HOSTS, unrelated]:
            assert repair_ingress_hostname(hostname, "web") == hostname


def _hostname_repair_ingress(hosts: list[IngressHost]) -> Ingress:
    return Ingress(
        name="web",
        enabled=True,
        ingress_class_name="nginx",
        backend_protocol="HTTPS",
        force_ssl_redirect=True,
        service_upstream=True,
        hosts=hosts,
        paths=[],
    )


class TestIngressHostnameRepair:
    @pytest.mark.parametrize("is_auto_generated", [True, False])
    def test_repairs_existing_and_demoted_hosts_preserving_working_routes(
        self, is_auto_generated: bool
    ) -> None:
        ingress = _hostname_repair_ingress(
            [
                IngressHost(
                    hostname=BROKEN_HOST,
                    tls_enabled=True,
                    is_auto_generated=is_auto_generated,
                ),
                *[
                    IngressHost(hostname=h, tls_enabled=True, is_auto_generated=False)
                    for h in VALID_HOSTS
                ],
            ]
        )
        assert _repair_ingress_hostnames(ingress)
        assert {h.hostname for h in ingress.hosts} == {REPAIRED_HOST, *VALID_HOSTS}
        rendered = render_ingress_object(
            ingress, RESOURCE_PREFIX, LABELS, process_names=["web"]
        )
        assert set(rendered.spec.tls[0].hosts) == {REPAIRED_HOST, *VALID_HOSTS}
        assert {r.host for r in rendered.spec.rules} == {REPAIRED_HOST, *VALID_HOSTS}
        assert all(
            r.http.paths[0].backend.service.name == f"{RESOURCE_PREFIX}-web"
            for r in rendered.spec.rules
        )
        assert not _repair_ingress_hostnames(ingress)

    def test_drops_invalid_duplicate_when_corrected_name_already_exists(self) -> None:
        ingress = _hostname_repair_ingress(
            [
                IngressHost(
                    hostname=BROKEN_HOST, tls_enabled=True, is_auto_generated=True
                ),
                IngressHost(
                    hostname=REPAIRED_HOST, tls_enabled=True, is_auto_generated=True
                ),
            ]
        )
        assert _repair_ingress_hostnames(ingress)
        assert [h.hostname for h in ingress.hosts] == [REPAIRED_HOST]

    def test_old_release_snapshot_cannot_reintroduce_invalid_certificate_name(
        self,
    ) -> None:
        ingress = _hostname_repair_ingress(
            [
                IngressHost(
                    hostname=BROKEN_HOST, tls_enabled=True, is_auto_generated=True
                ),
                IngressHost(
                    hostname="portal.pyladies.com",
                    tls_enabled=True,
                    is_auto_generated=False,
                ),
            ]
        )
        snapshot = IngressSnapshot(ingress.asdict)
        rendered = render_ingress_object(
            snapshot, RESOURCE_PREFIX, LABELS, process_names=["web"]
        )
        assert set(rendered.spec.tls[0].hosts) == {REPAIRED_HOST, "portal.pyladies.com"}
        assert {r.host for r in rendered.spec.rules} == {
            REPAIRED_HOST,
            "portal.pyladies.com",
        }
        assert rendered.spec.tls[0].secret_name == f"{RESOURCE_PREFIX}-web-tls"
        # Historical release data remains immutable.
        assert BROKEN_HOST in [h.hostname for h in snapshot.hosts]
