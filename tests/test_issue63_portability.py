"""Portability regression tests for Issue #63: zero hardcoded host paths or LAN IPs in code/ops/units."""
from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

from hearthcore.core import ROOT

FORBIDDEN_HOST_RE = re.compile(r"(/home/wonky\b|192\.168\.1\.3\b)")
SCAN_DIRS = ("hearthcore", "azeroth-watch", "ops", "tests", ".github")


class PortabilityTests(unittest.TestCase):
    def test_no_hardcoded_host_paths_or_lan_ip_in_executable_code(self):
        offenders: list[str] = []
        for dname in SCAN_DIRS:
            base = ROOT / dname
            if not base.exists():
                continue
            for p in sorted(base.rglob("*")):
                if not p.is_file() or "__pycache__" in p.parts:
                    continue
                if p.suffix not in (".py", ".sh", ".yml", ".yaml", ".service", ".timer", ".js"):
                    continue
                if p.name == Path(__file__).name:
                    continue
                text = p.read_text(encoding="utf-8", errors="ignore")
                for lineno, line in enumerate(text.splitlines(), 1):
                    # Allow comments showing usage examples like "(e.g., SERVER_IP=192.168.1.3)"
                    stripped = line.strip()
                    if stripped.startswith("#"):
                        continue
                    if "e.g." in line and "192.168.1.3" in line:
                        continue
                    if FORBIDDEN_HOST_RE.search(line):
                        offenders.append(f"{p.relative_to(ROOT)}:{lineno}: {line.strip()}")
        self.assertEqual(offenders, [], "Hardcoded host path or LAN IP found:\n" + "\n".join(offenders))

    def test_ci_workflow_pins_actions_to_full_40_hex_shas_with_read_permissions(self):
        ci_txt = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn("permissions:\n  contents: read", ci_txt)
        uses_lines = [ln.strip() for ln in ci_txt.splitlines() if "uses:" in ln]
        self.assertGreaterEqual(len(uses_lines), 2)
        for ln in uses_lines:
            self.assertRegex(ln, r"uses:\s+[\w.-]+/[\w.-]+@[0-9a-f]{40}\b", f"Unpinned action: {ln}")

    def test_azeroth_watch_imports_cleanly_from_repo_without_host_dir(self):
        aw_dir = ROOT / "azeroth-watch"
        res = subprocess.run(
            ["python3", "-c", "import sys; sys.path.insert(0, sys.argv[1]); import server; print(server.BASE_DIR)", str(aw_dir)],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(str(aw_dir.resolve()), res.stdout)


if __name__ == "__main__":
    unittest.main()
