import unittest

from app.auth import totp

RFC_KEY = b"12345678901234567890"  # RFC 6238 appendix B test key (SHA-1)


class Rfc6238Tests(unittest.TestCase):
    def test_published_vectors(self) -> None:
        # RFC 6238 gives 8-digit values; the 6-digit code is the same truncation modulo 10**6.
        for ts, eight in [
            (59, "94287082"),
            (1111111109, "07081804"),
            (1111111111, "14050471"),
            (1234567890, "89005924"),
            (2000000000, "69279037"),
        ]:
            with self.subTest(ts=ts):
                self.assertEqual(totp.hotp(RFC_KEY, ts // 30, 8), eight)
                self.assertEqual(totp.totp(RFC_KEY, ts), eight[-6:])

    def test_rfc4226_hotp_vectors(self) -> None:
        expected = [
            "755224",
            "287082",
            "359152",
            "969429",
            "338314",
            "254676",
            "287922",
            "162583",
            "399871",
            "520489",
        ]
        self.assertEqual([totp.hotp(RFC_KEY, i) for i in range(10)], expected)


class VerifyTests(unittest.TestCase):
    def test_window_and_replay(self) -> None:
        t = 1_700_000_000
        code = totp.totp(RFC_KEY, t)
        step = int(t // 30)
        self.assertEqual(totp.verify(RFC_KEY, code, t), step)
        self.assertEqual(totp.verify(RFC_KEY, code, t + 30), step)  # one step late
        self.assertEqual(totp.verify(RFC_KEY, code, t - 30), step)  # one step early
        self.assertIsNone(totp.verify(RFC_KEY, code, t + 90))  # outside the window
        self.assertIsNone(totp.verify(RFC_KEY, code, t, last_step=step))  # replay of the same step
        self.assertIsNone(totp.verify(RFC_KEY, code, t, last_step=step + 5))

    def test_malformed_codes(self) -> None:
        t = 1_700_000_000
        good = totp.totp(RFC_KEY, t)
        self.assertEqual(totp.verify(RFC_KEY, f"{good[:3]} {good[3:]}", t), int(t // 30))  # spaces are fine
        for bad in ("", "12345", "1234567", "abcdef", good + "\n\x00", "١٢٣٤٥٦", "-12345"):
            self.assertIsNone(totp.verify(RFC_KEY, bad, t), repr(bad))


class SecretAndUriTests(unittest.TestCase):
    def test_uri_is_well_formed_and_escaped(self) -> None:
        uri = totp.otpauth_uri("Orbit Nest", "a&b@x y", b"\x01" * 20)
        self.assertTrue(uri.startswith("otpauth://totp/Orbit%20Nest:a%26b%40x%20y?secret="))
        self.assertIn("&issuer=Orbit%20Nest", uri)
        self.assertNotIn(" ", uri)

    def test_seal_round_trip_and_tamper_detection(self) -> None:
        key = b"k" * 48
        sealed = totp.seal(key, RFC_KEY)
        self.assertNotIn(RFC_KEY.decode(), sealed)
        self.assertEqual(totp.unseal(key, sealed), RFC_KEY)
        self.assertNotEqual(totp.seal(key, RFC_KEY), sealed)  # fresh nonce
        self.assertIsNone(totp.unseal(b"x" * 48, sealed))  # wrong server key
        flipped = sealed[:-4] + ("AAAA" if not sealed.endswith("AAAA") else "BBBB")
        self.assertIsNone(totp.unseal(key, flipped))
        for junk in ("", "v1.", "v2.abcd", "v1.!!!", "plain"):
            self.assertIsNone(totp.unseal(key, junk))

    def test_recovery_codes(self) -> None:
        codes = totp.new_recovery_codes()
        self.assertEqual(len(set(codes)), 10)
        for c in codes:
            self.assertRegex(c, r"^[a-z2-9]{5}-[a-z2-9]{5}$")
            self.assertEqual(totp.hash_recovery(c), totp.hash_recovery(c.upper().replace("-", " ")))
        self.assertIsNone(totp.hash_recovery("short"))
        self.assertIsNone(totp.hash_recovery("aaaaa-aaaa0"))  # '0' is not in the alphabet


if __name__ == "__main__":
    unittest.main()
