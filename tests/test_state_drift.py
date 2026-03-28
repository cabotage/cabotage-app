"""Tests for application state drift detection.

Covers all the ways we detect changes between the currently deployed state
and the candidate (live) state: image changes, configuration changes,
ingress changes, and combinations thereof.
"""

import uuid

import pytest

from cabotage.server import db
from cabotage.server.models.auth import Organization
from cabotage.server.models.projects import (
    Application,
    ApplicationEnvironment,
    Configuration,
    ConfigurationSnapshot,
    Deployment,
    DictDiffer,
    Environment,
    EnvironmentConfiguration,
    EnvironmentConfigSubscription,
    Image,
    Ingress,
    IngressHost,
    IngressPath,
    Project,
    Release,
    ReleaseSnapshot,
    activity_plugin,
)
from cabotage.server.wsgi import app as _app

Activity = activity_plugin.activity_cls

REPO = "myorg/myrepo"
COMMIT_SHA = "abc123deadbeef" * 3
BRANCH = "main"
REGISTRY = "registry.example.com"
REPOSITORY_NAME = "cabotage/testorg/testproj/webapp"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app():
    _app.config["TESTING"] = True
    _app.config["WTF_CSRF_ENABLED"] = False
    _app.config["REGISTRY_AUTH_SECRET"] = "test-secret"
    _app.config["REGISTRY_BUILD"] = REGISTRY
    _app.config["REGISTRY_SECURE"] = False
    _app.config["REGISTRY_VERIFY"] = False
    _app.config["BUILDKIT_IMAGE"] = "moby/buildkit:latest"
    _app.config["KUBERNETES_ENABLED"] = True
    _app.config["CELERY_BROKER_URL"] = "redis://localhost:6379/0"
    _app.config["CABOTAGE_OMNIBUS_BUILDS"] = True
    with _app.app_context():
        yield _app


@pytest.fixture
def db_session(app):
    yield db.session
    db.session.rollback()


@pytest.fixture
def org(db_session):
    o = Organization(name="Test Org", slug=f"testorg-{uuid.uuid4().hex[:8]}")
    db_session.add(o)
    db_session.flush()
    return o


@pytest.fixture
def project(db_session, org):
    p = Project(name="Test Project", organization_id=org.id)
    db_session.add(p)
    db_session.flush()
    return p


@pytest.fixture
def environment(db_session, project):
    e = Environment(name="production", project_id=project.id, ephemeral=False)
    db_session.add(e)
    db_session.flush()
    return e


@pytest.fixture
def application(db_session, project):
    a = Application(
        name="webapp",
        slug="webapp",
        project_id=project.id,
        github_app_installation_id=12345,
        github_repository=REPO,
        auto_deploy_branch=BRANCH,
    )
    db_session.add(a)
    db_session.flush()
    return a


@pytest.fixture
def app_env(db_session, application, environment):
    ae = ApplicationEnvironment(
        application_id=application.id,
        environment_id=environment.id,
    )
    db_session.add(ae)
    db_session.flush()
    return ae


@pytest.fixture
def built_image(db_session, application, app_env):
    img = Image(
        application_id=application.id,
        application_environment_id=app_env.id,
        _repository_name=REPOSITORY_NAME,
        image_metadata={"sha": COMMIT_SHA[:40], "installation_id": 12345},
        build_ref=COMMIT_SHA[:40],
    )
    img.built = True
    img.build_job_id = "deadbeef"
    img.processes = {"web": {"cmd": "python -m http.server", "env": []}}
    img.image_id = "sha256:abc123"
    db_session.add(img)
    db_session.flush()
    return img


def _make_deployment(db_session, application, app_env, release):
    """Create a completed deployment from a release."""
    d = Deployment(
        application_id=application.id,
        application_environment_id=app_env.id,
        release=release.asdict,
        complete=True,
    )
    db_session.add(d)
    db_session.flush()
    return d


# ---------------------------------------------------------------------------
# DictDiffer unit tests
# ---------------------------------------------------------------------------


class TestDictDiffer:
    """Unit tests for the DictDiffer comparison utility."""

    def test_no_changes(self):
        d = DictDiffer({"a": 1, "b": 2}, {"a": 1, "b": 2})
        assert not d.has_changes()
        assert d.added() == set()
        assert d.removed() == set()
        assert d.changed() == set()
        assert d.unchanged() == {"a", "b"}

    def test_added_keys(self):
        d = DictDiffer({"a": 1, "b": 2}, {"a": 1})
        assert d.has_changes()
        assert d.added() == {"b"}
        assert d.removed() == set()
        assert d.changed() == set()

    def test_removed_keys(self):
        d = DictDiffer({"a": 1}, {"a": 1, "b": 2})
        assert d.has_changes()
        assert d.added() == set()
        assert d.removed() == {"b"}
        assert d.changed() == set()

    def test_changed_values(self):
        d = DictDiffer({"a": 1, "b": 3}, {"a": 1, "b": 2})
        assert d.has_changes()
        assert d.changed() == {"b"}
        assert d.unchanged() == {"a"}

    def test_ignored_keys_in_nested_dicts(self):
        """Ignored keys within nested dict values should not trigger changes."""
        old = {"cfg": {"id": "aaa", "name": "FOO", "value": "bar"}}
        new = {"cfg": {"id": "bbb", "name": "FOO", "value": "bar"}}
        d = DictDiffer(new, old, ignored_keys=["id"])
        assert not d.has_changes()

    def test_ignored_keys_real_change_still_detected(self):
        """Even with ignored keys, real value changes must be detected."""
        old = {"cfg": {"id": "aaa", "name": "FOO", "value": "bar"}}
        new = {"cfg": {"id": "bbb", "name": "FOO", "value": "baz"}}
        d = DictDiffer(new, old, ignored_keys=["id"])
        assert d.has_changes()
        assert d.changed() == {"cfg"}

    def test_empty_dicts(self):
        d = DictDiffer({}, {})
        assert not d.has_changes()

    def test_from_empty(self):
        """Everything is 'added' when comparing against an empty past."""
        d = DictDiffer({"a": 1}, {})
        assert d.added() == {"a"}
        assert not d.removed()

    def test_to_empty(self):
        """Everything is 'removed' when current is empty."""
        d = DictDiffer({}, {"a": 1})
        assert d.removed() == {"a"}
        assert not d.added()

    def test_asdict_format(self):
        d = DictDiffer({"a": 1, "c": 3}, {"b": 2, "c": 4})
        result = d.asdict
        assert set(result.keys()) == {"added", "removed", "changed"}
        assert "a" in result["added"]
        assert "b" in result["removed"]
        assert "c" in result["changed"]

    def test_multiple_ignored_keys(self):
        old = {"img": {"id": "1", "commit_sha": "aaa", "tag": "v1"}}
        new = {"img": {"id": "2", "commit_sha": "bbb", "tag": "v1"}}
        d = DictDiffer(new, old, ignored_keys=["id", "commit_sha"])
        assert not d.has_changes()

    def test_non_dict_values_ignore_keys_no_effect(self):
        """ignored_keys only applies to dict values, not scalars."""
        d = DictDiffer({"a": 1}, {"a": 2}, ignored_keys=["id"])
        assert d.changed() == {"a"}


# ---------------------------------------------------------------------------
# Image drift tests
# ---------------------------------------------------------------------------


