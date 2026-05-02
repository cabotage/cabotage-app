"""Tests for audit log diff computation helpers."""

import uuid

import pytest
from types import SimpleNamespace

from cabotage.server import db
from cabotage.server.audit_helpers import (
    compute_audit_changes,
    diff_versions,
    format_value,
    _compute_scale_changes,
    _compute_release_changes,
)
from cabotage.server.models.auth import Organization
from cabotage.server.models.projects import (
    Application,
    ApplicationEnvironment,
    Configuration,
    Environment,
    Project,
    Release,
    activity_plugin,
)
from cabotage.server.wsgi import app as _app

Activity = activity_plugin.activity_cls

REPOSITORY_NAME = "cabotage/testorg/testproj/webapp"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app():
    _app.config["TESTING"] = True
    _app.config["WTF_CSRF_ENABLED"] = False
    with _app.app_context():
        yield _app


@pytest.fixture
def db_session(app):
    yield db.session
    db.session.rollback()


@pytest.fixture
def org(db_session):
    o = Organization(name="AuditOrg", slug=f"auditorg-{uuid.uuid4().hex[:8]}")
    db_session.add(o)
    db_session.flush()
    return o


@pytest.fixture
def project(db_session, org):
    p = Project(name="AuditProject", organization_id=org.id)
    db_session.add(p)
    db_session.flush()
    return p


@pytest.fixture
def environment(db_session, project):
    e = Environment(name="default", project_id=project.id, ephemeral=False)
    db_session.add(e)
    db_session.flush()
    return e


@pytest.fixture
def application(db_session, project):
    a = Application(name="auditapp", slug="auditapp", project_id=project.id)
    db_session.add(a)
    db_session.flush()
    return a


@pytest.fixture
def app_env(db_session, application, environment):
    ae = ApplicationEnvironment(
        application_id=application.id, environment_id=environment.id
    )
    db_session.add(ae)
    db_session.flush()
    return ae


