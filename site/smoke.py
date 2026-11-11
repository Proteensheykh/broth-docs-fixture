"""After a deploy: every URL the site says it publishes opens, with its title.

Usage: python site/smoke.py [MANIFEST]   (default: _site/urls.json)

Reads the manifest the build wrote, requests each URL from the live site, and fails unless
each answers 200 with the page's title. A fresh Pages deploy can take a little while to
reach the CDN, so each URL is retried for up to two minutes before it counts as failed.
"""

from __future__ import annotations

import html
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEADLINE_SECONDS = 120


def _check(url: str, title: str) -> str | None:
    """None when the page is right, else what is wrong with it."""
    request = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read().decode("utf-8", errors="replace")
            status = response.status
    except urllib.error.HTTPError as exc:
        return f"status {exc.code}"
    except (urllib.error.URLError, TimeoutError) as exc:
        return f"no answer ({exc})"
    if status != 200:
        return f"status {status}"
    if f"<title>{html.escape(title, quote=True)}" not in body:
        return f"the title is not {title!r}"
    return None


def main(argv: list[str]) -> int:
    manifest = json.loads(Path(argv[1] if len(argv) > 1 else "_site/urls.json").read_text())
    pending = {entry["url"]: entry["title"] for entry in manifest}
    problems: dict[str, str] = {}
    deadline = time.monotonic() + DEADLINE_SECONDS
    while pending:
        for url, title in list(pending.items()):
            problem = _check(url, title)
            if problem is None:
                print(f"ok   {url}")
                del pending[url]
                problems.pop(url, None)
            else:
                problems[url] = problem
        if not pending or time.monotonic() > deadline:
            break
        time.sleep(10)
    for url, problem in problems.items():
        print(f"FAIL {url}: {problem}")
    print(f"{len(manifest) - len(problems)} of {len(manifest)} URLs open with their title")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
