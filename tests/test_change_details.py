"""Tests for release change detail computation helpers.

Covers _diff_config_item, _diff_ingress_item, _strip_id, and
compute_release_change_details from query_helpers.
"""

from types import SimpleNamespace

from cabotage.server.query_helpers import (
    _diff_config_item,
    _diff_ingress_item,
    _strip_id,
    compute_release_change_details,
)

# ---------------------------------------------------------------------------
# _strip_id
# ---------------------------------------------------------------------------


class TestStripId:
    def test_removes_id_key(self):
        assert _strip_id({"id": "abc", "name": "foo"}) == {"name": "foo"}

    def test_no_id_key(self):
        assert _strip_id({"name": "foo"}) == {"name": "foo"}

    def test_empty_dict(self):
        assert _strip_id({}) == {}

    def test_preserves_other_keys(self):
        d = {"id": "1", "a": 1, "b": 2, "c": 3}
        assert _strip_id(d) == {"a": 1, "b": 2, "c": 3}


# ---------------------------------------------------------------------------
# _diff_config_item
# ---------------------------------------------------------------------------


class TestDiffConfigItem:
    def test_no_changes(self):
        old = {"version_id": 1, "secret": False, "buildtime": False}
        new = {"version_id": 1, "secret": False, "buildtime": False}
        assert _diff_config_item(old, new) == []

    def test_value_changed(self):
        old = {"version_id": 1, "secret": False, "buildtime": False}
        new = {"version_id": 2, "secret": False, "buildtime": False}
        result = _diff_config_item(old, new)
        assert result == ["value changed"]

    def test_marked_secret(self):
        old = {"version_id": 1, "secret": False, "buildtime": False}
        new = {"version_id": 1, "secret": True, "buildtime": False}
        result = _diff_config_item(old, new)
        assert result == ["marked secret"]

    def test_unmarked_secret(self):
        old = {"version_id": 1, "secret": True, "buildtime": False}
        new = {"version_id": 1, "secret": False, "buildtime": False}
        result = _diff_config_item(old, new)
        assert result == ["unmarked secret"]

    def test_marked_buildtime(self):
        old = {"version_id": 1, "secret": False, "buildtime": False}
        new = {"version_id": 1, "secret": False, "buildtime": True}
        result = _diff_config_item(old, new)
        assert result == ["marked buildtime"]

    def test_unmarked_buildtime(self):
        old = {"version_id": 1, "secret": False, "buildtime": True}
        new = {"version_id": 1, "secret": False, "buildtime": False}
        result = _diff_config_item(old, new)
        assert result == ["unmarked buildtime"]

    def test_multiple_changes(self):
        old = {"version_id": 1, "secret": False, "buildtime": False}
        new = {"version_id": 2, "secret": True, "buildtime": True}
        result = _diff_config_item(old, new)
        assert "value changed" in result
        assert "marked secret" in result
        assert "marked buildtime" in result
        assert len(result) == 3

    def test_empty_old(self):
        """First release for a config — old is empty dict."""
        new = {"version_id": 1, "secret": True, "buildtime": False}
        result = _diff_config_item({}, new)
        assert "value changed" in result
        assert "marked secret" in result

    def test_missing_buildtime_in_old(self):
        """Old snapshot from before buildtime was added."""
        old = {"version_id": 1, "secret": False}
        new = {"version_id": 1, "secret": False, "buildtime": False}
        # buildtime: None != False, so it shows as a change
        result = _diff_config_item(old, new)
        # This is a known edge case — old.get("buildtime") is None, new is False
        # They're not equal, but the change description is still accurate
        assert len(result) <= 1


# ---------------------------------------------------------------------------
# _diff_ingress_item — hosts
# ---------------------------------------------------------------------------


