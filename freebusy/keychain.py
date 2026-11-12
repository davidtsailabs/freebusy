"""App passwords live in the system keychain, never in config, logs or chat.

Service name ``freebusy``, account = the connection id (for example
``caldav:dana@icloud.com``). The plugin reads a password only at the moment it
opens a connection and never returns it to a tool, a hook or the model.

The owner sets a password in a terminal on this machine, where it is typed
without echo:

    hermes freebusy password caldav:dana@icloud.com
    python -m freebusy.keychain set caldav:dana@icloud.com      (same thing, without Hermes)

``keyring`` picks the platform store: the macOS Keychain, the Secret Service on
Linux. Tests swap in a memory backend with ``use_backend``.
"""

from __future__ import annotations

import sys
from typing import Optional

SERVICE = "freebusy"
_BACKEND = None     # tests: an object with get_password / set_password / delete_password


class NoKeychain(RuntimeError):
    pass


def use_backend(backend) -> None:
    global _BACKEND
    _BACKEND = backend


def _kr():
    if _BACKEND is not None:
        return _BACKEND
    try:
        import keyring  # declared in plugin.yaml python_dependencies
    except ImportError as exc:
        raise NoKeychain("the keyring package is not installed; reinstall the plugin so its dependencies install") from exc
    return keyring


def get(account: str) -> Optional[str]:
    return _kr().get_password(SERVICE, account)


def has(account: str) -> bool:
    try:
        return bool(get(account))
    except Exception:
        return False


def store(account: str, password: str) -> None:
    if not password:
        raise ValueError("empty password")
    _kr().set_password(SERVICE, account, password)


def forget(account: str) -> None:
    try:
        _kr().delete_password(SERVICE, account)
    except Exception:
        pass


class MemoryBackend:
    """For tests only."""

    def __init__(self):
        self.store: dict = {}

    def get_password(self, service, account):
        return self.store.get((service, account))

    def set_password(self, service, account, password):
        self.store[(service, account)] = password

    def delete_password(self, service, account):
        self.store.pop((service, account), None)


def prompt_and_store(account: str, getpass=None) -> str:
    """Ask for the password without echo, twice, and store it. Returns a sentence without the password."""
    import getpass as _gp
    ask = getpass or _gp.getpass
    first = ask(f"App password for {account} (not shown): ")
    if not first:
        return "Nothing stored."
    if ask("Again, to confirm: ") != first:
        return "The two entries differ; nothing stored."
    store(account, first)
    return f"Stored in the system keychain (service '{SERVICE}', account '{account}'). Back in chat: /freebusy connect next"


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] in ("set", "password"):
        print(prompt_and_store(argv[1]))
        return 0
    if len(argv) == 2 and argv[0] == "forget":
        forget(argv[1])
        print(f"Removed {argv[1]} from the keychain, if it was there.")
        return 0
    print("usage: python -m freebusy.keychain set <connection id> | forget <connection id>")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
