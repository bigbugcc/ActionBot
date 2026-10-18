"""Optionally disable known upstream sponsor placements and navigation entries."""

import os
from pathlib import Path
import stat
import sys
import tempfile


TARGET = Path("internal/web/service/panel/sponsor.go")
SIDEBAR = Path("frontend/src/layouts/AppSidebar.tsx")
PALETTE = Path("frontend/src/components/command-palette/CommandPalette.tsx")
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


def remove_menu(root, relative, fragments):
    path = root / relative
    text = path.read_bytes().decode("utf-8")
    markers = ("CrownOutlined", "'sponsors'", "'/sponsors'", "menu.sponsors")
    if not any(marker in text for marker in markers):
        return "already-applied"
    replacement = text
    for fragment in fragments:
        if replacement.count(fragment) != 1:
            raise ValueError(f"Upstream {relative.name} menu changed; source was not modified")
        replacement = replacement.replace(fragment, "", 1)
    # If the icon or route gained another use, keep the whole file unchanged.
    if any(marker in replacement for marker in markers):
        raise ValueError(f"Upstream {relative.name} sponsor references changed; source was not modified")
    atomic_replace(path, replacement.encode("utf-8"))
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
        ("sidebar_status", "Sponsors sidebar menu", lambda: remove_menu(root, SIDEBAR, SIDEBAR_FRAGMENTS)),
        ("palette_status", "Sponsors command-palette entry", lambda: remove_menu(root, PALETTE, PALETTE_FRAGMENTS)),
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
