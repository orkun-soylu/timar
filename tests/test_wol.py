"""Magic packets, and the relay that gets them onto the right segment."""
import pytest

from timar import wol


class TestMagicPacket:
    def test_shape(self):
        packet = wol.magic_packet("aa:bb:cc:dd:ee:ff")
        assert len(packet) == 102          # 6 sync bytes + the MAC sixteen times
        assert packet[:6] == b"\xff" * 6
        assert packet[6:12] == bytes.fromhex("aabbccddeeff")

    @pytest.mark.parametrize("mac", ["AA-BB-CC-DD-EE-FF", "aabbccddeeff", "aa:bb:cc:dd:ee:ff"])
    def test_accepts_every_spelling_people_paste(self, mac):
        assert wol.magic_packet(mac) == wol.magic_packet("aa:bb:cc:dd:ee:ff")

    @pytest.mark.parametrize("mac", ["aa:bb:cc:dd:ee", "", "not-a-mac", "aa:bb:cc:dd:ee:ff:00"])
    def test_rejects_anything_else(self, mac):
        with pytest.raises(wol.WolError):
            wol.magic_packet(mac)


class TestRelayScript:
    """Two nested languages -- Python inside a shell command -- and one escaping tool each."""

    def test_the_script_is_valid_python(self):
        """It was not, once. `shlex.quote` returns a bare word for anything without shell
        metacharacters, so a hex MAC arrived in the Python source as an undefined identifier.
        The remote raised NameError; nothing local could have noticed."""
        compile(wol.relay_script("aa:bb:cc:dd:ee:ff", "10.0.0.255", 9), "<relay>", "exec")

    def test_stays_valid_python_for_awkward_values(self):
        for broadcast in ["10.0.0.255", "it's", '"quoted"', "back\\slash"]:
            compile(wol.relay_script("aa:bb:cc:dd:ee:ff", broadcast, 9), "<relay>", "exec")

    def test_the_mac_survives_as_a_string_literal(self):
        assert "'aabbccddeeff'" in wol.relay_script("aa:bb:cc:dd:ee:ff", "10.0.0.255", 9)

    def test_running_it_produces_a_real_magic_packet(self):
        """Executed locally against a fake socket -- the packet the relay would put on the wire."""
        import socket as socket_module

        captured = {}

        class FakeSocket:
            def __init__(self, *a): pass
            def setsockopt(self, *a): captured["broadcast_flag"] = a
            def sendto(self, payload, target): captured["payload"], captured["target"] = payload, target

        script = wol.relay_script("aa:bb:cc:dd:ee:ff", "10.0.0.255", 9)
        assert script.startswith("import socket;")
        body = script[len("import socket;"):]   # or the real module would replace the fake

        namespace = {"socket": type("m", (), {
            "socket": FakeSocket, "AF_INET": socket_module.AF_INET,
            "SOCK_DGRAM": socket_module.SOCK_DGRAM, "SOL_SOCKET": socket_module.SOL_SOCKET,
            "SO_BROADCAST": socket_module.SO_BROADCAST,
        })}
        exec(compile(body, "<relay>", "exec"), namespace)

        assert captured["payload"] == wol.magic_packet("aa:bb:cc:dd:ee:ff")
        assert captured["target"] == ("10.0.0.255", 9)
        assert captured["broadcast_flag"][2] == 1


class TestRelayCommand:
    def test_sets_SO_BROADCAST(self):
        """Without it the send fails with EACCES -- and only for broadcast addresses, which is
        every real use of a relay."""
        assert "SO_BROADCAST" in wol._relay_command("aa:bb:cc:dd:ee:ff", "10.0.0.255", 9)

    def test_falls_back_to_wakeonlan_then_reports_having_neither(self):
        command = wol._relay_command("aa:bb:cc:dd:ee:ff", "10.0.0.255", 9)
        assert "command -v python3" in command
        assert "command -v wakeonlan" in command
        assert "TIMAR_NO_TOOL" in command

    def test_does_not_reach_for_etherwake(self):
        """It needs an interface name and root; guessing the interface on someone else's machine
        is how you send the packet out of the wrong one and report success."""
        assert "etherwake" not in wol._relay_command("aa:bb:cc:dd:ee:ff", "10.0.0.255", 9)

    def test_arguments_are_quoted(self):
        command = wol._relay_command("aa:bb:cc:dd:ee:ff", "10.0.0.255; rm -rf /", 9)
        assert "; rm -rf /'" in command or "'10.0.0.255; rm -rf /'" in command
        # The dangerous text must survive only inside quotes, never as its own shell statement.
        assert not command.replace("'10.0.0.255; rm -rf /'", "").count("rm -rf /")


class TestWake:
    def test_no_mac_is_an_error_with_the_server_named(self):
        with pytest.raises(wol.WolError, match="gpu-01"):
            wol.wake({"name": "gpu-01"}, {})

    def test_without_a_relay_it_sends_locally(self, monkeypatch):
        sent = {}
        monkeypatch.setattr(wol, "send_magic_packet",
                            lambda mac, broadcast, *a: sent.update(mac=mac, broadcast=broadcast))
        wol.wake({"name": "a", "wol_mac": "aa:bb:cc:dd:ee:ff", "wol_broadcast": "10.0.0.255"}, {})
        assert sent == {"mac": "aa:bb:cc:dd:ee:ff", "broadcast": "10.0.0.255"}

    def test_with_a_relay_it_goes_through_the_relay(self, monkeypatch):
        used = {}
        monkeypatch.setattr(wol, "send_via_relay",
                            lambda relay, mac, broadcast, *a: used.update(relay=relay["name"]))
        monkeypatch.setattr(wol, "send_magic_packet",
                            lambda *a, **kw: pytest.fail("should have used the relay"))
        servers = {"office-nas": {"name": "office-nas", "host": "h", "user": "u"}}
        wol.wake({"name": "office-pc", "wol_mac": "aa:bb:cc:dd:ee:ff",
                  "wol_relay": "office-nas"}, servers)
        assert used["relay"] == "office-nas"

    def test_a_relay_that_is_not_configured_says_so(self):
        """Rather than silently falling back to a local send that cannot reach that subnet."""
        with pytest.raises(wol.WolError, match="not configured"):
            wol.wake({"name": "a", "wol_mac": "aa:bb:cc:dd:ee:ff", "wol_relay": "gone"}, {})