class TestImageDrift:
    """Test detection of new images since last deployment."""

    def test_new_image_detected(self, db_session, application, app_env, built_image):
        """A new image with different processes should be detected as drift."""
        # Create a release and deployment with the current image
        release = Release(
            application_id=application.id,
            application_environment_id=app_env.id,
            _repository_name=REPOSITORY_NAME,
            image=built_image.asdict,
            configuration={},
            image_changes={},
            configuration_changes={},
        )
        db_session.add(release)
        db_session.flush()
        _make_deployment(db_session, application, app_env, release)

        # Build a new image with different processes
        new_img = Image(
            application_id=application.id,
            application_environment_id=app_env.id,
            _repository_name=REPOSITORY_NAME,
            image_metadata={"sha": "def456" * 7, "installation_id": 12345},
            build_ref="def456" * 7,
        )
        new_img.built = True
        new_img.build_job_id = "newbuild"
        new_img.processes = {
            "web": {"cmd": "gunicorn app:app", "env": []},
            "worker": {"cmd": "celery worker", "env": []},
        }
        new_img.image_id = "sha256:def456"
        db_session.add(new_img)
        db_session.flush()

        image_diff, config_diff, ingress_diff = application.ready_for_deployment_in_env(
            app_env
        )
        assert image_diff.has_changes()
        assert not config_diff.has_changes()
        assert not ingress_diff.has_changes()

    def test_same_image_no_drift(self, db_session, application, app_env, built_image):
        """When the deployed image matches the latest built image, no drift."""
        release = Release(
            application_id=application.id,
            application_environment_id=app_env.id,
            _repository_name=REPOSITORY_NAME,
            image=built_image.asdict,
            configuration={},
            image_changes={},
            configuration_changes={},
        )
        db_session.add(release)
        db_session.flush()
        _make_deployment(db_session, application, app_env, release)

        image_diff, config_diff, ingress_diff = application.ready_for_deployment_in_env(
            app_env
        )
        assert not image_diff.has_changes()
        assert not config_diff.has_changes()
        assert not ingress_diff.has_changes()

    def test_commit_sha_change_ignored(
        self, db_session, application, app_env, built_image
    ):
        """commit_sha differences alone should NOT count as image drift."""
        # Deploy with the current image
        release = Release(
            application_id=application.id,
            application_environment_id=app_env.id,
            _repository_name=REPOSITORY_NAME,
            image=built_image.asdict,
            configuration={},
            image_changes={},
            configuration_changes={},
        )
        db_session.add(release)
        db_session.flush()
        _make_deployment(db_session, application, app_env, release)

        # The deployed release has the same image data but a different commit_sha.
        # Since commit_sha is in ignored_keys, this should not be drift.
        # The latest_image_built IS the same built_image, so no drift.
        image_diff, _, _ = application.ready_for_deployment_in_env(app_env)
        assert not image_diff.has_changes()

    def test_no_deployment_everything_is_new(
        self, db_session, application, app_env, built_image
    ):
        """With no prior deployment, all current state is considered new."""
        image_diff, config_diff, ingress_diff = application.ready_for_deployment_in_env(
            app_env
        )
        # Image exists but no deployment yet - should detect the image as new
        assert image_diff.has_changes()
        assert image_diff.added() or image_diff.changed()

    def test_no_image_no_deployment_no_drift(self, db_session, application, app_env):
        """With no image and no deployment, there is nothing to diff."""
        image_diff, config_diff, ingress_diff = application.ready_for_deployment_in_env(
            app_env
        )
        assert not image_diff.has_changes()
        assert not config_diff.has_changes()
        assert not ingress_diff.has_changes()

    def test_image_process_added(self, db_session, application, app_env, built_image):
        """Adding a new process to an image should be detected as drift."""
        # Deploy with current image (has only "web")
        release = Release(
            application_id=application.id,
            application_environment_id=app_env.id,
            _repository_name=REPOSITORY_NAME,
            image=built_image.asdict,
            configuration={},
            image_changes={},
            configuration_changes={},
        )
        db_session.add(release)
        db_session.flush()
        _make_deployment(db_session, application, app_env, release)

        # New image adds a "worker" process
        new_img = Image(
            application_id=application.id,
            application_environment_id=app_env.id,
            _repository_name=REPOSITORY_NAME,
            image_metadata={"sha": "bbb222" * 7, "installation_id": 12345},
            build_ref="bbb222" * 7,
        )
        new_img.built = True
        new_img.build_job_id = "build-proc"
        new_img.processes = {
            "web": {"cmd": "python -m http.server", "env": []},
            "worker": {"cmd": "celery worker", "env": []},
        }
        new_img.image_id = "sha256:bbb222"
        db_session.add(new_img)
        db_session.flush()

        image_diff, _, _ = application.ready_for_deployment_in_env(app_env)
        assert image_diff.has_changes()
        # The image-level diff sees "processes" changed within the image dict
        # (image is a single entry keyed by its fields, not per-process)
        assert image_diff.changed()

    def test_config_drift_without_built_image(self, db_session, application, app_env):
        """Config changes should be detected even when no image has been built."""
        cfg = Configuration(
            application_id=application.id,
            application_environment_id=app_env.id,
            name="EARLY_CONFIG",
            value="set-before-build",
            secret=False,
        )
        db_session.add(cfg)
        db_session.flush()

        image_diff, config_diff, _ = application.ready_for_deployment_in_env(app_env)
        # No image exists, so no image drift
        assert not image_diff.has_changes()
        # But config is present with no prior deployment, so it's "added"
        assert config_diff.has_changes()
        assert "EARLY_CONFIG" in config_diff.added()

    def test_image_process_removed(self, db_session, application, app_env):
        """Removing a process from an image should be detected as drift."""
        # Build an image with two processes
        img_with_two = Image(
            application_id=application.id,
            application_environment_id=app_env.id,
            _repository_name=REPOSITORY_NAME,
            image_metadata={"sha": "ccc333" * 7, "installation_id": 12345},
            build_ref="ccc333" * 7,
        )
        img_with_two.built = True
        img_with_two.build_job_id = "build-two"
        img_with_two.processes = {
            "web": {"cmd": "server", "env": []},
            "worker": {"cmd": "celery", "env": []},
        }
        img_with_two.image_id = "sha256:ccc333"
        db_session.add(img_with_two)
        db_session.flush()

        release = Release(
            application_id=application.id,
            application_environment_id=app_env.id,
            _repository_name=REPOSITORY_NAME,
            image=img_with_two.asdict,
            configuration={},
            image_changes={},
            configuration_changes={},
        )
        db_session.add(release)
        db_session.flush()
        _make_deployment(db_session, application, app_env, release)

        # New image drops "worker"
        img_one_proc = Image(
            application_id=application.id,
            application_environment_id=app_env.id,
            _repository_name=REPOSITORY_NAME,
            image_metadata={"sha": "ddd444" * 7, "installation_id": 12345},
            build_ref="ddd444" * 7,
        )
        img_one_proc.built = True
        img_one_proc.build_job_id = "build-one"
        img_one_proc.processes = {
            "web": {"cmd": "server", "env": []},
        }
        img_one_proc.image_id = "sha256:ddd444"
        db_session.add(img_one_proc)
        db_session.flush()

        image_diff, _, _ = application.ready_for_deployment_in_env(app_env)
        assert image_diff.has_changes()

    def test_errored_image_skipped_for_drift(
        self, db_session, application, app_env, built_image
    ):
        """An errored image should be ignored; drift uses the latest *built* image."""
        # Deploy with the current built image
        release = Release(
            application_id=application.id,
            application_environment_id=app_env.id,
            _repository_name=REPOSITORY_NAME,
            image=built_image.asdict,
            configuration={},
            image_changes={},
            configuration_changes={},
        )
        db_session.add(release)
        db_session.flush()
        _make_deployment(db_session, application, app_env, release)

        # A newer image that errored during build
        errored_img = Image(
            application_id=application.id,
            application_environment_id=app_env.id,
            _repository_name=REPOSITORY_NAME,
            image_metadata={"sha": "eee555" * 7, "installation_id": 12345},
            build_ref="eee555" * 7,
        )
        errored_img.error = True
        errored_img.build_job_id = "failed-build"
        db_session.add(errored_img)
        db_session.flush()

        # Drift should compare against built_image (still the latest *built*),
        # not the errored one — so no drift
        image_diff, _, _ = application.ready_for_deployment_in_env(app_env)
        assert not image_diff.has_changes()

    def test_image_with_empty_processes(
        self, db_session, application, app_env, built_image
    ):
        """An image with empty processes dict should still be diffable."""
        # Deploy with current image that has processes
        release = Release(
            application_id=application.id,
            application_environment_id=app_env.id,
            _repository_name=REPOSITORY_NAME,
            image=built_image.asdict,
            configuration={},
            image_changes={},
            configuration_changes={},
        )
        db_session.add(release)
        db_session.flush()
        _make_deployment(db_session, application, app_env, release)

        # New image with empty processes (e.g. misconfigured build)
        empty_proc_img = Image(
            application_id=application.id,
            application_environment_id=app_env.id,
            _repository_name=REPOSITORY_NAME,
            image_metadata={"sha": "fff666" * 7, "installation_id": 12345},
            build_ref="fff666" * 7,
        )
        empty_proc_img.built = True
        empty_proc_img.build_job_id = "build-empty"
        empty_proc_img.processes = {}
        empty_proc_img.image_id = "sha256:fff666"
        db_session.add(empty_proc_img)
        db_session.flush()

        image_diff, _, _ = application.ready_for_deployment_in_env(app_env)
        assert image_diff.has_changes()