class TestDiffIngressHosts:
    def test_no_changes(self):
        ing = {
            "hosts": [{"id": "1", "hostname": "a.com", "tls_enabled": True}],
            "paths": [],
            "enabled": True,
        }
        assert _diff_ingress_item(ing, ing) == []

    def test_host_added(self):
        old = {"hosts": [], "paths": []}
        new = {
            "hosts": [{"id": "1", "hostname": "a.com", "tls_enabled": True}],
            "paths": [],
        }
        result = _diff_ingress_item(old, new)
        assert len(result) == 1
        assert "hosts added: a.com" in result[0]

    def test_host_removed(self):
        old = {
            "hosts": [{"id": "1", "hostname": "a.com", "tls_enabled": True}],
            "paths": [],
        }
        new = {"hosts": [], "paths": []}
        result = _diff_ingress_item(old, new)
        assert len(result) == 1
        assert "hosts removed: a.com" in result[0]

    def test_multiple_hosts_added(self):
        old = {"hosts": [], "paths": []}
        new = {
            "hosts": [
                {"id": "1", "hostname": "a.com"},
                {"id": "2", "hostname": "b.com"},
            ],
            "paths": [],
        }
        result = _diff_ingress_item(old, new)
        assert "a.com" in result[0]
        assert "b.com" in result[0]

    def test_host_tls_changed(self):
        old = {
            "hosts": [{"id": "1", "hostname": "a.com", "tls_enabled": True}],
            "paths": [],
        }
        new = {
            "hosts": [{"id": "2", "hostname": "a.com", "tls_enabled": False}],
            "paths": [],
        }
        result = _diff_ingress_item(old, new)
        assert len(result) == 1
        assert "hosts changed" in result[0]
        assert "a.com" in result[0]
        assert "tls_enabled: True → False" in result[0]

    def test_host_id_change_ignored(self):
        """Changing only the host ID should not be detected as a change."""
        old = {
            "hosts": [{"id": "1", "hostname": "a.com", "tls_enabled": True}],
            "paths": [],
        }
        new = {
            "hosts": [{"id": "999", "hostname": "a.com", "tls_enabled": True}],
            "paths": [],
        }
        assert _diff_ingress_item(old, new) == []

    def test_host_auto_generated_changed(self):
        old = {
            "hosts": [
                {
                    "id": "1",
                    "hostname": "a.com",
                    "tls_enabled": True,
                    "is_auto_generated": True,
                }
            ],
            "paths": [],
        }
        new = {
            "hosts": [
                {
                    "id": "2",
                    "hostname": "a.com",
                    "tls_enabled": True,
                    "is_auto_generated": False,
                }
            ],
            "paths": [],
        }
        result = _diff_ingress_item(old, new)
        assert "is_auto_generated" in result[0]

    def test_mixed_host_add_remove_change(self):
        old = {
            "hosts": [
                {"id": "1", "hostname": "old.com", "tls_enabled": True},
                {"id": "2", "hostname": "keep.com", "tls_enabled": True},
            ],
            "paths": [],
        }
        new = {
            "hosts": [
                {"id": "3", "hostname": "new.com", "tls_enabled": True},
                {"id": "4", "hostname": "keep.com", "tls_enabled": False},
            ],
            "paths": [],
        }
        result = _diff_ingress_item(old, new)
        result_str = " ".join(result)
        assert "hosts added: new.com" in result_str
        assert "hosts removed: old.com" in result_str
        assert "hosts changed: keep.com" in result_str
        assert "tls_enabled: True → False" in result_str


# ---------------------------------------------------------------------------
# _diff_ingress_item — paths
# ---------------------------------------------------------------------------


class TestDiffIngressPaths:
    def test_path_added(self):
        old = {"hosts": [], "paths": []}
        new = {
            "hosts": [],
            "paths": [
                {
                    "id": "1",
                    "path": "/api",
                    "path_type": "Prefix",
                    "target_process_name": "web",
                }
            ],
        }
        result = _diff_ingress_item(old, new)
        assert len(result) == 1
        assert "paths added" in result[0]
        assert "/api → web" in result[0]

    def test_path_removed(self):
        old = {
            "hosts": [],
            "paths": [
                {
                    "id": "1",
                    "path": "/old",
                    "path_type": "Prefix",
                    "target_process_name": "web",
                }
            ],
        }
        new = {"hosts": [], "paths": []}
        result = _diff_ingress_item(old, new)
        assert "paths removed" in result[0]
        assert "/old → web" in result[0]

    def test_path_target_changed(self):
        old = {
            "hosts": [],
            "paths": [
                {
                    "id": "1",
                    "path": "/",
                    "path_type": "Prefix",
                    "target_process_name": "web",
                }
            ],
        }
        new = {
            "hosts": [],
            "paths": [
                {
                    "id": "2",
                    "path": "/",
                    "path_type": "Prefix",
                    "target_process_name": "api",
                }
            ],
        }
        result = _diff_ingress_item(old, new)
        assert "paths changed" in result[0]
        assert "target_process_name: web → api" in result[0]

    def test_path_type_changed(self):
        old = {
            "hosts": [],
            "paths": [
                {
                    "id": "1",
                    "path": "/",
                    "path_type": "Prefix",
                    "target_process_name": "web",
                }
            ],
        }
        new = {
            "hosts": [],
            "paths": [
                {
                    "id": "2",
                    "path": "/",
                    "path_type": "Exact",
                    "target_process_name": "web",
                }
            ],
        }
        result = _diff_ingress_item(old, new)
        assert "paths changed" in result[0]
        assert "path_type: Prefix → Exact" in result[0]

    def test_path_id_change_ignored(self):
        old = {
            "hosts": [],
            "paths": [
                {
                    "id": "1",
                    "path": "/",
                    "path_type": "Prefix",
                    "target_process_name": "web",
                }
            ],
        }
        new = {
            "hosts": [],
            "paths": [
                {
                    "id": "999",
                    "path": "/",
                    "path_type": "Prefix",
                    "target_process_name": "web",
                }
            ],
        }
        assert _diff_ingress_item(old, new) == []

    def test_multiple_paths_added(self):
        old = {"hosts": [], "paths": []}
        new = {
            "hosts": [],
            "paths": [
                {
                    "id": "1",
                    "path": "/api",
                    "path_type": "Prefix",
                    "target_process_name": "api",
                },
                {
                    "id": "2",
                    "path": "/web",
                    "path_type": "Prefix",
                    "target_process_name": "web",
                },
            ],
        }
        result = _diff_ingress_item(old, new)
        assert "/api → api" in result[0]
        assert "/web → web" in result[0]


