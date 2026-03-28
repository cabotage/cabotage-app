"""Tests for the interactive shell exec command construction."""

from cabotage.server.user.views import _shell_exec_command


class TestShellExecCommand:
    def test_wraps_in_posix_shell(self):
        cmd = _shell_exec_command()
        assert cmd[:2] == ["/bin/sh", "-c"]

    def test_prefers_bash_with_sh_fallback(self):
        script = _shell_exec_command()[2]
        assert "SHELL=$(command -v bash || echo /bin/sh)" in script

    def test_envconsul_launches_resolved_shell(self):
        script = _shell_exec_command()[2]
        assert script.endswith(
            "envconsul -config /etc/cabotage/envconsul-shell.hcl $SHELL"
        )

    def test_injects_vault_credentials(self):
        script = _shell_exec_command()[2]
        assert "CONSUL_TOKEN=$(cat /var/run/secrets/vault/consul-token)" in script
        assert "VAULT_TOKEN=$(cat /var/run/secrets/vault/vault-token)" in script