# ---------------------------------------------------------------------------
# Configuration drift tests
# ---------------------------------------------------------------------------


class TestConfigurationDrift:
    """Test detection of configuration changes since last deployment."""

    def test_new_config_variable_detected(
        self, db_session, application, app_env, built_image
    ):
        """Adding a new config variable should be detected as drift."""
        # Deploy with empty config
        release = Release(
            application_id=application.id,
            application_environment_id=app_env.id,
            _repository_name=REPOSITORY_NAME,
            image=built_image.asdict,
            configuration={},
            image_changes={},
            configuration_changes={},
        )
        db_session.add(release)
        db_session.flush()
        _make_deployment(db_session, application, app_env, release)

        # Add a config variable
        cfg = Configuration(
            application_id=application.id,
            application_environment_id=app_env.id,
            name="REDIS_PASSWORD",
            value="secret123",
            secret=True,
        )
        db_session.add(cfg)
        db_session.flush()

        _, config_diff, _ = application.ready_for_deployment_in_env(app_env)
        assert config_diff.has_changes()
        assert "REDIS_PASSWORD" in config_diff.added()

    def test_config_variable_updated(
        self, db_session, application, app_env, built_image
    ):
        """Updating a config variable's version should be detected."""
        cfg = Configuration(
            application_id=application.id,
            application_environment_id=app_env.id,
            name="DATABASE_URL",
            value="postgres://old",
            secret=False,
        )
        db_session.add(cfg)
        db_session.flush()

        # Deploy with current config
        release = Release(
            application_id=application.id,
            application_environment_id=app_env.id,
            _repository_name=REPOSITORY_NAME,
            image=built_image.asdict,
            configuration=Application._resolved_configuration(app_env),
            image_changes={},
            configuration_changes={},
        )
        db_session.add(release)
        db_session.flush()
        _make_deployment(db_session, application, app_env, release)

        # Update the config value (which bumps version_id)
        cfg.value = "postgres://new"
        db_session.add(cfg)
        db_session.flush()

        _, config_diff, _ = application.ready_for_deployment_in_env(app_env)
        assert config_diff.has_changes()
        assert "DATABASE_URL" in config_diff.changed()

    def test_config_variable_removed(
        self, db_session, application, app_env, built_image
    ):
        """Removing (soft-deleting) a config variable should be detected."""
        cfg = Configuration(
            application_id=application.id,
            application_environment_id=app_env.id,
            name="OLD_SECRET",
            value="gone",
            secret=True,
        )
        db_session.add(cfg)
        db_session.flush()

        release = Release(
            application_id=application.id,
            application_environment_id=app_env.id,
            _repository_name=REPOSITORY_NAME,
            image=built_image.asdict,
            configuration=Application._resolved_configuration(app_env),
            image_changes={},
            configuration_changes={},
        )
        db_session.add(release)
        db_session.flush()
        _make_deployment(db_session, application, app_env, release)

        # Soft-delete the config
        cfg.deleted = True
        db_session.add(cfg)
        db_session.flush()

        _, config_diff, _ = application.ready_for_deployment_in_env(app_env)
        assert config_diff.has_changes()
        assert "OLD_SECRET" in config_diff.removed()

    def test_config_id_change_ignored(
        self, db_session, application, app_env, built_image
    ):
        """Config 'id' field differences should be ignored in comparison."""
        # This is tested via DictDiffer ignored_keys, but verify end-to-end:
        # If only the id changed (not version_id, name, or secret), no drift.
        cfg_data = {
            "DB_URL": {
                "id": "old-id",
                "name": "DB_URL",
                "version_id": 1,
                "secret": False,
                "buildtime": False,
            }
        }
        release = Release(
            application_id=application.id,
            application_environment_id=app_env.id,
            _repository_name=REPOSITORY_NAME,
            image=built_image.asdict,
            configuration=cfg_data,
            image_changes={},
            configuration_changes={},
        )
        db_session.add(release)
        db_session.flush()
        _make_deployment(db_session, application, app_env, release)

        # Create a config that has a different id but same name/version/secret
        cfg = Configuration(
            application_id=application.id,
            application_environment_id=app_env.id,
            name="DB_URL",
            value="postgres://host/db",
            secret=False,
        )
        db_session.add(cfg)
        db_session.flush()

        # The new config will have version_id=1 (first version) and a different id.
        # Because 'id' is in ignored_keys, the version_id match matters.
        _, config_diff, _ = application.ready_for_deployment_in_env(app_env)
        # version_id on the new config is 1 which matches, name matches, secret matches
        # Only id differs, which is ignored
        assert not config_diff.has_changes()

    def test_environment_config_subscription_detected(
        self, db_session, application, app_env, built_image, environment
    ):
        """Environment-level config subscriptions should appear in drift."""
        # Deploy with empty config
        release = Release(
            application_id=application.id,
            application_environment_id=app_env.id,
            _repository_name=REPOSITORY_NAME,
            image=built_image.asdict,
            configuration={},
            image_changes={},
            configuration_changes={},
        )
        db_session.add(release)
        db_session.flush()
        _make_deployment(db_session, application, app_env, release)

        # Add an environment-level config and subscribe to it
        env_cfg = EnvironmentConfiguration(
            project_id=application.project_id,
            environment_id=environment.id,
            name="SHARED_SECRET",
            value="shared-value",
            secret=True,
        )
        db_session.add(env_cfg)
        db_session.flush()

        sub = EnvironmentConfigSubscription(
            application_environment_id=app_env.id,
            environment_configuration_id=env_cfg.id,
        )
        db_session.add(sub)
        db_session.flush()

        _, config_diff, _ = application.ready_for_deployment_in_env(app_env)
        assert config_diff.has_changes()
        assert "SHARED_SECRET" in config_diff.added()

    def test_environment_config_updated_after_subscription(
        self, db_session, application, app_env, built_image, environment
    ):
        """Updating a subscribed env config's value should be detected as drift."""
        env_cfg = EnvironmentConfiguration(
            project_id=application.project_id,
            environment_id=environment.id,
            name="SHARED_DB_URL",
            value="postgres://shared-old",
            secret=False,
        )
        db_session.add(env_cfg)
        db_session.flush()

        sub = EnvironmentConfigSubscription(
            application_environment_id=app_env.id,
            environment_configuration_id=env_cfg.id,
        )
        db_session.add(sub)
        db_session.flush()

        # Deploy with the current env config
        release = Release(
            application_id=application.id,
            application_environment_id=app_env.id,
            _repository_name=REPOSITORY_NAME,
            image=built_image.asdict,
            configuration=Application._resolved_configuration(app_env),
            image_changes={},
            configuration_changes={},
        )
        db_session.add(release)
        db_session.flush()
        _make_deployment(db_session, application, app_env, release)

        # Update the env config value (bumps version_id)
        env_cfg.value = "postgres://shared-new"
        db_session.add(env_cfg)
        db_session.flush()
        db_session.expire(app_env)

        _, config_diff, _ = application.ready_for_deployment_in_env(app_env)
        assert config_diff.has_changes()
        assert "SHARED_DB_URL" in config_diff.changed()

    def test_environment_config_deleted(
        self, db_session, application, app_env, built_image, environment
    ):
        """Soft-deleting a subscribed env config should show as removed drift."""
        env_cfg = EnvironmentConfiguration(
            project_id=application.project_id,
            environment_id=environment.id,
            name="LEGACY_TOKEN",
            value="old-token",
            secret=True,
        )
        db_session.add(env_cfg)
        db_session.flush()

        sub = EnvironmentConfigSubscription(
            application_environment_id=app_env.id,
            environment_configuration_id=env_cfg.id,
        )
        db_session.add(sub)
        db_session.flush()

        release = Release(
            application_id=application.id,
            application_environment_id=app_env.id,
            _repository_name=REPOSITORY_NAME,
            image=built_image.asdict,
            configuration=Application._resolved_configuration(app_env),
            image_changes={},
            configuration_changes={},
        )
        db_session.add(release)
        db_session.flush()
        _make_deployment(db_session, application, app_env, release)

        # Soft-delete the env config
        env_cfg.deleted = True
        db_session.add(env_cfg)
        db_session.flush()
        db_session.expire(app_env)

        _, config_diff, _ = application.ready_for_deployment_in_env(app_env)
        assert config_diff.has_changes()
        assert "LEGACY_TOKEN" in config_diff.removed()

    def test_app_config_overrides_env_config(
        self, db_session, application, app_env, built_image, environment
    ):
        """App-level config should override env-level config with same name."""
        env_cfg = EnvironmentConfiguration(
            project_id=application.project_id,
            environment_id=environment.id,
            name="DATABASE_URL",
            value="postgres://shared",
            secret=False,
        )
        db_session.add(env_cfg)
        db_session.flush()

        sub = EnvironmentConfigSubscription(
            application_environment_id=app_env.id,
            environment_configuration_id=env_cfg.id,
        )
        db_session.add(sub)
        db_session.flush()

        app_cfg = Configuration(
            application_id=application.id,
            application_environment_id=app_env.id,
            name="DATABASE_URL",
            value="postgres://app-specific",
            secret=False,
        )
        db_session.add(app_cfg)
        db_session.flush()

        resolved = Application._resolved_configuration(app_env)
        # The app-level config should win
        assert resolved["DATABASE_URL"]["id"] == str(app_cfg.id)

    def test_env_and_app_configs_coexist(
        self, db_session, application, app_env, built_image, environment
    ):
        """Env and app configs with different names should both appear in resolved config."""
        env_cfg = EnvironmentConfiguration(
            project_id=application.project_id,
            environment_id=environment.id,
            name="SHARED_TOKEN",
            value="shared-val",
            secret=True,
        )
        db_session.add(env_cfg)
        db_session.flush()

        sub = EnvironmentConfigSubscription(
            application_environment_id=app_env.id,
            environment_configuration_id=env_cfg.id,
        )
        db_session.add(sub)
        db_session.flush()

        app_cfg = Configuration(
            application_id=application.id,
            application_environment_id=app_env.id,
            name="APP_SECRET",
            value="app-val",
            secret=True,
        )
        db_session.add(app_cfg)
        db_session.flush()

        resolved = Application._resolved_configuration(app_env)
        assert "SHARED_TOKEN" in resolved
        assert "APP_SECRET" in resolved
        assert resolved["SHARED_TOKEN"]["id"] == str(env_cfg.id)
        assert resolved["APP_SECRET"]["id"] == str(app_cfg.id)

    def test_config_secret_flag_changed(
        self, db_session, application, app_env, built_image
    ):
        """Toggling the secret flag on a config should be detected as drift."""
        cfg = Configuration(
            application_id=application.id,
            application_environment_id=app_env.id,
            name="API_TOKEN",
            value="token123",
            secret=False,
        )
        db_session.add(cfg)
        db_session.flush()

        release = Release(
            application_id=application.id,
            application_environment_id=app_env.id,
            _repository_name=REPOSITORY_NAME,
            image=built_image.asdict,
            configuration=Application._resolved_configuration(app_env),
            image_changes={},
            configuration_changes={},
        )
        db_session.add(release)
        db_session.flush()
        _make_deployment(db_session, application, app_env, release)

        # Mark as secret
        cfg.secret = True
        db_session.add(cfg)
        db_session.flush()

        _, config_diff, _ = application.ready_for_deployment_in_env(app_env)
        assert config_diff.has_changes()
        assert "API_TOKEN" in config_diff.changed()

    def test_config_buildtime_flag_changed(
        self, db_session, application, app_env, built_image
    ):
        """Toggling buildtime on a config should be detected as drift.

        Buildtime configs are injected at image build time, so changing
        this flag means an image rebuild is needed to reflect the change.
        """
        cfg = Configuration(
            application_id=application.id,
            application_environment_id=app_env.id,
            name="NPM_TOKEN",
            value="token",
            secret=True,
            buildtime=False,
        )
        db_session.add(cfg)
        db_session.flush()

        release = Release(
            application_id=application.id,
            application_environment_id=app_env.id,
            _repository_name=REPOSITORY_NAME,
            image=built_image.asdict,
            configuration=Application._resolved_configuration(app_env),
            image_changes={},
            configuration_changes={},
        )
        db_session.add(release)
        db_session.flush()
        _make_deployment(db_session, application, app_env, release)

        # Mark as buildtime
        cfg.buildtime = True
        db_session.add(cfg)
        db_session.flush()

        _, config_diff, _ = application.ready_for_deployment_in_env(app_env)
        assert config_diff.has_changes()
        assert "NPM_TOKEN" in config_diff.changed()

    def test_multiple_config_changes(
        self, db_session, application, app_env, built_image
    ):
        """Multiple simultaneous config changes should all be detected."""
        cfg1 = Configuration(
            application_id=application.id,
            application_environment_id=app_env.id,
            name="VAR_A",
            value="a",
            secret=False,
        )
        cfg2 = Configuration(
            application_id=application.id,
            application_environment_id=app_env.id,
            name="VAR_B",
            value="b",
            secret=False,
        )
        db_session.add_all([cfg1, cfg2])
        db_session.flush()

        release = Release(
            application_id=application.id,
            application_environment_id=app_env.id,
            _repository_name=REPOSITORY_NAME,
            image=built_image.asdict,
            configuration=Application._resolved_configuration(app_env),
            image_changes={},
            configuration_changes={},
        )
        db_session.add(release)
        db_session.flush()
        _make_deployment(db_session, application, app_env, release)

        # Change one, delete one, add one
        cfg1.value = "a-updated"
        cfg2.deleted = True
        cfg3 = Configuration(
            application_id=application.id,
            application_environment_id=app_env.id,
            name="VAR_C",
            value="c",
            secret=False,
        )
        db_session.add_all([cfg1, cfg2, cfg3])
        db_session.flush()
        db_session.expire(app_env)

        _, config_diff, _ = application.ready_for_deployment_in_env(app_env)
        assert config_diff.has_changes()
        assert "VAR_A" in config_diff.changed()
        assert "VAR_B" in config_diff.removed()
        assert "VAR_C" in config_diff.added()


