#!/usr/bin/env python3
"""Polite offline mirror of the Dwarf Fortress Wiki.

One page per request, one request every ``--delay`` seconds (10 by default),
resumable, and it never re-fetches a page it already has.  The wiki is small
and frequently knocked over by impolite crawlers; this is deliberately slower
than it needs to be.

    ./crawl.py enumerate          # list every page we intend to mirror
    ./crawl.py fetch              # download them, one every 10s (resumable)
    ./crawl.py images             # optional: the image files pages link to
    ./crawl.py render             # rebuild display/ from mirror/, no network
    ./crawl.py index              # rebuild the README indexes, no network
    ./crawl.py update             # re-fetch only what changed since last run

Each fetched page lands in three places:

    mirror/wikitext/<Namespace>/<Title>.wiki   exact wikitext, as served
    mirror/html/<Namespace>/<Title>.html       exact rendered HTML, as served
    display/<Namespace>/<Title>.md             GitHub-readable Markdown

``state/`` holds the page index and the fetch log, so an interrupted run picks
up where it stopped.
"""

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timezone

import requests

from wikimd import WIKI, Converter

API = f"{WIKI}/api.php"
ROOT = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(ROOT, "state")
WIKITEXT_DIR = os.path.join(ROOT, "mirror", "wikitext")
HTML_DIR = os.path.join(ROOT, "mirror", "html")
FILES_DIR = os.path.join(ROOT, "mirror", "files")
DISPLAY_DIR = os.path.join(ROOT, "display")

INDEX = os.path.join(STATE, "index.jsonl")
FETCHED = os.path.join(STATE, "fetched.jsonl")
IMAGES = os.path.join(STATE, "images.txt")
IMAGES_DONE = os.path.join(STATE, "images_fetched.txt")
META = os.path.join(STATE, "meta.json")

USER_AGENT = (
    "df-wiki-mirror/1.0 (personal offline mirror of dwarffortresswiki.org; "
    "one page per 10s; https://github.com/magnus-ISU/df-wiki)"
)

# Content namespaces.  Talk/User pages are conversation, not reference, and
# they triple the crawl; pass --namespaces to include them anyway.
DEFAULT_NAMESPACES = [0, 4, 10, 12, 14, 100, 102, 104, 116, 200, 828, 1000]

# Everything the wiki has, for a wider re-run:
#   6 File, 106 40d, 108 Unused, 110 23a, 112 v0.31, 114 v0.34
ALL_NAMESPACES = [0, 4, 6, 10, 12, 14, 100, 102, 104, 106, 108, 110, 112,
                  114, 116, 200, 828, 1000]

# Fetch order: current reference material first, so the mirror is useful long
# before the crawl finishes.
NS_PRIORITY = [0, 116, 102, 104, 1000, 200, 12, 100, 4, 14, 10, 828,
               106, 114, 112, 110, 6, 108]

stop = False


class PageError(Exception):
    """One page could not be fetched; the crawl carries on without it."""


def _on_signal(signum, frame):
    global stop
    stop = True
    log("signal received - stopping after the page in flight")


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# --------------------------------------------------------------------- paths

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"con", "prn", "aux", "nul", "clock$"} | {
    f"{p}{i}" for p in ("com", "lpt") for i in range(1, 10)
}


def safe_component(part):
    """A filename that survives Linux, Windows and GitHub's checkout rules."""
    part = _UNSAFE.sub(lambda m: "%%%02X" % ord(m.group()), part)
    part = part.replace(" ", "_")
    if part.split(".")[0].lower() in _RESERVED:
        part = "%" + part
    part = re.sub(r"[. ]+$", lambda m: "%2E" * len(m.group()), part)
    if len(part.encode()) > 120:
        keep = part.encode()[:100].decode(errors="ignore")
        part = keep + "~" + "%04x" % (hash(part) & 0xFFFF)
    return part or "_"


def title_to_relpath(title, ns, ns_names):
    """'DF2014:Cat' -> 'DF2014/Cat';  'Foo/Bar' (ns 0) -> 'Main/Foo/Bar'."""
    ns_name = ns_names.get(str(ns), "") or ns_names.get(ns, "")
    if ns and title.startswith(ns_name + ":"):
        rest = title[len(ns_name) + 1:]
    else:
        rest = title
    folder = safe_component(ns_name) if ns else "Main"
    parts = [safe_component(p) for p in rest.split("/") if p != ""]
    return "/".join([folder] + (parts or ["_"]))


