import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).with_name("agents_status.sh")


class ProviderTests(unittest.TestCase):
    def run_status(self, files, extra_env=None):
        with tempfile.TemporaryDirectory() as home:
            for name, data in files.items():
                path = Path(home, name)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            result = subprocess.run(
                ["bash", str(SCRIPT)],
                env={"PATH": os.defpath, "HOME": home, "LC_ALL": "C.UTF-8", **(extra_env or {})},
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

    def test_kilo_config_precedence_and_model_boundary(self):
        cases = [
            ({"model": "github-copilot/gpt-6-astra"}, "github-copilot"),
            ({"model": "openrouter/openai/gpt-6-astra"}, "openrouter"),
            ({"model": "gpt-6-astra"}, "unknown"),
            ({"model": "/gpt-6-astra"}, "unknown"),
            ({"model": "openrouter/"}, "unknown"),
            ({}, "unknown"),
        ]
        for filename in ("kilo.json", "kilo.jsonc"):
            for config, expected in cases:
                with self.subTest(filename=filename, config=config):
                    output = self.run_status({f".config/kilo/{filename}": config})
                    self.assertEqual(output["kilo"], expected)
        output = self.run_status({
            ".config/kilo/kilo.jsonc": {"model": "github-copilot/gpt-6-astra"},
            ".config/kilo/kilo.json": {"model": "anthropic/claude"},
            ".local/state/kilo/model.json": {"recent": [{"providerID": "openrouter"}]},
        })
        self.assertEqual(output["kilo"], "github-copilot")

    def test_kilo_saved_provider_and_xdg_state(self):
        with tempfile.TemporaryDirectory() as state_home:
            for state, env in ((".local/state", {}), (state_home, {"XDG_STATE_HOME": state_home})):
                with self.subTest(state=state):
                    output = self.run_status({
                        ".config/kilo/kilo.jsonc": {},
                        f"{state}/kilo/model.json": {"recent": [{"providerID": "github-copilot"}]},
                    }, env)
                    self.assertEqual(output["kilo"], "github-copilot (최근 사용)")

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