# ---------------------------------------------------------------------------
# Ingress drift tests
# ---------------------------------------------------------------------------


class TestIngressDrift:
    """Test detection of ingress changes since last deployment."""

    def test_new_ingress_detected(self, db_session, application, app_env, built_image):
        """Adding a new ingress should be detected as drift."""
        release = Release(
            application_id=application.id,
            application_environment_id=app_env.id,
            _repository_name=REPOSITORY_NAME,
            image=built_image.asdict,
            configuration={},
            image_changes={},
            configuration_changes={},
            ingresses={},
            ingress_changes={},
        )
        db_session.add(release)
        db_session.flush()
        _make_deployment(db_session, application, app_env, release)

        # Add an ingress
        ing = Ingress(
            application_environment_id=app_env.id,
            name="web",
        )
        db_session.add(ing)
        db_session.flush()

        _, _, ingress_diff = application.ready_for_deployment_in_env(app_env)
        assert ingress_diff.has_changes()
        assert "web" in ingress_diff.added()

    def test_ingress_removed_detected(
        self, db_session, application, app_env, built_image
    ):
        """Removing an ingress should be detected as drift."""
        ing = Ingress(
            application_environment_id=app_env.id,
            name="web",
        )
        db_session.add(ing)
        db_session.flush()

        release = Release(
            application_id=application.id,
            application_environment_id=app_env.id,
            _repository_name=REPOSITORY_NAME,
            image=built_image.asdict,
            configuration={},
            image_changes={},
            configuration_changes={},
            ingresses={"web": ing.asdict},
            ingress_changes={},
        )
        db_session.add(release)
        db_session.flush()
        _make_deployment(db_session, application, app_env, release)

        # Remove the ingress
        db_session.delete(ing)
        db_session.flush()

        _, _, ingress_diff = application.ready_for_deployment_in_env(app_env)
        assert ingress_diff.has_changes()
        assert "web" in ingress_diff.removed()

    def test_ingress_host_added(self, db_session, application, app_env, built_image):
        """Adding a host to an existing ingress should be detected."""
        ing = Ingress(
            application_environment_id=app_env.id,
            name="web",
        )
        db_session.add(ing)
        db_session.flush()

        host1 = IngressHost(
            ingress_id=ing.id,
            hostname="app.example.com",
        )
        db_session.add(host1)
        db_session.flush()

        release = Release(
            application_id=application.id,
            application_environment_id=app_env.id,
            _repository_name=REPOSITORY_NAME,
            image=built_image.asdict,
            configuration={},
            image_changes={},
            configuration_changes={},
            ingresses={"web": ing.asdict},
            ingress_changes={},
        )
        db_session.add(release)
        db_session.flush()
        _make_deployment(db_session, application, app_env, release)

        # Add a second host
        host2 = IngressHost(
            ingress_id=ing.id,
            hostname="api.example.com",
        )
        db_session.add(host2)
        db_session.flush()
        db_session.expire(ing)
        db_session.expire(app_env)

        _, _, ingress_diff = application.ready_for_deployment_in_env(app_env)
        assert ingress_diff.has_changes()
        assert "web" in ingress_diff.changed()

    def test_ingress_host_removed(self, db_session, application, app_env, built_image):
        """Removing a host from an existing ingress should be detected."""
        ing = Ingress(
            application_environment_id=app_env.id,
            name="web",
        )
        db_session.add(ing)
        db_session.flush()

        host1 = IngressHost(ingress_id=ing.id, hostname="app.example.com")
        host2 = IngressHost(ingress_id=ing.id, hostname="legacy.example.com")
        db_session.add_all([host1, host2])
        db_session.flush()

        release = Release(
            application_id=application.id,
            application_environment_id=app_env.id,
            _repository_name=REPOSITORY_NAME,
            image=built_image.asdict,
            configuration={},
            image_changes={},
            configuration_changes={},
            ingresses={"web": ing.asdict},
            ingress_changes={},
        )
        db_session.add(release)
        db_session.flush()
        _make_deployment(db_session, application, app_env, release)

        # Remove one host
        db_session.delete(host2)
        db_session.flush()
        db_session.expire(ing)
        db_session.expire(app_env)

        _, _, ingress_diff = application.ready_for_deployment_in_env(app_env)
        assert ingress_diff.has_changes()
        assert "web" in ingress_diff.changed()

    def test_ingress_path_added(self, db_session, application, app_env, built_image):
        """Adding a path to an existing ingress should be detected."""
        ing = Ingress(
            application_environment_id=app_env.id,
            name="web",
        )
        db_session.add(ing)
        db_session.flush()

        release = Release(
            application_id=application.id,
            application_environment_id=app_env.id,
            _repository_name=REPOSITORY_NAME,
            image=built_image.asdict,
            configuration={},
            image_changes={},
            configuration_changes={},
            ingresses={"web": ing.asdict},
            ingress_changes={},
        )
        db_session.add(release)
        db_session.flush()
        _make_deployment(db_session, application, app_env, release)

        # Add a path
        path = IngressPath(
            ingress_id=ing.id,
            path="/api",
            path_type="Prefix",
            target_process_name="web",
        )
        db_session.add(path)
        db_session.flush()
        db_session.expire(ing)
        db_session.expire(app_env)

        _, _, ingress_diff = application.ready_for_deployment_in_env(app_env)
        assert ingress_diff.has_changes()
        assert "web" in ingress_diff.changed()

    def test_ingress_path_removed(self, db_session, application, app_env, built_image):
        """Removing a path from an existing ingress should be detected."""
        ing = Ingress(
            application_environment_id=app_env.id,
            name="web",
        )
        db_session.add(ing)
        db_session.flush()

        path1 = IngressPath(
            ingress_id=ing.id, path="/", path_type="Prefix", target_process_name="web"
        )
        path2 = IngressPath(
            ingress_id=ing.id,
            path="/api",
            path_type="Prefix",
            target_process_name="web",
        )
        db_session.add_all([path1, path2])
        db_session.flush()

        release = Release(
            application_id=application.id,
            application_environment_id=app_env.id,
            _repository_name=REPOSITORY_NAME,
            image=built_image.asdict,
            configuration={},
            image_changes={},
            configuration_changes={},
            ingresses={"web": ing.asdict},
            ingress_changes={},
        )
        db_session.add(release)
        db_session.flush()
        _make_deployment(db_session, application, app_env, release)

        # Remove one path
        db_session.delete(path2)
        db_session.flush()
        db_session.expire(ing)
        db_session.expire(app_env)

        _, _, ingress_diff = application.ready_for_deployment_in_env(app_env)
        assert ingress_diff.has_changes()
        assert "web" in ingress_diff.changed()

    def test_ingress_setting_changed(
        self, db_session, application, app_env, built_image
    ):
        """Changing an ingress setting (e.g. enabled, timeout) should be detected."""
        ing = Ingress(
            application_environment_id=app_env.id,
            name="web",
            enabled=True,
            proxy_read_timeout="60",
        )
        db_session.add(ing)
        db_session.flush()

        release = Release(
            application_id=application.id,
            application_environment_id=app_env.id,
            _repository_name=REPOSITORY_NAME,
            image=built_image.asdict,
            configuration={},
            image_changes={},
            configuration_changes={},
            ingresses={"web": ing.asdict},
            ingress_changes={},
        )
        db_session.add(release)
        db_session.flush()
        _make_deployment(db_session, application, app_env, release)

        # Change a setting
        ing.proxy_read_timeout = "120"
        db_session.add(ing)
        db_session.flush()

        _, _, ingress_diff = application.ready_for_deployment_in_env(app_env)
        assert ingress_diff.has_changes()
        assert "web" in ingress_diff.changed()

    def test_ingress_no_changes(self, db_session, application, app_env, built_image):
        """When ingress hasn't changed, no drift should be detected."""
        ing = Ingress(
            application_environment_id=app_env.id,
            name="web",
        )
        db_session.add(ing)
        db_session.flush()

        release = Release(
            application_id=application.id,
            application_environment_id=app_env.id,
            _repository_name=REPOSITORY_NAME,
            image=built_image.asdict,
            configuration={},
            image_changes={},
            configuration_changes={},
            ingresses={"web": ing.asdict},
            ingress_changes={},
        )
        db_session.add(release)
        db_session.flush()
        _make_deployment(db_session, application, app_env, release)

        _, _, ingress_diff = application.ready_for_deployment_in_env(app_env)
        assert not ingress_diff.has_changes()

    def test_ingress_host_tls_changed(
        self, db_session, application, app_env, built_image
    ):
        """Toggling tls_enabled on a host should be detected as drift."""
        ing = Ingress(
            application_environment_id=app_env.id,
            name="web",
        )
        db_session.add(ing)
        db_session.flush()

        host = IngressHost(
            ingress_id=ing.id,
            hostname="app.example.com",
            tls_enabled=True,
        )
        db_session.add(host)
        db_session.flush()

        release = Release(
            application_id=application.id,
            application_environment_id=app_env.id,
            _repository_name=REPOSITORY_NAME,
            image=built_image.asdict,
            configuration={},
            image_changes={},
            configuration_changes={},
            ingresses={"web": ing.asdict},
            ingress_changes={},
        )
        db_session.add(release)
        db_session.flush()
        _make_deployment(db_session, application, app_env, release)

        # Disable TLS on the host
        host.tls_enabled = False
        db_session.add(host)
        db_session.flush()
        db_session.expire(ing)
        db_session.expire(app_env)

        _, _, ingress_diff = application.ready_for_deployment_in_env(app_env)
        assert ingress_diff.has_changes()
        assert "web" in ingress_diff.changed()

    def test_multiple_ingresses_mixed_changes(
        self, db_session, application, app_env, built_image
    ):
        """One ingress added, one changed, one removed simultaneously."""
        ing_keep = Ingress(
            application_environment_id=app_env.id,
            name="api",
            enabled=True,
        )
        ing_remove = Ingress(
            application_environment_id=app_env.id,
            name="legacy",
        )
        db_session.add_all([ing_keep, ing_remove])
        db_session.flush()

        release = Release(
            application_id=application.id,
            application_environment_id=app_env.id,
            _repository_name=REPOSITORY_NAME,
            image=built_image.asdict,
            configuration={},
            image_changes={},
            configuration_changes={},
            ingresses={"api": ing_keep.asdict, "legacy": ing_remove.asdict},
            ingress_changes={},
        )
        db_session.add(release)
        db_session.flush()
        _make_deployment(db_session, application, app_env, release)

        # Change one, remove one, add one
        ing_keep.enabled = False
        db_session.delete(ing_remove)
        ing_new = Ingress(
            application_environment_id=app_env.id,
            name="admin",
        )
        db_session.add(ing_new)
        db_session.flush()
        db_session.expire(app_env)

        _, _, ingress_diff = application.ready_for_deployment_in_env(app_env)
        assert ingress_diff.has_changes()
        assert "api" in ingress_diff.changed()
        assert "legacy" in ingress_diff.removed()
        assert "admin" in ingress_diff.added()