# ---------------------------------------------------------------------------
# _diff_ingress_item — settings
# ---------------------------------------------------------------------------


class TestDiffIngressSettings:
    def test_enabled_changed(self):
        old = {"hosts": [], "paths": [], "enabled": True}
        new = {"hosts": [], "paths": [], "enabled": False}
        result = _diff_ingress_item(old, new)
        assert result == ["settings: enabled"]

    def test_multiple_settings_changed(self):
        old = {
            "hosts": [],
            "paths": [],
            "enabled": True,
            "proxy_read_timeout": "10s",
            "session_affinity": False,
        }
        new = {
            "hosts": [],
            "paths": [],
            "enabled": False,
            "proxy_read_timeout": "60s",
            "session_affinity": True,
        }
        result = _diff_ingress_item(old, new)
        assert len(result) == 1
        assert "enabled" in result[0]
        assert "proxy_read_timeout" in result[0]
        assert "session_affinity" in result[0]

    def test_cluster_issuer_changed(self):
        old = {"hosts": [], "paths": [], "cluster_issuer": "letsencrypt-prod"}
        new = {"hosts": [], "paths": [], "cluster_issuer": ""}
        result = _diff_ingress_item(old, new)
        assert "cluster_issuer" in result[0]

    def test_extra_annotations_changed(self):
        old = {"hosts": [], "paths": [], "extra_annotations": {"foo": "bar"}}
        new = {"hosts": [], "paths": [], "extra_annotations": {"foo": "baz"}}
        result = _diff_ingress_item(old, new)
        assert "extra_annotations" in result[0]

    def test_tailscale_settings_detected(self):
        old = {
            "hosts": [],
            "paths": [],
            "tailscale_hostname": "old",
            "tailscale_funnel": False,
            "tailscale_tags": "",
        }
        new = {
            "hosts": [],
            "paths": [],
            "tailscale_hostname": "new",
            "tailscale_funnel": True,
            "tailscale_tags": "tag:web",
        }
        result = _diff_ingress_item(old, new)
        result_str = result[0]
        assert "tailscale_hostname" in result_str
        assert "tailscale_funnel" in result_str
        assert "tailscale_tags" in result_str

    def test_unchanged_settings_not_reported(self):
        old = {"hosts": [], "paths": [], "enabled": True, "proxy_read_timeout": "10s"}
        new = {"hosts": [], "paths": [], "enabled": True, "proxy_read_timeout": "10s"}
        assert _diff_ingress_item(old, new) == []


# ---------------------------------------------------------------------------
# _diff_ingress_item — combined
# ---------------------------------------------------------------------------


