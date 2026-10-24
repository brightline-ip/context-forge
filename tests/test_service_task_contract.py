import unittest
from pathlib import Path


class ServiceTaskContractTests(unittest.TestCase):
    def test_ensure_service_is_authenticated_fail_closed_and_loopback_only(self):
        script = (Path(__file__).parents[1] / "scripts" / "Ensure-Service.ps1").read_text(encoding="utf-8")
        for token in ("127.0.0.1", "Authorization", "security_revision", "auth_required", "Get-NetTCPConnection", "listener exists"):
            self.assertIn(token, script)
        self.assertNotIn("Stop-Process", script)

    def test_task_is_limited_single_instance_and_reconciles(self):
        script = (Path(__file__).parents[1] / "scripts" / "Install-ServiceTask.ps1").read_text(encoding="utf-8")
        for token in ("LogonType Interactive", "RunLevel Limited", "MultipleInstances IgnoreNew", "RepetitionInterval (New-TimeSpan -Minutes 5)", "AllowStartIfOnBatteries", "DontStopIfGoingOnBatteries", "DontStopOnIdleEnd", "Settings.IdleSettings.StopOnIdleEnd", "Resolve-TaskPrincipalSid", "Get-LocalUser", "existingSid -eq $accountSid", "installedSid -ne $accountSid", "Refusing to overwrite"):
            self.assertIn(token, script)


if __name__ == "__main__":
    unittest.main()