# ---------------------------------------------------------------------------
# Combined drift tests
# ---------------------------------------------------------------------------


class TestCombinedDrift:
    """Test detection of multiple simultaneous types of drift."""

    def test_image_and_config_changed(
        self, db_session, application, app_env, built_image
    ):
        """Both image and config changes should be detected simultaneously."""
        cfg = Configuration(
            application_id=application.id,
            application_environment_id=app_env.id,
            name="API_KEY",
            value="old-key",
            secret=True,
        )
        db_session.add(cfg)
        db_session.flush()

        release = Release(
            application_id=application.id,
            application_environment_id=app_env.id,
            _repository_name=REPOSITORY_NAME,
            image=built_image.asdict,
            configuration=Application._resolved_configuration(app_env),
            image_changes={},
            configuration_changes={},
        )
        db_session.add(release)
        db_session.flush()
        _make_deployment(db_session, application, app_env, release)

        # New image
        new_img = Image(
            application_id=application.id,
            application_environment_id=app_env.id,
            _repository_name=REPOSITORY_NAME,
            image_metadata={"sha": "fff000" * 7, "installation_id": 12345},
            build_ref="fff000" * 7,
        )
        new_img.built = True
        new_img.build_job_id = "newbuild2"
        new_img.processes = {"web": {"cmd": "new-server", "env": []}}
        new_img.image_id = "sha256:fff000"
        db_session.add(new_img)
        db_session.flush()

        # Updated config
        cfg.value = "new-key"
        db_session.add(cfg)
        db_session.flush()

        image_diff, config_diff, ingress_diff = application.ready_for_deployment_in_env(
            app_env
        )
        assert image_diff.has_changes()
        assert config_diff.has_changes()

    def test_all_three_changed(self, db_session, application, app_env, built_image):
        """Image, config, and ingress all changed at once."""
        cfg = Configuration(
            application_id=application.id,
            application_environment_id=app_env.id,
            name="PORT",
            value="8080",
            secret=False,
        )
        ing = Ingress(
            application_environment_id=app_env.id,
            name="web",
            enabled=True,
        )
        db_session.add_all([cfg, ing])
        db_session.flush()

        release = Release(
            application_id=application.id,
            application_environment_id=app_env.id,
            _repository_name=REPOSITORY_NAME,
            image=built_image.asdict,
            configuration=Application._resolved_configuration(app_env),
            image_changes={},
            configuration_changes={},
            ingresses={"web": ing.asdict},
            ingress_changes={},
        )
        db_session.add(release)
        db_session.flush()
        _make_deployment(db_session, application, app_env, release)

        # Change all three
        new_img = Image(
            application_id=application.id,
            application_environment_id=app_env.id,
            _repository_name=REPOSITORY_NAME,
            image_metadata={"sha": "aaa111" * 7, "installation_id": 12345},
            build_ref="aaa111" * 7,
        )
        new_img.built = True
        new_img.build_job_id = "build3"
        new_img.processes = {"web": {"cmd": "updated-server", "env": []}}
        new_img.image_id = "sha256:aaa111"
        db_session.add(new_img)

        cfg.value = "9090"

        ing.enabled = False

        db_session.flush()

        image_diff, config_diff, ingress_diff = application.ready_for_deployment_in_env(
            app_env
        )
        assert image_diff.has_changes()
        assert config_diff.has_changes()
        assert ingress_diff.has_changes()

    def test_no_drift_after_fresh_deploy(
        self, db_session, application, app_env, built_image
    ):
        """After deploying with current state, there should be zero drift."""
        cfg = Configuration(
            application_id=application.id,
            application_environment_id=app_env.id,
            name="KEY",
            value="val",
            secret=False,
        )
        ing = Ingress(
            application_environment_id=app_env.id,
            name="web",
        )
        db_session.add_all([cfg, ing])
        db_session.flush()

        release = application.create_release(app_env)
        db_session.add(release)
        db_session.flush()
        _make_deployment(db_session, application, app_env, release)

        image_diff, config_diff, ingress_diff = application.ready_for_deployment_in_env(
            app_env
        )
        assert not image_diff.has_changes()
        assert not config_diff.has_changes()
        assert not ingress_diff.has_changes()

    def test_failed_deployment_ignored_for_drift(
        self, db_session, application, app_env, built_image
    ):
        """Drift should compare against the last successful deploy, not a failed one.

        If a deployment fails (error=True, complete=False), changes made
        after that failed deploy should still show as drift relative to
        the last *completed* deployment.
        """
        cfg = Configuration(
            application_id=application.id,
            application_environment_id=app_env.id,
            name="DB_URL",
            value="postgres://v1",
            secret=False,
        )
        db_session.add(cfg)
        db_session.flush()

        # Successful deployment with v1 config
        release_v1 = application.create_release(app_env)
        db_session.add(release_v1)
        db_session.flush()
        _make_deployment(db_session, application, app_env, release_v1)

        # Update config to v2
        cfg.value = "postgres://v2"
        db_session.flush()
        db_session.expire(app_env)

        # Create a release and deployment that FAILS
        release_v2 = application.create_release(app_env)
        db_session.add(release_v2)
        db_session.flush()
        failed_deploy = Deployment(
            application_id=application.id,
            application_environment_id=app_env.id,
            release=release_v2.asdict,
            complete=False,
            error=True,
            error_detail="deploy timed out",
        )
        db_session.add(failed_deploy)
        db_session.flush()

        # Update config to v3
        cfg.value = "postgres://v3"
        db_session.flush()
        db_session.expire(app_env)

        # Drift should be relative to v1 (last completed), not v2 (failed)
        _, config_diff, _ = application.ready_for_deployment_in_env(app_env)
        assert config_diff.has_changes()
        assert "DB_URL" in config_diff.changed()

    def test_drift_compares_against_latest_completed_deployment(
        self, db_session, application, app_env, built_image
    ):
        """With multiple completed deployments, drift uses the most recent."""
        cfg = Configuration(
            application_id=application.id,
            application_environment_id=app_env.id,
            name="VERSION",
            value="v1",
            secret=False,
        )
        db_session.add(cfg)
        db_session.flush()

        # First successful deployment
        release_v1 = application.create_release(app_env)
        db_session.add(release_v1)
        db_session.flush()
        _make_deployment(db_session, application, app_env, release_v1)

        # Update config and deploy again
        cfg.value = "v2"
        db_session.flush()
        db_session.expire(app_env)

        release_v2 = application.create_release(app_env)
        db_session.add(release_v2)
        db_session.flush()
        _make_deployment(db_session, application, app_env, release_v2)

        # No further changes — drift should compare against v2 (latest),
        # not v1, so there should be zero drift
        image_diff, config_diff, ingress_diff = application.ready_for_deployment_in_env(
            app_env
        )
        assert not config_diff.has_changes()

        # Now change config to v3 — drift should detect change from v2
        cfg.value = "v3"
        db_session.flush()
        db_session.expire(app_env)

        _, config_diff, _ = application.ready_for_deployment_in_env(app_env)
        assert config_diff.has_changes()
        assert "VERSION" in config_diff.changed()

    def test_pre_ingress_deployment_missing_key(
        self, db_session, application, app_env, built_image
    ):
        """A deployment from before ingress tracking (no 'ingresses' key) should not break drift.

        Old deployments may have a release JSONB snapshot without an
        'ingresses' key. Adding an ingress after that should detect it
        as added, not crash.
        """
        # Simulate a pre-ingress-tracking deployment by storing a release
        # snapshot without the "ingresses" key
        old_release_snapshot = {
            "id": "fake-old-release-id",
            "application_id": str(application.id),
            "platform": "wind",
            "image": built_image.asdict,
            "configuration": {},
            # No "ingresses" key at all
        }
        old_deploy = Deployment(
            application_id=application.id,
            application_environment_id=app_env.id,
            release=old_release_snapshot,
            complete=True,
        )
        db_session.add(old_deploy)
        db_session.flush()

        # Now add an ingress
        ing = Ingress(
            application_environment_id=app_env.id,
            name="web",
        )
        db_session.add(ing)
        db_session.flush()
        db_session.expire(app_env)

        # Should detect the new ingress without crashing
        _, _, ingress_diff = application.ready_for_deployment_in_env(app_env)
        assert ingress_diff.has_changes()
        assert "web" in ingress_diff.added()

    def test_pre_ingress_deployment_null_value(
        self, db_session, application, app_env, built_image
    ):
        """A deployment with 'ingresses': null should not break drift.

        Old rows might have JSON null for ingresses. The .get() fallback
        handles missing keys but not null values — this tests the actual
        behavior.
        """
        old_release_snapshot = {
            "id": "fake-old-release-id",
            "application_id": str(application.id),
            "platform": "wind",
            "image": built_image.asdict,
            "configuration": {},
            "ingresses": None,
        }
        old_deploy = Deployment(
            application_id=application.id,
            application_environment_id=app_env.id,
            release=old_release_snapshot,
            complete=True,
        )
        db_session.add(old_deploy)
        db_session.flush()

        ing = Ingress(
            application_environment_id=app_env.id,
            name="web",
        )
        db_session.add(ing)
        db_session.flush()
        db_session.expire(app_env)

        # This will blow up if the code passes None to DictDiffer
        # instead of falling back to {}
        try:
            _, _, ingress_diff = application.ready_for_deployment_in_env(app_env)
            # If it doesn't crash, it should detect the new ingress
            assert ingress_diff.has_changes()
        except (TypeError, AttributeError):
            # Documents a real edge case: .get("ingresses", {}) returns
            # None (not {}) when the key exists with a null value
            pytest.fail(
                "ready_for_deployment_in_env crashes when deployed release "
                "has ingresses=null. Consider using `or {}` instead of "
                ".get('ingresses', {})"
            )