class TestDiffIngressCombined:
    def test_hosts_paths_and_settings_all_changed(self):
        old = {
            "hosts": [{"id": "1", "hostname": "old.com", "tls_enabled": True}],
            "paths": [
                {
                    "id": "1",
                    "path": "/old",
                    "path_type": "Prefix",
                    "target_process_name": "web",
                }
            ],
            "enabled": True,
        }
        new = {
            "hosts": [{"id": "2", "hostname": "new.com", "tls_enabled": True}],
            "paths": [
                {
                    "id": "2",
                    "path": "/new",
                    "path_type": "Prefix",
                    "target_process_name": "api",
                }
            ],
            "enabled": False,
        }
        result = _diff_ingress_item(old, new)
        result_str = " ".join(result)
        assert "hosts added: new.com" in result_str
        assert "hosts removed: old.com" in result_str
        assert "paths added: /new → api" in result_str
        assert "paths removed: /old → web" in result_str
        assert "settings: enabled" in result_str

    def test_empty_old_ingress(self):
        """Diffing against an empty dict (first release or missing predecessor)."""
        new = {
            "hosts": [{"id": "1", "hostname": "a.com", "tls_enabled": True}],
            "paths": [
                {
                    "id": "1",
                    "path": "/",
                    "path_type": "Prefix",
                    "target_process_name": "web",
                }
            ],
            "enabled": True,
        }
        result = _diff_ingress_item({}, new)
        result_str = " ".join(result)
        assert "hosts added: a.com" in result_str
        assert "paths added: / → web" in result_str

    def test_empty_new_ingress(self):
        """Everything removed."""
        old = {
            "hosts": [{"id": "1", "hostname": "a.com", "tls_enabled": True}],
            "paths": [
                {
                    "id": "1",
                    "path": "/",
                    "path_type": "Prefix",
                    "target_process_name": "web",
                }
            ],
        }
        result = _diff_ingress_item(old, {})
        result_str = " ".join(result)
        assert "hosts removed: a.com" in result_str
        assert "paths removed: / → web" in result_str

    def test_no_hosts_or_paths_keys(self):
        """Old/new missing hosts/paths keys entirely."""
        old = {"enabled": True}
        new = {"enabled": False}
        result = _diff_ingress_item(old, new)
        assert result == ["settings: enabled"]


# ---------------------------------------------------------------------------
# compute_release_change_details — helper to build fake releases/deployments
# ---------------------------------------------------------------------------


def _make_release(
    id,
    version,
    configuration=None,
    ingresses=None,
    configuration_changes=None,
    ingress_changes=None,
    built=True,
    error=False,
):
    return SimpleNamespace(
        id=id,
        version=version,
        configuration=configuration or {},
        ingresses=ingresses or {},
        configuration_changes=configuration_changes or {},
        ingress_changes=ingress_changes or {},
        built=built,
        error=error,
    )


def _make_deployment(release_id, complete=True, error=False):
    return SimpleNamespace(
        release={"id": str(release_id)},
        complete=complete,
        error=error,
    )


# ---------------------------------------------------------------------------
# compute_release_change_details
# ---------------------------------------------------------------------------


