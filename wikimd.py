"""Convert the Dwarf Fortress Wiki's rendered HTML into GitHub-flavored Markdown.

The crawler stores the wiki's own HTML verbatim under ``mirror/html/``; this
module turns that HTML into the browsable ``display/`` tree.  Nothing here
touches the network, so the conversion can be re-run offline (``crawl.py
render``) whenever the rules below get better.

Design notes:

* Tables become GFM pipe tables only when they really fit one (no spans, no
  block content in a cell).  Everything else is emitted as sanitized raw HTML,
  which GitHub renders -- minus the inline ``style`` colors it strips.
* Links into the wiki are rewritten to relative ``.md`` paths when the target
  is part of the mirror, and to absolute wiki URLs when it is not.
* Section fragments are re-slugified to GitHub's anchor rules.
"""

import re
import urllib.parse

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

WIKI = "https://dwarffortresswiki.org"

# Chrome that carries no content once the page is a file on disk.
DROP_SELECTORS = [
    "script",
    "style",
    "link",
    "meta",
    ".mw-editsection",
    "#toc",
    ".toc",
    "#page-quality-rating",
    ".printfooter",
    ".mw-jump-link",
    ".mw-indicators",
    "#siteNotice",
    "#siteSub",
    "#contentSub",
    "#contentSub2",
    "#jump-to-nav",
]

BLOCK_TAGS = {
    "p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "dl", "pre",
    "table", "blockquote", "hr", "center", "section", "figure", "details",
    "address", "fieldset", "form", "li", "dt", "dd", "tr", "td", "th",
}

INLINE_PASSTHROUGH = {"span", "font", "abbr", "cite", "q", "bdi", "time", "data", "ins", "var"}

_WS = re.compile(r"[ \t\r\n ]+")
# Characters that would otherwise start Markdown constructs mid-text.
_ESCAPE = re.compile(r"([\\`*\[\]<>])")  # '|' is escaped only inside tables
_LINE_START = re.compile(r"^(\s*)([#>+\-=]|\d+[.)])(\s)")


def _clean_text(s):
    return _WS.sub(" ", s)


def escape_md(s):
    s = _ESCAPE.sub(r"\\\1", s)
    # Underscores only matter as emphasis at word boundaries.
    s = re.sub(r"(?<![A-Za-z0-9])_", r"\\_", s)
    s = re.sub(r"_(?![A-Za-z0-9])", r"\\_", s)
    return s


def escape_line_start(line):
    return _LINE_START.sub(lambda m: m.group(1) + "\\" + m.group(2) + m.group(3), line)


def github_slug(text):
    """GitHub's heading-anchor rule: lowercase, drop punctuation, spaces to '-'."""
    s = text.strip().lower()
    s = s.replace("_", " ")
    s = re.sub(r"[^\w\s-]", "", s, flags=re.UNICODE)
    s = re.sub(r"[\s]+", "-", s)
    return s


def normalize_title(raw):
    """Turn a URL title fragment into the wiki's canonical title form."""
    t = urllib.parse.unquote(raw).replace("_", " ").strip()
    if not t:
        return t
    # MediaWiki upper-cases the first letter of the title (after any namespace).
    if ":" in t:
        ns, _, rest = t.partition(":")
        rest = rest[:1].upper() + rest[1:]
        return f"{ns}:{rest}"
    return t[:1].upper() + t[1:]