# ----------------------------------------------------------------------- api

class Api:
    def __init__(self, delay, timeout=60):
        self.delay = delay
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.last = 0.0

    def wait(self):
        gap = self.delay - (time.monotonic() - self.last)
        while gap > 0 and not stop:
            time.sleep(min(gap, 1.0))
            gap = self.delay - (time.monotonic() - self.last)

    # The wiki's front end sporadically 404s (or 500s) a request that works a
    # few seconds later; that is flakiness, not a refusal, so retry it quickly.
    # Its Varnish caches those errors against the exact URL, so any repeat of
    # the same URL re-serves the cached failure -- hence the requestid below.
    # 429/503 are the server actually asking for room, so back off hard.
    SOFT = {403, 404, 500, 502, 504}
    HARD = {429, 503}

    def get(self, params, tries=8):
        """One API call, with backoff.  Returns parsed JSON or raises."""
        params = dict(params, format="json", formatversion="2", maxlag="5")
        backoff = 5
        for attempt in range(1, tries + 1):
            self.wait()
            self.last = time.monotonic()
            # Vary the URL so Varnish cannot hand back a cached error.  Each
            # page is requested exactly once in a crawl, so a cache hit could
            # only ever be somebody else's identical query -- there is nothing
            # to lose here, and a cached 404 costs a whole retry.
            params["requestid"] = "df-wiki-%d" % (time.time_ns() // 1000)
            try:
                r = self.session.get(API, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                wait = max(backoff, 15)
                log(f"  network error ({exc.__class__.__name__}); retry in {wait}s")
                self._sleep(wait)
                backoff = min(backoff * 2, 300)
                continue
            if r.status_code in self.SOFT or r.status_code in self.HARD:
                if r.status_code in self.HARD:
                    wait = int(r.headers.get("Retry-After") or max(backoff, 60))
                    backoff = min(max(backoff * 2, 120), 900)
                else:
                    wait = backoff
                    backoff = min(backoff * 2, 120)
                log(f"  HTTP {r.status_code}; retry in {wait}s")
                self._sleep(wait)
                continue
            if r.status_code != 200:
                raise PageError(f"HTTP {r.status_code} for {params.get('page') or params}")
            try:
                data = r.json()
            except ValueError:
                log(f"  non-JSON reply ({len(r.content)} bytes); retry in {backoff}s")
                self._sleep(backoff)
                backoff = min(backoff * 2, 120)
                continue
            if isinstance(data.get("error"), dict) and data["error"].get("code") == "maxlag":
                log("  wiki is lagging; retry in 60s")
                self._sleep(60)
                continue
            return data
        raise PageError(f"gave up after {tries} failures")

    def _sleep(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end and not stop:
            time.sleep(min(1.0, end - time.monotonic()))

    def download(self, url, dest):
        self.wait()
        self.last = time.monotonic()
        r = self.session.get(url, timeout=self.timeout, stream=True)
        if r.status_code != 200:
            return r.status_code
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = dest + ".part"
        with open(tmp, "wb") as fh:
            for chunk in r.iter_content(65536):
                fh.write(chunk)
        os.replace(tmp, dest)
        return 200


# --------------------------------------------------------------------- state

def read_jsonl(path):
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def append_jsonl(path, record):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def load_meta():
    if os.path.exists(META):
        with open(META, encoding="utf-8") as fh:
            return json.load(fh)
    return {}


def save_meta(meta):
    os.makedirs(STATE, exist_ok=True)
    with open(META, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2, sort_keys=True)
        fh.write("\n")


def write_file(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


# ------------------------------------------------------------------ git sync

def git(*args, check=False):
    return subprocess.run(["git", "-C", ROOT, *args], check=check,
                          capture_output=True, text=True)


def sync(message, push):
    if git("status", "--porcelain", "--", "mirror", "display", "state").stdout.strip() == "":
        return
    # Only the crawl's own output; a source edit in flight is not ours to commit.
    git("add", "--", "mirror", "display", "state")
    res = git("commit", "-m", message)
    if res.returncode != 0 and "nothing to commit" not in res.stdout:
        log(f"  git commit failed: {res.stdout.strip() or res.stderr.strip()}")
        return
    log(f"  committed: {message}")
    if push:
        res = git("push")
        if res.returncode != 0:
            log(f"  git push failed (will retry next time): {res.stderr.strip().splitlines()[-1:]}")


# ----------------------------------------------------------------- enumerate

def cmd_enumerate(args):
    api = Api(args.delay if args.delay is not None else 2.0)
    info = api.get({"action": "query", "meta": "siteinfo",
                    "siprop": "general|namespaces|statistics"})["query"]
    ns_names = {str(k): (v.get("name") or "") for k, v in info["namespaces"].items()}
    namespaces = args.namespaces or DEFAULT_NAMESPACES

    pages = []
    for ns in namespaces:
        cont = {}
        count = 0
        while True:
            params = {"action": "query", "list": "allpages", "apnamespace": ns,
                      "aplimit": "500", "apfilterredir": "all"}
            params.update(cont)
            data = api.get(params)
            for page in data.get("query", {}).get("allpages", []):
                pages.append({"t": page["title"], "ns": ns, "id": page["pageid"]})
                count += 1
            cont = data.get("continue") or {}
            if not cont or stop:
                break
        log(f"namespace {ns} ({ns_names.get(str(ns)) or 'Main'}): {count} pages")
        if stop:
            break

    pages.sort(key=lambda p: (p["ns"], p["t"]))
    used = {}
    for page in pages:
        base = title_to_relpath(page["t"], page["ns"], ns_names)
        key = base.lower()
        if key in used and used[key] != page["t"]:
            # Titles differing only in case would collide on a case-insensitive
            # checkout; disambiguate deterministically.
            n = 2
            while f"{base}~{n}".lower() in used:
                n += 1
            base = f"{base}~{n}"
            key = base.lower()
        used[key] = page["t"]
        page["p"] = base

    os.makedirs(STATE, exist_ok=True)
    with open(INDEX, "w", encoding="utf-8") as fh:
        for page in pages:
            fh.write(json.dumps(page, ensure_ascii=False, sort_keys=True) + "\n")

    meta = load_meta()
    meta.update({
        "site": info["general"]["sitename"],
        "generator": info["general"]["generator"],
        "statistics": info["statistics"],
        "namespaces": {str(ns): ns_names.get(str(ns), "") for ns in namespaces},
        "ns_names": ns_names,
        "enumerated_at": now_iso(),
        "page_count": len(pages),
    })
    save_meta(meta)
    log(f"indexed {len(pages)} pages across {len(namespaces)} namespaces")
    sync(f"state: index {len(pages)} pages", args.push)


# --------------------------------------------------------------------- fetch

class Renderer:
    """Turns one stored page into its display/ Markdown file."""

    def __init__(self, pages):
        self.by_title = {p["t"]: p["p"] for p in pages}
        # Redirects and links often point at a title's non-canonical casing.
        self.by_key = {p["t"].lower(): p["p"] for p in pages}

    def lookup(self, title):
        return self.by_title.get(title) or self.by_key.get(title.lower())

    def render(self, page, html, revid, categories):
        relpath = page["p"]
        here = os.path.dirname(os.path.join(DISPLAY_DIR, relpath + ".md"))

        def link_target(title, frag):
            target = self.lookup(title)
            if not target:
                return None
            rel = os.path.relpath(os.path.join(DISPLAY_DIR, target + ".md"), here)
            rel = urllib.parse.quote(rel, safe="/._-~!$&'()*+,;=:@")
            return rel + ("#" + frag if frag else "")

        def image_target(src):
            path = urllib.parse.urlsplit(src).path
            if not path.startswith("/images/"):
                return None
            local = os.path.join(FILES_DIR, path.lstrip("/"))
            if not os.path.exists(local):
                return None
            rel = os.path.relpath(local, here)
            return urllib.parse.quote(rel, safe="/._-~!$&'()*+,;=:@")

        body = Converter(link_target, image_target).convert(html)
        url = f"{WIKI}/index.php/{urllib.parse.quote(page['t'].replace(' ', '_'), safe=':/')}"
        depth = relpath.count("/") + 1
        up = "../" * depth
        wikitext_link = urllib.parse.quote(
            f"{up}mirror/wikitext/{relpath}.wiki", safe="/._-~!$&'()*+,;=:@")
        head = [
            f"<!-- Mirrored from {url} (revision {revid}).",
            "     Generated by crawl.py - edit the wiki, not this file. -->",
            "",
            f"# {page['t']}",
            "",
            f"*[Source]({url}) &middot; revision {revid} &middot; retrieved {now_iso()[:10]}"
            f" &middot; [wikitext]({wikitext_link})*",
            "",
            "---",
            "",
        ]
        foot = []
        if categories:
            cats = []
            for cat in categories:
                name = cat if isinstance(cat, str) else cat.get("category", "")
                name = name.replace("_", " ")
                target = link_target(f"Category:{name}", "")
                cats.append(f"[{name}]({target})" if target else name)
            foot = ["", "---", "", "**Categories:** " + " &middot; ".join(cats)]
        foot += [
            "",
            "---",
            "",
            f"*Mirror of the [Dwarf Fortress Wiki]({WIKI}); content is under the wiki's "
            f"[copyright terms]({WIKI}/index.php/Dwarf_Fortress_Wiki:Copyrights).*",
            "",
        ]
        return "\n".join(head) + body + "\n".join(foot)


IMG_SRC = re.compile(r'<img[^>]+src="([^"]+)"')


def cmd_fetch(args):
    pages = read_jsonl(INDEX)
    if not pages:
        sys.exit("no state/index.jsonl - run `./crawl.py enumerate` first")
    done = {r["t"] for r in read_jsonl(FETCHED)} if not args.force else set()
    stored = sum(1 for p in pages if p["t"] in done)
    todo = [p for p in pages if p["t"] not in done]
    todo.sort(key=lambda p: (NS_PRIORITY.index(p["ns"]) if p["ns"] in NS_PRIORITY else 99,
                             p["t"].lower()))
    if args.limit:
        todo = todo[:args.limit]
    log(f"{len(pages)} pages indexed, {stored} already stored, "
        f"{len(todo)} to fetch (~{len(todo) * args.delay / 3600:.1f}h at {args.delay:g}s/page)")

    api = Api(args.delay)
    renderer = Renderer(pages)
    images = set()
    if os.path.exists(IMAGES):
        images = {ln.strip() for ln in open(IMAGES, encoding="utf-8") if ln.strip()}
    fetched = 0
    since_commit = 0
    failures = 0

    for page in todo:
        if stop:
            break
        try:
            data = api.get({"action": "parse", "page": page["t"],
                            "prop": "wikitext|text|revid|categories",
                            "disablelimitreport": "1", "disableeditsection": "1",
                            "disabletoc": "1"})
        except PageError as exc:
            log(f"! {page['t']}: {exc}")
            failures += 1
            if failures >= 20:
                log("20 pages in a row failed - the wiki looks down; stopping")
                break
            continue
        failures = 0
        if "error" in data:
            code = data["error"].get("code")
            log(f"! {page['t']}: api error {code}")
            append_jsonl(FETCHED, {"t": page["t"], "error": code, "at": now_iso()})
            continue
        parse = data["parse"]
        html = parse.get("text") or ""
        wikitext = parse.get("wikitext") or ""
        revid = parse.get("revid", 0)

        write_file(os.path.join(WIKITEXT_DIR, page["p"] + ".wiki"), wikitext)
        write_file(os.path.join(HTML_DIR, page["p"] + ".html"), html)
        write_file(os.path.join(DISPLAY_DIR, page["p"] + ".md"),
                   renderer.render(page, html, revid, parse.get("categories") or []))
        for src in IMG_SRC.findall(html):
            if src.startswith("//"):
                src = "https:" + src
            if urllib.parse.urlsplit(src).path.startswith("/images/"):
                images.add(urllib.parse.urlsplit(src).path)
        cats = [c if isinstance(c, str) else c.get("category", "")
                for c in (parse.get("categories") or [])]
        append_jsonl(FETCHED, {"t": page["t"], "rev": revid, "at": now_iso(), "cats": cats})
        fetched += 1
        since_commit += 1
        log(f"{fetched}/{len(todo)} {page['t']} (rev {revid}, {len(wikitext)} bytes)")

        if since_commit >= args.commit_every:
            write_file(IMAGES, "\n".join(sorted(images)) + "\n")
            build_indexes(pages)
            sync(f"mirror: {fetched} pages this run (through {page['t']})", args.push)
            since_commit = 0

    if images:
        write_file(IMAGES, "\n".join(sorted(images)) + "\n")
    meta = load_meta()
    meta["last_fetch"] = now_iso()
    meta["fetched_pages"] = len(read_jsonl(FETCHED))
    save_meta(meta)
    build_indexes(pages)
    sync(f"mirror: {fetched} pages this run", args.push)
    log(f"done: {fetched} pages this run")


# -------------------------------------------------------------------- images

def cmd_images(args):
    if not os.path.exists(IMAGES):
        sys.exit("no state/images.txt - fetch some pages first")
    wanted = [ln.strip() for ln in open(IMAGES, encoding="utf-8") if ln.strip()]
    done = set()
    if os.path.exists(IMAGES_DONE):
        done = {ln.strip() for ln in open(IMAGES_DONE, encoding="utf-8") if ln.strip()}
    todo = [p for p in wanted if p not in done]
    if args.limit:
        todo = todo[:args.limit]
    log(f"{len(wanted)} images referenced, {len(todo)} to download "
        f"(~{len(todo) * args.delay / 3600:.1f}h at {args.delay:g}s each)")
    api = Api(args.delay)
    got = 0
    for path in todo:
        if stop:
            break
        dest = os.path.join(FILES_DIR, path.lstrip("/"))
        url = WIKI + urllib.parse.quote(path, safe="/%:")
        try:
            code = api.download(url, dest)
        except requests.RequestException as exc:
            log(f"! {path}: {exc.__class__.__name__}")
            continue
        with open(IMAGES_DONE, "a", encoding="utf-8") as fh:
            fh.write(path + "\n")
        got += 1
        log(f"{got}/{len(todo)} {path} ({'ok' if code == 200 else code})")
        if got % args.commit_every == 0:
            sync(f"mirror: {got} image files this run", args.push)
    sync(f"mirror: {got} image files this run", args.push)
    log(f"done: {got} files this run")


# -------------------------------------------------------------------- render

def cmd_render(args):
    pages = read_jsonl(INDEX)
    renderer = Renderer(pages)
    fetch_log = {r["t"]: r for r in read_jsonl(FETCHED)}
    count = 0
    for page in pages:
        src = os.path.join(HTML_DIR, page["p"] + ".html")
        if not os.path.exists(src):
            continue
        with open(src, encoding="utf-8") as fh:
            html = fh.read()
        record = fetch_log.get(page["t"], {})
        write_file(os.path.join(DISPLAY_DIR, page["p"] + ".md"),
                   renderer.render(page, html, record.get("rev", 0), record.get("cats") or []))
        count += 1
        if count % 500 == 0:
            log(f"rendered {count}")
    build_indexes(pages)
    log(f"rendered {count} pages")
    sync(f"display: re-render {count} pages", args.push)


# ------------------------------------------------------------------- indexes

def build_indexes(pages):
    """A README.md in each display/ folder, so the tree is browsable."""
    stored = {p["p"] for p in pages
              if os.path.exists(os.path.join(DISPLAY_DIR, p["p"] + ".md"))}
    tree = {}
    for page in pages:
        if page["p"] not in stored:
            continue
        folder, _, name = page["p"].rpartition("/")
        tree.setdefault(folder, {"pages": [], "subs": set()})["pages"].append((page["t"], name))
        while folder:
            parent, _, leaf = folder.rpartition("/")
            tree.setdefault(parent, {"pages": [], "subs": set()})["subs"].add(leaf)
            if not parent:
                break
            folder = parent

    meta = load_meta()
    for folder, node in tree.items():
        lines = [f"# {folder or 'Dwarf Fortress Wiki mirror'}", ""]
        if not folder:
            lines += [
                "Browsable Markdown rendering of the mirrored wiki. The exact",
                "source of every page is under [`mirror/`](../mirror).", "",
            ]
        if node["subs"]:
            lines.append("## Sections")
            lines.append("")
            lines += [f"- [{s}]({urllib.parse.quote(s)}/)" for s in sorted(node["subs"])]
            lines.append("")
        if node["pages"]:
            lines.append(f"## Pages ({len(node['pages'])})")
            lines.append("")
            for title, name in sorted(node["pages"], key=lambda x: x[0].lower()):
                href = urllib.parse.quote(name + ".md", safe="._-~!$&'()*+,;=:@")
                lines.append(f"- [{title}]({href})")
            lines.append("")
        if not folder and meta.get("enumerated_at"):
            lines += [f"*Index generated {now_iso()[:10]}; "
                      f"{len(stored)} of {meta.get('page_count', '?')} pages stored.*", ""]
        write_file(os.path.join(DISPLAY_DIR, folder, "README.md"), "\n".join(lines))


def cmd_index(args):
    pages = read_jsonl(INDEX)
    build_indexes(pages)
    log("indexes rebuilt")
    sync("display: rebuild indexes", args.push)


# -------------------------------------------------------------------- update

def cmd_update(args):
    """Re-fetch pages that changed since the last crawl, then any new ones."""
    pages = read_jsonl(INDEX)
    if not pages:
        sys.exit("no state/index.jsonl - run `./crawl.py enumerate` first")
    known = {p["t"]: p for p in pages}
    meta = load_meta()
    since = args.since or meta.get("last_update") or meta.get("last_fetch")
    if not since:
        sys.exit("nothing to update from - run a full fetch first")
    api = Api(args.delay if args.delay is not None else 2.0)
    changed, cont = set(), {}
    while True:
        params = {"action": "query", "list": "recentchanges", "rclimit": "500",
                  "rcdir": "newer", "rcstart": since, "rcprop": "title|ids",
                  "rcnamespace": "|".join(str(n) for n in sorted({p["ns"] for p in pages}))}
        params.update(cont)
        data = api.get(params)
        for change in data.get("query", {}).get("recentchanges", []):
            changed.add(change["title"])
        cont = data.get("continue") or {}
        if not cont or stop:
            break
    log(f"{len(changed)} pages changed since {since}")
    fetched = {r["t"] for r in read_jsonl(FETCHED)}
    todo = [known[t] for t in sorted(changed) if t in known]
    todo += [p for p in pages if p["t"] not in fetched and p["t"] not in changed]
    if not todo:
        log("nothing to do")
        return
    # Re-run the fetch loop over just these pages.
    with open(FETCHED + ".tmp", "w", encoding="utf-8") as fh:
        for record in read_jsonl(FETCHED):
            if record["t"] not in changed:
                fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    os.replace(FETCHED + ".tmp", FETCHED)
    meta["last_update"] = now_iso()
    save_meta(meta)
    args.delay = args.page_delay
    args.force = False
    args.limit = 0
    cmd_fetch(args)


# ----------------------------------------------------------------------- cli

def main():
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    def common(p, delay=10.0):
        p.add_argument("--delay", type=float, default=delay,
                       help="seconds between requests (default %(default)s)")
        p.add_argument("--commit-every", type=int, default=200,
                       help="git commit after this many items (default %(default)s)")
        p.add_argument("--push", action="store_true", help="git push after each commit")
        p.add_argument("--limit", type=int, default=0, help="stop after N items")

    p = sub.add_parser("enumerate", help="build the page index")
    p.add_argument("--namespaces", type=int, nargs="*", default=None,
                   help=f"namespace ids (default: {DEFAULT_NAMESPACES})")
    common(p, delay=2.0)
    p.set_defaults(func=cmd_enumerate)

    p = sub.add_parser("fetch", help="download indexed pages")
    p.add_argument("--force", action="store_true", help="re-fetch pages already stored")
    common(p)
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("images", help="download referenced image files")
    common(p)
    p.set_defaults(func=cmd_images)

    p = sub.add_parser("render", help="rebuild display/ from mirror/ (offline)")
    common(p)
    p.set_defaults(func=cmd_render)

    p = sub.add_parser("index", help="rebuild folder READMEs (offline)")
    common(p)
    p.set_defaults(func=cmd_index)

    p = sub.add_parser("update", help="re-fetch what changed since the last run")
    p.add_argument("--since", help="ISO timestamp to diff from")
    p.add_argument("--page-delay", type=float, default=10.0,
                   help="seconds between page fetches (default %(default)s)")
    common(p, delay=2.0)
    p.set_defaults(func=cmd_update)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
