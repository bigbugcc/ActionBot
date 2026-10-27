"""Optionally disable known upstream sponsor data, navigation, routes and queries."""

import os
from pathlib import Path
import stat
import sys
import tempfile


TARGET = Path("internal/web/service/panel/sponsor.go")
SIDEBAR = Path("frontend/src/layouts/AppSidebar.tsx")
PALETTE = Path("frontend/src/components/command-palette/CommandPalette.tsx")
API_ROUTES = Path("internal/web/controller/index.go")
PAGE_ROUTES = Path("frontend/src/routes.tsx")
QUERY = Path("frontend/src/api/queries/useSponsorsQuery.ts")
PANEL_ROUTES = Path("frontend/src/routes/panelRoutes.tsx")
SPONSOR_MARKERS = ("CrownOutlined", "'sponsors'", "'/sponsors'", "menu.sponsors")
PANEL_ROUTE_FRAGMENTS = (
    "  CrownOutlined,\n",
    "const SponsorsPage = lazy(() => import('@/pages/sponsors/SponsorsPage'));\n",
    "  { path: '/sponsors', titleKey: 'menu.sponsors', icon: CrownOutlined, component: SponsorsPage },\n",
)
PANEL_ROUTE_MARKERS = (*SPONSOR_MARKERS, "SponsorsPage")
PALETTE_KEYWORDS_FRAGMENT = "  '/sponsors': ['sponsors', 'sponsor', 'partners'],\n"
API_ROUTE_FRAGMENTS = (
    '\tg.GET("/sponsors", a.sponsors)\n',
    '\tg.GET("/sponsors/logo/:name", a.sponsorLogo)\n',
)
PAGE_ROUTE_FRAGMENTS = (
    "const SponsorsPage = lazy(() => import('@/pages/sponsors/SponsorsPage'));\n",
    "      { path: 'sponsors', element: withSuspense(<SponsorsPage />) },\n",
)
ORIGINAL_QUERY = """import { useQuery } from '@tanstack/react-query';

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
DISABLED_QUERY = """import type { SponsorList } from '@/generated/types';

const EMPTY: SponsorList = { sponsors: [] };

