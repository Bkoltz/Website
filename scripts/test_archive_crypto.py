#!/usr/bin/env python3
"""
Tests for the encrypted archive.

The committed archive is the only copy of removed content that a key holder can
still read, so these lean on the failure paths: a wrong key, a tampered blob
and a truncated file all have to be refused loudly rather than half-decoded.

Run: python -m unittest discover -s scripts -p 'test_*.py'
"""

import base64
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import archive_crypto as ac

KEY_A = base64.b64encode(b"A" * 32).decode()
KEY_B = base64.b64encode(b"B" * 32).decode()

SAMPLE = (
    b"-- WebODM Discord #help archive.\n"
    b"INSERT INTO users VALUES ('1','someone','Someone',NULL,'Someone',0);\n"
    * 200
)


class TestKeyLoading(unittest.TestCase):
    def test_missing_key_is_optional_when_not_required(self):
        with mock.patch.dict(os.environ, {ac.KEY_ENV: ""}, clear=False):
            self.assertIsNone(ac.load_key(required=False))

    def test_missing_key_raises_when_required(self):
        with mock.patch.dict(os.environ, {ac.KEY_ENV: ""}, clear=False):
            with self.assertRaises(ac.ArchiveCryptoError):
                ac.load_key()

    def test_non_base64_key_is_rejected(self):
        with mock.patch.dict(os.environ, {ac.KEY_ENV: "not base64!!"}, clear=False):
            with self.assertRaises(ac.ArchiveCryptoError):
                ac.load_key()

    def test_wrong_length_key_is_rejected(self):
        short = base64.b64encode(b"tooshort").decode()
        with mock.patch.dict(os.environ, {ac.KEY_ENV: short}, clear=False):
            with self.assertRaisesRegex(ac.ArchiveCryptoError, "8 bytes"):
                ac.load_key()

    def test_generated_key_round_trips(self):
        with mock.patch.dict(os.environ, {ac.KEY_ENV: ac.generate_key()}, clear=False):
            self.assertEqual(len(ac.load_key()), ac.KEY_BYTES)

    def test_generated_keys_differ(self):
        self.assertNotEqual(ac.generate_key(), ac.generate_key())


class TestRoundTrip(unittest.TestCase):
    def setUp(self):
        self.key = base64.b64decode(KEY_A)

    def test_round_trip_preserves_bytes(self):
        blob = ac.encrypt_bytes(SAMPLE, self.key)
        self.assertEqual(ac.decrypt_bytes(blob, self.key), SAMPLE)

    def test_ciphertext_does_not_contain_plaintext(self):
        blob = ac.encrypt_bytes(SAMPLE, self.key)
        self.assertNotIn(b"someone", blob)
        self.assertNotIn(b"INSERT INTO", blob)

    def test_compression_shrinks_repetitive_dump(self):
        # Without the gzip step git would store a full-size blob every sync.
        blob = ac.encrypt_bytes(SAMPLE, self.key)
        self.assertLess(len(blob), len(SAMPLE) // 2)

    def test_same_plaintext_encrypts_differently_each_time(self):
        # A fresh nonce per run; equal files must not produce equal ciphertext.
        a = ac.encrypt_bytes(SAMPLE, self.key)
        b = ac.encrypt_bytes(SAMPLE, self.key)
        self.assertNotEqual(a, b)

    def test_empty_input_round_trips(self):
        blob = ac.encrypt_bytes(b"", self.key)
        self.assertEqual(ac.decrypt_bytes(blob, self.key), b"")


class TestRejection(unittest.TestCase):
    def setUp(self):
        self.key = base64.b64decode(KEY_A)
        self.blob = ac.encrypt_bytes(SAMPLE, self.key)

    def test_wrong_key_is_refused(self):
        with self.assertRaisesRegex(ac.ArchiveCryptoError, "wrong key"):
            ac.decrypt_bytes(self.blob, base64.b64decode(KEY_B))

    def test_flipped_ciphertext_bit_is_refused(self):
        tampered = bytearray(self.blob)
        tampered[-20] ^= 0x01
        with self.assertRaises(ac.ArchiveCryptoError):
            ac.decrypt_bytes(bytes(tampered), self.key)

    def test_flipped_nonce_bit_is_refused(self):
        tampered = bytearray(self.blob)
        tampered[len(ac.MAGIC)] ^= 0x01
        with self.assertRaises(ac.ArchiveCryptoError):
            ac.decrypt_bytes(bytes(tampered), self.key)

    def test_altered_header_is_refused(self):
        # The header is the AEAD's associated data, so it cannot be rewritten.
        tampered = b"WEBODM-ARCHIVE-v2\n" + self.blob[len(ac.MAGIC):]
        with self.assertRaises(ac.ArchiveCryptoError):
            ac.decrypt_bytes(tampered, self.key)

    def test_plaintext_file_is_refused_with_a_clear_message(self):
        with self.assertRaisesRegex(ac.ArchiveCryptoError, "bad header"):
            ac.decrypt_bytes(SAMPLE, self.key)

    def test_truncated_file_is_refused(self):
        with self.assertRaisesRegex(ac.ArchiveCryptoError, "truncated"):
            ac.decrypt_bytes(ac.MAGIC + b"\x00" * 4, self.key)


class TestFileHelpers(unittest.TestCase):
    def test_encrypt_then_decrypt_file(self):
        key = base64.b64decode(KEY_A)
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "discord.sql"
            enc = Path(tmp) / "discord.sql.enc"
            out = Path(tmp) / "roundtrip.sql"
            src.write_bytes(SAMPLE)
            ac.encrypt_file(src, enc, key)
            self.assertTrue(enc.exists())
            ac.decrypt_file(enc, out, key)
            self.assertEqual(out.read_bytes(), SAMPLE)


if __name__ == "__main__":
    unittest.main()