class Converter:
    """Renders one page.  ``link_target`` and ``image_target`` do path lookup."""

    def __init__(self, link_target=None, image_target=None):
        self.link_target = link_target or (lambda title, frag: None)
        self.image_target = image_target or (lambda src: None)

    # ------------------------------------------------------------------ urls

    def resolve_href(self, href):
        if not href:
            return None
        if href.startswith("#"):
            return "#" + github_slug(href[1:])
        if href.startswith("//"):
            href = "https:" + href
        parsed = urllib.parse.urlsplit(href)
        if parsed.scheme in ("http", "https") and parsed.netloc not in (
            "dwarffortresswiki.org",
            "www.dwarffortresswiki.org",
        ):
            return href
        path, frag = parsed.path, parsed.fragment
        title = None
        if path.startswith("/index.php/"):
            title = path[len("/index.php/"):]
        elif path in ("/index.php", "/") and parsed.query:
            qs = urllib.parse.parse_qs(parsed.query)
            # Anything with an action= is a live-wiki operation, not a page.
            if set(qs) - {"title"}:
                title = None
            elif "title" in qs:
                title = qs["title"][0]
        if title is None:
            return urllib.parse.urljoin(WIKI, href)
        title = normalize_title(title)
        local = self.link_target(title, github_slug(frag) if frag else "")
        if local:
            return local
        url = f"{WIKI}/index.php/{urllib.parse.quote(title.replace(' ', '_'), safe=':/')}"
        return url + ("#" + frag if frag else "")

    def resolve_src(self, src):
        if not src:
            return None
        if src.startswith("//"):
            src = "https:" + src
        local = self.image_target(src)
        if local:
            return local
        return urllib.parse.urljoin(WIKI, src)

    # --------------------------------------------------------------- inline

    def inline(self, node):
        if isinstance(node, NavigableString):
            return escape_md(_clean_text(str(node)))
        if not isinstance(node, Tag):
            return ""
        name = node.name
        if name == "br":
            return "<br>"
        if name == "img":
            url = self.resolve_src(node.get("src"))
            alt = _clean_text(node.get("alt") or "").replace("]", ")")
            return f"![{alt}]({url})" if url else alt
        if name == "a":
            inner = self.inline_children(node).strip()
            url = self.resolve_href(node.get("href"))
            if not url:
                return inner
            if not inner:
                inner = url
            return f"[{inner}]({url.replace(' ', '%20')})"
        if name in ("b", "strong"):
            inner = self.inline_children(node).strip()
            return f"**{inner}**" if inner else ""
        if name in ("i", "em"):
            inner = self.inline_children(node).strip()
            return f"*{inner}*" if inner else ""
        if name in ("code", "tt", "kbd", "samp"):
            inner = node.get_text()
            inner = _clean_text(inner)
            if not inner:
                return ""
            fence = "`"
            while fence in inner:
                fence += "`"
            return f"{fence}{inner}{fence}"
        if name in ("s", "strike", "del"):
            inner = self.inline_children(node).strip()
            return f"~~{inner}~~" if inner else ""
        if name in ("sup", "sub"):
            inner = self.inline_children(node).strip()
            return f"<{name}>{inner}</{name}>" if inner else ""
        if name in ("small", "big", "u"):
            return self.inline_children(node)
        if name in INLINE_PASSTHROUGH:
            return self.inline_children(node)
        # A block tag met in inline position: flatten it.
        return " ".join(b for b in self.blocks(node) if b)

    def inline_children(self, node):
        return "".join(self.inline(c) for c in node.children)

    # --------------------------------------------------------------- blocks

    def blocks(self, node):
        """Render a node's children as a list of Markdown block strings."""
        out = []
        buf = []

        def flush():
            if buf:
                text = "".join(buf).strip()
                buf.clear()
                if text:
                    out.append(text)

        for child in node.children:
            if isinstance(child, Tag) and child.name in BLOCK_TAGS:
                flush()
                out.extend(self.block(child))
            else:
                buf.append(self.inline(child))
        flush()
        return [b for b in out if b.strip()]

    def block(self, el):
        name = el.name
        if name == "hr":
            return ["---"]
        if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
            level = min(int(name[1]) + 1, 6)
            text = self.inline_children(el).strip()
            return [f"{'#' * level} {text}"] if text else []
        if name in ("ul", "ol"):
            return self.list_block(el, ordered=(name == "ol"))
        if name == "dl":
            return self.definition_list(el)
        if name == "pre":
            text = el.get_text()
            fence = "```"
            while fence in text:
                fence += "`"
            return [f"{fence}\n{text.rstrip()}\n{fence}"]
        if name == "table":
            return self.table(el)
        if name == "blockquote":
            inner = self.blocks(el)
            body = "\n\n".join(inner)
            return ["\n".join("> " + ln if ln else ">" for ln in body.split("\n"))]
        if name == "figure":
            return self.blocks(el)
        if name == "details":
            summary = el.find("summary")
            title = self.inline_children(summary).strip() if summary else "Details"
            if summary:
                summary.extract()
            body = "\n\n".join(self.blocks(el))
            return [f"<details><summary>{title}</summary>\n\n{body}\n\n</details>"]
        # p, div, center, li/td met out of context, ...
        return self.blocks(el)

    def list_block(self, el, ordered):
        lines = []
        index = 0
        for li in el.find_all("li", recursive=False):
            index += 1
            marker = f"{index}. " if ordered else "- "
            pad = " " * len(marker)
            nested = []
            for sub in li.find_all(["ul", "ol"], recursive=False):
                nested.append(sub.extract())
            body = "\n\n".join(self.blocks(li)).strip()
            if not body and not nested:
                continue
            body_lines = body.split("\n") if body else [""]
            lines.append(marker + escape_line_start(body_lines[0]))
            lines.extend(pad + ln for ln in body_lines[1:])
            for sub in nested:
                for chunk in self.list_block(sub, sub.name == "ol"):
                    lines.extend(pad + ln for ln in chunk.split("\n"))
        return ["\n".join(lines)] if lines else []

    def definition_list(self, el):
        lines = []
        for child in el.find_all(["dt", "dd"], recursive=False):
            body = "\n\n".join(self.blocks(child)).strip()
            if not body:
                continue
            if child.name == "dt":
                lines.append(f"**{body}**" if "\n" not in body else body)
            else:
                lines.extend("> " + ln if ln else ">" for ln in body.split("\n"))
        return ["\n".join(lines)] if lines else []

    # --------------------------------------------------------------- tables

    def table(self, el):
        rows = []
        simple = True
        for tr in el.find_all("tr"):
            # Skip rows belonging to a nested table; find_all is recursive.
            if tr.find_parent("table") is not el:
                continue
            cells = []
            for cell in tr.find_all(["td", "th"], recursive=False):
                if int(cell.get("colspan", 1) or 1) != 1 or int(cell.get("rowspan", 1) or 1) != 1:
                    simple = False
                if cell.find(["table", "ul", "ol", "dl", "pre", "p", "div"]):
                    simple = False
                text = self.inline_children(cell).strip().replace("|", "\\|")
                if "\n" in text:
                    simple = False
                cells.append((cell.name, text))
            if cells:
                rows.append(cells)
        if not rows:
            return []
        width = len(rows[0])
        if any(len(r) != width for r in rows) or width == 0:
            simple = False
        if simple and width > 0:
            head = rows[0]
            if all(kind == "th" for kind, _ in head):
                body = rows[1:]
                header = [t for _, t in head]
            else:
                body = rows
                header = [""] * width
            out = ["| " + " | ".join(header) + " |",
                   "| " + " | ".join(["---"] * width) + " |"]
            for row in body:
                out.append("| " + " | ".join(t for _, t in row) + " |")
            caption = el.find("caption")
            prefix = []
            if caption:
                cap = self.inline_children(caption).strip()
                if cap:
                    prefix = [f"**{cap}**"]
            return prefix + ["\n".join(out)]
        return [self.raw_table(el)]

    KEEP_ATTRS = {"colspan", "rowspan", "align", "valign", "scope"}

    def raw_table(self, el):
        """Emit a span-bearing table as HTML GitHub will still render."""
        copy = BeautifulSoup(str(el), "lxml").find("table")
        for tag in [copy] + copy.find_all(True):
            if tag.name == "a":
                href = self.resolve_href(tag.get("href"))
                tag.attrs = {"href": href} if href else {}
                continue
            if tag.name == "img":
                src = self.resolve_src(tag.get("src"))
                attrs = {"src": src} if src else {}
                if tag.get("alt"):
                    attrs["alt"] = tag["alt"]
                for dim in ("width", "height"):
                    if tag.get(dim):
                        attrs[dim] = tag[dim]
                tag.attrs = attrs
                continue
            tag.attrs = {k: v for k, v in tag.attrs.items() if k in self.KEEP_ATTRS}
        # Styling wrappers lose their meaning once GitHub strips style=.
        for tag in copy.find_all(["span", "font"]):
            if not tag.attrs:
                tag.unwrap()
        return str(copy)

    # ----------------------------------------------------------------- page

    def convert(self, html):
        soup = BeautifulSoup(html, "lxml")
        for sel in DROP_SELECTORS:
            for tag in soup.select(sel):
                tag.decompose()
        for comment in soup.find_all(string=lambda s: isinstance(s, Comment)):
            comment.extract()
        root = soup.find("div", class_="mw-parser-output") or soup.find("body") or soup
        blocks = self.blocks(root)
        text = "\n\n".join(blocks)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip() + "\n"