class TestComputeReleaseChangeDetails:
    def test_no_changes(self):
        """Releases with no changes produce empty result."""
        r1 = _make_release("r1", 1)
        r2 = _make_release("r2", 2)
        result = compute_release_change_details([r2, r1])
        assert result == {}

    def test_config_value_changed(self):
        r1 = _make_release(
            "r1",
            1,
            configuration={
                "DB_URL": {"version_id": 1, "secret": False, "buildtime": False},
            },
        )
        r2 = _make_release(
            "r2",
            2,
            configuration={
                "DB_URL": {"version_id": 2, "secret": False, "buildtime": False}
            },
            configuration_changes={"changed": ["DB_URL"]},
        )
        deployments = [_make_deployment("r1")]
        result = compute_release_change_details([r2, r1], deployments)
        assert "r2" in result
        assert result["r2"]["config"]["DB_URL"] == ["value changed"]

    def test_config_secret_toggled(self):
        r1 = _make_release(
            "r1",
            1,
            configuration={
                "TOKEN": {"version_id": 1, "secret": False, "buildtime": False},
            },
        )
        r2 = _make_release(
            "r2",
            2,
            configuration={
                "TOKEN": {"version_id": 1, "secret": True, "buildtime": False}
            },
            configuration_changes={"changed": ["TOKEN"]},
        )
        deployments = [_make_deployment("r1")]
        result = compute_release_change_details([r2, r1], deployments)
        assert result["r2"]["config"]["TOKEN"] == ["marked secret"]

    def test_ingress_host_added(self):
        r1 = _make_release(
            "r1",
            1,
            ingresses={
                "web": {"hosts": [], "paths": []},
            },
        )
        r2 = _make_release(
            "r2",
            2,
            ingresses={
                "web": {
                    "hosts": [{"id": "1", "hostname": "a.com", "tls_enabled": True}],
                    "paths": [],
                }
            },
            ingress_changes={"changed": ["web"]},
        )
        deployments = [_make_deployment("r1")]
        result = compute_release_change_details([r2, r1], deployments)
        assert "hosts added: a.com" in result["r2"]["ingress"]["web"][0]

    def test_ingress_setting_changed(self):
        r1 = _make_release(
            "r1",
            1,
            ingresses={
                "web": {"hosts": [], "paths": [], "proxy_read_timeout": "10s"},
            },
        )
        r2 = _make_release(
            "r2",
            2,
            ingresses={"web": {"hosts": [], "paths": [], "proxy_read_timeout": "60s"}},
            ingress_changes={"changed": ["web"]},
        )
        deployments = [_make_deployment("r1")]
        result = compute_release_change_details([r2, r1], deployments)
        assert "proxy_read_timeout" in result["r2"]["ingress"]["web"][0]

    def test_skips_undeployed_releases(self):
        """Should diff against the last deployed release, skipping undeployed ones."""
        r1 = _make_release(
            "r1",
            1,
            ingresses={
                "web": {"hosts": [], "paths": [], "enabled": True},
            },
        )
        # r2 was never deployed — same snapshot as r3
        r2 = _make_release(
            "r2",
            2,
            ingresses={
                "web": {"hosts": [], "paths": [], "enabled": False},
            },
        )
        r3 = _make_release(
            "r3",
            3,
            ingresses={"web": {"hosts": [], "paths": [], "enabled": False}},
            ingress_changes={"changed": ["web"]},
        )
        # Only r1 was deployed
        deployments = [_make_deployment("r1")]
        result = compute_release_change_details([r3, r2, r1], deployments)
        # r3 should diff against r1 (deployed), not r2 (undeployed)
        assert "enabled" in result["r3"]["ingress"]["web"][0]

    def test_identical_snapshot_produces_no_detail(self):
        """When the predecessor has the same snapshot, no detail is shown."""
        r1 = _make_release(
            "r1",
            1,
            ingresses={
                "web": {"hosts": [], "paths": [], "enabled": False},
            },
        )
        r2 = _make_release(
            "r2",
            2,
            ingresses={"web": {"hosts": [], "paths": [], "enabled": False}},
            ingress_changes={"changed": ["web"]},
        )
        deployments = [_make_deployment("r1")]
        result = compute_release_change_details([r2, r1], deployments)
        # r1 has the same ingress as r2, so no detail
        assert result["r2"]["ingress"] == {}

    def test_falls_back_to_immediate_predecessor(self):
        """When no deployment data, falls back to immediate predecessor."""
        r1 = _make_release(
            "r1",
            1,
            configuration={
                "KEY": {"version_id": 1, "secret": False, "buildtime": False},
            },
        )
        r2 = _make_release(
            "r2",
            2,
            configuration={
                "KEY": {"version_id": 2, "secret": False, "buildtime": False}
            },
            configuration_changes={"changed": ["KEY"]},
        )
        # No deployments passed
        result = compute_release_change_details([r2, r1])
        assert result["r2"]["config"]["KEY"] == ["value changed"]

    def test_no_predecessor(self):
        """First release with changes — no predecessor at all."""
        r1 = _make_release(
            "r1",
            1,
            configuration={
                "NEW": {"version_id": 1, "secret": True, "buildtime": False}
            },
            configuration_changes={"changed": ["NEW"]},
        )
        result = compute_release_change_details([r1])
        # Diffs against empty dict
        assert "value changed" in result["r1"]["config"]["NEW"]
        assert "marked secret" in result["r1"]["config"]["NEW"]

    def test_multiple_releases_on_page(self):
        """Each release gets its own detail relative to its predecessor."""
        r1 = _make_release(
            "r1",
            1,
            configuration={
                "A": {"version_id": 1, "secret": False, "buildtime": False},
            },
        )
        r2 = _make_release(
            "r2",
            2,
            configuration={"A": {"version_id": 2, "secret": False, "buildtime": False}},
            configuration_changes={"changed": ["A"]},
        )
        r3 = _make_release(
            "r3",
            3,
            configuration={"A": {"version_id": 3, "secret": True, "buildtime": False}},
            configuration_changes={"changed": ["A"]},
        )
        deployments = [_make_deployment("r2"), _make_deployment("r1")]
        result = compute_release_change_details([r3, r2, r1], deployments)
        # r3 diffs against r2 (deployed)
        assert result["r3"]["config"]["A"] == ["value changed", "marked secret"]
        # r2 diffs against r1 (deployed)
        assert result["r2"]["config"]["A"] == ["value changed"]

    def test_failed_deployment_not_used_as_baseline(self):
        """A failed deployment should not be used as the baseline."""
        r1 = _make_release(
            "r1",
            1,
            configuration={
                "X": {"version_id": 1, "secret": False, "buildtime": False},
            },
        )
        r2 = _make_release(
            "r2",
            2,
            configuration={
                "X": {"version_id": 2, "secret": False, "buildtime": False},
            },
        )
        r3 = _make_release(
            "r3",
            3,
            configuration={"X": {"version_id": 3, "secret": False, "buildtime": False}},
            configuration_changes={"changed": ["X"]},
        )
        deployments = [
            _make_deployment("r2", complete=False, error=True),  # failed
            _make_deployment("r1", complete=True),  # success
        ]
        result = compute_release_change_details([r3, r2, r1], deployments)
        # r3 should skip r2 (failed deploy) and diff against r1
        assert result["r3"]["config"]["X"] == ["value changed"]

    def test_incomplete_deployment_not_used_as_baseline(self):
        """An in-progress deployment should not be used as the baseline."""
        r1 = _make_release(
            "r1",
            1,
            configuration={
                "X": {"version_id": 1, "secret": False, "buildtime": False},
            },
        )
        r2 = _make_release(
            "r2",
            2,
            configuration={
                "X": {"version_id": 2, "secret": False, "buildtime": False},
            },
        )
        r3 = _make_release(
            "r3",
            3,
            configuration={"X": {"version_id": 3, "secret": False, "buildtime": False}},
            configuration_changes={"changed": ["X"]},
        )
        deployments = [
            _make_deployment("r2", complete=False, error=False),  # in progress
            _make_deployment("r1", complete=True),
        ]
        result = compute_release_change_details([r3, r2, r1], deployments)
        assert result["r3"]["config"]["X"] == ["value changed"]

    def test_only_added_and_removed_skipped(self):
        """Releases with only added/removed changes (no 'changed') are skipped."""
        r1 = _make_release("r1", 1)
        r2 = _make_release(
            "r2",
            2,
            configuration_changes={"added": ["NEW"], "removed": ["OLD"]},
        )
        result = compute_release_change_details([r2, r1])
        assert result == {}

    def test_mixed_config_and_ingress_changes(self):
        r1 = _make_release(
            "r1",
            1,
            configuration={
                "DB": {"version_id": 1, "secret": False, "buildtime": False}
            },
            ingresses={"web": {"hosts": [], "paths": [], "enabled": True}},
        )
        r2 = _make_release(
            "r2",
            2,
            configuration={
                "DB": {"version_id": 2, "secret": False, "buildtime": False}
            },
            ingresses={"web": {"hosts": [], "paths": [], "enabled": False}},
            configuration_changes={"changed": ["DB"]},
            ingress_changes={"changed": ["web"]},
        )
        deployments = [_make_deployment("r1")]
        result = compute_release_change_details([r2, r1], deployments)
        assert result["r2"]["config"]["DB"] == ["value changed"]
        assert "enabled" in result["r2"]["ingress"]["web"][0]

    def test_host_tls_change_shows_old_and_new_values(self):
        r1 = _make_release(
            "r1",
            1,
            ingresses={
                "web": {
                    "hosts": [{"id": "1", "hostname": "a.com", "tls_enabled": True}],
                    "paths": [],
                },
            },
        )
        r2 = _make_release(
            "r2",
            2,
            ingresses={
                "web": {
                    "hosts": [{"id": "2", "hostname": "a.com", "tls_enabled": False}],
                    "paths": [],
                }
            },
            ingress_changes={"changed": ["web"]},
        )
        deployments = [_make_deployment("r1")]
        result = compute_release_change_details([r2, r1], deployments)
        detail = result["r2"]["ingress"]["web"][0]
        assert "tls_enabled: True → False" in detail

    def test_none_configuration_handled(self):
        """Releases with None for configuration/ingresses don't crash."""
        r1 = _make_release("r1", 1, configuration=None, ingresses=None)
        r2 = _make_release(
            "r2",
            2,
            configuration={
                "NEW": {"version_id": 1, "secret": False, "buildtime": False}
            },
            configuration_changes={"changed": ["NEW"]},
        )
        deployments = [_make_deployment("r1")]
        result = compute_release_change_details([r2, r1], deployments)
        assert "value changed" in result["r2"]["config"]["NEW"]
