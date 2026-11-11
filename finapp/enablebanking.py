"""
A small client for the Enable Banking API (https://enablebanking.com/docs/api/reference/), used to read EU bank
accounts through PSD2 instead of exporting CSV files.

Every request carries a short-lived JWT signed with your application's private key (RS256). The key never leaves
this computer or server and is never stored in the database: the app reads it from a file you put in place
(`FINAPP_EB_KEY_FILE`, by default `~/FinanceApp/enablebanking.pem`, or `/run/secrets/enablebanking.pem` in the
container). Only read access is requested; this client has no payment calls.
"""
import base64
import json
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path

import requests

API = "https://api.enablebanking.com"
TIMEOUT = 30
JWT_TTL = 3600                      # the API accepts up to 24 h; one hour is plenty for a sync
SECRET_PATH = Path("/run/secrets/enablebanking.pem")


class EBError(RuntimeError):
    """An error from the API. `code` is Enable Banking's error name (e.g. EXPIRED_SESSION) when it sent one."""

    def __init__(self, message: str, code: str = "", status: int = 0):
        super().__init__(message)
        self.code = code
        self.status = status


# ---------------------------------------------------------------- configuration

@dataclass
class Config:
    app_id: str
    key_file: Path
    redirect_url: str


def default_key_file(env=os.environ) -> Path:
    if env.get("FINAPP_EB_KEY_FILE"):
        return Path(env["FINAPP_EB_KEY_FILE"]).expanduser()
    if SECRET_PATH.exists():
        return SECRET_PATH
    from . import db
    return db.DEFAULT_DIR / "enablebanking.pem"


def config(env=os.environ, saved: dict | None = None) -> Config:
    """Settings come from the environment first (self-hosted), then from what was saved on the settings page.
    Neither the app ID nor the redirect URL is a secret; the private key is only ever a file path here."""
    saved = saved or {}
    app_id = (env.get("FINAPP_EB_APP_ID") or saved.get("app_id") or "").strip()
    redirect = (env.get("FINAPP_EB_REDIRECT_URL") or saved.get("redirect_url") or "http://localhost:8501/import").strip()
    return Config(app_id=app_id, key_file=default_key_file(env), redirect_url=redirect)


def key_problem(path: Path) -> str | None:
    """Why the key file can't be used, or None when it's fine. Group/other-readable keys are refused on Linux/macOS."""
    if not path.exists():
        return f"no private key at `{path}`"
    if os.name != "nt":
        mode = path.stat().st_mode
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            return f"`{path}` can be read by other users on this machine. Run `chmod 600 {path}` (or 400)."
    return None


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def make_jwt(app_id: str, private_key_pem: bytes, now: int | None = None, ttl: int = JWT_TTL) -> str:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    now = int(time.time()) if now is None else now
    header = {"typ": "JWT", "alg": "RS256", "kid": app_id}
    body = {"iss": "enablebanking.com", "aud": "api.enablebanking.com", "iat": now, "exp": now + ttl}
    signing_input = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(body).encode())}"
    key = serialization.load_pem_private_key(private_key_pem, password=None)
    sig = key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input}.{_b64(sig)}"


# ---------------------------------------------------------------- client

class Client:
    def __init__(self, app_id: str, private_key_pem: bytes, http=None, base: str = API):
        if not app_id:
            raise EBError("No Enable Banking application ID is set.")
        self.app_id = app_id
        self._key = private_key_pem
        self._http = http or requests.Session()
        self._base = base.rstrip("/")
        make_jwt(app_id, private_key_pem)          # fail now, with a clear message, if the key can't be read

    @classmethod
    def from_config(cls, cfg: Config, http=None) -> "Client":
        why = key_problem(cfg.key_file)
        if why:
            raise EBError(f"Can't use the Enable Banking key: {why}.")
        try:
            return cls(cfg.app_id, cfg.key_file.read_bytes(), http=http)
        except EBError:
            raise
        except Exception as e:  # noqa: BLE001  (bad PEM, wrong format, password-protected key)
            raise EBError(f"Couldn't read the private key in `{cfg.key_file}`: {e}") from e

    def _call(self, method: str, path: str, params=None, body=None) -> dict:
        headers = {"Authorization": f"Bearer {make_jwt(self.app_id, self._key)}", "Accept": "application/json"}
        try:
            r = self._http.request(method, self._base + path, params=params, json=body, headers=headers, timeout=TIMEOUT)
        except requests.RequestException as e:
            raise EBError(f"Couldn't reach Enable Banking: {e}") from e
        try:
            data = r.json() if r.content else {}
        except ValueError:
            data = {}
        if r.status_code >= 400:
            code = str(data.get("error") or "") if isinstance(data, dict) else ""
            msg = (data.get("message") or data.get("detail") or r.reason) if isinstance(data, dict) else r.reason
            raise EBError(f"Enable Banking said: {msg} ({code or r.status_code})", code=code, status=r.status_code)
        return data

    # -- linking
    def aspsps(self, country: str, psu_type: str = "personal") -> list[dict]:
        return self._call("GET", "/aspsps", params={"country": country, "psu_type": psu_type}).get("aspsps", [])

    def start_auth(self, aspsp_name: str, country: str, redirect_url: str, state: str, valid_until: str,
                   psu_type: str = "personal", language: str = "en") -> dict:
        body = {"access": {"valid_until": valid_until, "balances": True, "transactions": True},
                "aspsp": {"name": aspsp_name, "country": country}, "state": state, "redirect_url": redirect_url,
                "psu_type": psu_type, "language": language}
        return self._call("POST", "/auth", body=body)

    def create_session(self, code: str) -> dict:
        return self._call("POST", "/sessions", body={"code": code})

    def get_session(self, session_id: str) -> dict:
        return self._call("GET", f"/sessions/{session_id}")

    def delete_session(self, session_id: str) -> None:
        self._call("DELETE", f"/sessions/{session_id}")

    # -- data
    def balances(self, uid: str) -> list[dict]:
        return self._call("GET", f"/accounts/{uid}/balances").get("balances", [])

    def transactions(self, uid: str, date_from: str, date_to: str | None = None, max_pages: int = 200) -> list[dict]:
        """Booked transactions from date_from (inclusive), following continuation keys to the end. The API can return
        empty pages with a key, so keep going until the key is gone."""
        params = {"date_from": date_from, "transaction_status": "BOOK"}
        if date_to:
            params["date_to"] = date_to
        out = []
        for _ in range(max_pages):
            data = self._call("GET", f"/accounts/{uid}/transactions", params=params)
            out.extend(data.get("transactions") or [])
            key = data.get("continuation_key")
            if not key:
                return out
            params = {**params, "continuation_key": key}
        raise EBError("The bank kept sending more pages of transactions; stopped to be safe. Try a later start date.")
