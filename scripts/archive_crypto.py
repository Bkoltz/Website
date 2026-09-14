#!/usr/bin/env python3
"""
Symmetric encryption for the committed Discord archive.

The archive mirrors real people's messages. Publishing the plaintext dump to a
public repository would keep every message ever synced readable in git history
forever -- including messages later removed on request, which the live site no
longer shows. Encrypting the committed file narrows that from "public forever"
to "readable by key holders", so a removal request is no longer undone by
`git log -p`.

Design notes
------------
The whole file is encrypted as one unit rather than line by line. Line-by-line
encryption would preserve git's line diffs, but only by being deterministic:
identical plaintext lines produce identical ciphertext, so each commit would
advertise exactly which rows changed. Correlated against the live site that
reveals what was removed and when -- precisely what this is meant to protect.

The file is gzipped first, which costs nothing and cuts the per-commit blob
roughly 5x (1.1 MB -> 200 KB). Encrypted output is incompressible, so without
this step git would store the full ciphertext every week.

Cipher: AES-256-GCM (AEAD).
  - Authenticated: this file drives a public site, so silent tampering with the
    committed blob has to be detectable, not merely unlikely.
  - AES-NI hardware acceleration on CI runners and modern laptops.
  - 96-bit random nonce. At roughly one sync per week the birthday bound for
    nonce collision (2^32 messages) is not a consideration.
  - The header line is passed as associated data, so a v1 blob cannot be
    replayed as a future format version.

The key is 32 random bytes, base64-encoded, held in the DISCORD_ARCHIVE_KEY
environment variable (a repository secret in CI). It is a raw key rather than a
passphrase run through a KDF: nobody types it, so there is no reason to accept
the weakness of something memorable.

Losing the key is recoverable, not fatal: the archive is a mirror, so a full
re-sync from Discord rebuilds it.

Requirements: pip install cryptography
"""

import base64
import gzip
import os
import secrets
import sys
from pathlib import Path

try:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError:  # pragma: no cover - exercised only on a broken install
    AESGCM = None
    InvalidTag = Exception

KEY_ENV = "DISCORD_ARCHIVE_KEY"
MAGIC = b"WEBODM-ARCHIVE-v1\n"
NONCE_BYTES = 12
KEY_BYTES = 32


class ArchiveCryptoError(Exception):
    """A key is missing, malformed, or does not match the file."""


def generate_key() -> str:
    """A fresh base64 key, for `python scripts/archive_crypto.py keygen`."""
    return base64.b64encode(secrets.token_bytes(KEY_BYTES)).decode()


def load_key(required: bool = True) -> bytes | None:
    """
    Read and validate the key from the environment.

    Returns None when unset and not required, so callers can degrade to the
    plaintext path instead of failing a build that never needed the archive.
    """
    raw = os.environ.get(KEY_ENV, "").strip()
    if not raw:
        if required:
            raise ArchiveCryptoError(
                f"{KEY_ENV} is not set. Generate one with "
                "`python scripts/archive_crypto.py keygen` and store it as a "
                "repository secret."
            )
        return None
    try:
        key = base64.b64decode(raw, validate=True)
    except Exception as exc:
        raise ArchiveCryptoError(f"{KEY_ENV} is not valid base64: {exc}") from exc
    if len(key) != KEY_BYTES:
        raise ArchiveCryptoError(
            f"{KEY_ENV} decodes to {len(key)} bytes, expected {KEY_BYTES}."
        )
    return key


def _cipher(key: bytes) -> "AESGCM":
    if AESGCM is None:
        raise ArchiveCryptoError(
            "The 'cryptography' package is required: pip install cryptography"
        )
    return AESGCM(key)


def encrypt_bytes(plaintext: bytes, key: bytes) -> bytes:
    """gzip, then seal. Layout: MAGIC || nonce || ciphertext+tag."""
    nonce = secrets.token_bytes(NONCE_BYTES)
    packed = gzip.compress(plaintext, 9)
    sealed = _cipher(key).encrypt(nonce, packed, MAGIC)
    return MAGIC + nonce + sealed


def decrypt_bytes(blob: bytes, key: bytes) -> bytes:
    """Reverse encrypt_bytes, raising ArchiveCryptoError on any mismatch."""
    if not blob.startswith(MAGIC):
        raise ArchiveCryptoError(
            "Not a WebODM encrypted archive (bad header). Was the file "
            "committed in plaintext, or truncated?"
        )
    body = blob[len(MAGIC):]
    if len(body) <= NONCE_BYTES:
        raise ArchiveCryptoError("Encrypted archive is truncated.")
    nonce, sealed = body[:NONCE_BYTES], body[NONCE_BYTES:]
    try:
        packed = _cipher(key).decrypt(nonce, sealed, MAGIC)
    except InvalidTag as exc:
        raise ArchiveCryptoError(
            "Could not decrypt the archive: wrong key, or the file was "
            "modified since it was written."
        ) from exc
    return gzip.decompress(packed)


def encrypt_file(src: Path, dest: Path, key: bytes) -> int:
    """Encrypt src to dest, returning the bytes written."""
    blob = encrypt_bytes(Path(src).read_bytes(), key)
    Path(dest).write_bytes(blob)
    return len(blob)


def decrypt_file(src: Path, dest: Path, key: bytes) -> int:
    """Decrypt src to dest, returning the bytes written."""
    data = decrypt_bytes(Path(src).read_bytes(), key)
    Path(dest).write_bytes(data)
    return len(data)


def main() -> None:
    """CLI: keygen | encrypt <src> <dest> | decrypt <src> <dest>."""
    action = sys.argv[1] if len(sys.argv) > 1 else "help"
    if action == "keygen":
        print(generate_key())
        print(
            f"\nStore this as the {KEY_ENV} repository secret and keep a copy "
            "somewhere safe.\nWithout it the committed archive cannot be read, "
            "though it can be rebuilt\nby re-syncing from Discord.",
            file=sys.stderr,
        )
        return
    if action in ("encrypt", "decrypt") and len(sys.argv) == 4:
        key = load_key()
        src, dest = Path(sys.argv[2]), Path(sys.argv[3])
        fn = encrypt_file if action == "encrypt" else decrypt_file
        written = fn(src, dest, key)
        print(f"Wrote {dest} ({written} bytes)")
        return
    print(__doc__)
    print("Usage: python scripts/archive_crypto.py [keygen|encrypt SRC DEST|"
          "decrypt SRC DEST]")


if __name__ == "__main__":
    main()
