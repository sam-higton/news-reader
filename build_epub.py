#!/usr/bin/env python3
"""
Daily RSS -> EPUB builder.

Reads feeds.json, collects articles published in the last `windowHours`,
fetches full article text where the feed only carries an excerpt, and
builds an EPUB with a categorized contents page.

Outputs:
  daily.epub                  - the latest edition (repo root)
  archive/YYYY-MM-DD.epub     - dated copy (date in the configured timezone)
  build-report.md             - per-feed fetch status for the last run
  state/seen.json             - article GUIDs already published (dedupe)

Article text is included verbatim - no summarizing or editorializing.
"""

import json
import os
import re
import sys
import html as htmlmod
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
import feedparser
from lxml import html as lxml_html
from lxml_html_clean import Cleaner
from readability import Document
from ebooklib import epub

ROOT = os.path.dirname(os.path.abspath(__file__))
USER_AGENT = (
  "Mozilla/5.0 (X11; Linux x86_64) NewsReaderEpub/1.0 "
  "(personal daily digest; github.com/sam-higton/news-reader)"
)
FETCH_TIMEOUT = 30
FULLTEXT_MIN_CHARS = 600  # feed content shorter than this is treated as an excerpt
SEEN_RETENTION_DAYS = 7

ALLOWED_TAGS = {
  "p", "br", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li",
  "blockquote", "pre", "code", "em", "strong", "b", "i", "u", "s",
  "a", "table", "thead", "tbody", "tr", "th", "td", "figure",
  "figcaption", "hr", "sup", "sub", "div", "span", "dl", "dt", "dd",
}

CLEANER = Cleaner(
  scripts=True, javascript=True, comments=True, style=True, inline_style=True,
  links=True, meta=True, page_structure=False, processing_instructions=True,
  embedded=True, frames=True, forms=True, annoying_tags=True,
  kill_tags=["img", "picture", "source", "video", "audio", "svg", "button",
             "nav", "aside", "footer", "header", "form", "iframe", "noscript"],
  safe_attrs_only=True, safe_attrs=frozenset(["href"]),
)

STYLE = """
body { font-family: serif; line-height: 1.45; margin: 0 0.4em; }
h1 { font-size: 1.5em; margin: 0.6em 0 0.2em 0; }
h2 { font-size: 1.25em; }
a { color: inherit; }
.meta { font-size: 0.85em; color: #555; margin: 0 0 1.2em 0; }
.toc-cat { margin-top: 1.2em; border-bottom: 1px solid #999; }
.toc-list { list-style: none; padding-left: 0; margin-top: 0.4em; }
.toc-list li { margin-bottom: 0.7em; }
.toc-src { display: block; font-size: 0.8em; color: #555; }
.edition-title { text-align: center; margin-top: 3em; }
.edition-sub { text-align: center; color: #555; }
blockquote { margin-left: 1em; padding-left: 0.8em; border-left: 3px solid #999; }
pre { white-space: pre-wrap; font-size: 0.85em; }
hr.art-end { margin: 2em auto; width: 30%; }
"""


def log(msg):
  print(msg, flush=True)


def http_get(session, url):
  resp = session.get(url, timeout=FETCH_TIMEOUT, headers={"User-Agent": USER_AGENT})
  resp.raise_for_status()
  return resp


def entry_datetime(entry):
  for key in ("published_parsed", "updated_parsed"):
    st = entry.get(key)
    if st:
      return datetime.fromtimestamp(time.mktime(st), tz=timezone.utc)
  return None


def entry_guid(entry):
  return entry.get("id") or entry.get("link") or entry.get("title", "")


def feed_content_html(entry):
  """Best HTML carried inside the feed entry itself."""
  candidates = []
  for c in entry.get("content", []) or []:
    if c.get("value"):
      candidates.append(c["value"])
  if entry.get("summary"):
    candidates.append(entry["summary"])
  if not candidates:
    return ""
  return max(candidates, key=len)


def text_length(fragment_html):
  try:
    tree = lxml_html.fromstring(f"<div>{fragment_html}</div>")
    return len(" ".join(tree.text_content().split()))
  except Exception:
    return len(fragment_html)


