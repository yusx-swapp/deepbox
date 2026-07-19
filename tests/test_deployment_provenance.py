import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DeploymentProvenanceTests(unittest.TestCase):
    def test_bicep_wires_git_commit_into_app_settings(self):
        bicep = (ROOT / "infra" / "main.bicep").read_text(encoding="utf-8")
        self.assertIn("param gitCommit string", bicep)
        self.assertIn("{ name: 'DEEPBOX_GIT_COMMIT', value: gitCommit }", bicep)

    def test_deploy_script_resolves_and_passes_exact_commit(self):
        script = (ROOT / "scripts" / "deploy-azure.ps1").read_text(encoding="utf-8")
        self.assertIn("git -C $repoRoot rev-parse HEAD", script)
        self.assertIn("gitCommit=$gitCommit", script)


if __name__ == "__main__":
    unittest.main()
