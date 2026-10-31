import unittest
from pathlib import Path

from app.services import images
from app.services.templates import BUILTIN_TEMPLATES

DOCKER = Path(__file__).resolve().parents[2] / "docker"


class ImagePlanTests(unittest.TestCase):
    def test_every_build_context_has_a_dockerfile_and_matching_tag(self) -> None:
        for ctx, tag in images.buildable().items():
            with self.subTest(ctx=ctx):
                text = (DOCKER / ctx / "Dockerfile").read_text()
                if ctx not in images.NO_BASE:
                    self.assertIn("FROM orbitnest/stream-base:", text)
                self.assertTrue(tag.startswith("orbitnest/"))
        self.assertTrue((DOCKER / "stream-base" / "Dockerfile").is_file())
        self.assertTrue((DOCKER / "stream-base" / "orbitnest-session").is_file())

    def test_stream_templates_all_buildable(self) -> None:
        streamed = {
            t.build_context
            for t in BUILTIN_TEMPLATES
            if t.access == "stream" and t.enabled and t.build_context
        }
        self.assertEqual(streamed | images.NO_BASE, set(images.buildable()))

    def test_plan_builds_bases_first_and_picks_the_right_one(self) -> None:
        steps = images.plan(["stream-desktop-ubuntu"])
        self.assertIn("orbitnest/stream-base:ubuntu-1", steps[0])
        self.assertFalse(any("orbitnest/stream-base:debian-1" in s for s in steps))
        full = images.plan(None)
        bases = [s for s in full if "stream-base" in s[-1]]
        self.assertEqual(len(bases), 3)  # debian, kali, ubuntu
        self.assertEqual(full.index(bases[2]), 2)
        self.assertTrue(all(s[:2] == ["docker", "build"] for s in full))

    def test_security_lab_needs_no_stream_base(self) -> None:
        steps = images.plan(["security-lab"])
        self.assertEqual(len(steps), 1)
        self.assertIn("orbitnest/security-lab:1", steps[0])

    def test_unknown_names_rejected(self) -> None:
        for bad in ("../etc", "stream-base", "nope", "stream-tor; rm -rf /"):
            with self.assertRaises(ValueError):
                images.plan([bad])

    def test_tor_dockerfile_fails_closed(self) -> None:
        text = (DOCKER / "stream-tor" / "Dockerfile").read_text()
        self.assertIn("gpg --batch --verify", text)
        self.assertIn("fingerprint mismatch", text)
        self.assertIn("EF6E286DDA85EA2A4BA7DE684E2C6E8793298290", text)

    def test_session_script_keeps_vnc_on_loopback(self) -> None:
        text = (DOCKER / "stream-base" / "orbitnest-session").read_text()
        self.assertIn("-localhost", text)
        self.assertNotIn("0.0.0.0", text)


if __name__ == "__main__":
    unittest.main()
