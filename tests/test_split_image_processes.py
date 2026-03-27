"""Tests for split_image_processes including job process extraction."""

from unittest.mock import MagicMock

from cabotage.server.query_helpers import split_image_processes


def _make_image(processes):
    image = MagicMock()
    image.processes = processes
    return image


class TestSplitImageProcesses:
    def test_empty_image(self):
        assert split_image_processes(None) == ({}, {}, {}, {})

    def test_no_processes(self):
        image = _make_image(None)
        assert split_image_processes(image) == ({}, {}, {}, {})

    def test_basic_split(self):
        image = _make_image(
            {
                "web": {"cmd": "gunicorn", "env": []},
                "worker": {"cmd": "celery", "env": []},
                "release": {"cmd": "migrate", "env": []},
                "postdeploy": {"cmd": "seed", "env": []},
            }
        )
        service, release, postdeploy, jobs = split_image_processes(image)
        assert set(service.keys()) == {"web", "worker"}
        assert set(release.keys()) == {"release"}
        assert set(postdeploy.keys()) == {"postdeploy"}
        assert jobs == {}

    def test_job_processes_extracted(self):
        image = _make_image(
            {
                "web": {"cmd": "gunicorn", "env": []},
                "job-cleanup": {
                    "cmd": "python cleanup.py",
                    "env": [("SCHEDULE", "0 */6 * * *")],
                },
                "job-reports": {
                    "cmd": "python reports.py",
                    "env": [("SCHEDULE", "0 0 * * 1")],
                },
            }
        )
        service, release, postdeploy, jobs = split_image_processes(image)
        assert set(service.keys()) == {"web"}
        assert release == {}
        assert postdeploy == {}
        assert set(jobs.keys()) == {"job-cleanup", "job-reports"}

    def test_jobs_excluded_from_service_procs(self):
        image = _make_image(
            {
                "web": {"cmd": "gunicorn", "env": []},
                "job-cleanup": {
                    "cmd": "python cleanup.py",
                    "env": [("SCHEDULE", "0 * * * *")],
                },
            }
        )
        service, _, _, _ = split_image_processes(image)
        assert "job-cleanup" not in service

    def test_all_categories_together(self):
        image = _make_image(
            {
                "web": {"cmd": "gunicorn", "env": []},
                "tcp-cache": {"cmd": "redis", "env": []},
                "worker": {"cmd": "celery", "env": []},
                "release": {"cmd": "migrate", "env": []},
                "postdeploy": {"cmd": "seed", "env": []},
                "job-cleanup": {
                    "cmd": "python cleanup.py",
                    "env": [("SCHEDULE", "0 */6 * * *")],
                },
            }
        )
        service, release, postdeploy, jobs = split_image_processes(image)
        assert set(service.keys()) == {"web", "tcp-cache", "worker"}
        assert set(release.keys()) == {"release"}
        assert set(postdeploy.keys()) == {"postdeploy"}
        assert set(jobs.keys()) == {"job-cleanup"}
