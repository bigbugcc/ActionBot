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

INDEX_SOURCE = '''func (a *IndexController) initRouter(g *gin.RouterGroup) {
	g.GET("/", a.index)
	g.GET("/csrf-token", a.csrfToken)
	g.GET("/sponsors", a.sponsors)
	g.GET("/sponsors/logo/:name", a.sponsorLogo)
	g.POST("/login", middleware.CSRFMiddleware(), a.login)
}
func (a *IndexController) sponsors(c *gin.Context) { /* retained handler */ }
'''
ROUTES_SOURCE = """const IndexPage = lazy(() => import('@/pages/index/IndexPage'));
const SponsorsPage = lazy(() => import('@/pages/sponsors/SponsorsPage'));
const children = [
      { index: true, element: withSuspense(<IndexPage />) },
      { path: 'sponsors', element: withSuspense(<SponsorsPage />) },
];
"""
QUERY_SOURCE = """import { useQuery } from '@tanstack/react-query';

import { HttpUtil } from '@/utils';
import { keys } from '@/api/queryKeys';
import type { SponsorList } from '@/generated/types';

const EMPTY: SponsorList = { sponsors: [] };

async function fetchSponsors(): Promise<SponsorList> {
  const msg = await HttpUtil.get<SponsorList>('/sponsors', undefined, { silent: true });
  if (!msg?.success || !msg.obj) return EMPTY;
  return { contact: msg.obj.contact, sponsors: msg.obj.sponsors ?? [] };
}

export function useSponsorsQuery() {
  const query = useQuery({
    queryKey: keys.sponsors(),
    queryFn: fetchSponsors,
    staleTime: 60 * 60 * 1000,
    retry: false,
  });
  return { data: query.data ?? EMPTY, fetched: query.isFetched };
}
"""
NEW_FILES = (
    (Path('internal/web/controller/index.go'), INDEX_SOURCE, 'remove_api_routes', 'api_routes_status'),
    (Path('frontend/src/routes.tsx'), ROUTES_SOURCE, 'remove_page_route', 'page_route_status'),
    (Path('frontend/src/api/queries/useSponsorsQuery.ts'), QUERY_SOURCE, 'disable_query', 'query_status'),
)


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
        for relative, source, _, _ in NEW_FILES:
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(source.encode())

    def test_routes_removed_and_query_never_fetches(self):
        for relative, _, function, _ in NEW_FILES:
            self.assertEqual(getattr(remover, function)(self.root), 'applied')
        index = (self.root / NEW_FILES[0][0]).read_text()
        self.assertNotIn('g.GET("/sponsors', index)
        self.assertIn('g.GET("/csrf-token", a.csrfToken)', index)
        self.assertIn('g.POST("/login", middleware.CSRFMiddleware(), a.login)', index)
        self.assertIn('func (a *IndexController) sponsors', index)
        routes = (self.root / NEW_FILES[1][0]).read_text()
        self.assertNotIn('SponsorsPage', routes)
        self.assertNotIn("path: 'sponsors'", routes)
        self.assertIn('withSuspense(<IndexPage />)', routes)
        query = (self.root / NEW_FILES[2][0]).read_text()
        self.assertNotIn('HttpUtil', query)
        self.assertNotIn('useQuery', query)
        self.assertIn('return { data: EMPTY, fetched: true };', query)

    def test_new_patches_repeat_without_writes(self):
        for relative, _, function, _ in NEW_FILES:
            apply = getattr(remover, function)
            apply(self.root)
            before = (self.root / relative).read_bytes()
            with patch.object(remover.os, 'replace', side_effect=AssertionError('unexpected write')):
                self.assertEqual(apply(self.root), 'already-applied')
            self.assertEqual((self.root / relative).read_bytes(), before)

    def test_new_patches_missing_files_warn_and_do_not_block_other_patches(self):
        for relative, _, _, key in NEW_FILES:
            (self.root / relative).unlink()
            result, output = self.invoke()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(key + '=skipped', output.splitlines())
            self.assertFalse((self.root / relative).exists())
            self.assertIn('::warning::', result.stdout)

    def test_new_patches_reject_drift_and_preserve_original_on_write_failure(self):
        for relative, source, function, key in NEW_FILES:
            for changed in (source.replace('sponsors', 'partners'), source + source):
                with self.subTest(path=relative, changed=changed):
                    path = self.root / relative
                    path.write_bytes(changed.encode())
                    result, output = self.invoke()
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn(key + '=skipped', output.splitlines())
                    self.assertEqual(path.read_bytes(), changed.encode())
            for operation in ('fsync', 'replace'):
                path.write_bytes(source.encode())
                with patch.object(remover.os, operation, side_effect=OSError('disk failure')):
                    with self.assertRaises(OSError):
                        getattr(remover, function)(self.root)
                self.assertEqual(path.read_bytes(), source.encode())
                self.assertFalse(list(path.parent.glob('.*.tmp')))

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
                self.assertEqual(remover.remove_fragments(self.root, relative, fragments), "applied")
                result = (self.root / relative).read_text()
                self.assertNotIn("sponsors", result)
                self.assertNotIn("CrownOutlined", result)
                self.assertIn("DashboardOutlined", result)
                self.assertIn("t('menu.dashboard')", result)
                self.assertEqual(result, sidebar_expected if relative == SIDEBAR else palette_expected)
        self.assertIn("https://donate.sanaei.dev/", (self.root / SIDEBAR).read_text())

    def test_menu_removal_is_idempotent(self):
        for relative, _, fragments in self.menus():
            remover.remove_fragments(self.root, relative, fragments)
            before = (self.root / relative).read_bytes()
            with patch.object(remover.os, "replace", side_effect=AssertionError("unexpected write")):
                self.assertEqual(remover.remove_fragments(self.root, relative, fragments), "already-applied")
            self.assertEqual((self.root / relative).read_bytes(), before)

    def test_changed_menu_or_reused_icon_is_rejected_without_writes(self):
        for relative, source, fragments in self.menus():
            for changed in (source.replace("'/sponsors'", "'/partners'"),
                            source + "\nconst otherIcon = CrownOutlined;\n", source + source):
                with self.subTest(path=relative, source=changed):
                    path = self.root / relative
                    path.write_bytes(changed.encode())
                    with self.assertRaises(ValueError):
                        remover.remove_fragments(self.root, relative, fragments)
                    self.assertEqual(path.read_bytes(), changed.encode())
                    self.assertEqual(list(path.parent.iterdir()), [path])

    def test_failed_menu_write_or_replace_preserves_the_whole_file(self):
        for relative, source, fragments in self.menus():
            for operation in ("fsync", "replace"):
                with self.subTest(path=relative, operation=operation):
                    with patch.object(remover.os, operation, side_effect=OSError("disk failure")):
                        with self.assertRaises(OSError):
                            remover.remove_fragments(self.root, relative, fragments)
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
        output.unlink(missing_ok=True)
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
        for _, _, _, key in NEW_FILES:
            self.assertIn(key + '=applied', output.splitlines())
        result, output = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("status=already-applied\n", output)
        self.assertIn("sidebar_status=already-applied\n", output)
        self.assertIn("palette_status=already-applied\n", output)
        for _, _, _, key in NEW_FILES:
            self.assertIn(key + '=already-applied', output.splitlines())

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
        for env_key, output_key in (("SIDEBAR_STATUS", "sidebar_status"), ("PALETTE_STATUS", "palette_status"),
                                    ("API_ROUTES_STATUS", "api_routes_status"),
                                    ("PAGE_ROUTE_STATUS", "page_route_status"), ("QUERY_STATUS", "query_status")):
            self.assertIn(f"steps.sponsors.outputs.{output_key}", report["env"][env_key])
            self.assertIn("steps.sponsors.outcome == 'success'", report["env"][env_key])
            self.assertIn(f"steps.sponsor_result.outputs.{output_key} || 'failed'", release["with"]["body"])
            self.assertIn(f'record_status {output_key} ', report['run'])
        env_keys = ('SPONSOR_STATUS', 'SIDEBAR_STATUS', 'PALETTE_STATUS',
                    'API_ROUTES_STATUS', 'PAGE_ROUTE_STATUS', 'QUERY_STATUS')
        output_keys = ('status', 'sidebar_status', 'palette_status',
                       'api_routes_status', 'page_route_status', 'query_status')
        scenarios = [(value,) * 6 for value in ('applied', 'already-applied', 'skipped', 'failed', '', 'unexpected')]
        scenarios.append(('applied', 'skipped', 'already-applied', '', 'failed', 'applied'))
        for values in scenarios:
            with self.subTest(statuses=values):
                output = self.root / 'report-output'
                summary = self.root / 'report-summary'
                output.unlink(missing_ok=True)
                summary.unlink(missing_ok=True)
                result = subprocess.run(
                    ['bash', '-c', report['run']], capture_output=True, text=True,
                    env={**os.environ, **dict(zip(env_keys, values)),
                         'GITHUB_OUTPUT': output.as_posix(), 'GITHUB_STEP_SUMMARY': summary.as_posix()},
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                expected = [value if value in ('applied', 'already-applied', 'skipped') else 'failed'
                            for value in values]
                self.assertEqual(output.read_text().splitlines(),
                                 [f'{key}={value}' for key, value in zip(output_keys, expected)])
                self.assertEqual(len(summary.read_text().splitlines()), 6)


if __name__ == "__main__":
    unittest.main()
