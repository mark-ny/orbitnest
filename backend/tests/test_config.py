from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

from app.core import config
from app.core.config import ConfigError, Settings, default_data_dir

STRONG = "x" * 40


class FromEnvTests(unittest.TestCase):
    def test_defaults_are_loopback_and_development(self) -> None:
        s = Settings.from_env({"HOME": "/h"})
        self.assertEqual(s.host, "127.0.0.1")
        self.assertEqual(s.env, "development")
        self.assertIsNone(s.secret_key)

    def test_parses_values(self) -> None:
        s = Settings.from_env(
            {
                "HOME": "/h",
                "ORBITNEST_PORT": "9000",
                "ORBITNEST_LOG_LEVEL": "debug",
                "ORBITNEST_CORS_ORIGINS": "https://a.example, https://b.example:8443",
                "ORBITNEST_LOG_JSON": "false",
            }
        )
        self.assertEqual(s.port, 9000)
        self.assertEqual(s.log_level, "DEBUG")
        self.assertEqual(s.cors_origins, ["https://a.example", "https://b.example:8443"])
        self.assertFalse(s.log_json)

    def test_invalid_port_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            Settings.from_env({"HOME": "/h", "ORBITNEST_PORT": "70000"})

    def test_wildcard_cors_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            Settings.from_env({"HOME": "/h", "ORBITNEST_CORS_ORIGINS": "*"})

    def test_cors_origin_with_path_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            Settings.from_env({"HOME": "/h", "ORBITNEST_CORS_ORIGINS": "https://a.example/app"})

    def test_error_never_echoes_secret_value(self) -> None:
        bad = "short-secret-value-abc"
        with self.assertRaises(ConfigError) as ctx:
            Settings.from_env({"HOME": "/h", "ORBITNEST_SECRET_KEY": bad})
        self.assertNotIn(bad, str(ctx.exception))


class SafetyRuleTests(unittest.TestCase):
    def test_production_requires_secret(self) -> None:
        with self.assertRaises(ConfigError):
            Settings.from_env({"HOME": "/h", "ORBITNEST_ENV": "production"})

    def test_production_with_strong_secret_ok(self) -> None:
        s = Settings.from_env({"HOME": "/h", "ORBITNEST_ENV": "production", "ORBITNEST_SECRET_KEY": STRONG})
        self.assertTrue(s.secret_from_env)

    def test_weak_and_placeholder_secrets_rejected(self) -> None:
        for bad in ("short", "changeme", "replace_with_random_secret"):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                Settings.from_env({"HOME": "/h", "ORBITNEST_SECRET_KEY": bad})

    def test_non_loopback_refused_without_opt_in(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            Settings.from_env({"HOME": "/h", "ORBITNEST_HOST": "0.0.0.0"})
        self.assertIn("non-loopback", str(ctx.exception))

    def test_non_loopback_refused_while_auth_not_enforced(self) -> None:
        self.assertFalse(config.AUTH_ENFORCED, "update this test in Phase 2 when auth is enforced")
        with self.assertRaises(ConfigError) as ctx:
            Settings.from_env(
                {
                    "HOME": "/h",
                    "ORBITNEST_HOST": "0.0.0.0",
                    "ORBITNEST_ALLOW_NON_LOOPBACK": "true",
                    "ORBITNEST_SECRET_KEY": STRONG,
                }
            )
        self.assertIn("authentication is not enforced", str(ctx.exception))


class SecretResolutionTests(unittest.TestCase):
    def test_test_env_gets_ephemeral_secret_without_writing_a_file(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            s = Settings.model_validate({"env": "test", "data_dir": Path(d)}).with_resolved_secret()
            self.assertGreaterEqual(len(s.secret_bytes), 32)
            self.assertFalse((Path(d) / "secret.key").exists())

    @unittest.skipIf(sys.platform.startswith("win"), "POSIX permission bits")
    def test_dev_secret_persisted_with_0600_and_reused(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            base = Settings.model_validate({"env": "development", "data_dir": Path(d)})
            first = base.with_resolved_secret().secret_bytes
            second = base.with_resolved_secret().secret_bytes
            self.assertEqual(first, second)
            mode = stat.S_IMODE(os.stat(Path(d) / "secret.key").st_mode)
            self.assertEqual(mode, 0o600)

    def test_secret_not_in_repr(self) -> None:
        s = Settings.from_env({"HOME": "/h", "ORBITNEST_SECRET_KEY": STRONG})
        self.assertNotIn(STRONG, repr(s))
        self.assertNotIn(STRONG, s.model_dump_json())


class DataDirTests(unittest.TestCase):
    def test_termux(self) -> None:
        p = default_data_dir({"HOME": "/data/data/com.termux/files/home", "TERMUX_VERSION": "0.118"}, "linux")
        self.assertEqual(p, Path("/data/data/com.termux/files/home/.orbitnest"))

    def test_windows_uses_localappdata(self) -> None:
        p = default_data_dir({"LOCALAPPDATA": r"C:\Users\me\AppData\Local", "HOME": "x"}, "win32")
        self.assertEqual(p.name, "OrbitNest")

    def test_linux_xdg(self) -> None:
        env = {"XDG_DATA_HOME": "/xdg", "HOME": "/h"}
        self.assertEqual(default_data_dir(env, "linux"), Path("/xdg/orbitnest"))

    def test_linux_default(self) -> None:
        self.assertEqual(default_data_dir({"HOME": "/h"}, "linux"), Path("/h/.local/share/orbitnest"))


class AllowedHostsTests(unittest.TestCase):
    def test_includes_public_url_host(self) -> None:
        s = Settings.from_env({"HOME": "/h", "ORBITNEST_PUBLIC_URL": "https://nest.example.com"})
        self.assertIn("nest.example.com", s.effective_allowed_hosts)
        self.assertIn("localhost", s.effective_allowed_hosts)


if __name__ == "__main__":
    unittest.main()