export function useSponsorsQuery() {
  return { data: EMPTY, fetched: true };
}
"""
SIDEBAR_FRAGMENTS = (
    "  CrownOutlined,\n",
    "  | 'sponsors'\n",
    "  sponsors: CrownOutlined,\n",
    "      { key: '/sponsors', icon: 'sponsors', title: t('menu.sponsors') },\n",
)
PALETTE_FRAGMENTS = (
    "  CrownOutlined,\n",
    "      {\n"
    "        path: '/sponsors',\n"
    "        title: t('menu.sponsors'),\n"
    "        keywords: ['sponsors', 'sponsor', 'partners'],\n"
    "        icon: <CrownOutlined />,\n"
    "      },\n",
)
SPONSOR_LIST = '''type SponsorList struct {
	Contact  string    `json:"contact,omitempty" example:"https://t.me/example"`
	Sponsors []Sponsor `json:"sponsors"`
}'''
ORIGINAL = '''func (s *PanelService) GetSponsors() (*SponsorList, error) {
	raw, err := cachedSponsors()
	if err != nil {
		return nil, err
	}
	return activeSponsors(raw, sponsorNow()), nil
}'''
DISABLED = '''func (s *PanelService) GetSponsors() (*SponsorList, error) {
	return &SponsorList{Sponsors: []Sponsor{}}, nil
}'''


def remove_sponsors(root):
    path = root / TARGET
    original = path.read_bytes()
    text = original.decode("utf-8")
    # Match the complete API type and function, not arbitrary braces or URLs.
    # Unknown upstream layouts must remain byte-for-byte unchanged.
    if text.count(SPONSOR_LIST) != 1:
        raise ValueError("Upstream SponsorList changed; source was not modified")
    if text.count(DISABLED) == 1 and ORIGINAL not in text:
        return "already-applied"
    if text.count(ORIGINAL) != 1 or DISABLED in text:
        raise ValueError("Upstream GetSponsors changed; source was not modified")
    replacement = text.replace(ORIGINAL, DISABLED, 1).encode("utf-8")
    atomic_replace(path, replacement)
    return "applied"


def without_fragments(text, relative, fragments, markers):
    if not any(marker in text for marker in markers):
        return text
    replacement = text
    for fragment in fragments:
        if replacement.count(fragment) != 1:
            raise ValueError(f"Upstream {relative.name} changed; source was not modified")
        replacement = replacement.replace(fragment, "", 1)
    # If the icon or route gained another use, keep the whole file unchanged.
    if any(marker in replacement for marker in markers):
        raise ValueError(f"Upstream {relative.name} sponsor references changed; source was not modified")
    return replacement


def remove_fragments(root, relative, fragments, markers=SPONSOR_MARKERS):
    path = root / relative
    text = path.read_bytes().decode("utf-8")
    replacement = without_fragments(text, relative, fragments, markers)
    if replacement == text:
        return "already-applied"
    atomic_replace(path, replacement.encode("utf-8"))
    return "applied"


def remove_navigation(root, relative, legacy_fragments, markers=SPONSOR_MARKERS):
    path = root / relative
    text = path.read_bytes().decode("utf-8")
    manifest_path = root / PANEL_ROUTES
    if "PANEL_ROUTES" not in text and not manifest_path.exists():
        return remove_fragments(root, relative, legacy_fragments, markers)
    # Since v3.11.0 these consumers share one navigation/route manifest.
    if (text.count("import { PANEL_ROUTES } from '@/routes/panelRoutes';\n") != 1
            or text.count('PANEL_ROUTES.map((route) =>') != 1):
        raise ValueError(f"Upstream {relative.name} route consumer changed; source was not modified")
    fragments = (PALETTE_KEYWORDS_FRAGMENT,) if relative == PALETTE else ()
    replacement = without_fragments(text, relative, fragments, markers)
    manifest = manifest_path.read_bytes().decode("utf-8")
    if (manifest.count('export const PANEL_ROUTES: PanelRoute[] = [\n') != 1
            or manifest.count('export const PANEL_ROUTE_PATHS = PANEL_ROUTES.map((route) => route.path);\n') != 1):
        raise ValueError("Upstream panelRoutes.tsx layout changed; source was not modified")
    manifest_replacement = without_fragments(
        manifest, PANEL_ROUTES, PANEL_ROUTE_FRAGMENTS, PANEL_ROUTE_MARKERS)
    # Validate both sources before writing. Never infer success just because
    # a consumer no longer declares its own sponsor link.
    changed = False
    for target, before, after in ((manifest_path, manifest, manifest_replacement),
                                  (path, text, replacement)):
        if after != before:
            atomic_replace(target, after.encode("utf-8"))
            changed = True
    return "applied" if changed else "already-applied"


def remove_api_routes(root):
    return remove_fragments(
        root, API_ROUTES, API_ROUTE_FRAGMENTS, ('"/sponsors', 'a.sponsors', 'a.sponsorLogo'))


def remove_page_route(root):
    return remove_navigation(
        root, PAGE_ROUTES, PAGE_ROUTE_FRAGMENTS, ("SponsorsPage", "'sponsors'", "'/sponsors'"))


def disable_query(root):
    path = root / QUERY
    text = path.read_bytes().decode("utf-8")
    if text == DISABLED_QUERY:
        return "already-applied"
    if text != ORIGINAL_QUERY:
        raise ValueError("Upstream useSponsorsQuery changed; source was not modified")
    atomic_replace(path, DISABLED_QUERY.encode("utf-8"))
    return "applied"


def atomic_replace(path, replacement):
    # A failed write can only damage the sibling temporary file. Replacing the
    # source happens once, after all validation and writes have completed.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(replacement)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def warning(message):
    escaped = str(message).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print(f"::warning::{escaped}")


def main():
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    results = {}
    for key, label, apply in (
        ("status", "Sponsor API", lambda: remove_sponsors(root)),
        ("sidebar_status", "Sponsors sidebar menu", lambda: remove_navigation(root, SIDEBAR, SIDEBAR_FRAGMENTS)),
        ("palette_status", "Sponsors command-palette entry", lambda: remove_navigation(root, PALETTE, PALETTE_FRAGMENTS)),
        ("api_routes_status", "Sponsors backend routes", lambda: remove_api_routes(root)),
        ("page_route_status", "Sponsors page route", lambda: remove_page_route(root)),
        ("query_status", "Sponsors frontend query", lambda: disable_query(root)),
    ):
        try:
            results[key] = apply()
        except Exception as error:
            # Optional patches fail independently; unknown source stays intact.
            warning(f"{label} removal skipped: {error}. Upstream ads or menu entries may remain.")
            results[key] = "skipped"
        print(f"{label}: {results[key]}")
    if output := os.environ.get("GITHUB_OUTPUT"):
        with Path(output).open("a", encoding="utf-8", newline="\n") as stream:
            stream.write("".join(f"{key}={status}\n" for key, status in results.items()))


if __name__ == "__main__":
    main()
