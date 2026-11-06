"""Static checks of the install/uninstall/wrapper scripts. They never run the scripts."""
import os
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ['install.ps1', 'uninstall.ps1', 'Context-Forge.ps1', 'start.ps1', 'run-normalize.ps1',
           'scripts/initialize-authentication.ps1', 'scripts/Ensure-Service.ps1', 'scripts/Install-ServiceTask.ps1',
           'installer/common.ps1', 'installer/setup.ps1', 'installer/open.ps1', 'installer/stop.ps1', 'installer/remove.ps1']
PARSE = ("$e=$null;$t=$null;[void][Management.Automation.Language.Parser]::ParseFile($env:CF_SCRIPT,[ref]$t,[ref]$e);"
         "if($e.Count){$e|%{$_.Message};exit 1}")


class InstallScriptTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt' and shutil.which('powershell'), 'Windows PowerShell required')
    def test_scripts_parse(self):
        for name in SCRIPTS:
            with self.subTest(script=name):
                result = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', PARSE],
                                        capture_output=True, text=True, timeout=60,
                                        env={**os.environ, 'CF_SCRIPT': str(ROOT / name)})
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_installer_contract(self):
        install = (ROOT / 'install.ps1').read_text(encoding='utf-8')
        for token in ('requirements-lock.txt', '--only-binary=:all:', 'npm ci', '--ignore-scripts',
                      'initialize-authentication.ps1', 'mcpServers', 'Nothing was started or registered'):
            self.assertIn(token, install)
        for forbidden in ('Register-ScheduledTask', 'Start-Process', 'Set-ExecutionPolicy', 'New-NetFirewallRule', 'HKLM:', 'HKCU:'):
            self.assertNotIn(forbidden, install)

    def test_uninstaller_keeps_data_by_default_and_stays_inside_the_install_directory(self):
        uninstall = (ROOT / 'uninstall.ps1').read_text(encoding='utf-8')
        for token in ('SupportsShouldProcess', '-RemoveData', 'StartsWith($root', 'Unregister-ScheduledTask'):
            self.assertIn(token, uninstall)

    def test_no_owner_specific_paths_ship(self):
        for path in [*ROOT.glob('*.ps1'), *ROOT.glob('scripts/*'), *ROOT.glob('docs/*.md'), ROOT / 'README.md']:
            if path.is_file():
                text = path.read_text(encoding='utf-8', errors='ignore')
                self.assertNotIn('AI-' + 'PROJECTS', text, path.name)


if __name__ == '__main__':
    unittest.main()
