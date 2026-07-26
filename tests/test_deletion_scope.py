"""Blast radius of the three deletion paths (issue #376)."""

import pytest

from cabotage.server.user.views import (
    _soft_delete_app_env,
    _soft_delete_application,
    _soft_delete_environment,
)


@pytest.fixture
def envs(make_environment):
    return {slug: make_environment(slug) for slug in ("test", "production")}


@pytest.fixture
def enrolled(application, envs, make_app_env):
    return {slug: make_app_env(application, env) for slug, env in envs.items()}


@pytest.fixture
def unenrolled_from_test(db_session, org, enrolled, no_k8s_cleanup):
    _soft_delete_app_env(enrolled["test"], org)
    db_session.flush()
    return enrolled


@pytest.fixture
def test_env_deleted(db_session, org, envs, enrolled, no_k8s_cleanup):
    _soft_delete_environment(envs["test"], org)
    db_session.flush()
    return enrolled


@pytest.fixture
def application_deleted(db_session, org, application, enrolled, no_k8s_cleanup):
    _soft_delete_application(application, org)
    db_session.flush()
    return enrolled


def _enrolled_slugs(application):
    return sorted(
        ae.environment.slug for ae in application.active_application_environments
    )


class TestUnenrollFromOneEnvironment:
    def test_leaves_the_application_alive(self, application, unenrolled_from_test):
        assert application.deleted_at is None

    def test_leaves_other_environments_enrolled(
        self, application, unenrolled_from_test
    ):
        assert _enrolled_slugs(application) == ["production"]

    def test_removes_only_the_named_enrolment(self, unenrolled_from_test):
        assert unenrolled_from_test["test"].deleted_at is not None
        assert unenrolled_from_test["production"].deleted_at is None


class TestDeleteEnvironment:
    def test_application_survives(self, application, test_env_deleted):
        assert application.deleted_at is None

    def test_other_environment_untouched(self, envs, test_env_deleted):
        assert envs["production"].deleted_at is None
        assert test_env_deleted["production"].deleted_at is None

    def test_drops_only_its_own_enrolments(self, application, test_env_deleted):
        assert test_env_deleted["test"].deleted_at is not None
        assert _enrolled_slugs(application) == ["production"]

    def test_project_stays_and_lists_the_sibling(self, project, test_env_deleted):
        assert project.deleted_at is None
        assert [e.slug for e in project.active_environments] == ["production"]


class TestDeleteApplication:
    def test_removes_every_enrolment(self, application, application_deleted):
        assert application.deleted_at is not None
        assert application.active_application_environments == []
        assert application_deleted["test"].deleted_at is not None
        assert application_deleted["production"].deleted_at is not None

    def test_environments_themselves_survive(self, envs, application_deleted):
        assert envs["test"].deleted_at is None
        assert envs["production"].deleted_at is None