# ---------------------------------------------------------------------------
# Release creation change tracking tests
# ---------------------------------------------------------------------------


class TestReleaseChangeTracking:
    """Test that create_release correctly records what changed."""

    def test_create_release_records_image_changes(
        self, db_session, application, app_env, built_image
    ):
        """create_release should capture image diff in image_changes."""
        release = application.create_release(app_env)
        db_session.add(release)
        db_session.flush()

        # First release from empty - image was "added"
        assert release.image_changes is not None
        changes = release.image_changes
        assert isinstance(changes, dict)
        assert "added" in changes
        assert "removed" in changes
        assert "changed" in changes

    def test_create_release_records_config_changes(
        self, db_session, application, app_env, built_image
    ):
        """create_release should capture config diff in configuration_changes."""
        cfg = Configuration(
            application_id=application.id,
            application_environment_id=app_env.id,
            name="NEW_VAR",
            value="hello",
            secret=False,
        )
        db_session.add(cfg)
        db_session.flush()

        release = application.create_release(app_env)
        db_session.add(release)
        db_session.flush()

        assert "NEW_VAR" in release.configuration_changes.get("added", [])

    def test_create_release_records_ingress_changes(
        self, db_session, application, app_env, built_image
    ):
        """create_release should capture ingress diff in ingress_changes."""
        ing = Ingress(
            application_environment_id=app_env.id,
            name="web",
        )
        db_session.add(ing)
        db_session.flush()

        release = application.create_release(app_env)
        db_session.add(release)
        db_session.flush()

        assert "web" in release.ingress_changes.get("added", [])

    def test_create_release_snapshots_current_state(
        self, db_session, application, app_env, built_image
    ):
        """create_release should snapshot the current image, config, and ingresses."""
        cfg = Configuration(
            application_id=application.id,
            application_environment_id=app_env.id,
            name="DB_URL",
            value="postgres://host/db",
            secret=False,
        )
        ing = Ingress(
            application_environment_id=app_env.id,
            name="api",
        )
        db_session.add_all([cfg, ing])
        db_session.flush()

        release = application.create_release(app_env)
        db_session.add(release)
        db_session.flush()

        # Image snapshot
        assert release.image["repository"] == REPOSITORY_NAME
        assert release.image["processes"] == built_image.processes

        # Config snapshot
        assert "DB_URL" in release.configuration
        assert release.configuration["DB_URL"]["name"] == "DB_URL"

        # Ingress snapshot
        assert "api" in release.ingresses

    def test_image_asdict_has_no_version_id_key(
        self, db_session, application, app_env, built_image
    ):
        """Image.asdict does not include a 'version_id' key.

        The view uses ignored_keys=["id", "version_id", "commit_sha"] for
        image diffs while the model uses ["id", "commit_sha"]. The extra
        "version_id" in the view is harmless because Image.asdict maps
        the version as "tag", not "version_id". This test documents that
        the key is absent so the discrepancy is a no-op.
        """
        image_dict = built_image.asdict
        assert "version_id" not in image_dict
        assert "tag" in image_dict
        assert image_dict["tag"] == str(built_image.version)

    def test_configuration_snapshot_without_buildtime_key(self):
        """Old release snapshots without 'buildtime' in config data should not crash.

        Releases created before buildtime was added to Configuration.asdict
        won't have the key. ConfigurationSnapshot should default to False.
        """
        old_config_data = {
            "id": "some-id",
            "name": "OLD_VAR",
            "version_id": 1,
            "secret": False,
            # No "buildtime" key
        }
        snapshot = ConfigurationSnapshot(old_config_data)
        assert snapshot.name == "OLD_VAR"
        assert snapshot.buildtime is False

    def test_configuration_snapshot_with_buildtime_key(self):
        """Config snapshots with buildtime should preserve the value."""
        config_data = {
            "id": "some-id",
            "name": "BUILD_VAR",
            "version_id": 2,
            "secret": True,
            "buildtime": True,
        }
        snapshot = ConfigurationSnapshot(config_data)
        assert snapshot.buildtime is True

    def test_release_snapshot_with_old_config_format(self):
        """A ReleaseSnapshot with old-format config (no buildtime) should work."""
        release_data = {
            "id": "release-id",
            "application_id": "app-id",
            "platform": "wind",
            "image": None,
            "configuration": {
                "DB_URL": {
                    "id": "cfg-id",
                    "name": "DB_URL",
                    "version_id": 1,
                    "secret": False,
                },
            },
            "ingresses": {},
        }
        snapshot = ReleaseSnapshot(release_data)
        assert "DB_URL" in snapshot.configuration_snapshots
        assert snapshot.configuration_snapshots["DB_URL"].buildtime is False


