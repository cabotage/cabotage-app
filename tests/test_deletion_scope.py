"""Blast radius of the three deletion paths (issue #376)."""

import pytest

from cabotage.server.user.views import (
    _soft_delete_app_env,
    _soft_delete_application,
    _soft_delete_environment,
)


@pytest.fixture
def two_envs(make_environment):
    return make_environment("test"), make_environment("production")


@pytest.fixture
def enrolled(application, two_envs, make_app_env):
    test_env, prod_env = two_envs
    return {
        "test": make_app_env(application, test_env, auto_deploy_branch="feature-x"),
        "production": make_app_env(application, prod_env),
    }


class TestUnenrollFromOneEnvironment:
    def test_leaves_the_application_alive(
        self, db_session, org, application, enrolled, no_k8s_cleanup
    ):
        _soft_delete_app_env(enrolled["test"], org)
        db_session.flush()

        assert application.deleted_at is None

    def test_leaves_other_environments_enrolled(
        self, db_session, org, application, enrolled, no_k8s_cleanup
    ):
        _soft_delete_app_env(enrolled["test"], org)
        db_session.flush()

        remaining = application.active_application_environments
        assert [ae.environment.slug for ae in remaining] == ["production"]

    def test_removes_only_the_named_enrolment(
        self, db_session, org, enrolled, no_k8s_cleanup
    ):
        _soft_delete_app_env(enrolled["test"], org)
        db_session.flush()

        assert enrolled["test"].deleted_at is not None
        assert enrolled["production"].deleted_at is None


class TestDeleteEnvironment:
    def test_application_survives(
        self, db_session, org, application, two_envs, enrolled, no_k8s_cleanup
    ):
        test_env, _ = two_envs

        _soft_delete_environment(test_env, org)
        db_session.flush()

        assert application.deleted_at is None

    def test_other_environment_untouched(
        self, db_session, org, two_envs, enrolled, no_k8s_cleanup
    ):
        test_env, prod_env = two_envs

        _soft_delete_environment(test_env, org)
        db_session.flush()

        assert prod_env.deleted_at is None
        assert enrolled["production"].deleted_at is None

    def test_drops_only_its_own_enrolments(
        self, db_session, org, application, two_envs, enrolled, no_k8s_cleanup
    ):
        test_env, _ = two_envs

        _soft_delete_environment(test_env, org)
        db_session.flush()

        assert enrolled["test"].deleted_at is not None
        assert [
            ae.environment.slug for ae in application.active_application_environments
        ] == ["production"]

    def test_project_and_sibling_env_remain_listed(
        self, db_session, org, project, two_envs, enrolled, no_k8s_cleanup
    ):
        test_env, _ = two_envs

        _soft_delete_environment(test_env, org)
        db_session.flush()

        assert project.deleted_at is None
        assert [e.slug for e in project.active_environments] == ["production"]


class TestDeleteApplication:
    def test_removes_every_enrolment(
        self, db_session, org, application, enrolled, no_k8s_cleanup
    ):
        _soft_delete_application(application, org)
        db_session.flush()

        assert application.deleted_at is not None
        assert application.active_application_environments == []
        assert enrolled["test"].deleted_at is not None
        assert enrolled["production"].deleted_at is not None

    def test_environments_themselves_survive(
        self, db_session, org, application, two_envs, enrolled, no_k8s_cleanup
    ):
        test_env, prod_env = two_envs

        _soft_delete_application(application, org)
        db_session.flush()

        assert test_env.deleted_at is None
        assert prod_env.deleted_at is None

    def test_blast_radius_is_wider_than_unenrolling(
        self, db_session, org, application, enrolled, no_k8s_cleanup
    ):
        before = len(application.active_application_environments)
        assert before == 2

        _soft_delete_app_env(enrolled["test"], org)
        db_session.flush()
        assert len(application.active_application_environments) == 1
        assert application.deleted_at is None

        _soft_delete_application(application, org)
        db_session.flush()
        assert application.active_application_environments == []
        assert application.deleted_at is not None
