# df-wiki

A personal offline mirror of the [Dwarf Fortress Wiki](https://dwarffortresswiki.org),
kept here so the wiki is still readable when the site is down.

Every mirrored page is stored twice: exactly as the wiki serves it, and again
as Markdown that GitHub renders in the browser.

```
mirror/wikitext/<Namespace>/<Title>.wiki   exact page source, byte for byte
mirror/html/<Namespace>/<Title>.html       exact rendered HTML, as served
mirror/files/images/...                    image files (optional, see below)
display/<Namespace>/<Title>.md             readable Markdown, links rewritten
state/                                     page index + fetch log (resume data)
```

Namespace folders follow the wiki's own: `Main/`, `DF2014/`, `v0.34/`, `40d/`,
`Template/`, `Category/`, `Utility/`, `Modification/`, `Masterwork/` and so on.
Start browsing at [`display/`](display/) — each folder has a generated
`README.md` listing its pages.

## Crawling

The wiki is small, community-run, and regularly knocked over by AI scrapers.
This crawler is deliberately gentle:

* **one page per HTTP request, one request every 10 seconds** — the whole
  mirror takes days, on purpose;
* one `action=parse` call returns both the wikitext and the rendered HTML, so a
  page is never fetched twice;
* it identifies itself in the `User-Agent` and sets `maxlag=5`, so it backs off
  when the wiki's database is under load;
* HTTP 429/502/503/504 trigger exponential backoff and `Retry-After` is honored;
* everything is resumable — the fetch log in `state/fetched.jsonl` means an
  interrupted run never re-downloads a page it already has.

```sh
./crawl.py enumerate     # list every page to mirror (a few index calls)
./crawl.py fetch --push  # download them, one every 10s; commits as it goes
./crawl.py update        # later: re-fetch only what changed since last run
```

Offline commands, safe to run any time — they never touch the network:

```sh
./crawl.py render        # rebuild display/ from mirror/html/
./crawl.py index         # rebuild the folder READMEs
```

Useful flags: `--delay` (seconds between requests, default 10),
`--commit-every` (default 200), `--limit`, `--push`,
`--namespaces` (for `enumerate`; defaults to the content namespaces and skips
Talk and User pages).

### Images

Image files are not fetched by default — pages link to the live wiki for them.
To pull down every image the mirrored pages reference (at the same 10s pace)
and have `display/` point at the local copies instead:

```sh
./crawl.py images --push   # then:
./crawl.py render
```

## Layout details

* Titles map to paths with `/` subpages becoming folders, and characters that
  are illegal on Windows percent-encoded, so the tree checks out anywhere.
* Titles differing only in letter case (the wiki has several) get a `~2`
  suffix on the later one; `state/index.jsonl` records the exact mapping.
* Links between mirrored pages are rewritten to relative `.md` paths and their
  section anchors re-slugified to GitHub's rules. Links to anything not
  mirrored stay absolute and point back at the live wiki.
* Tables become real Markdown tables when they fit one, and sanitized raw HTML
  when they use row/column spans. GitHub strips inline `style`, so the wiki's
  color-coded tile tables lose their colors but keep their text.

## Provenance

All page content belongs to the Dwarf Fortress Wiki and its contributors, and
is redistributed here under the wiki's own
[copyright terms](https://dwarffortresswiki.org/index.php/Dwarf_Fortress_Wiki:Copyrights).
`crawl.py` and `wikimd.py` are the only hand-written files in this repository.