def _make_entry(**kwargs):
    """Create a fake audit log entry as a SimpleNamespace."""
    defaults = {
        "id": 1,
        "object_type": "Application",
        "verb": "edit",
        "object_id": uuid.uuid4(),
        "object_tx_id": 100,
        "transaction_id": 100,
        "raw_data": None,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


# ---------------------------------------------------------------------------
# format_value
# ---------------------------------------------------------------------------


class TestFormatValue:
    def test_none(self):
        assert format_value(None) is None

    def test_bool_true(self):
        assert format_value(True) == "yes"

    def test_bool_false(self):
        assert format_value(False) == "no"

    def test_dict(self):
        assert format_value({"a": 1}) == '{"a":1}'

    def test_empty_string(self):
        assert format_value("") == "(none)"

    def test_string(self):
        assert format_value("hello") == "hello"

    def test_int(self):
        assert format_value(42) == "42"


# ---------------------------------------------------------------------------
# diff_versions
# ---------------------------------------------------------------------------


class TestDiffVersions:
    def test_no_prev(self):
        cur = SimpleNamespace(name="foo")
        assert diff_versions(None, cur, {"name": "name"}) == []

    def test_no_cur(self):
        prev = SimpleNamespace(name="foo")
        assert diff_versions(prev, None, {"name": "name"}) == []

    def test_no_changes(self):
        prev = SimpleNamespace(name="foo", enabled=True)
        cur = SimpleNamespace(name="foo", enabled=True)
        assert diff_versions(prev, cur, {"name": "name", "enabled": "enabled"}) == []

    def test_simple_change(self):
        prev = SimpleNamespace(name="old")
        cur = SimpleNamespace(name="new")
        result = diff_versions(prev, cur, {"name": "name"})
        assert result == [{"field": "name", "old": "old", "new": "new"}]

    def test_bool_change(self):
        prev = SimpleNamespace(enabled=False)
        cur = SimpleNamespace(enabled=True)
        result = diff_versions(prev, cur, {"enabled": "enabled"})
        assert result == [{"field": "enabled", "old": "no", "new": "yes"}]

    def test_skips_none_to_empty_string(self):
        prev = SimpleNamespace(path=None)
        cur = SimpleNamespace(path="")
        result = diff_versions(prev, cur, {"path": "path"})
        assert result == []

    def test_skips_empty_string_to_none(self):
        prev = SimpleNamespace(path="")
        cur = SimpleNamespace(path=None)
        result = diff_versions(prev, cur, {"path": "path"})
        assert result == []

    def test_none_to_value(self):
        prev = SimpleNamespace(branch=None)
        cur = SimpleNamespace(branch="main")
        result = diff_versions(prev, cur, {"branch": "branch"})
        assert result == [{"field": "branch", "old": None, "new": "main"}]

    def test_value_to_none(self):
        prev = SimpleNamespace(branch="main")
        cur = SimpleNamespace(branch=None)
        result = diff_versions(prev, cur, {"branch": "branch"})
        assert result == [{"field": "branch", "old": "main", "new": None}]

    def test_multiple_changes(self):
        prev = SimpleNamespace(name="a", timeout=180)
        cur = SimpleNamespace(name="b", timeout=360)
        result = diff_versions(prev, cur, {"name": "name", "timeout": "timeout"})
        assert len(result) == 2
        fields = {r["field"] for r in result}
        assert fields == {"name", "timeout"}

    def test_dict_change(self):
        prev = SimpleNamespace(counts={"web": 1})
        cur = SimpleNamespace(counts={"web": 2})
        result = diff_versions(prev, cur, {"counts": "counts"})
        assert result[0]["old"] == '{"web":1}'
        assert result[0]["new"] == '{"web":2}'


# ---------------------------------------------------------------------------
# _compute_scale_changes (pure — no DB needed)
# ---------------------------------------------------------------------------


class TestComputeScaleChanges:
    def test_basic_scale(self):
        entry = _make_entry(
            id=1,
            object_type="Application",
            verb="scale",
            raw_data={
                "changes": {"web": {"process_count": {"old_value": 0, "new_value": 2}}}
            },
        )
        result = _compute_scale_changes([entry])
        assert result == {1: [{"field": "web", "old": "0", "new": "2"}]}

    def test_multiple_processes(self):
        entry = _make_entry(
            id=1,
            verb="scale",
            raw_data={
                "changes": {
                    "web": {"process_count": {"old_value": 1, "new_value": 2}},
                    "worker": {"process_count": {"old_value": 4, "new_value": 0}},
                }
            },
        )
        result = _compute_scale_changes([entry])
        assert len(result[1]) == 2
        fields = {c["field"] for c in result[1]}
        assert fields == {"web", "worker"}

    def test_pod_class_change(self):
        entry = _make_entry(
            id=1,
            verb="scale",
            raw_data={
                "changes": {
                    "web": {
                        "process_count": {"old_value": 1, "new_value": 1},
                        "pod_class": {"old_value": "m1.small", "new_value": "m1.large"},
                    }
                }
            },
        )
        result = _compute_scale_changes([entry])
        assert len(result[1]) == 1
        assert result[1][0]["field"] == "web pod class"

    def test_no_changes_data(self):
        entry = _make_entry(id=1, verb="scale", raw_data={"user_id": "test"})
        result = _compute_scale_changes([entry])
        assert result == {}

    def test_empty_raw_data(self):
        entry = _make_entry(id=1, verb="scale", raw_data=None)
        result = _compute_scale_changes([entry])
        assert result == {}

    def test_same_count_no_change(self):
        entry = _make_entry(
            id=1,
            verb="scale",
            raw_data={
                "changes": {"web": {"process_count": {"old_value": 2, "new_value": 2}}}
            },
        )
        result = _compute_scale_changes([entry])
        assert result == {}


# ---------------------------------------------------------------------------
# _compute_release_changes (uses DB for Release.query)
# ---------------------------------------------------------------------------


class TestComputeReleaseChanges:
    def test_image_tag_change(self, db_session, application, app_env):
        rel = Release(
            application_id=application.id,
            application_environment_id=app_env.id,
            _repository_name=REPOSITORY_NAME,
            image={"tag": "5", "commit_sha": "abc1234def5678"},
            image_changes={"added": [], "changed": ["tag"], "removed": []},
            configuration={},
            configuration_changes={"added": [], "changed": [], "removed": []},
            ingresses={},
            ingress_changes={},
        )
        db_session.add(rel)
        db_session.flush()

        entry = _make_entry(
            id=1, object_type="Release", verb="create", object_id=rel.id
        )
        result = _compute_release_changes([entry])
        assert result[1] == [{"field": "image", "old": None, "new": "#5 (abc1234)"}]

    def test_image_sha_only(self, db_session, application, app_env):
        rel = Release(
            application_id=application.id,
            application_environment_id=app_env.id,
            _repository_name=REPOSITORY_NAME,
            image={"commit_sha": "deadbeef12345"},
            image_changes={"added": [], "changed": ["commit_sha"], "removed": []},
            configuration={},
            configuration_changes={"added": [], "changed": [], "removed": []},
            ingresses={},
        )
        db_session.add(rel)
        db_session.flush()

        entry = _make_entry(
            id=1, object_type="Release", verb="create", object_id=rel.id
        )
        result = _compute_release_changes([entry])
        assert result[1] == [{"field": "image", "old": None, "new": "deadbee"}]

    def test_config_added(self, db_session, application, app_env):
        rel = Release(
            application_id=application.id,
            application_environment_id=app_env.id,
            _repository_name=REPOSITORY_NAME,
            image={},
            image_changes={"added": [], "changed": [], "removed": []},
            configuration={},
            configuration_changes={
                "added": ["FOO", "BAR"],
                "changed": [],
                "removed": [],
            },
            ingresses={},
        )
        db_session.add(rel)
        db_session.flush()

        entry = _make_entry(
            id=1, object_type="Release", verb="create", object_id=rel.id
        )
        result = _compute_release_changes([entry])
        assert len(result[1]) == 1
        assert result[1][0]["field"] == "config added"
        assert result[1][0]["new"] == "FOO, BAR"

    def test_config_truncation(self, db_session, application, app_env):
        names = [f"VAR_{i}" for i in range(10)]
        rel = Release(
            application_id=application.id,
            application_environment_id=app_env.id,
            _repository_name=REPOSITORY_NAME,
            image={},
            image_changes={"added": [], "changed": [], "removed": []},
            configuration={},
            configuration_changes={"added": names, "changed": [], "removed": []},
            ingresses={},
        )
        db_session.add(rel)
        db_session.flush()

        entry = _make_entry(
            id=1, object_type="Release", verb="create", object_id=rel.id
        )
        result = _compute_release_changes([entry])
        assert "(+7 more)" in result[1][0]["new"]

    def test_no_changes(self, db_session, application, app_env):
        rel = Release(
            application_id=application.id,
            application_environment_id=app_env.id,
            _repository_name=REPOSITORY_NAME,
            image={},
            image_changes={"added": [], "changed": [], "removed": []},
            configuration={},
            configuration_changes={"added": [], "changed": [], "removed": []},
            ingresses={},
            ingress_changes={},
        )
        db_session.add(rel)
        db_session.flush()

        entry = _make_entry(
            id=1, object_type="Release", verb="create", object_id=rel.id
        )
        result = _compute_release_changes([entry])
        assert 1 not in result

    def test_ingress_changed(self, db_session, application, app_env):
        rel = Release(
            application_id=application.id,
            application_environment_id=app_env.id,
            _repository_name=REPOSITORY_NAME,
            image={},
            image_changes={"added": [], "changed": [], "removed": []},
            configuration={},
            configuration_changes={"added": [], "changed": [], "removed": []},
            ingresses={},
            ingress_changes={"added": [], "changed": ["web"], "removed": []},
        )
        db_session.add(rel)
        db_session.flush()

        entry = _make_entry(
            id=1, object_type="Release", verb="create", object_id=rel.id
        )
        result = _compute_release_changes([entry])
        assert result[1] == [{"field": "ingress changed", "old": None, "new": "web"}]


# ---------------------------------------------------------------------------
# compute_audit_changes — integration tests with version tables
# ---------------------------------------------------------------------------


class TestComputeAuditChangesIntegration:
    def _create_activity(self, db_session, verb, obj, **data_kwargs):
        data = {"user_id": "test", "timestamp": "2026-01-01T00:00:00"}
        data.update(data_kwargs)
        a = Activity(verb=verb, object=obj, data=data)
        db_session.add(a)
        db_session.flush()
        return a

    def _audit_entry_for(self, db_session, activity):
        """Fetch the AuditLog row corresponding to an Activity."""
        from cabotage.server.models.audit import AuditLog

        return AuditLog.query.filter_by(id=activity.id).first()

    def test_config_create_shows_value(self, db_session, application, app_env):
        cfg = Configuration(
            application_id=application.id,
            application_environment_id=app_env.id,
            name="MY_VAR",
            value="hello-world",
            secret=False,
        )
        db_session.add(cfg)
        db_session.flush()
        act = self._create_activity(db_session, "create", cfg)
        entry = self._audit_entry_for(db_session, act)

        result = compute_audit_changes([entry])
        assert entry.id in result
        assert result[entry.id][0]["field"] == "MY_VAR"
        assert result[entry.id][0]["new"] == "hello-world"
        assert result[entry.id][0]["old"] is None

    def test_config_create_secret_skipped(self, db_session, application, app_env):
        cfg = Configuration(
            application_id=application.id,
            application_environment_id=app_env.id,
            name="SECRET_VAR",
            value="top-secret",
            secret=True,
        )
        db_session.add(cfg)
        db_session.flush()
        act = self._create_activity(db_session, "create", cfg)
        entry = self._audit_entry_for(db_session, act)

        result = compute_audit_changes([entry])
        assert entry.id not in result

    def test_config_edit_shows_old_new(self, db_session, application, app_env):
        cfg = Configuration(
            application_id=application.id,
            application_environment_id=app_env.id,
            name="EDIT_VAR",
            value="old-value",
            secret=False,
        )
        db_session.add(cfg)
        db_session.flush()
        self._create_activity(db_session, "create", cfg)
        # Commit so continuum creates version with distinct transaction_id
        db_session.commit()

        # Edit
        cfg.value = "new-value"
        db_session.flush()
        act = self._create_activity(db_session, "edit", cfg)
        db_session.commit()
        entry = self._audit_entry_for(db_session, act)

        result = compute_audit_changes([entry])
        assert entry.id in result
        changes = result[entry.id]
        assert changes[0]["field"] == "EDIT_VAR"
        assert changes[0]["old"] == "old-value"
        assert changes[0]["new"] == "new-value"

    def test_config_edit_truncates_long_values(self, db_session, application, app_env):
        long_val = "x" * 100
        cfg = Configuration(
            application_id=application.id,
            application_environment_id=app_env.id,
            name="LONG_VAR",
            value="short",
            secret=False,
        )
        db_session.add(cfg)
        db_session.flush()
        self._create_activity(db_session, "create", cfg)
        db_session.commit()

        cfg.value = long_val
        db_session.flush()
        act = self._create_activity(db_session, "edit", cfg)
        db_session.commit()
        entry = self._audit_entry_for(db_session, act)

        result = compute_audit_changes([entry])
        assert result[entry.id][0]["new"].endswith("...")
        assert len(result[entry.id][0]["new"]) == 60

    def test_app_edit_shows_changed_fields(self, db_session, application):
        self._create_activity(db_session, "create", application)
        db_session.commit()

        application.auto_deploy_branch = "main"
        db_session.flush()
        act = self._create_activity(db_session, "edit", application)
        db_session.commit()
        entry = self._audit_entry_for(db_session, act)

        result = compute_audit_changes([entry])
        assert entry.id in result
        fields = {c["field"] for c in result[entry.id]}
        assert "auto deploy branch" in fields

    def test_app_edit_procfile_path_uses_human_label(self, db_session, application):
        self._create_activity(db_session, "create", application)
        db_session.commit()

        application.procfile_path = "deploy/Procfile.web"
        db_session.flush()
        act = self._create_activity(db_session, "edit", application)
        db_session.commit()
        entry = self._audit_entry_for(db_session, act)

        result = compute_audit_changes([entry])
        assert entry.id in result
        fields = {c["field"] for c in result[entry.id]}
        assert "Procfile path" in fields

    def test_app_edit_no_op(self, db_session, application):
        self._create_activity(db_session, "create", application)
        db_session.commit()

        # "Edit" without changing anything
        db_session.flush()
        act = self._create_activity(db_session, "edit", application)
        db_session.commit()
        entry = self._audit_entry_for(db_session, act)

        result = compute_audit_changes([entry])
        # No meaningful changes → not in result
        assert entry.id not in result

    def test_scale_from_raw_data(self, db_session, application):
        self._create_activity(db_session, "create", application)

        act = self._create_activity(
            db_session,
            "scale",
            application,
            changes={"web": {"process_count": {"old_value": 0, "new_value": 3}}},
        )
        entry = self._audit_entry_for(db_session, act)

        result = compute_audit_changes([entry])
        assert entry.id in result
        assert result[entry.id][0]["field"] == "web"
        assert result[entry.id][0]["old"] == "0"
        assert result[entry.id][0]["new"] == "3"

    def test_project_edit(self, db_session, project):
        self._create_activity(db_session, "create", project)
        db_session.commit()

        project.environments_enabled = True
        db_session.flush()
        act = self._create_activity(db_session, "edit", project)
        db_session.commit()
        entry = self._audit_entry_for(db_session, act)

        result = compute_audit_changes([entry])
        assert entry.id in result
        fields = {c["field"] for c in result[entry.id]}
        assert "environments" in fields

    def test_empty_entries(self):
        assert compute_audit_changes([]) == {}

    def test_mixed_page(self, db_session, application, app_env):
        """A page with mixed event types returns changes for each."""
        cfg = Configuration(
            application_id=application.id,
            application_environment_id=app_env.id,
            name="MIX_VAR",
            value="val",
            secret=False,
        )
        db_session.add(cfg)
        db_session.flush()
        act_cfg = self._create_activity(db_session, "create", cfg)

        act_scale = self._create_activity(
            db_session,
            "scale",
            application,
            changes={"web": {"process_count": {"old_value": 0, "new_value": 1}}},
        )

        entries = [
            self._audit_entry_for(db_session, act_cfg),
            self._audit_entry_for(db_session, act_scale),
        ]
        result = compute_audit_changes(entries)
        assert len(result) == 2

    def test_config_edit_noop_still_shows_value(self, db_session, application, app_env):
        """Even when the value doesn't change, show both old and new."""
        cfg = Configuration(
            application_id=application.id,
            application_environment_id=app_env.id,
            name="NOOP_VAR",
            value="same-value",
            secret=False,
        )
        db_session.add(cfg)
        db_session.flush()
        self._create_activity(db_session, "create", cfg)
        db_session.commit()

        # Toggle buildtime to force a new version row, but keep value the same
        cfg.buildtime = True
        db_session.flush()
        cfg.buildtime = False
        db_session.flush()
        act = self._create_activity(db_session, "edit", cfg)
        db_session.commit()
        entry = self._audit_entry_for(db_session, act)

        result = compute_audit_changes([entry])
        assert entry.id in result
        changes = result[entry.id]
        # Value unchanged but still shown
        value_changes = [c for c in changes if c["field"] == "NOOP_VAR"]
        assert len(value_changes) == 1
        assert value_changes[0]["old"] == "same-value"
        assert value_changes[0]["new"] == "same-value"

    def test_config_delete_shows_removed_value(self, db_session, application, app_env):
        """Delete events show the deleted variable name and value in red."""
        cfg = Configuration(
            application_id=application.id,
            application_environment_id=app_env.id,
            name="DOOMED_VAR",
            value="goodbye",
            secret=False,
        )
        db_session.add(cfg)
        db_session.flush()
        self._create_activity(db_session, "create", cfg)
        db_session.commit()

        # Delete
        act = self._create_activity(db_session, "delete", cfg)
        db_session.commit()
        entry = self._audit_entry_for(db_session, act)

        result = compute_audit_changes([entry])
        assert entry.id in result
        changes = result[entry.id]
        assert changes[0]["field"] == "DOOMED_VAR"
        assert changes[0]["old"] == "goodbye"
        assert changes[0]["new"] is None

    def test_config_field_uses_var_name(self, db_session, application, app_env):
        """Config diffs use the variable name as the field, not 'value'."""
        cfg = Configuration(
            application_id=application.id,
            application_environment_id=app_env.id,
            name="FLASK_APP",
            value="old.py",
            secret=False,
        )
        db_session.add(cfg)
        db_session.flush()
        self._create_activity(db_session, "create", cfg)
        db_session.commit()

        cfg.value = "new:app"
        db_session.flush()
        act = self._create_activity(db_session, "edit", cfg)
        db_session.commit()
        entry = self._audit_entry_for(db_session, act)

        result = compute_audit_changes([entry])
        assert result[entry.id][0]["field"] == "FLASK_APP"


class TestReleaseConfigVerification:
    """Tests for _verify_config_changes filtering false positives."""

    def test_false_positive_filtered(self, db_session, application, app_env):
        """Configs marked 'changed' but with identical values are filtered out."""
        from cabotage.server.audit_helpers import _compute_release_changes

        # Create two releases with identical config values but the second
        # claims all configs changed (simulating the buildtime key bug)
        cfg_snap = {"MY_VAR": {"value": "hello", "secret": False, "buildtime": False}}
        cfg_snap_no_bt = {"MY_VAR": {"value": "hello", "secret": False}}

        prev_rel = Release(
            application_id=application.id,
            application_environment_id=app_env.id,
            _repository_name=REPOSITORY_NAME,
            image={},
            image_changes={"added": [], "changed": [], "removed": []},
            configuration=cfg_snap,
            configuration_changes={"added": [], "changed": [], "removed": []},
            ingresses={},
        )
        db_session.add(prev_rel)
        db_session.flush()

        cur_rel = Release(
            application_id=application.id,
            application_environment_id=app_env.id,
            _repository_name=REPOSITORY_NAME,
            image={},
            image_changes={"added": [], "changed": [], "removed": []},
            configuration=cfg_snap_no_bt,
            configuration_changes={"added": [], "changed": ["MY_VAR"], "removed": []},
            ingresses={},
        )
        db_session.add(cur_rel)
        db_session.flush()

        entry = _make_entry(
            id=1, object_type="Release", verb="create", object_id=cur_rel.id
        )
        result = _compute_release_changes([entry])
        # MY_VAR should be filtered out — values are identical
        assert 1 not in result

    def test_real_change_not_filtered(self, db_session, application, app_env):
        """Configs that actually changed still show up."""
        from cabotage.server.audit_helpers import _compute_release_changes

        prev_rel = Release(
            application_id=application.id,
            application_environment_id=app_env.id,
            _repository_name=REPOSITORY_NAME,
            image={},
            image_changes={"added": [], "changed": [], "removed": []},
            configuration={"MY_VAR": {"value": "old", "secret": False}},
            configuration_changes={"added": [], "changed": [], "removed": []},
            ingresses={},
        )
        db_session.add(prev_rel)
        db_session.flush()

        cur_rel = Release(
            application_id=application.id,
            application_environment_id=app_env.id,
            _repository_name=REPOSITORY_NAME,
            image={},
            image_changes={"added": [], "changed": [], "removed": []},
            configuration={"MY_VAR": {"value": "new", "secret": False}},
            configuration_changes={"added": [], "changed": ["MY_VAR"], "removed": []},
            ingresses={},
        )
        db_session.add(cur_rel)
        db_session.flush()

        entry = _make_entry(
            id=1, object_type="Release", verb="create", object_id=cur_rel.id
        )
        result = _compute_release_changes([entry])
        assert 1 in result
        assert result[1][0]["field"] == "config changed"
        assert result[1][0]["new"] == "MY_VAR"
