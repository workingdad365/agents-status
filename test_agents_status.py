import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).with_name("agents_status.sh")


class ProviderTests(unittest.TestCase):
    def run_status(self, files):
        with tempfile.TemporaryDirectory() as home:
            for name, data in files.items():
                path = Path(home, name)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            result = subprocess.run(
                ["bash", str(SCRIPT)],
                env={"PATH": os.defpath, "HOME": home, "LC_ALL": "C.UTF-8"},
                capture_output=True, text=True, check=True,
            )
            return {
                line.split(":", 1)[0].split()[-1]: line.split(" | provider: ", 1)[1]
                for line in result.stdout.splitlines() if " | provider: " in line
            }

    def test_commandcode_provider_precedence_and_model_boundary(self):
        cases = [
            ({"provider": "codex", "modelProvider": "openrouter", "model": "gpt-6-astra"}, "codex"),
            ({"modelProvider": "openrouter", "model": "gpt-6-astra"}, "openrouter"),
            ({"model": "openrouter/openai/gpt-6-astra"}, "openrouter"),
            ({"model": "gpt-6-astra"}, "unknown"),
            ({"model": "/gpt-6-astra"}, "unknown"),
            ({"model": "openrouter/"}, "unknown"),
            ({}, "unknown"),
        ]
        for config, expected in cases:
            with self.subTest(config=config):
                output = self.run_status({".commandcode/config.json": config})
                self.assertEqual(output["commandcode"], expected)

    def test_pi_explicit_default_and_auth_candidates(self):
        cases = [
            ({"defaultProvider": "anthropic"}, ["openrouter"], "anthropic"),
            ({}, ["openrouter"], "openrouter (인증 기반 추정)"),
            ({}, ["openrouter", "anthropic"], "자동 선택 (인증 후보: openrouter,anthropic)"),
            ({}, [], "unknown"),
        ]
        for settings, providers, expected in cases:
            with self.subTest(settings=settings, providers=providers):
                output = self.run_status({
                    ".pi/agent/settings.json": settings,
                    ".pi/agent/auth.json": {p: {"type": "api_key", "key": "test-secret"} for p in providers},
                })
                self.assertEqual(output["pi"], expected)


if __name__ == "__main__":
    unittest.main()
