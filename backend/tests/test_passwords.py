from __future__ import annotations

import unittest

from app.auth.passwords import PasswordHasher, PasswordPolicyError, validate_password
from tests.helpers import fast_hasher


class PolicyTests(unittest.TestCase):
    def test_accepts_long_passphrase(self) -> None:
        validate_password("correct horse battery", "bob")

    def test_rejections(self) -> None:
        cases = {
            "short": "too short",
            "a" * 300: "too long",
            "aaaaaaaaaaaaaaaa": "low variety",
            "passwordpassword": "common",
            "Bobby-Tables-1": "same as username (case-insens.)",
        }
        for pw, why in cases.items():
            with self.subTest(why=why), self.assertRaises(PasswordPolicyError):
                validate_password(pw, "bobby-tables-1")

    def test_length_counts_normalised_characters(self) -> None:
        with self.assertRaises(PasswordPolicyError):
            validate_password("é" * 11)


class HasherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = fast_hasher()

    def test_roundtrip_and_wrong_password(self) -> None:
        stored = self.h.hash("correct horse battery")
        self.assertTrue(self.h.verify("correct horse battery", stored))
        self.assertFalse(self.h.verify("correct horse batterz", stored))

    def test_salted(self) -> None:
        self.assertNotEqual(self.h.hash("same password here"), self.h.hash("same password here"))

    def test_no_plaintext_in_hash(self) -> None:
        self.assertNotIn("password", self.h.hash("password-in-hash-test"))

    def test_unicode_normalisation(self) -> None:
        stored = self.h.hash("café au lait 123")  # precomposed
        self.assertTrue(self.h.verify("café au lait 123", stored))  # decomposed

    def test_verify_never_raises_on_garbage(self) -> None:
        for bad in (
            "",
            "x",
            "$scrypt$",
            "$scrypt$ln=99,r=8,p=1$AAAA$AAAA",
            "$argon2id$junk",
            "$scrypt$ln=a,r=1,p=1$$",
        ):
            with self.subTest(bad=bad):
                self.assertFalse(self.h.verify("whatever password", bad))

    def test_tampered_parameters_cannot_force_huge_work(self) -> None:
        # log_n 30 would need terabytes; parameter bounds must reject it instantly.
        self.assertFalse(self.h.verify("pw", "$scrypt$ln=30,r=8,p=1$AAAAAAAAAAAAAAAAAAAAAA$AAAA"))

    def test_needs_rehash_when_parameters_change(self) -> None:
        stored = self.h.hash("rehash me please now")
        self.assertFalse(self.h.needs_rehash(stored))
        stronger = PasswordHasher(scrypt_log_n=11, scrypt_r=1, scrypt_p=1, prefer_argon2=False)
        self.assertTrue(stronger.needs_rehash(stored))
        self.assertTrue(stronger.verify("rehash me please now", stored))

    def test_production_default_parameters_work(self) -> None:
        prod = PasswordHasher(prefer_argon2=False)
        self.assertEqual(prod.scheme, "scrypt")
        stored = prod.hash("production params ok")
        self.assertIn("ln=15,r=8,p=3", stored)
        self.assertTrue(prod.verify("production params ok", stored))


if __name__ == "__main__":
    unittest.main()
