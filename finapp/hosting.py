"""
Who may open the app. It has no login of its own, so there are two ways to run it:

- **Desktop** (the default): only from this computer, at http://localhost:8501. Anything that looks like
  a network address or a reverse proxy is refused.
- **Self-hosted**: `FINAPP_MODE=server`, on a server behind a reverse proxy that does the login. The proxy
  adds a secret header to every request (`X-Finapp-Proxy-Secret`, the value of `FINAPP_PROXY_SECRET`), and
  the app refuses any request without it, so nobody can skip the login by going to the server's port directly.

`FINAPP_ALLOW_REMOTE=1` still turns every check off, for anyone who already relies on it.
"""
import hmac

LOCAL = {"localhost", "127.0.0.1", "::1"}
PROXY_HEADERS = ("x-forwarded-for", "x-forwarded-host", "forwarded", "x-real-ip")
SECRET_HEADER = "x-finapp-proxy-secret"   # noqa: S105  (a header name, not a password)
MIN_SECRET = 32


def mode(env) -> str:
    return "server" if (env.get("FINAPP_MODE") or "").strip().lower() == "server" else "desktop"


def _host(headers) -> str:
    host = headers.get("host", "")
    return host.rsplit("]", 1)[0].lstrip("[") if host.startswith("[") else host.split(":")[0]


def problem(env, server_address, headers) -> str | None:
    """Why this request must not see the app, or None when it may. `headers` has lower-case names."""
    if env.get("FINAPP_ALLOW_REMOTE") == "1":
        return None
    if mode(env) == "server":
        secret = env.get("FINAPP_PROXY_SECRET") or ""
        if len(secret) < MIN_SECRET:
            return (f"it's in self-hosted mode, but `FINAPP_PROXY_SECRET` is missing or shorter than {MIN_SECRET} "
                    "characters. Set it on the server and in your reverse proxy (see the README).")
        if not hmac.compare_digest(headers.get(SECRET_HEADER, "").encode(), secret.encode()):
            return "this request didn't come through your reverse proxy and its login. Open the app at its proxy address."
        return None
    addr = str(server_address or "").strip().lower()
    if addr not in LOCAL:
        return (f"it is listening on **{addr or 'every network interface'}** instead of localhost. Start it with "
                "`run.bat` / `run.sh`, which keep it on this computer.")
    host = _host(headers)
    if any(h in headers for h in PROXY_HEADERS) or (host and host.lower() not in LOCAL):
        return "it is being reached through a proxy or another hostname, so other people may be able to see it."
    return None


def refusal_message(env, why) -> str:
    if mode(env) == "server":
        return f"**Household Finance won't open here:** {why}"
    return ("**Household Finance won't open here:** " + why + "\n\nThe app has no login, so it only runs for this "
            "computer, at http://localhost:8501. To run it on a server instead, see *Self-hosting* in the README.")
