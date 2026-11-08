"""Who may open the app: desktop (localhost only) and self-hosted (only through the proxy that does the login)."""
import pytest

from finapp import hosting

SECRET = "s" * 40
SERVER = {"FINAPP_MODE": "server", "FINAPP_PROXY_SECRET": SECRET}


def test_desktop_is_the_default():
    assert hosting.mode({}) == "desktop"
    assert hosting.mode({"FINAPP_MODE": "Server "}) == "server"


@pytest.mark.parametrize("addr, headers", [
    ("localhost", {"host": "localhost:8501"}),
    ("127.0.0.1", {"host": "127.0.0.1:8501"}),
    ("::1", {"host": "[::1]:8501"}),
    ("localhost", {}),
])
def test_desktop_opens_locally(addr, headers):
    assert hosting.problem({}, addr, headers) is None


@pytest.mark.parametrize("addr, headers, words", [
    ("0.0.0.0", {"host": "localhost"}, "listening on"),
    ("", {}, "every network interface"),
    ("localhost", {"host": "finance.example.org"}, "another hostname"),
    ("localhost", {"host": "localhost", "x-forwarded-for": "10.0.0.5"}, "proxy"),
])
def test_desktop_refuses_network_and_proxies(addr, headers, words):
    assert words in hosting.problem({}, addr, headers)


def test_desktop_ignores_a_secret_header():
    """The secret only matters in server mode; on a desktop a proxy is still refused."""
    env = {"FINAPP_PROXY_SECRET": SECRET}
    headers = {"host": "finance.example.org", hosting.SECRET_HEADER: SECRET}
    assert hosting.problem(env, "localhost", headers)


def test_server_opens_through_the_proxy():
    headers = {"host": "finance.example.org", "x-forwarded-for": "10.0.0.5", hosting.SECRET_HEADER: SECRET}
    assert hosting.problem(SERVER, "0.0.0.0", headers) is None


@pytest.mark.parametrize("headers", [
    {"host": "192.168.1.28:8104"},                                  # straight to the port, skipping the login
    {"host": "finance.example.org", hosting.SECRET_HEADER: "wrong"},
    {"host": "finance.example.org", hosting.SECRET_HEADER: ""},
])
def test_server_refuses_requests_that_skip_the_proxy(headers):
    assert "reverse proxy" in hosting.problem(SERVER, "0.0.0.0", headers)


@pytest.mark.parametrize("secret", [None, "", "short"])
def test_server_needs_a_long_secret(secret):
    env = {"FINAPP_MODE": "server"}
    if secret is not None:
        env["FINAPP_PROXY_SECRET"] = secret
    assert "FINAPP_PROXY_SECRET" in hosting.problem(env, "0.0.0.0", {hosting.SECRET_HEADER: secret or ""})


def test_allow_remote_still_overrides():
    assert hosting.problem({"FINAPP_ALLOW_REMOTE": "1"}, "0.0.0.0", {}) is None