def sanitize(fragment_html):
  """Whitelist-clean an HTML fragment and return XHTML-safe markup."""
  try:
    tree = lxml_html.fromstring(f"<div>{fragment_html}</div>")
    tree = CLEANER.clean_html(tree)
    for el in tree.iter():
      if not isinstance(el.tag, str):
        continue
      if el.tag not in ALLOWED_TAGS and el.tag != "div":
        el.tag = "div"
    out = lxml_html.tostring(tree, encoding="unicode", method="xml")
    return re.sub(r"^<div>|</div>$", "", out)
  except Exception as e:
    log(f"    sanitize failed ({e}); falling back to escaped text")
    tree = lxml_html.fromstring(f"<div>{fragment_html}</div>")
    paras = [p for p in tree.text_content().split("\n") if p.strip()]
    return "".join(f"<p>{htmlmod.escape(p)}</p>" for p in paras)


def fetch_full_text(session, url):
  """Fetch the article page and extract the main content as HTML."""
  resp = http_get(session, url)
  doc = Document(resp.text)
  return doc.summary(html_partial=True)


def collect(config, now_utc):
  window = timedelta(hours=config["settings"].get("windowHours", 24))
  cutoff = now_utc - window
  session = requests.Session()

  seen_path = os.path.join(ROOT, "state", "seen.json")
  try:
    with open(seen_path) as f:
      seen = json.load(f)
  except Exception:
    seen = {}

  categories = []
  report_rows = []

  for cat in config["categories"]:
    cat_articles = []
    for feed_cfg in cat["feeds"]:
      name, url = feed_cfg["name"], feed_cfg["url"]
      allow_fulltext_fetch = feed_cfg.get("fullText", True)
      log(f"Feed: {name}")
      try:
        raw = http_get(session, url).content
        parsed = feedparser.parse(raw)
        if parsed.bozo and not parsed.entries:
          raise ValueError(f"not a parseable feed ({parsed.bozo_exception})")
      except Exception as e:
        log(f"  ERROR fetching feed: {e}")
        report_rows.append((cat["name"], name, "FEED ERROR", 0, str(e)[:160]))
        continue

      fresh, fetched_full, notes = [], 0, []
      for entry in parsed.entries:
        dt = entry_datetime(entry)
        guid = entry_guid(entry)
        if not guid or guid in seen:
          continue
        if dt is not None and dt < cutoff:
          continue
        if dt is None:
          dt = now_utc  # undated entry: include once, dedupe via seen.json

        body = feed_content_html(entry)
        source_note = ""
        if text_length(body) < FULLTEXT_MIN_CHARS and allow_fulltext_fetch and entry.get("link"):
          try:
            body = fetch_full_text(session, entry["link"])
            fetched_full += 1
          except Exception as e:
            source_note = "Full article could not be fetched; feed excerpt shown."
            notes.append(f"fulltext failed for '{entry.get('title','?')[:60]}': {str(e)[:100]}")

        fresh.append({
          "title": entry.get("title", "(untitled)"),
          "link": entry.get("link", ""),
          "date": dt,
          "source": name,
          "body": sanitize(body),
          "note": source_note,
          "guid": guid,
        })

      log(f"  {len(fresh)} new article(s), {fetched_full} full-text fetched")
      report_rows.append((cat["name"], name, "OK", len(fresh), "; ".join(notes)[:300]))
      cat_articles.extend(fresh)

    cat_articles.sort(key=lambda a: a["date"], reverse=True)
    categories.append({"name": cat["name"], "articles": cat_articles})

  # update seen state
  now_iso = now_utc.isoformat()
  for cat in categories:
    for art in cat["articles"]:
      seen[art["guid"]] = now_iso
  retention_cutoff = now_utc - timedelta(days=SEEN_RETENTION_DAYS)
  seen = {
    g: ts for g, ts in seen.items()
    if datetime.fromisoformat(ts) > retention_cutoff
  }
  os.makedirs(os.path.dirname(seen_path), exist_ok=True)
  with open(seen_path, "w") as f:
    json.dump(seen, f, indent=2)

  return categories, report_rows


