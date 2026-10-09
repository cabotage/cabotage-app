"""Tests for Procfile parsing, including quoted env values."""

import pytest

from cabotage.utils.procfile import loads


class TestBasicParsing:
    def test_simple_process(self):
        result = loads("web: python app.py")
        assert result == {"web": {"cmd": "python app.py", "env": []}}

    def test_ignores_blank_lines(self):
        result = loads("\nweb: python app.py\n\nworker: celery -A app worker\n")
        assert result == {
            "web": {"cmd": "python app.py", "env": []},
            "worker": {"cmd": "celery -A app worker", "env": []},
        }

    def test_ignores_comment_lines(self):
        result = loads(
            "# top-level comment\n  # indented comment\nweb: python app.py\n# tail comment"
        )
        assert result == {"web": {"cmd": "python app.py", "env": []}}

    def test_multiple_processes(self):
        result = loads("web: gunicorn app:app\nworker: celery -A app worker")
        assert "web" in result
        assert "worker" in result

    def test_env_vars(self):
        result = loads("worker: env FOO=bar python worker.py")
        assert result["worker"]["env"] == [("FOO", "bar")]
        assert result["worker"]["cmd"] == "python worker.py"

    def test_multiple_env_vars(self):
        result = loads("worker: env FOO=bar BAZ=qux python worker.py")
        assert result["worker"]["env"] == [("FOO", "bar"), ("BAZ", "qux")]

    def test_duplicate_process_type_raises(self):
        with pytest.raises(ValueError):
            loads("web: python app.py\nweb: gunicorn app:app")

    def test_duplicate_env_var_raises(self):
        with pytest.raises(ValueError):
            loads("worker: env FOO=bar FOO=baz python worker.py")


class TestQuotedEnvValues:
    def test_quoted_value_with_spaces(self):
        result = loads('job-cleanup: env SCHEDULE="0 */6 * * *" python cleanup.py')
        assert result["job-cleanup"]["env"] == [("SCHEDULE", "0 */6 * * *")]
        assert result["job-cleanup"]["cmd"] == "python cleanup.py"

    def test_quoted_and_unquoted_mixed(self):
        result = loads(
            'job-reports: env SCHEDULE="30 2 * * 1" RETRIES=3 python reports.py'
        )
        assert result["job-reports"]["env"] == [
            ("SCHEDULE", "30 2 * * 1"),
            ("RETRIES", "3"),
        ]
        assert result["job-reports"]["cmd"] == "python reports.py"

    def test_quoted_empty_value(self):
        result = loads('worker: env EMPTY="" python worker.py')
        assert result["worker"]["env"] == [("EMPTY", "")]

    def test_every_minute_schedule(self):
        result = loads('job-ping: env SCHEDULE="* * * * *" ping.sh')
        assert result["job-ping"]["env"] == [("SCHEDULE", "* * * * *")]

    def test_complex_cron_expression(self):
        result = loads(
            'job-quarterly: env SCHEDULE="0 0 1 1,4,7,10 *" python quarterly.py'
        )
        assert result["job-quarterly"]["env"] == [("SCHEDULE", "0 0 1 1,4,7,10 *")]

    def test_unquoted_env_still_works(self):
        """Ensure backward compatibility with unquoted values."""
        result = loads("worker: env PORT=8000 python worker.py")
        assert result["worker"]["env"] == [("PORT", "8000")]
