# thrivingtogetherpt.com

Static snapshot of the Thriving Together PT website, served by GitHub Pages.

## Layout

```
.
├── index.html                # homepage
├── contact-1.html            # contact page
├── community-offerings.html  # community offerings page
├── assets/
│   ├── img/                  # all images + favicon
│   └── fonts/                # web fonts
├── CNAME                     # custom domain for GitHub Pages
├── .nojekyll                 # tell Pages not to run Jekyll
└── scraper/                  # tooling that re-generates the snapshot
    ├── scrape.py
    ├── requirements.txt
    └── README.md
```

## Editing the site

The HTML files are post-Wix, post-rendering snapshots. They're verbose
because Wix-generated CSS is verbose, but the visible text and images can
be edited by hand. Search for `tt-form-placeholder` to find spots that need
human attention.

## Re-running the scrape

Only needed if you make changes to the live Wix site (during the migration
window) and want to pull them down. After the DNS cutover this directory
*is* the source of truth and you should edit files directly.

```
cd scraper
source .venv/bin/activate    # one-time: see scraper/README.md for setup
python scrape.py --output ..
```

## Deployment

Pushes to `main` are published automatically by GitHub Pages.
See `MIGRATION.md` for the one-time setup steps.
