"""Key generation, and the command shapes that make enrolment safe.

The commands are asserted as strings rather than executed: the properties that matter are that
the sudoers file is validated before it is installed, that the key append is idempotent, and
that operator input is quoted. All three are visible in the command, and all three are the kind
of thing a refactor quietly loses.
"""
import importlib

import pytest

from timar import enroll
from timar.platforms import OpenWrt, Platform


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("TIMAR_DATA", str(tmp_path))
    from timar import config, keys
    importlib.reload(config)
    importlib.reload(keys)
    return tmp_path


class TestKeys:
    def test_generated_on_first_use_and_reused_after(self, data_dir):
        """Regenerating on every start would mean re-enrolling every host after each restart —
        which is the kind of friction that gets solved by turning key checking off."""
        from timar import keys
        first = keys.public_key()
        importlib.reload(keys)
        assert keys.public_key() == first

    def test_is_an_openssh_ed25519_line(self, data_dir):
        from timar import keys
        assert keys.public_key().startswith("ssh-ed25519 ")
        assert keys.public_key().endswith(" timar")

    def test_private_key_is_not_world_readable(self, data_dir):
        from timar import config, keys
        keys.ensure()
        assert oct(config.path(config.SSH_KEY).stat().st_mode)[-3:] == "600"

    def test_fingerprint_matches_the_ssh_keygen_format(self, data_dir):
        from timar import keys
        assert keys.fingerprint().startswith("SHA256:")


class TestAuthorizedKeysPath:
    def test_openwrt_uses_dropbear_not_dot_ssh(self):
        """dropbear ignores ~/.ssh/authorized_keys. A key written there on a router succeeds
        and never works, which looks identical to a wrong password until you go and look."""
        assert OpenWrt().authorized_keys == "/etc/dropbear/authorized_keys"
        assert Platform().authorized_keys == ".ssh/authorized_keys"

    def test_no_platform_uses_a_tilde(self):
        """A tilde cannot survive shell quoting, and the failure is silent.

        The first version wrote `~/.ssh/authorized_keys`; `shlex.quote` wrapped it in single
        quotes, the shell did not expand it, and the command created a literal directory named
        `~`, wrote the key into it, and reported success. Caught only by connecting with the
        key afterwards.
        """
        for platform in (Platform(), OpenWrt()):
            assert "~" not in platform.authorized_keys

    def test_home_relative_paths_expand_via_HOME(self):
        assert enroll._remote_path(".ssh/authorized_keys") == '"$HOME"/.ssh/authorized_keys'

    def test_absolute_paths_are_left_alone(self):
        assert enroll._remote_path("/etc/dropbear/authorized_keys") == "/etc/dropbear/authorized_keys"


class FakeChannel:
    def __init__(self, code=0):
        self._code = code

    def recv_exit_status(self):
        return self._code

    def shutdown_write(self):
        pass


class FakeStream:
    def __init__(self, text="", code=0):
        self._text = text.encode()
        self.channel = FakeChannel(code)
        self.written = ""

    def read(self):
        return self._text

    def write(self, text):
        self.written += text

    def flush(self):
        pass


class FakeClient:
    """Captures the command instead of running it."""

    def __init__(self, stdout="", code=0):
        self.commands = []
        self.stdin = FakeStream()
        self._stdout, self._code = stdout, code

    def exec_command(self, command, timeout=None):
        self.commands.append(command)
        return self.stdin, FakeStream(self._stdout, self._code), FakeStream()

    def close(self):
        pass


