"""Python's ``$`` also matches before a trailing newline, so ``re.match(r"^x$", "x\\n")`` succeeds.

Every validator regex must reject that, or ``"alice\\n"`` style values slip through validation.
"""

import unittest

from app.auth.service import USERNAME_RE
from app.runtimes.base import WORKSPACE_ID_RE
from app.runtimes.docker_runtime import ASSET_PATH_RE, RESOURCE_ID_RE
from app.services.streams import ASSET_RE
from app.services.templates import DIGEST_RE, IMAGE_RE, TEMPLATE_ID_RE
from app.services.workspaces import IDEMPOTENCY_RE, LABEL_KEY_RE, LABEL_VALUE_RE, NAME_RE
from app.storage.manager import ID_RE


class AnchorTests(unittest.TestCase):
    def test_trailing_newline_is_rejected_everywhere(self) -> None:
        samples = {
            USERNAME_RE: "alice",
            WORKSPACE_ID_RE: "01JABCDEFGHJKMNPQRSTVWXYZ0"[:26],
            ID_RE: "01JABCDEFGHJKMNPQRSTVWXYZ0"[:26],
            RESOURCE_ID_RE: "a" * 64,
            ASSET_PATH_RE: "core/rfb.js",
            ASSET_RE: "core/rfb.js",
            DIGEST_RE: "sha256:" + "a" * 64,
            IMAGE_RE: "python:3.12-slim",
            TEMPLATE_ID_RE: "dev-python",
            NAME_RE: "My workspace",
            LABEL_KEY_RE: "team",
            LABEL_VALUE_RE: "blue",
            IDEMPOTENCY_RE: "abcdefgh-1234",
        }
        for pattern, good in samples.items():
            with self.subTest(pattern=pattern.pattern):
                self.assertTrue(pattern.match(good), "sample should be valid")
                self.assertIsNone(pattern.match(good + "\n"))


if __name__ == "__main__":
    unittest.main()
