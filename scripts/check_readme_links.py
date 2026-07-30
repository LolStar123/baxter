"""Fail when a local README link, image, or section target does not resolve."""

from pathlib import Path
import re
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
MARKDOWN_LINK = re.compile(r"!?\[[^\]]*]\(([^)]+)\)")
HTML_LINK = re.compile(r'\b(?:src|srcset)="([^"]+)"')


def heading_anchors(text):
    anchors = set()
    for heading in re.findall(r"^#{1,6}\s+(.+?)\s*$", text, re.MULTILINE):
        anchor = re.sub(r"[^\w -]", "", heading.lower())
        anchors.add(re.sub(r"[\s_]+", "-", anchor).strip("-"))
    return anchors


def main():
    text = README.read_text(encoding="utf-8")
    targets = MARKDOWN_LINK.findall(text) + HTML_LINK.findall(text)
    anchors = heading_anchors(text)
    failures = []
    checked = 0

    for raw in targets:
        target = raw.strip().split()[0]
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        checked += 1
        if target.startswith("#"):
            if unquote(target[1:]).lower() not in anchors:
                failures.append(f"{raw} -> missing README heading")
            continue
        path_text, _, fragment = target.partition("#")
        path = (README.parent / unquote(path_text)).resolve()
        if not path.is_relative_to(ROOT) or not path.is_file():
            failures.append(f"{raw} -> missing file")
        elif fragment and path == README and unquote(fragment).lower() not in anchors:
            failures.append(f"{raw} -> missing README heading")

    if failures:
        print("\n".join(failures))
        return 1
    print(f"README links OK: {checked} local targets resolve")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
