"""HTML to Markdown extraction for model-facing fetches.

Agents read Markdown far better than flattened text: heading levels, links,
lists and fenced code survive. This is a dependency-free approximation of the
``turndown``/``readability`` pipeline used by opencode and Crush:

* navigation, footers, forms and scripts are dropped;
* when a ``<main>``/``<article>`` region exists only that region is kept;
* links become ``[text](absolute-url)`` resolved against the page URL;
* code blocks become fenced blocks, tables become Markdown tables.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import urljoin

_SKIP = frozenset({
    "script", "style", "noscript", "svg", "template", "iframe", "head",
    "canvas", "nav", "aside", "footer", "form", "button", "select", "input",
    "textarea", "label",
})
_BLOCK = frozenset({
    "p", "div", "section", "article", "main", "header", "h1", "h2", "h3",
    "h4", "h5", "h6", "pre", "blockquote", "ul", "ol", "tr", "table",
    "figure", "figcaption", "hr",
})
_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
_CONTAINERS = frozenset({"main", "article"})
_WS = re.compile(r"[ \t\f\v\r]+")


class _MarkdownExtractor(HTMLParser):
    def __init__(self, base_url: Optional[str], only_container: bool):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.only_container = only_container
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self._skip_depth = 0
        self._container_depth = 0
        self._in_title = False
        self._pre_depth = 0
        self._link: list[str] = []
        self._table_row: list[str] = []
        self._table_rows = 0
        self._cell: list[str] | None = None
        self._bold = 0
        self._italic = 0
        self._list_depth = 0

    # -- helpers -----------------------------------------------------
    def _active(self) -> bool:
        if self._skip_depth:
            return False
        if self.only_container and self._container_depth == 0:
            return False
        return True

    def _emit(self, text: str) -> None:
        if text and self._active():
            self.parts.append(text)

    # -- parser hooks ------------------------------------------------
    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag in _SKIP:
            self._skip_depth += 1
            return
        if tag in _CONTAINERS:
            self._container_depth += 1
            return
        if tag == "title":
            self._in_title = True
            return
        if tag in {"br"}:
            self._emit("\n")
            return
        if tag == "hr":
            self._emit("\n\n---\n\n")
            return
        if tag in _HEADINGS:
            self._emit("\n\n" + "#" * _HEADINGS[tag] + " ")
            return
        if tag == "pre":
            self._pre_depth += 1
            self._emit("\n\n```\n")
            return
        if tag == "code" and self._pre_depth == 0:
            self._emit("`")
            return
        if tag in {"strong", "b"}:
            self._bold += 1
            self._emit("**")
            return
        if tag in {"em", "i"}:
            self._italic += 1
            self._emit("*")
            return
        if tag == "a":
            href = (attributes.get("href") or "").strip()
            if href and not href.startswith(("javascript:", "mailto:")):
                absolute = urljoin(self.base_url or "", href)
                self._link.append(absolute)
                self._emit("[")
            else:
                self._link.append("")
            return
        if tag == "li":
            self._emit("\n" + "  " * self._list_depth + "- ")
            return
        if tag in {"ul", "ol"}:
            self._list_depth += 1
            self._emit("\n")
            return
        if tag == "tr":
            self._table_row = []
            return
        if tag in {"td", "th"}:
            self._cell = []
            return
        if tag in _BLOCK:
            self._emit("\n\n")

    def handle_startendtag(self, tag, attrs):
        if tag == "br":
            self._emit("\n")
        elif tag == "hr":
            self._emit("\n\n---\n\n")

    def handle_endtag(self, tag):
        if tag in _SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag in _CONTAINERS:
            self._container_depth = max(0, self._container_depth - 1)
            return
        if tag == "title":
            self._in_title = False
            return
        if tag in _HEADINGS:
            self._emit("\n\n")
            return
        if tag == "pre":
            self._pre_depth = max(0, self._pre_depth - 1)
            self._emit("\n```\n\n")
            return
        if tag == "code" and self._pre_depth == 0:
            self._emit("`")
            return
        if tag in {"strong", "b"}:
            self._bold = max(0, self._bold - 1)
            self._emit("**")
            return
        if tag in {"em", "i"}:
            self._italic = max(0, self._italic - 1)
            self._emit("*")
            return
        if tag == "a":
            target = self._link.pop() if self._link else ""
            self._emit(f"]({target})" if target else "")
            if not target:
                self._emit("]")
            return
        if tag in {"td", "th"}:
            if self._cell is not None:
                self._table_row.append(" ".join("".join(self._cell).split()))
                self._cell = None
            return
        if tag == "tr":
            if self._table_row:
                row = "| " + " | ".join(self._table_row) + " |"
                self._emit("\n" + row + "\n")
                self._table_rows += 1
                if self._table_rows == 1:
                    self._emit("| " + " | ".join("---" for _ in self._table_row) + " |\n")
            return
        if tag in {"ul", "ol"}:
            self._list_depth = max(0, self._list_depth - 1)
            self._emit("\n")
            return
        if tag in _BLOCK:
            self._emit("\n\n")

    def handle_data(self, data):
        if self._in_title:
            self.title_parts.append(data)
            return
        if not self._active():
            return
        if self._cell is not None:
            self._cell.append(data)
            return
        if self._pre_depth:
            self.parts.append(data)
            return
        self.parts.append(_WS.sub(" ", data))

    @property
    def title(self) -> str:
        return " ".join("".join(self.title_parts).split())


def _tidy(markdown: str) -> str:
    text = markdown.replace("\xa0", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def html_to_markdown(html: str, base_url: Optional[str] = None) -> str:
    """Convert an HTML document to Markdown, preferring the main content."""
    source = html or ""
    only_container = bool(re.search(r"(?is)<(main|article)[\s>]", source))
    parser = _MarkdownExtractor(base_url, only_container)
    try:
        parser.feed(source)
        parser.close()
    except Exception:
        pass
    body = _tidy("".join(parser.parts))
    title = parser.title
    if title:
        heading = f"# {title}"
        if not body.startswith("# "):
            return f"{heading}\n\n{body}" if body else heading
    return body
