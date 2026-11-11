"""The site build publishes every page at the URL Broth cites, or refuses.

The variants' expected.json files were written by Broth's own resolver
(broth.connectors.github_docs.pages.all_pages), not by this build, so they are an
independent oracle. The platform fixtures are broth-service's held-out URL set; every one
of them uses a contract feature this build does not support, so each must be refused.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import build  # noqa: E402

REPO = HERE.parents[1]
VARIANTS = HERE / "variants"
PLATFORMS = HERE / "platforms"


def _build(repo: Path) -> tuple[Path, list[dict]]:
    out = Path(tempfile.mkdtemp()) / "_site"
    build.build(repo, out)
    return out, json.loads((out / "urls.json").read_text())


def _served_file(out: Path, site: str, url: str) -> Path:
    """The file GitHub Pages serves for a URL under the site."""
    relative = urlsplit(url).path.removeprefix(urlsplit(site).path.rstrip("/")).strip("/")
    return out / relative / "index.html" if relative else out / "index.html"


class Kebab(unittest.TestCase):
    # Copied from broth-service tests/unit/connectors/github_docs/test_templates.py.
    CASES = [
        ("createCard", "create-card"),
        ("Card Lifecycle", "card-lifecycle"),
        ("/cards/{id}", "cards-id"),
        ("getHTTPStatus", "get-http-status"),
        ("v2Cards", "v2-cards"),
        ("card2Fa", "card2-fa"),
        ("  --Hello,  World!!--  ", "hello-world"),
        ("Café crème", "caf-cr-me"),
        ("already-kebab", "already-kebab"),
        ("snake_case_name", "snake-case-name"),
        ("", ""),
    ]

    def test_matches_broth(self) -> None:
        for text, expected in self.CASES:
            with self.subTest(text=text):
                self.assertEqual(build.kebab(text), expected)


class Variants(unittest.TestCase):
    def check_variant(self, name: str) -> None:
        repo = VARIANTS / name / "repo"
        expected = json.loads((VARIANTS / name / "expected.json").read_text())["pages"]
        site = build.load_contract(repo).site
        out, manifest = _build(repo)
        published = {entry["source"]: {"url": entry["url"], "title": entry["title"]} for entry in manifest}
        self.assertEqual(published, expected)
        for entry in manifest:
            served = _served_file(out, site, entry["url"])
            self.assertTrue(served.is_file(), f"{entry['url']} is not served")
            self.assertIn(f"<title>{build.escape(entry['title'])}", served.read_text())

    def test_nesting(self) -> None:
        self.check_variant("nesting")

    def test_operations(self) -> None:
        self.check_variant("operations")

    def test_conflict_refuses(self) -> None:
        with self.assertRaises(build.BuildError) as raised:
            _build(VARIANTS / "conflict" / "repo")
        self.assertIn("docs/a.mdx", str(raised.exception))
        self.assertIn("docs/a/index.mdx", str(raised.exception))


class Platforms(unittest.TestCase):
    def test_every_platform_is_refused(self) -> None:
        for platform in sorted(p.name for p in PLATFORMS.iterdir() if p.is_dir()):
            with self.subTest(platform=platform), self.assertRaises(build.BuildError):
                _build(PLATFORMS / platform / "repo")


class Fixture(unittest.TestCase):
    """The repository's own docs: the replay."""

    def setUp(self) -> None:
        self.out, self.manifest = _build(REPO)
        self.site = build.load_contract(REPO).site

    def test_every_page_and_operation_is_served(self) -> None:
        sources = sorted(entry["source"] for entry in self.manifest)
        self.assertEqual(
            sources,
            [
                "docs/card-lifecycle/card-renewal-and-replacement.mdx",
                "docs/get-started/onboarding.mdx",
                "docs/release-notes/banking-live-2.22.mdx",
                "openapi/banking-live.yaml#cardRenewal",
                "openapi/banking-live.yaml#cardRenewalV2",
                "openapi/banking-live.yaml#createCard",
            ],
        )
        for entry in self.manifest:
            self.assertTrue(_served_file(self.out, self.site, entry["url"]).is_file())

    def test_front_matter_is_not_shown(self) -> None:
        page = _served_file(self.out, self.site, self.site + "/bankinglive/docs/get-started/onboarding")
        text = page.read_text()
        self.assertNotIn("broth_labels", text)
        self.assertIn("<title>Getting started with Banking.Live", text)

    def test_one_heading_per_page(self) -> None:
        for entry in self.manifest:
            with self.subTest(url=entry["url"]):
                text = _served_file(self.out, self.site, entry["url"]).read_text()
                self.assertEqual(text.count("<h1"), 1)

    def test_index_404_and_stylesheet(self) -> None:
        index = (self.out / "index.html").read_text()
        for entry in self.manifest:
            self.assertIn(build.escape(entry["title"]), index)
        self.assertTrue((self.out / "404.html").is_file())
        self.assertTrue((self.out / "style.css").is_file())


class Refusals(unittest.TestCase):
    def write(self, contract: str, files: dict[str, str]) -> Path:
        repo = Path(tempfile.mkdtemp())
        (repo / "broth.docs.yaml").write_text(contract)
        for path, text in files.items():
            (repo / path).parent.mkdir(parents=True, exist_ok=True)
            (repo / path).write_text(text)
        return repo

    def test_unsupported_placeholder(self) -> None:
        repo = self.write(
            'version: 1\nsite: https://h.example\nopenapi:\n  - spec: s.yaml\n    url: "/r/{summary:kebab}"\n',
            {"s.yaml": "openapi: 3.0.3\ninfo: {title: S, version: '1'}\npaths: {}\n"},
        )
        with self.assertRaisesRegex(build.BuildError, "summary"):
            _build(repo)

    def test_unreadable_spec(self) -> None:
        repo = self.write(
            'version: 1\nsite: https://h.example\nopenapi:\n  - spec: s.yaml\n    url: "/r/{operationId}"\n',
            {"s.yaml": "openapi: [unclosed\n"},
        )
        with self.assertRaisesRegex(build.BuildError, "s.yaml"):
            _build(repo)

    def test_invalid_front_matter(self) -> None:
        repo = self.write(
            'version: 1\nsite: https://h.example\npages:\n  - include: ["*.mdx"]\n    url: "/{path}"\n',
            {"p.mdx": "---\ntitle: [unclosed\n---\nbody\n"},
        )
        with self.assertRaisesRegex(build.BuildError, "p.mdx"):
            _build(repo)


if __name__ == "__main__":
    unittest.main()
