# news-reader

Daily RSS → EPUB digest, built automatically by GitHub Actions.

## How it works

Every day at **5:15 AM AWST** (21:15 UTC) the [build workflow](.github/workflows/build-daily-epub.yml)
reads [`feeds.json`](feeds.json), collects everything published in the last 24 hours,
fetches full article text where a feed only carries an excerpt (verbatim — no
summarizing), and commits:

- **`daily.epub`** — always the latest edition
- **`archive/YYYY-MM-DD.epub`** — dated copy (AWST date)
- **`build-report.md`** — per-feed status of the last run
- **`state/seen.json`** — GUIDs of already-published articles (prevents repeats)

## Downloading from a script

```sh
curl -sL -H "Authorization: Bearer $GITHUB_TOKEN" \
  -H "Accept: application/vnd.github.raw" \
  https://api.github.com/repos/sam-higton/news-reader/contents/daily.epub \
  -o daily.epub
```

(Token needs `contents: read` on this repo.)

## Maintaining the feed list

Edit `feeds.json` and push — the next build picks it up automatically.

```jsonc
{
  "settings": {
    "title": "Daily News",        // epub title
    "timezone": "Australia/Perth",
    "windowHours": 24             // how far back each edition looks
  },
  "categories": [
    {
      "name": "Games",            // section heading on the contents page
      "feeds": [
        {
          "name": "Rock Paper Shotgun — News",
          "url": "https://www.rockpapershotgun.com/feed/news",
          "fullText": true        // optional; false = never fetch the article
                                  // page, use whatever the feed carries
        }
      ]
    }
  ]
}
```

## Running manually

Trigger the **Build daily epub** workflow from the Actions tab
(`workflow_dispatch`), or locally:

```sh
pip install -r requirements.txt
python build_epub.py
```
