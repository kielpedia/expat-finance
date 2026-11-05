"""
Where the Anthropic API key lives: the ANTHROPIC_API_KEY environment variable, or your operating system's
credential store (Windows Credential Manager, macOS Keychain, Secret Service on Linux) via `keyring`.
Never in the database, so a backup or a copied database file doesn't carry it.
"""
import os

SERVICE = "expat-finance"
USER = "anthropic-api-key"


def _keyring():
    try:
        import keyring
        from keyring.backends import fail
        kr = keyring.get_keyring()
        if isinstance(kr, fail.Keyring):
            return None
        return keyring
    except Exception:  # noqa: BLE001  (missing package or broken backend)
        return None


def store_available() -> bool:
    return _keyring() is not None


def from_env() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def get_key() -> str | None:
    if from_env():
        return os.environ["ANTHROPIC_API_KEY"]
    kr = _keyring()
    if kr is None:
        return None
    try:
        return kr.get_password(SERVICE, USER)
    except Exception:  # noqa: BLE001
        return None


def has_saved_key() -> bool:
    kr = _keyring()
    if kr is None:
        return False
    try:
        return bool(kr.get_password(SERVICE, USER))
    except Exception:  # noqa: BLE001
        return False


def set_key(key: str) -> None:
    kr = _keyring()
    if kr is None:
        raise RuntimeError("No credential store is available on this computer. Set the ANTHROPIC_API_KEY "
                           "environment variable instead.")
    kr.set_password(SERVICE, USER, key.strip())


def delete_key() -> None:
    kr = _keyring()
    if kr is None:
        return
    try:
        kr.delete_password(SERVICE, USER)
    except Exception:  # noqa: BLE001  (nothing saved)
        pass


def migrate_from_db(con) -> None:
    """Older versions kept the key in the settings table. Move it to the credential store if there is one,
    and remove it from the database either way (it can be re-entered; leaving it in plain text is worse)."""
    from . import db
    cfg = db.get_setting(con, "ai", {}) or {}
    old = cfg.pop("api_key", None)
    if not old:
        return
    if store_available() and not has_saved_key():
        try:
            set_key(old)
        except Exception:  # noqa: BLE001
            pass
    db.set_setting(con, "ai", cfg)
