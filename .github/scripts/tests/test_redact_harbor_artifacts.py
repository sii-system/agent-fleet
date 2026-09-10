import importlib.util
import tempfile
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "redact_harbor_artifacts",
    Path(__file__).resolve().parents[1] / "redact_harbor_artifacts.py",
)
redactor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(redactor)


class RedactionTest(unittest.TestCase):
    def test_collects_credentials_without_treating_token_limits_as_secrets(self):
        values = redactor.credential_values(
            {
                "EXA_API_KEY": "fake-exa",
                "MODAL_TOKEN_ID": "fake-modal-id",
                "MODAL_TOKEN_SECRET": "fake-modal-secret",
                "HARBOR_MAX_TOKENS": "8192",
                "HARBOR_ANALYZER_ENABLED": "1",
                "API_KEY": "",
                "HARBOR_LLM_KWARGS": '{"api_key":"fake-llm","max_tokens":8192,"extra_headers":{"x-auth":"fake-header"}}',
                "OPENCODE_CONFIG_CONTENT": '{"provider":{"custom":{"options":{"apiKey":"fake-opencode"}}}}',
                "OPENCODE_RUNTIME_SECRETS_JSON": '{"CUSTOM_AUTH":"fake-runtime"}',
                "HARBOR_ANTHROPIC_CUSTOM_HEADERS": "Authorization: Bearer fake-bearer",
                "HTTPS_PROXY": "https://user:fake-password@proxy.example.test",
            }
        )
        self.assertEqual(
            set(values),
            {
                b"fake-exa",
                b"fake-modal-id",
                b"fake-modal-secret",
                b"fake-llm",
                b"fake-header",
                b"fake-opencode",
                b"fake-runtime",
                b"Bearer fake-bearer",
                b"fake-password",
            },
        )

    def test_redacts_overlapping_values_in_binary_and_hidden_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / ".transcript"
            path.write_bytes(b"\xff fake-long-key fake-long 8192")
            redactor.redact(root, [b"fake-long-key", b"fake-long"])
            self.assertEqual(path.read_bytes(), b"\xff *** *** 8192")

    def test_rejects_secret_filenames_and_symlinks(self):
        for kind in ("filename", "symlink"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                if kind == "filename":
                    (root / "fake-secret.log").touch()
                else:
                    (root / "link").symlink_to(root / "missing")
                with self.assertRaises(ValueError):
                    redactor.redact(root, [b"fake-secret"])


if __name__ == "__main__":
    unittest.main()
