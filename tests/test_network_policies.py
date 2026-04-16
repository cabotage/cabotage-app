"""Tests for tenant namespace network policy support."""

from unittest.mock import MagicMock

from kubernetes.client.rest import ApiException

from cabotage.celery.tasks.deploy import (
    TENANT_NETWORK_POLICIES,
    ensure_network_policies,
    render_namespace,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _api_exception(status):
    resp = MagicMock()
    resp.status = status
    resp.reason = "mock"
    resp.data = b""
    return ApiException(status=status, http_resp=resp)


class FakeRelease:
    """Minimal release-like object for render_namespace."""

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
            k8s_namespace = "myorg-abc123-production-xyz"

        k8s_identifier = "appenv-123"
        environment = Env()

    application = App()
    application_environment = AppEnv()


# ---------------------------------------------------------------------------
# render_namespace — label
# ---------------------------------------------------------------------------


class TestRenderNamespace:
    def test_has_resident_namespace_label(self):
        ns = render_namespace(FakeRelease())
        labels = ns.metadata.labels
        assert labels["resident-namespace.cabotage.io"] == "true"

    def test_namespace_name_set(self):
        ns = render_namespace(FakeRelease())
        assert ns.metadata.name is not None
        assert len(ns.metadata.name) > 0


# ---------------------------------------------------------------------------
# TENANT_NETWORK_POLICIES — structure
# ---------------------------------------------------------------------------


class TestTenantNetworkPoliciesData:
    def test_policies_defined(self):
        assert len(TENANT_NETWORK_POLICIES) == 8

    def test_policy_names(self):
        names = [p["name"] for p in TENANT_NETWORK_POLICIES]
        assert "default-deny-ingress" in names
        assert "allow-ingress-from-redis-operator" in names
        assert "allow-ingress-from-traefik" in names
        assert "allow-ingress-from-tailscale" in names
        assert "allow-intra-namespace" in names
        assert "restrict-egress" in names

    def test_redis_operator_ingress_rule(self):
        policy = next(
            p
            for p in TENANT_NETWORK_POLICIES
            if p["name"] == "allow-ingress-from-redis-operator"
        )
        assert policy["spec"]["podSelector"]["matchLabels"] == {
            "resident-redis.cabotage.io": "true"
        }
        from_clause = policy["spec"]["ingress"][0]["from"][0]
        assert (
            from_clause["namespaceSelector"]["matchLabels"][
                "kubernetes.io/metadata.name"
            ]
            == "redis"
        )
        assert from_clause["podSelector"]["matchLabels"] == {"name": "redis-operator"}
        ports = policy["spec"]["ingress"][0]["ports"]
        assert ports == [{"port": 6379, "protocol": "TCP"}]

    def test_default_deny_ingress_has_no_ingress_rules(self):
        policy = next(
            p for p in TENANT_NETWORK_POLICIES if p["name"] == "default-deny-ingress"
        )
        assert "ingress" not in policy["spec"]
        assert policy["spec"]["policyTypes"] == ["Ingress"]

    def test_traefik_ingress_port(self):
        policy = next(
            p
            for p in TENANT_NETWORK_POLICIES
            if p["name"] == "allow-ingress-from-traefik"
        )
        ports = policy["spec"]["ingress"][0]["ports"]
        assert len(ports) == 2
        assert ports[0]["port"] == 8000
        assert ports[0]["protocol"] == "TCP"
        assert ports[1]["port"] == 8089
        assert ports[1]["protocol"] == "TCP"

    def test_traefik_ingress_namespace_selector(self):
        policy = next(
            p
            for p in TENANT_NETWORK_POLICIES
            if p["name"] == "allow-ingress-from-traefik"
        )
        ns_selector = policy["spec"]["ingress"][0]["from"][0]["namespaceSelector"]
        assert ns_selector["matchLabels"]["kubernetes.io/metadata.name"] == "traefik"

    def test_tailscale_ingress_port(self):
        policy = next(
            p
            for p in TENANT_NETWORK_POLICIES
            if p["name"] == "allow-ingress-from-tailscale"
        )
        ports = policy["spec"]["ingress"][0]["ports"]
        assert len(ports) == 1
        assert ports[0]["port"] == 8000
        assert ports[0]["protocol"] == "TCP"

    def test_tailscale_ingress_selectors(self):
        policy = next(
            p
            for p in TENANT_NETWORK_POLICIES
            if p["name"] == "allow-ingress-from-tailscale"
        )
        from_clause = policy["spec"]["ingress"][0]["from"][0]
        ns_selector = from_clause["namespaceSelector"]
        assert ns_selector["matchLabels"]["kubernetes.io/metadata.name"] == "tailscale"
        pod_selector = from_clause["podSelector"]
        assert pod_selector["matchLabels"]["tailscale.com/managed"] == "true"

    def test_restrict_egress_allows_dns(self):
        policy = next(
            p for p in TENANT_NETWORK_POLICIES if p["name"] == "restrict-egress"
        )
        assert policy["spec"]["podSelector"]["matchExpressions"] == [
            {
                "key": "cnpg.io/cluster",
                "operator": "DoesNotExist",
            }
        ]
        dns_rule = policy["spec"]["egress"][0]
        ns_label = dns_rule["to"][0]["namespaceSelector"]["matchLabels"]
        assert ns_label["kubernetes.io/metadata.name"] == "kube-system"
        ports = {(p["port"], p["protocol"]) for p in dns_rule["ports"]}
        assert (53, "UDP") in ports
        assert (53, "TCP") in ports

    def test_allow_egress_cnpg_pods_targets_only_cnpg_pods(self):
        policy = next(
            p for p in TENANT_NETWORK_POLICIES if p["name"] == "allow-egress-cnpg-pods"
        )
        assert policy["spec"]["podSelector"]["matchExpressions"] == [
            {
                "key": "cnpg.io/cluster",
                "operator": "Exists",
            }
        ]
        assert policy["spec"]["policyTypes"] == ["Egress"]
        assert policy["spec"]["egress"] == [{}]

    def test_restrict_egress_allows_vault(self):
        policy = next(
            p for p in TENANT_NETWORK_POLICIES if p["name"] == "restrict-egress"
        )
        vault_rule = policy["spec"]["egress"][1]
        target = vault_rule["to"][0]
        assert (
            target["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"]
            == "cabotage"
        )
        assert target["podSelector"]["matchLabels"]["app"] == "vault"
        ports = {(p["port"], p["protocol"]) for p in vault_rule["ports"]}
        assert (443, "TCP") in ports
        assert (8200, "TCP") in ports

    def test_restrict_egress_allows_consul(self):
        policy = next(
            p for p in TENANT_NETWORK_POLICIES if p["name"] == "restrict-egress"
        )
        consul_rule = policy["spec"]["egress"][2]
        target = consul_rule["to"][0]
        assert target["podSelector"]["matchLabels"]["app"] == "consul"
        ports = [(p["port"], p["protocol"]) for p in consul_rule["ports"]]
        assert (8443, "TCP") in ports

    def test_restrict_egress_allows_legacy_services(self):
        policy = next(
            p for p in TENANT_NETWORK_POLICIES if p["name"] == "restrict-egress"
        )
        egress = policy["spec"]["egress"]
        # Find rules by namespace name
        rules_by_ns = {}
        for rule in egress:
            for target in rule.get("to", []):
                ns_sel = target.get("namespaceSelector", {}).get("matchLabels", {})
                ns_name = ns_sel.get("kubernetes.io/metadata.name")
                if ns_name:
                    rules_by_ns[ns_name] = rule

        # ClickHouse
        assert "clickhouse" in rules_by_ns
        ch_ports = {
            (p["port"], p["protocol"]) for p in rules_by_ns["clickhouse"]["ports"]
        }
        assert (8443, "TCP") in ch_ports
        assert (9440, "TCP") in ch_ports

        # Redis
        assert "redis" in rules_by_ns
        redis_ports = {
            (p["port"], p["protocol"]) for p in rules_by_ns["redis"]["ports"]
        }
        assert (6379, "TCP") in redis_ports

        # Elasticsearch
        assert "elasticsearch" in rules_by_ns
        es_ports = {
            (p["port"], p["protocol"]) for p in rules_by_ns["elasticsearch"]["ports"]
        }
        assert (9200, "TCP") in es_ports

        # PostgreSQL
        assert "postgres" in rules_by_ns
        pg_ports = {
            (p["port"], p["protocol"]) for p in rules_by_ns["postgres"]["ports"]
        }
        assert (5432, "TCP") in pg_ports

    def test_restrict_egress_blocks_cluster_internal_cidrs(self):
        policy = next(
            p for p in TENANT_NETWORK_POLICIES if p["name"] == "restrict-egress"
        )
        # Find the internet rule (ipBlock with 0.0.0.0/0)
        internet_rule = None
        for rule in policy["spec"]["egress"]:
            for target in rule.get("to", []):
                if "ipBlock" in target:
                    internet_rule = rule
                    break
        assert internet_rule is not None
        ip_block = internet_rule["to"][0]["ipBlock"]
        assert ip_block["cidr"] == "0.0.0.0/0"
        assert "10.0.0.0/8" in ip_block["except"]
        assert "172.16.0.0/12" in ip_block["except"]
        assert "192.168.0.0/16" in ip_block["except"]


# ---------------------------------------------------------------------------
# ensure_network_policies — API interactions
# ---------------------------------------------------------------------------


class TestEnsureNetworkPolicies:
    def test_creates_policies_on_404(self):
        api = MagicMock()
        api.read_namespaced_network_policy.side_effect = _api_exception(404)

        ensure_network_policies(api, "tenant-ns")

        assert api.create_namespaced_network_policy.call_count == 8
        created_names = [
            c.args[1]["metadata"]["name"]
            for c in api.create_namespaced_network_policy.call_args_list
        ]
        assert "default-deny-ingress" in created_names
        assert "allow-ingress-from-redis-operator" in created_names
        assert "allow-ingress-from-traefik" in created_names
        assert "allow-ingress-from-tailscale" in created_names
        assert "allow-intra-namespace" in created_names
        assert "restrict-egress" in created_names

    def test_patches_existing_policies(self):
        api = MagicMock()
        api.read_namespaced_network_policy.return_value = MagicMock()

        ensure_network_policies(api, "tenant-ns")

        assert api.patch_namespaced_network_policy.call_count == 8
        assert api.create_namespaced_network_policy.call_count == 0

    def test_sets_namespace_on_created_policies(self):
        api = MagicMock()
        api.read_namespaced_network_policy.side_effect = _api_exception(404)

        ensure_network_policies(api, "my-tenant")

        for c in api.create_namespaced_network_policy.call_args_list:
            assert c.args[0] == "my-tenant"
            assert c.args[1]["metadata"]["namespace"] == "my-tenant"

    def test_raises_on_non_404_api_error(self):
        api = MagicMock()
        api.read_namespaced_network_policy.side_effect = _api_exception(403)

        try:
            ensure_network_policies(api, "tenant-ns")
            assert False, "Should have raised"
        except Exception as exc:
            assert "NetworkPolicy" in str(exc)

    def test_mixed_create_and_patch(self):
        """Some policies exist, others don't."""
        api = MagicMock()
        existing = {"default-deny-ingress", "restrict-egress"}

        def side_effect(name, namespace):
            if name in existing:
                return MagicMock()
            raise _api_exception(404)

        api.read_namespaced_network_policy.side_effect = side_effect

        ensure_network_policies(api, "tenant-ns")

        assert api.patch_namespaced_network_policy.call_count == 2
        assert api.create_namespaced_network_policy.call_count == 6


# ---------------------------------------------------------------------------
# fetch_namespace — label backfill
# ---------------------------------------------------------------------------


class TestFetchNamespaceLabel:
    def test_patches_when_label_missing(self):
        from cabotage.celery.tasks.deploy import fetch_namespace

        core_api = MagicMock()
        ns = MagicMock()
        ns.metadata.labels = {}
        core_api.read_namespace.return_value = ns

        fetch_namespace(core_api, FakeRelease())

        core_api.patch_namespace.assert_called_once()
        patch_arg = core_api.patch_namespace.call_args.args[1]
        assert patch_arg.metadata.labels["resident-namespace.cabotage.io"] == "true"

    def test_skips_patch_when_label_present(self):
        from cabotage.celery.tasks.deploy import fetch_namespace

        core_api = MagicMock()
        ns = MagicMock()
        ns.metadata.labels = {"resident-namespace.cabotage.io": "true"}
        core_api.read_namespace.return_value = ns

        fetch_namespace(core_api, FakeRelease())

        core_api.patch_namespace.assert_not_called()

    def test_creates_with_label_on_404(self):
        from cabotage.celery.tasks.deploy import fetch_namespace

        core_api = MagicMock()
        core_api.read_namespace.side_effect = _api_exception(404)
        created_ns = MagicMock()
        core_api.create_namespace.return_value = created_ns

        result = fetch_namespace(core_api, FakeRelease())

        assert result == created_ns
        create_arg = core_api.create_namespace.call_args.args[0]
        assert create_arg.metadata.labels["resident-namespace.cabotage.io"] == "true"