def build_epub(config, categories, now_local):
  tz_label = "AWST"
  title = config["settings"].get("title", "Daily News")
  edition_date = now_local.strftime("%A %d %B %Y")
  total = sum(len(c["articles"]) for c in categories)

  book = epub.EpubBook()
  book.set_identifier(f"daily-news-{now_local.strftime('%Y%m%d')}")
  book.set_title(f"{title} — {edition_date}")
  book.set_language("en")
  book.add_author("news-reader")

  css = epub.EpubItem(uid="style", file_name="style/style.css",
                      media_type="text/css", content=STYLE.encode())
  book.add_item(css)

  def make_page(uid, file_name, page_title, body_html):
    page = epub.EpubHtml(uid=uid, title=page_title, file_name=file_name, lang="en")
    page.content = f"<html><head><title>{htmlmod.escape(page_title)}</title></head><body>{body_html}</body></html>"
    page.add_item(css)
    book.add_item(page)
    return page

  title_page = make_page(
    "titlepage", "title.xhtml", title,
    f'<h1 class="edition-title">{htmlmod.escape(title)}</h1>'
    f'<p class="edition-sub">{edition_date}</p>'
    f'<p class="edition-sub">{total} articles · compiled '
    f'{now_local.strftime("%-I:%M %p")} {tz_label}</p>',
  )

  # article pages
  article_pages = []  # (category, [(page, article), ...])
  idx = 0
  for cat in categories:
    pages = []
    for art in cat["articles"]:
      idx += 1
      fname = f"art-{idx:03d}.xhtml"
      when = art["date"].astimezone(now_local.tzinfo).strftime("%-I:%M %p, %a %d %b")
      note = f'<p class="meta"><em>{htmlmod.escape(art["note"])}</em></p>' if art["note"] else ""
      link = (f' · <a href="{htmlmod.escape(art["link"])}">original</a>') if art["link"] else ""
      body = (
        f"<h1>{htmlmod.escape(art['title'])}</h1>"
        f'<p class="meta">{htmlmod.escape(art["source"])} · {when} {tz_label}{link}</p>'
        f"{note}{art['body']}<hr class=\"art-end\"/>"
      )
      pages.append((make_page(f"art{idx}", fname, art["title"], body), art))
    article_pages.append((cat, pages))

  # contents page
  toc_html = ["<h1>Contents</h1>"]
  if total == 0:
    toc_html.append("<p>No new articles in this edition.</p>")
  for cat, pages in article_pages:
    if not pages:
      continue
    toc_html.append(f'<h2 class="toc-cat">{htmlmod.escape(cat["name"])} ({len(pages)})</h2>')
    toc_html.append('<ul class="toc-list">')
    for page, art in pages:
      when = art["date"].astimezone(now_local.tzinfo).strftime("%-I:%M %p")
      toc_html.append(
        f'<li><a href="{page.file_name}">{htmlmod.escape(art["title"])}</a>'
        f'<span class="toc-src">{htmlmod.escape(art["source"])} · {when} {tz_label}</span></li>'
      )
    toc_html.append("</ul>")
  contents_page = make_page("contents", "contents.xhtml", "Contents", "".join(toc_html))

  # navigation
  book.toc = [title_page, contents_page] + [
    (epub.Section(cat["name"], href=pages[0][0].file_name if pages else "contents.xhtml"),
     [p for p, _ in pages])
    for cat, pages in article_pages if pages
  ]
  book.add_item(epub.EpubNcx())
  book.add_item(epub.EpubNav())
  book.spine = [title_page, contents_page, "nav"] + [
    p for _, pages in article_pages for p, _ in pages
  ]
  return book


def write_report(report_rows, now_local, total):
  lines = [
    "# Build report",
    "",
    f"Last run: {now_local.strftime('%Y-%m-%d %H:%M %Z')} — {total} article(s) in the edition.",
    "",
    "| Category | Feed | Status | New articles | Notes |",
    "|---|---|---|---|---|",
  ]
  for cat, feed, status, count, notes in report_rows:
    lines.append(f"| {cat} | {feed} | {status} | {count} | {notes or ''} |")
  with open(os.path.join(ROOT, "build-report.md"), "w") as f:
    f.write("\n".join(lines) + "\n")


def main():
  with open(os.path.join(ROOT, "feeds.json")) as f:
    config = json.load(f)

  tz = ZoneInfo(config["settings"].get("timezone", "Australia/Perth"))
  now_utc = datetime.now(timezone.utc)
  now_local = now_utc.astimezone(tz)

  categories, report_rows = collect(config, now_utc)
  total = sum(len(c["articles"]) for c in categories)
  book = build_epub(config, categories, now_local)

  os.makedirs(os.path.join(ROOT, "archive"), exist_ok=True)
  daily_path = os.path.join(ROOT, "daily.epub")
  archive_path = os.path.join(ROOT, "archive", f"{now_local.strftime('%Y-%m-%d')}.epub")
  epub.write_epub(daily_path, book)
  epub.write_epub(archive_path, book)
  write_report(report_rows, now_local, total)

  log(f"Wrote {daily_path} and {archive_path} ({total} articles)")
  feed_errors = [r for r in report_rows if r[2] != "OK"]
  if feed_errors:
    log(f"WARNING: {len(feed_errors)} feed(s) failed — see build-report.md")


if __name__ == "__main__":
  sys.exit(main())
