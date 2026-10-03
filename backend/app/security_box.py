"""Encrypts secrets stored in the database (the Anthropic API key).

CONTRACT:

encrypt(plaintext: str) -> str   # opaque token, safe to store in JSON
decrypt(token: str) -> str       # raises SecretUnreadable if the token can't be decrypted
                                 # (e.g. the installation's secret key changed)
class SecretUnreadable(Exception)

The key is derived from get_settings().secret_key (the per-installation secret), so a copy
of the database alone does not reveal the API key. Uses cryptography's Fernet.
"""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings

# Separates this key from every other use of the installation secret (e.g. session tokens).
_CONTEXT = b"tax-automaton/settings-secrets/v1\x00"


class SecretUnreadable(Exception):
    """A stored secret can't be decrypted with this installation's secret key."""


def _fernet() -> Fernet:
    secret = (get_settings().secret_key or "").encode()
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(_CONTEXT + secret).digest()))


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode("ascii")


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode("ascii")).decode()
    except (InvalidToken, ValueError, TypeError, AttributeError) as exc:
        # UnicodeError is a ValueError; a non-string token raises AttributeError.
        raise SecretUnreadable(
            "The saved secret can't be decrypted with this installation's secret key."
        ) from exc