class TestInstallKey:
    def test_is_idempotent(self, data_dir):
        """Re-enrolling a host must be safe; a second run must not append the key twice."""
        client = FakeClient(stdout=enroll.ALREADY)
        installed, already = enroll._install_key(client, Platform(), "ssh-ed25519 AAAA timar")
        assert (installed, already) == (False, True)
        assert "grep -qxF" in client.commands[0]

    def test_writes_to_the_platform_path(self, data_dir):
        client = FakeClient(stdout=enroll.OK)
        enroll._install_key(client, OpenWrt(), "ssh-ed25519 AAAA timar")
        assert "/etc/dropbear/authorized_keys" in client.commands[0]

    def test_key_material_is_quoted(self, data_dir):
        # The key is not operator input, but the same path carries a comment field, and an
        # unquoted value here would be a shell injection into a root-adjacent file.
        client = FakeClient(stdout=enroll.OK)
        enroll._install_key(client, Platform(), "ssh-ed25519 AAAA a; rm -rf /")
        assert "; rm -rf /" not in client.commands[0].replace("'ssh-ed25519 AAAA a; rm -rf /'", "")

    def test_failure_to_write_is_an_error(self, data_dir):
        client = FakeClient(stdout="", code=1)
        with pytest.raises(enroll.EnrollError):
            enroll._install_key(client, Platform(), "ssh-ed25519 AAAA timar")


class TestGrantSudo:
    def test_validates_with_visudo_before_installing(self, data_dir):
        """The load-bearing property. A malformed file in /etc/sudoers.d breaks sudo for
        everyone on the machine, including the session you would repair it from."""
        client = FakeClient(stdout=enroll.OK)
        enroll._grant_sudo(client, "deploy", "pw")
        command = client.commands[0]
        assert "visudo -cqf" in command
        # The install must be conditional on that check, not merely nearby.
        assert command.index("visudo -cqf") < command.index("install -m 0440")

    def test_a_rejected_file_changes_nothing_and_says_so(self, data_dir):
        client = FakeClient(stdout=enroll.INVALID)
        with pytest.raises(enroll.EnrollError, match="nothing was changed"):
            enroll._grant_sudo(client, "deploy", "pw")

    def test_password_goes_to_stdin_never_the_command_line(self, data_dir):
        """On the command line it would be visible in `ps` to every other user on the host."""
        client = FakeClient(stdout=enroll.OK)
        enroll._grant_sudo(client, "deploy", "hunter2-hunter2")
        assert "hunter2-hunter2" not in client.commands[0]
        assert client.stdin.written == "hunter2-hunter2\n"
        assert "sudo -S" in client.commands[0]

    def test_username_is_quoted_into_the_rule(self, data_dir):
        client = FakeClient(stdout=enroll.OK)
        enroll._grant_sudo(client, "de ploy", "pw")
        assert "de ploy ALL=(ALL) NOPASSWD:ALL" in client.commands[0]

    def test_a_wrong_sudo_password_is_reported_as_such(self, data_dir):
        client = FakeClient(stdout="", code=1)
        client.exec_command = lambda command, timeout=None: (
            client.stdin, FakeStream("", 1), FakeStream("Sorry, try again.", 1))
        with pytest.raises(enroll.EnrollError, match="not accepted by sudo"):
            enroll._grant_sudo(client, "deploy", "wrong")


class TestSudoSkipping:
    def test_openwrt_is_skipped_with_a_reason(self, data_dir, monkeypatch):
        """Offering a button that cannot work is worse than not offering it."""
        monkeypatch.setattr(enroll, "_connect", lambda *a, **kw: FakeClient(stdout=enroll.OK))
        result = enroll.enroll(
            {"name": "r", "host": "h", "user": "root", "platform": "openwrt"},
            "pw", grant_sudo=True)
        assert not result.sudo_granted
        assert "no sudo" in result.sudo_skipped_reason

    def test_root_is_skipped_with_a_reason(self, data_dir, monkeypatch):
        monkeypatch.setattr(enroll, "_connect", lambda *a, **kw: FakeClient(stdout=enroll.OK))
        result = enroll.enroll(
            {"name": "s", "host": "h", "user": "root", "platform": "linux"},
            "pw", grant_sudo=True)
        assert not result.sudo_granted
        assert "already root" in result.sudo_skipped_reason
