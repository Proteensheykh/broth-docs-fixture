"""Build the fixture docs site from broth.docs.yaml, at the URLs Broth cites.

Usage: python site/build.py [REPO] [OUT]   (defaults: the repository root and _site/)

The link contract is the single source of URLs: every page and operation is written to
<url path under the site>/index.html, so GitHub Pages serves it at exactly the URL Broth
resolves for it. The URL rules mirror broth-service's connectors/github_docs (pages.py,
templates.py, openapi.py, front_matter.py) for the subset of the contract this build
supports. Anything outside that subset is refused, never approximated, because a site
that publishes a different URL than Broth cites is the failure this build exists to stop.

Writes, besides the pages: index.html (every page and operation), 404.html, style.css,
and urls.json (every published URL with its source file and title).
"""

from __future__ import annotations

import html
import json
import posixpath
import re
import shutil
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import markdown
import yaml

HERE = Path(__file__).resolve().parent
PAGE_EXTENSIONS = (".mdx", ".md")
METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
PAGE_KEYS = {"root", "include", "exclude", "url", "unpublished_when"}
SPEC_KEYS = {"spec", "url", "unpublished_when"}
PAGE_PLACEHOLDERS = {"path"}
SPEC_PLACEHOLDERS = {"tag", "operationId"}

_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")
_NAME = re.compile(r"^([A-Za-z]+)(?::([a-z]+))?$")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_OTHER = re.compile(r"[^a-z0-9]+")
_SLASHES = re.compile(r"/{2,}")
_FRONT_MATTER = re.compile(r"\A---[ \t]*\r?\n(.*?)(?:\r?\n)?^---[ \t]*(?:\r?\n|\Z)", re.S | re.M)
_HEADING = re.compile(r"^#[ \t]+(.+?)[ \t#]*$")
_FENCE = re.compile(r"^(```|~~~)")


class BuildError(Exception):
    """The repository cannot be published exactly; the message names the file and why."""


def escape(text: str) -> str:
    return html.escape(text, quote=True)


# ---- the contract ----


@dataclass(frozen=True)
class PageRule:
    root: str
    include: tuple[str, ...]
    exclude: tuple[str, ...]
    url: str
    unpublished_when: tuple[str, ...]


@dataclass(frozen=True)
class SpecEntry:
    spec: str
    url: str
    unpublished_when: tuple[str, ...]


@dataclass(frozen=True)
class Contract:
    site: str
    pages: tuple[PageRule, ...]
    openapi: tuple[SpecEntry, ...]

    @property
    def site_path(self) -> str:
        """The site's own path, for links inside the site: "" or "/project"."""
        return urlsplit(self.site).path.rstrip("/")