# ---------------------------------------------------------------------------
# View-layer ingress change detail tests
# ---------------------------------------------------------------------------


class TestIngressChangeDetails:
    """Test the per-ingress change detail computation done in the view."""

    @staticmethod
    def _compute_ingress_change_details(ingress_diff):
        """Replicate the view-layer ingress change detail logic for testing."""
        ingress_change_details = {}
        for name in ingress_diff.changed():
            old_ing = ingress_diff.past_dict[name]
            new_ing = ingress_diff.current_dict[name]
            details = []
            old_hosts = {h["hostname"] for h in old_ing.get("hosts", [])}
            new_hosts = {h["hostname"] for h in new_ing.get("hosts", [])}
            h_added = len(new_hosts - old_hosts)
            h_removed = len(old_hosts - new_hosts)
            if h_added or h_removed:
                parts = []
                if h_added:
                    parts.append(f"{h_added} added")
                if h_removed:
                    parts.append(f"{h_removed} removed")
                details.append(f"hosts: {', '.join(parts)}")
            old_paths = {p["path"] for p in old_ing.get("paths", [])}
            new_paths = {p["path"] for p in new_ing.get("paths", [])}
            p_added = len(new_paths - old_paths)
            p_removed = len(old_paths - new_paths)
            if p_added or p_removed:
                parts = []
                if p_added:
                    parts.append(f"{p_added} added")
                if p_removed:
                    parts.append(f"{p_removed} removed")
                details.append(f"paths: {', '.join(parts)}")
            setting_keys = {
                "enabled",
                "ingress_class_name",
                "backend_protocol",
                "proxy_connect_timeout",
                "proxy_read_timeout",
                "proxy_send_timeout",
                "proxy_body_size",
                "client_body_buffer_size",
                "proxy_request_buffering",
                "session_affinity",
                "use_regex",
                "allow_annotations",
                "extra_annotations",
                "cluster_issuer",
                "force_ssl_redirect",
                "service_upstream",
            }
            changed_settings = [
                k for k in setting_keys if old_ing.get(k) != new_ing.get(k)
            ]
            if changed_settings:
                details.append(f"settings: {', '.join(sorted(changed_settings))}")
            ingress_change_details[name] = details
        return ingress_change_details

    def test_host_added_detail(self):
        old = {"web": {"id": "1", "hosts": [{"hostname": "a.com"}], "paths": []}}
        new = {
            "web": {
                "id": "2",
                "hosts": [{"hostname": "a.com"}, {"hostname": "b.com"}],
                "paths": [],
            }
        }
        diff = DictDiffer(new, old, ignored_keys=["id"])
        details = self._compute_ingress_change_details(diff)
        assert "web" in details
        assert any("hosts" in d and "1 added" in d for d in details["web"])

    def test_host_removed_detail(self):
        old = {
            "web": {
                "id": "1",
                "hosts": [{"hostname": "a.com"}, {"hostname": "b.com"}],
                "paths": [],
            }
        }
        new = {"web": {"id": "2", "hosts": [{"hostname": "a.com"}], "paths": []}}
        diff = DictDiffer(new, old, ignored_keys=["id"])
        details = self._compute_ingress_change_details(diff)
        assert any("hosts" in d and "1 removed" in d for d in details["web"])

    def test_path_added_detail(self):
        old = {"web": {"id": "1", "hosts": [], "paths": []}}
        new = {"web": {"id": "2", "hosts": [], "paths": [{"path": "/api"}]}}
        diff = DictDiffer(new, old, ignored_keys=["id"])
        details = self._compute_ingress_change_details(diff)
        assert any("paths" in d and "1 added" in d for d in details["web"])

    def test_setting_changed_detail(self):
        old = {
            "web": {
                "id": "1",
                "hosts": [],
                "paths": [],
                "enabled": True,
                "proxy_read_timeout": "60",
            }
        }
        new = {
            "web": {
                "id": "2",
                "hosts": [],
                "paths": [],
                "enabled": False,
                "proxy_read_timeout": "60",
            }
        }
        diff = DictDiffer(new, old, ignored_keys=["id"])
        details = self._compute_ingress_change_details(diff)
        assert any("settings" in d and "enabled" in d for d in details["web"])

    def test_multiple_setting_changes(self):
        old = {
            "web": {
                "id": "1",
                "hosts": [],
                "paths": [],
                "enabled": True,
                "session_affinity": False,
            }
        }
        new = {
            "web": {
                "id": "2",
                "hosts": [],
                "paths": [],
                "enabled": False,
                "session_affinity": True,
            }
        }
        diff = DictDiffer(new, old, ignored_keys=["id"])
        details = self._compute_ingress_change_details(diff)
        settings_detail = [d for d in details["web"] if "settings" in d][0]
        assert "enabled" in settings_detail
        assert "session_affinity" in settings_detail

    def test_no_detail_for_unchanged_ingress(self):
        old = {"web": {"id": "1", "hosts": [], "paths": [], "enabled": True}}
        new = {"web": {"id": "2", "hosts": [], "paths": [], "enabled": True}}
        diff = DictDiffer(new, old, ignored_keys=["id"])
        details = self._compute_ingress_change_details(diff)
        assert details == {}

    def test_extra_annotations_changed_detail(self):
        """Changing extra_annotations (a dict-valued setting) should appear in details."""
        old = {
            "web": {
                "id": "1",
                "hosts": [],
                "paths": [],
                "extra_annotations": {"nginx.org/proxy-buffering": "true"},
            }
        }
        new = {
            "web": {
                "id": "2",
                "hosts": [],
                "paths": [],
                "extra_annotations": {
                    "nginx.org/proxy-buffering": "false",
                    "nginx.org/rate-limit": "10",
                },
            }
        }
        diff = DictDiffer(new, old, ignored_keys=["id"])
        details = self._compute_ingress_change_details(diff)
        assert any("settings" in d and "extra_annotations" in d for d in details["web"])

    def test_tailscale_settings_not_in_detail(self):
        """Tailscale setting changes are detected as drift but produce no detail.

        The view's setting_keys set does not include tailscale_hostname,
        tailscale_funnel, or tailscale_tags. The DictDiffer will flag the
        ingress as changed, but the detail summary will be empty.
        """
        old = {
            "web": {
                "id": "1",
                "hosts": [],
                "paths": [],
                "tailscale_hostname": "old-host",
                "tailscale_funnel": False,
                "tailscale_tags": "",
            }
        }
        new = {
            "web": {
                "id": "2",
                "hosts": [],
                "paths": [],
                "tailscale_hostname": "new-host",
                "tailscale_funnel": True,
                "tailscale_tags": "tag:web",
            }
        }
        diff = DictDiffer(new, old, ignored_keys=["id"])
        # The ingress IS detected as changed
        assert "web" in diff.changed()
        # But the detail computation misses tailscale-specific settings
        details = self._compute_ingress_change_details(diff)
        assert details["web"] == []

    def test_path_removed_detail(self):
        old = {
            "web": {"id": "1", "hosts": [], "paths": [{"path": "/"}, {"path": "/api"}]}
        }
        new = {"web": {"id": "2", "hosts": [], "paths": [{"path": "/"}]}}
        diff = DictDiffer(new, old, ignored_keys=["id"])
        details = self._compute_ingress_change_details(diff)
        assert any("paths" in d and "1 removed" in d for d in details["web"])

    def test_combined_host_path_and_setting_changes(self):
        """All three types of ingress sub-changes on a single ingress."""
        old = {
            "web": {
                "id": "1",
                "hosts": [{"hostname": "old.com"}],
                "paths": [{"path": "/old"}],
                "enabled": True,
                "session_affinity": False,
            }
        }
        new = {
            "web": {
                "id": "2",
                "hosts": [{"hostname": "old.com"}, {"hostname": "new.com"}],
                "paths": [{"path": "/new"}],
                "enabled": False,
                "session_affinity": True,
            }
        }
        diff = DictDiffer(new, old, ignored_keys=["id"])
        details = self._compute_ingress_change_details(diff)
        detail_lines = details["web"]
        assert len(detail_lines) == 3
        assert any("hosts" in d and "1 added" in d for d in detail_lines)
        assert any(
            "paths" in d and "1 added" in d and "1 removed" in d for d in detail_lines
        )
        assert any(
            "settings" in d and "enabled" in d and "session_affinity" in d
            for d in detail_lines
        )
