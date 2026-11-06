"""Template rendering: sandboxed Jinja, minimal Markdown, HTML to PDF."""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Any

from jinja2 import FileSystemLoader, StrictUndefined, TemplateError, UndefinedError
from jinja2.sandbox import SandboxedEnvironment

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>{title}</title>
<style>
body {{ font-family: sans-serif; font-size: 11pt; line-height: 1.4; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ border: 1px solid #999; padding: 4px 8px; text-align: left; }}
h1, h2, h3 {{ margin: 1em 0 0.4em; }}
</style></head><body>
{body}
</body></html>
"""


class TemplateFailed(ValueError):
    """The template is invalid or the data does not fill it."""


def _inline(text: str) -> str:
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html.escape(text, quote=False))


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def markdown_to_html(src: str) -> str:
    """Headings, bullet and numbered lists, pipe tables, **bold**, paragraphs. Text is escaped."""
    out: list[str] = []
    lines = src.splitlines()
    i = 0
    para: list[str] = []

    def flush() -> None:
        if para:
            out.append("<p>" + "<br>".join(_inline(p) for p in para) + "</p>")
            para.clear()

    while i < len(lines):
        line = lines[i].rstrip()
        if not line.strip():
            flush()
        elif m := re.match(r"^(#{1,6})\s+(.*)$", line):
            flush()
            out.append(f"<h{len(m[1])}>{_inline(m[2])}</h{len(m[1])}>")
        elif re.match(r"^\s*([-*]|\d+\.)\s+", line):
            flush()
            tag = "ol" if re.match(r"^\s*\d+\.", line) else "ul"
            items = []
            while i < len(lines) and (m := re.match(r"^\s*(?:[-*]|\d+\.)\s+(.*)$", lines[i])):
                items.append(f"<li>{_inline(m[1])}</li>")
                i += 1
            out.append(f"<{tag}>{''.join(items)}</{tag}>")
            continue
        elif (
            line.lstrip().startswith("|")
            and i + 1 < len(lines)
            and re.match(r"^\s*\|?[\s:|-]+\|[\s:|-]*$", lines[i + 1])
        ):
            flush()
            head = "".join(f"<th>{_inline(c)}</th>" for c in _cells(line))
            rows = []
            i += 2
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                rows.append("".join(f"<td>{_inline(c)}</td>" for c in _cells(lines[i])))
                i += 1
            body = "".join(f"<tr>{r}</tr>" for r in rows)
            out.append(f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>")
            continue
        else:
            para.append(line)
        i += 1
    flush()
    return "\n".join(out)


def render_html(template: Path, data: dict[str, Any], title: str = "") -> str:
    """Render a .md/.html Jinja template. Missing fields raise; HTML output is autoescaped.

    Markdown sources render with autoescape off because markdown_to_html escapes the
    result exactly once.
    """
    is_html = template.suffix.lower() in (".html", ".htm")
    env = SandboxedEnvironment(
        loader=FileSystemLoader(str(template.parent)),
        undefined=StrictUndefined,
        autoescape=is_html,
        keep_trailing_newline=True,
    )
    try:
        text = env.get_template(template.name).render(**data)
    except UndefinedError as exc:
        raise TemplateFailed(f"template field missing: {exc.message}") from exc
    except TemplateError as exc:
        raise TemplateFailed(f"template error: {exc}") from exc
    if is_html:
        return text
    return PAGE.format(title=html.escape(title), body=markdown_to_html(text))


def _no_fetch(url: str, *args: Any, **kwargs: Any) -> Any:
    raise ValueError(f"external resource refused: {url[:60]}")


def pdf_available() -> bool:
    try:
        import weasyprint  # noqa: F401
    except (ImportError, OSError):
        return False
    return True


def html_to_pdf(source: str) -> bytes:
    """WeasyPrint, imported lazily (it needs the Pango system library). Never fetches URLs."""
    try:
        import weasyprint
    except (ImportError, OSError) as exc:
        raise RuntimeError(f"PDF rendering unavailable (WeasyPrint/Pango): {exc}") from exc
    return bytes(weasyprint.HTML(string=source, url_fetcher=_no_fetch).write_pdf())