def _strings(value: Any, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise BuildError(f"broth.docs.yaml: {where} must be a list of strings")
    return tuple(value)


def _template(value: Any, where: str, allowed: set[str]) -> str:
    if not isinstance(value, str) or not value.startswith("/"):
        raise BuildError(f"broth.docs.yaml: {where} must be a path starting with /")
    for match in _PLACEHOLDER.finditer(value):
        parsed = _NAME.match(match.group(1))
        if parsed is None:
            raise BuildError(f'broth.docs.yaml: {where}: "{{{match.group(1)}}}" is not a placeholder')
        name, filter_ = parsed.groups()
        if name not in allowed:
            raise BuildError(f"broth.docs.yaml: {where}: the site build does not support {{{name}}}")
        if filter_ not in (None, "kebab"):
            raise BuildError(f"broth.docs.yaml: {where}: unknown filter :{filter_}")
    if "{" in _PLACEHOLDER.sub("", value) or "}" in _PLACEHOLDER.sub("", value):
        raise BuildError(f"broth.docs.yaml: {where}: unbalanced braces")
    return value


def _unsupported(entry: Mapping[str, Any], allowed: set[str], where: str) -> None:
    extra = sorted(set(entry) - allowed)
    if extra:
        raise BuildError(f"broth.docs.yaml: {where}: the site build does not support {', '.join(extra)}")


def load_contract(repo: Path) -> Contract:
    path = repo / "broth.docs.yaml"
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise BuildError(f"broth.docs.yaml cannot be read: {exc}") from exc
    if not isinstance(raw, Mapping) or raw.get("version") != 1:
        raise BuildError("broth.docs.yaml: version must be 1")
    _unsupported(raw, {"version", "site", "pages", "openapi"}, "top level")
    site = raw.get("site")
    parts = urlsplit(site) if isinstance(site, str) else None
    if parts is None or parts.scheme != "https" or not parts.hostname:
        raise BuildError("broth.docs.yaml: site must be an https URL")
    if parts.query or parts.fragment:
        raise BuildError("broth.docs.yaml: site must not have a query or fragment")
    pages: list[PageRule] = []
    for index, entry in enumerate(raw.get("pages") or []):
        where = f"pages[{index}]"
        if not isinstance(entry, Mapping):
            raise BuildError(f"broth.docs.yaml: {where} must be a mapping")
        _unsupported(entry, PAGE_KEYS, where)
        root = entry.get("root") or ""
        if not isinstance(root, str):
            raise BuildError(f"broth.docs.yaml: {where}.root must be a string")
        include = _strings(entry.get("include"), f"{where}.include")
        if not include:
            raise BuildError(f"broth.docs.yaml: {where}.include is required")
        pages.append(
            PageRule(
                root=root.strip("/"),
                include=include,
                exclude=_strings(entry.get("exclude"), f"{where}.exclude"),
                url=_template(entry.get("url"), f"{where}.url", PAGE_PLACEHOLDERS),
                unpublished_when=_strings(entry.get("unpublished_when"), f"{where}.unpublished_when"),
            )
        )
    specs: list[SpecEntry] = []
    for index, entry in enumerate(raw.get("openapi") or []):
        where = f"openapi[{index}]"
        if not isinstance(entry, Mapping) or not isinstance(entry.get("spec"), str):
            raise BuildError(f"broth.docs.yaml: {where}.spec is required")
        _unsupported(entry, SPEC_KEYS, where)
        specs.append(
            SpecEntry(
                spec=entry["spec"],
                url=_template(entry.get("url"), f"{where}.url", SPEC_PLACEHOLDERS),
                unpublished_when=_strings(entry.get("unpublished_when"), f"{where}.unpublished_when"),
            )
        )
    return Contract(site.rstrip("/"), tuple(pages), tuple(specs))


# ---- URLs (templates.py) ----


def kebab(text: str) -> str:
    return _OTHER.sub("-", _CAMEL.sub("-", text).lower()).strip("-")


def render(template: str, values: Mapping[str, str | None]) -> str | None:
    """The filled template, or None when a value it needs is missing."""
    out: list[str] = []
    last = 0
    for match in _PLACEHOLDER.finditer(template):
        name, filter_ = _NAME.match(match.group(1)).groups()  # type: ignore[union-attr]
        value = values.get(name)
        if value is None or (value == "" and name != "path"):
            return None
        out.append(template[last : match.start()])
        out.append(kebab(value) if filter_ == "kebab" else value)
        last = match.end()
    out.append(template[last:])
    return "".join(out)


def url_path(path: str) -> str:
    """Collapse runs of /, drop a trailing / unless the path is /, refuse . and .."""
    collapsed = _SLASHES.sub("/", "/" + path)
    normalized = collapsed if collapsed == "/" else collapsed.rstrip("/") or "/"
    if any(segment in {".", ".."} for segment in normalized.split("/")):
        raise BuildError(f"the URL path {normalized} leaves the site")
    return normalized


# ---- pages (pages.py, front_matter.py) ----


def _glob(pattern: str) -> re.Pattern[str]:
    out: list[str] = []
    index = 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            out.append("(?:.*/)?")
            index += 3
        elif pattern.startswith("**", index):
            out.append(".*")
            index += 2
        elif pattern[index] == "*":
            out.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            out.append("[^/]")
            index += 1
        else:
            out.append(re.escape(pattern[index]))
            index += 1
    return re.compile("".join(out) + r"\Z")


def _matches(patterns: Iterable[str], path: str) -> bool:
    return any(_glob(pattern).match(path) for pattern in patterns)


def _rule_for(contract: Contract, path: str) -> tuple[PageRule, str] | None:
    """The first rule that includes and does not exclude the file decides it."""
    if posixpath.splitext(path)[1].lower() not in PAGE_EXTENSIONS:
        return None
    for rule in contract.pages:
        if rule.root:
            if not path.startswith(rule.root + "/"):
                continue
            relative = path[len(rule.root) + 1 :]
        else:
            relative = path
        if _matches(rule.include, relative) and not _matches(rule.exclude, relative):
            return rule, relative
    return None


def split_front_matter(text: str, source: str) -> tuple[Mapping[str, Any], str]:
    text = text.removeprefix("﻿")
    match = _FRONT_MATTER.match(text)
    if match is None:
        return {}, text
    try:
        loaded = yaml.safe_load(match.group(1)) if match.group(1).strip() else {}
    except yaml.YAMLError as exc:
        raise BuildError(f"{source}: the front matter is not valid YAML") from exc
    if not isinstance(loaded, Mapping):
        raise BuildError(f"{source}: the front matter is not a mapping of fields")
    return {str(key): value for key, value in loaded.items()}, text[match.end() :]


def _first_heading(body: str) -> str | None:
    fenced = False
    for line in body.splitlines():
        if _FENCE.match(line.strip()):
            fenced = not fenced
            continue
        if not fenced and (match := _HEADING.match(line.strip())):
            return match.group(1).strip()
    return None


def _opens_with_h1(body: str) -> bool:
    """Whether the first line with text is a level-1 heading, which then heads the page."""
    for line in body.splitlines():
        if line.strip():
            return _HEADING.match(line.strip()) is not None
    return False


def _drop_index(path: str) -> str:
    return "" if path == "index" else path.removesuffix("/index")


@dataclass
class Entry:
    source: str
    title: str
    path: str  # the URL path under the site, "/" for its root
    body: str  # HTML
    kind: str  # "page" or "operation"
    meta: str = ""  # a line under the title
    own_heading: bool = False  # the body opens with its own level-1 heading
    folders: tuple[str, ...] = field(default_factory=tuple)


def _md(text: str) -> str:
    return markdown.markdown(text, extensions=["fenced_code", "tables", "sane_lists"])


def page_entries(contract: Contract, repo: Path) -> list[Entry]:
    entries: list[Entry] = []
    files = sorted(
        p.relative_to(repo).as_posix()
        for p in repo.rglob("*")
        if p.is_file() and ".git" not in p.relative_to(repo).parts
    )
    for source in files:
        claimed = _rule_for(contract, source)
        if claimed is None:
            continue
        rule, relative = claimed
        fields, body = split_front_matter((repo / source).read_text(encoding="utf-8", errors="replace"), source)
        stem = posixpath.splitext(posixpath.basename(source))[0]
        title_field = fields.get("title")
        title = (
            title_field.strip()
            if isinstance(title_field, str) and title_field.strip()
            else _first_heading(body) or stem
        )
        if any(fields.get(name) is True for name in rule.unpublished_when):
            continue
        rendered = render(rule.url, {"path": _drop_index(posixpath.splitext(relative)[0])})
        assert rendered is not None  # {path} is never missing
        entries.append(
            Entry(
                source=source,
                title=title,
                path=url_path(rendered),
                body=_md(body),
                kind="page",
                own_heading=_opens_with_h1(body),
                folders=tuple(part for part in posixpath.dirname(source).split("/") if part),
            )
        )
    return entries


# ---- operations (openapi.py) ----


def _resolve(document: Mapping[str, Any], node: Any, source: str, stack: tuple[str, ...] = ()) -> Any:
    if isinstance(node, Mapping):
        ref = node.get("$ref")
        if isinstance(ref, str):
            if not ref.startswith("#"):
                raise BuildError(f"{source}: the reference {ref} is outside the document")
            if ref in stack:
                return {"$ref": ref}
            target: Any = document
            for raw in ref[2:].split("/") if ref != "#" else []:
                part = raw.replace("~1", "/").replace("~0", "~")
                if isinstance(target, Mapping) and part in target:
                    target = target[part]
                else:
                    raise BuildError(f"{source}: the reference {ref} points at nothing")
            resolved = _resolve(document, target, source, (*stack, ref))
            siblings = {k: _resolve(document, v, source, stack) for k, v in node.items() if k != "$ref"}
            return {**resolved, **siblings} if isinstance(resolved, Mapping) else resolved
        return {str(k): _resolve(document, v, source, stack) for k, v in node.items()}
    if isinstance(node, list):
        return [_resolve(document, item, source, stack) for item in node]
    return node


def _json(value: Any) -> str:
    return json.dumps(value, indent=2, default=str, ensure_ascii=False)


def _schema_block(content: Any) -> str:
    if not isinstance(content, Mapping):
        return ""
    out = []
    for media_type, media in content.items():
        schema = media.get("schema") if isinstance(media, Mapping) else None
        out.append(f"<p class=\"media\"><code>{escape(str(media_type))}</code></p>")
        if schema is not None:
            out.append(f"<pre><code>{escape(_json(schema))}</code></pre>")
    return "".join(out)


def _operation_body(operation: Mapping[str, Any]) -> str:
    out: list[str] = []
    if isinstance(operation.get("description"), str):
        out.append(_md(operation["description"]))
    parameters = [p for p in operation.get("parameters") or [] if isinstance(p, Mapping)]
    if parameters:
        rows = "".join(
            "<tr>"
            f"<td><code>{escape(str(p.get('name', '')))}</code></td>"
            f"<td>{escape(str(p.get('in', '')))}</td>"
            f"<td>{'yes' if p.get('required') else 'no'}</td>"
            f"<td><code>{escape(str((p.get('schema') or {}).get('type', '')))}</code></td>"
            f"<td>{escape(str(p.get('description', '')))}</td>"
            "</tr>"
            for p in parameters
        )
        out.append(
            "<h2>Parameters</h2><table><thead><tr><th>Name</th><th>In</th><th>Required</th>"
            f"<th>Type</th><th>Description</th></tr></thead><tbody>{rows}</tbody></table>"
        )
    body = operation.get("requestBody")
    if isinstance(body, Mapping):
        out.append("<h2>Request body</h2>")
        if isinstance(body.get("description"), str):
            out.append(_md(body["description"]))
        out.append(_schema_block(body.get("content")))
    responses = operation.get("responses")
    if isinstance(responses, Mapping) and responses:
        out.append("<h2>Responses</h2>")
        for status, response in responses.items():
            response = response if isinstance(response, Mapping) else {}
            out.append(
                f"<h3><code>{escape(str(status))}</code> {escape(str(response.get('description', '')))}</h3>"
            )
            out.append(_schema_block(response.get("content")))
    return "".join(out)


def operation_entries(contract: Contract, repo: Path) -> list[Entry]:
    entries: list[Entry] = []
    for spec in contract.openapi:
        try:
            document = yaml.safe_load((repo / spec.spec).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            raise BuildError(f"{spec.spec} cannot be read as YAML or JSON") from exc
        if not isinstance(document, Mapping) or not str(document.get("openapi", "")).startswith("3."):
            raise BuildError(f"{spec.spec}: only OpenAPI 3.0 and 3.1 are read")
        info = document.get("info") if isinstance(document.get("info"), Mapping) else {}
        spec_title = info.get("title") if isinstance(info.get("title"), str) else posixpath.basename(spec.spec)
        seen: set[str] = set()
        paths = document.get("paths") or {}
        if not isinstance(paths, Mapping):
            raise BuildError(f"{spec.spec}: its paths are not a mapping")
        for route, raw_item in paths.items():
            item = _resolve(document, raw_item, spec.spec)
            if not isinstance(item, Mapping):
                raise BuildError(f"{spec.spec}: the path {route} is not a mapping")
            shared = item.get("parameters") if isinstance(item.get("parameters"), list) else []
            for method in METHODS:
                operation = item.get(method)
                if not isinstance(operation, Mapping):
                    continue
                own = operation.get("parameters") if isinstance(operation.get("parameters"), list) else []
                own_keys = {(p.get("name"), p.get("in")) for p in own if isinstance(p, Mapping)}
                merged = [
                    p for p in shared if not (isinstance(p, Mapping) and (p.get("name"), p.get("in")) in own_keys)
                ] + list(own)
                operation = {**operation, "parameters": merged}
                operation_id = operation.get("operationId")
                operation_id = operation_id if isinstance(operation_id, str) and operation_id.strip() else None
                if operation_id is not None:
                    if operation_id in seen:
                        raise BuildError(f"{spec.spec}: the operationId {operation_id} is used twice")
                    seen.add(operation_id)
                key = f"{spec.spec}#{operation_id or f'{method.upper()} {route}'}"
                summary = operation.get("summary")
                summary = summary.strip() if isinstance(summary, str) and summary.strip() else None
                title = summary or f"{method.upper()} {route}"
                if any(operation.get(name) is True for name in spec.unpublished_when):
                    continue
                tags = operation.get("tags")
                tag = tags[0] if isinstance(tags, list) and tags and isinstance(tags[0], str) and tags[0].strip() else None
                rendered = render(spec.url, {"tag": tag, "operationId": operation_id})
                if rendered is None:
                    # Broth leaves it out too (template-value-missing): it has no URL to serve.
                    print(f"skipped {key}: its URL template needs a value it does not have", file=sys.stderr)
                    continue
                entries.append(
                    Entry(
                        source=key,
                        title=title,
                        path=url_path(rendered),
                        body=_operation_body(operation),
                        kind="operation",
                        meta=f"<span class=\"method\">{method.upper()}</span> <code>{escape(str(route))}</code>",
                        folders=tuple(part for part in (spec_title, tag) if part),
                    )
                )
    return entries


# ---- the site ----


def _layout(contract: Contract, title: str, main: str) -> str:
    base = contract.site_path
    return (
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{escape(title)} - Banking.Live docs fixture</title>"
        f"<link rel=\"stylesheet\" href=\"{base}/style.css\"></head><body>"
        f"<header><a href=\"{base}/\">Banking.Live docs fixture</a>"
        "<span>Synthetic docs that Broth's docs source is tested against</span></header>"
        f"<main>{main}</main></body></html>\n"
    )


def _check_conflicts(entries: list[Entry]) -> None:
    by_path: dict[str, list[str]] = {}
    for entry in entries:
        by_path.setdefault(entry.path, []).append(entry.source)
    shared = {path: sources for path, sources in by_path.items() if len(sources) > 1}
    if shared:
        detail = "; ".join(f"{path}: {', '.join(sources)}" for path, sources in sorted(shared.items()))
        raise BuildError(f"files share one URL, so Broth leaves them all out: {detail}")


def build(repo: Path, out: Path) -> list[dict[str, str]]:
    contract = load_contract(repo)
    entries = page_entries(contract, repo) + operation_entries(contract, repo)
    _check_conflicts(entries)
    if any(entry.path == "/" for entry in entries):
        raise BuildError("a page at the site root would replace the site index")
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    manifest: list[dict[str, str]] = []
    for entry in sorted(entries, key=lambda e: e.source):
        target = out / entry.path.strip("/") / "index.html"
        target.parent.mkdir(parents=True, exist_ok=True)
        trail = " / ".join(escape(f) for f in entry.folders)
        meta = f"<p class=\"meta\">{entry.meta}</p>" if entry.meta else ""
        heading = "" if entry.own_heading else f"<h1>{escape(entry.title)}</h1>"
        main = f"<p class=\"trail\">{trail}</p>{heading}{meta}<article>{entry.body}</article>"
        target.write_text(_layout(contract, entry.title, main), encoding="utf-8")
        manifest.append(
            {"url": contract.site + entry.path, "title": entry.title, "source": entry.source, "kind": entry.kind}
        )

    def listing(kind: str) -> str:
        items = "".join(
            f"<li><a href=\"{contract.site_path}{escape(m_entry.path)}\">{escape(m_entry.title)}</a>"
            f"<small>{escape(m_entry.source)}</small></li>"
            for m_entry in sorted(entries, key=lambda e: e.path)
            if m_entry.kind == kind
        )
        return f"<ul class=\"listing\">{items}</ul>" if items else "<p>None.</p>"

    index = (
        "<h1>Banking.Live docs fixture</h1>"
        "<p>Every page and API operation in this repository, at the URL Broth cites for it. "
        "The content is synthetic.</p>"
        f"<h2>Guides</h2>{listing('page')}<h2>API operations</h2>{listing('operation')}"
    )
    (out / "index.html").write_text(_layout(contract, "All pages", index), encoding="utf-8")
    missing = (
        "<h1>Not in the fixture</h1><p>This address is not a page of the Banking.Live docs fixture. "
        f"<a href=\"{contract.site_path}/\">See every page</a>.</p>"
    )
    (out / "404.html").write_text(_layout(contract, "Not found", missing), encoding="utf-8")
    shutil.copyfile(HERE / "style.css", out / "style.css")
    (out / "urls.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return manifest


def main(argv: list[str]) -> int:
    repo = Path(argv[1]) if len(argv) > 1 else HERE.parent
    out = Path(argv[2]) if len(argv) > 2 else repo / "_site"
    try:
        manifest = build(repo, out)
    except BuildError as exc:
        print(f"build refused: {exc}", file=sys.stderr)
        return 1
    for entry in manifest:
        print(entry["url"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
