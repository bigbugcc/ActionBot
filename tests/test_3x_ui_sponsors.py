"""Check optional sponsor removal without cloning or modifying a real checkout."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin/3x-ui/remove_sponsors.py"
TARGET = Path("internal/web/service/panel/sponsor.go")
SIDEBAR = Path("frontend/src/layouts/AppSidebar.tsx")
PALETTE = Path("frontend/src/components/command-palette/CommandPalette.tsx")
spec = importlib.util.spec_from_file_location("remove_sponsors", SCRIPT)
remover = importlib.util.module_from_spec(spec)
spec.loader.exec_module(remover)

# Relevant declarations from upstream 608ef4e4 (v3.10.2). Keep these independent
# of the patch constants so a changed replacement cannot silently change tests.
SOURCE = '''package panel

type Sponsor struct { Name string }

type SponsorList struct {
	Contact  string    `json:"contact,omitempty" example:"https://t.me/example"`
	Sponsors []Sponsor `json:"sponsors"`
}

func (s *PanelService) GetSponsors() (*SponsorList, error) {
	raw, err := cachedSponsors()
	if err != nil {
		return nil, err
	}
	return activeSponsors(raw, sponsorNow()), nil
}

// Unrelated functionality must be preserved verbatim.
func (s *PanelService) Status() string { return "healthy" }
'''

SIDEBAR_SOURCE = '''import {
  CrownOutlined,
  DashboardOutlined,
} from '@ant-design/icons';
type IconName =
  | 'dashboard'
  | 'sponsors'
  | 'logout';
const iconByName = {
  dashboard: DashboardOutlined,
  sponsors: CrownOutlined,
};
const tabs = [
      { key: '/', icon: 'dashboard', title: t('menu.dashboard') },
      { key: '/sponsors', icon: 'sponsors', title: t('menu.sponsors') },
      { key: LOGOUT_KEY, icon: 'logout', title: t('logout') },
];
const DONATE_URL = 'https://donate.sanaei.dev/';
'''
PALETTE_SOURCE = '''import {
  CrownOutlined,
  DashboardOutlined,
} from '@ant-design/icons';
const pages = [
      {
        path: '/',
        title: t('menu.dashboard'),
        icon: <DashboardOutlined />,
      },
      {
        path: '/sponsors',
        title: t('menu.sponsors'),
        keywords: ['sponsors', 'sponsor', 'partners'],
        icon: <CrownOutlined />,
      },
];
'''


class SponsorRemovalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.target = self.root / TARGET
        self.target.parent.mkdir(parents=True)
        self.target.write_bytes(SOURCE.encode())
        for relative, source in ((SIDEBAR, SIDEBAR_SOURCE), (PALETTE, PALETTE_SOURCE)):
            path = self.root / relative
            path.parent.mkdir(parents=True)
            path.write_bytes(source.encode())

    def menus(self):
        return ((SIDEBAR, SIDEBAR_SOURCE, remover.SIDEBAR_FRAGMENTS),
                (PALETTE, PALETTE_SOURCE, remover.PALETTE_FRAGMENTS))

    def test_menu_entries_and_unused_icons_removed_without_touching_other_navigation(self):
        sidebar_expected = SIDEBAR_SOURCE.replace("  CrownOutlined,\n", "").replace(
            "  | 'sponsors'\n", "").replace("  sponsors: CrownOutlined,\n", "").replace(
            "      { key: '/sponsors', icon: 'sponsors', title: t('menu.sponsors') },\n", "")
        palette_expected = PALETTE_SOURCE.replace("  CrownOutlined,\n", "").replace(
            "      {\n        path: '/sponsors',\n        title: t('menu.sponsors'),\n"
            "        keywords: ['sponsors', 'sponsor', 'partners'],\n"
            "        icon: <CrownOutlined />,\n      },\n", "")
        for relative, _, fragments in self.menus():
            with self.subTest(path=relative):
                self.assertEqual(remover.remove_menu(self.root, relative, fragments), "applied")
                result = (self.root / relative).read_text()
                self.assertNotIn("sponsors", result)
                self.assertNotIn("CrownOutlined", result)
                self.assertIn("DashboardOutlined", result)
                self.assertIn("t('menu.dashboard')", result)
                self.assertEqual(result, sidebar_expected if relative == SIDEBAR else palette_expected)
        self.assertIn("https://donate.sanaei.dev/", (self.root / SIDEBAR).read_text())

    def test_menu_removal_is_idempotent(self):
        for relative, _, fragments in self.menus():
            remover.remove_menu(self.root, relative, fragments)
            before = (self.root / relative).read_bytes()
            with patch.object(remover.os, "replace", side_effect=AssertionError("unexpected write")):
                self.assertEqual(remover.remove_menu(self.root, relative, fragments), "already-applied")
            self.assertEqual((self.root / relative).read_bytes(), before)

    def test_changed_menu_or_reused_icon_is_rejected_without_writes(self):
        for relative, source, fragments in self.menus():
            for changed in (source.replace("'/sponsors'", "'/partners'"),
                            source + "\nconst otherIcon = CrownOutlined;\n", source + source):
                with self.subTest(path=relative, source=changed):
                    path = self.root / relative
                    path.write_bytes(changed.encode())
                    with self.assertRaises(ValueError):
                        remover.remove_menu(self.root, relative, fragments)
                    self.assertEqual(path.read_bytes(), changed.encode())
                    self.assertEqual(list(path.parent.iterdir()), [path])

    def test_failed_menu_write_or_replace_preserves_the_whole_file(self):
        for relative, source, fragments in self.menus():
            for operation in ("fsync", "replace"):
                with self.subTest(path=relative, operation=operation):
                    with patch.object(remover.os, operation, side_effect=OSError("disk failure")):
                        with self.assertRaises(OSError):
                            remover.remove_menu(self.root, relative, fragments)
                    path = self.root / relative
                    self.assertEqual(path.read_bytes(), source.encode())
                    self.assertEqual(list(path.parent.iterdir()), [path])

    def test_removes_only_sponsor_fetch_and_preserves_other_code(self):
        self.assertEqual(remover.remove_sponsors(self.root), "applied")
        expected = SOURCE.replace(
            '\traw, err := cachedSponsors()\n\tif err != nil {\n\t\treturn nil, err\n\t}\n'
            '\treturn activeSponsors(raw, sponsorNow()), nil',
            '\treturn &SponsorList{Sponsors: []Sponsor{}}, nil',
        )
        self.assertEqual(self.target.read_bytes(), expected.encode())
        self.assertEqual(list(self.target.parent.iterdir()), [self.target])

    def test_repeated_application_is_a_noop(self):
        remover.remove_sponsors(self.root)
        before = self.target.read_bytes()
        with patch.object(remover.os, "replace", side_effect=AssertionError("unexpected write")):
            self.assertEqual(remover.remove_sponsors(self.root), "already-applied")
        self.assertEqual(self.target.read_bytes(), before)

    def test_changed_function_or_type_is_rejected_without_writes(self):
        for source in (
            SOURCE.replace("cachedSponsors()", "loadSponsors()"),
            SOURCE.replace("Sponsors []Sponsor", "Sponsors *[]Sponsor"),
            SOURCE.replace('Contact  string', 'Contact  *string'),
            SOURCE.replace("GetSponsors()", "GetSponsors(ctx context.Context)"),
            SOURCE + SOURCE,
        ):
            with self.subTest(source=source):
                before = source.encode()
                self.target.write_bytes(before)
                with self.assertRaises(ValueError):
                    remover.remove_sponsors(self.root)
                self.assertEqual(self.target.read_bytes(), before)
                self.assertEqual(list(self.target.parent.iterdir()), [self.target])

    def test_missing_file_does_not_create_anything(self):
        self.target.unlink()
        with self.assertRaises(OSError):
            remover.remove_sponsors(self.root)
        self.assertEqual(list(self.target.parent.iterdir()), [])

    def test_failed_write_or_replace_leaves_original_and_no_temporary_file(self):
        for operation in ("fsync", "replace"):
            with self.subTest(operation=operation):
                with patch.object(remover.os, operation, side_effect=OSError("disk failure")):
                    with self.assertRaises(OSError):
                        remover.remove_sponsors(self.root)
                self.assertEqual(self.target.read_bytes(), SOURCE.encode())
                self.assertEqual(list(self.target.parent.iterdir()), [self.target])

    def invoke(self):
        output = self.root / "outputs"
        result = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), str(self.root)],
            env={**os.environ, "GITHUB_OUTPUT": str(output)},
            capture_output=True, text=True,
        )
        return result, output.read_text()

    def test_cli_reports_applied_and_repeated_results(self):
        result, output = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("status=applied\n", output)
        self.assertIn("sidebar_status=applied\n", output)
        self.assertIn("palette_status=applied\n", output)
        result, output = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("status=already-applied\n", output)
        self.assertIn("sidebar_status=already-applied\n", output)
        self.assertIn("palette_status=already-applied\n", output)

    def test_cli_reports_each_patch_separately_when_one_menu_changed(self):
        path = self.root / SIDEBAR
        before = SIDEBAR_SOURCE.replace("'/sponsors'", "'/partners'").encode()
        path.write_bytes(before)
        result, output = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("::warning::", result.stdout)
        self.assertIn("status=applied", output.splitlines())
        self.assertIn("sidebar_status=skipped", output.splitlines())
        self.assertIn("palette_status=applied", output.splitlines())
        self.assertEqual(path.read_bytes(), before)
        self.assertNotIn("sponsors", (self.root / PALETTE).read_text())

    def test_cli_keeps_building_with_missing_menu_source(self):
        (self.root / PALETTE).unlink()
        result, output = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("status=applied", output.splitlines())
        self.assertIn("sidebar_status=applied", output.splitlines())
        self.assertIn("palette_status=skipped", output.splitlines())
        self.assertFalse((self.root / PALETTE).exists())

    def test_cli_warns_and_continues_for_upstream_drift(self):
        before = SOURCE.replace("cachedSponsors()", "changedUpstream()").encode()
        self.target.write_bytes(before)
        result, output = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("::warning::", result.stdout)
        self.assertIn("status=skipped\n", output)
        self.assertEqual(self.target.read_bytes(), before)

    def test_cli_warns_and_continues_for_missing_source(self):
        self.target.unlink()
        result, output = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("::warning::", result.stdout)
        self.assertIn("status=skipped\n", output)
        self.assertFalse(self.target.exists())

    def test_workflow_keeps_patch_optional_before_build_and_records_result(self):
        result = subprocess.run(
            ["node", "-e", "const fs = require('fs'); const yaml = require('js-yaml'); "
             "console.log(JSON.stringify(yaml.load(fs.readFileSync(process.argv[1], 'utf8'))));",
             str(ROOT / ".github/workflows/3x-ui-Docker.yml")],
            cwd=ROOT, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        steps = json.loads(result.stdout)["jobs"]["build"]["steps"]
        removal = next(step for step in steps if step.get("id") == "sponsors")
        report = next(step for step in steps if step.get("id") == "sponsor_result")
        build = next(step for step in steps if step["name"] == "Configure official build")
        source = next(step for step in steps if step.get("id") == "source")
        self.assertLess(steps.index(source), steps.index(removal))
        self.assertLess(steps.index(removal), steps.index(report))
        self.assertLess(steps.index(report), steps.index(build))
        self.assertIs(removal["continue-on-error"], True)
        self.assertIs(report["continue-on-error"], True)
        self.assertEqual(removal["run"], "python3 bin/3x-ui/remove_sponsors.py workdir")
        self.assertIn("steps.sponsors.outcome == 'success'", report["env"]["SPONSOR_STATUS"])
        self.assertIn("|| 'failed'", report["env"]["SPONSOR_STATUS"])
        release = next(step for step in steps if step["name"] == "Create release")
        self.assertIn("steps.sponsor_result.outputs.status || 'failed'", release["with"]["body"])
        self.assertIn("GITHUB_STEP_SUMMARY", report["run"])
        for env_key, output_key in (("SIDEBAR_STATUS", "sidebar_status"), ("PALETTE_STATUS", "palette_status")):
            self.assertIn(f"steps.sponsors.outputs.{output_key}", report["env"][env_key])
            self.assertIn("steps.sponsors.outcome == 'success'", report["env"][env_key])
            self.assertIn(f"steps.sponsor_result.outputs.{output_key} || 'failed'", release["with"]["body"])


if __name__ == "__main__":
    unittest.main()
