# Wix → GitHub Pages migration

One-time playbook for cutting the live site over from Wix to this repo.
Read the whole thing once before starting — there's a recommended order.

## What you're about to do

1. Push this repo to GitHub.
2. Turn on GitHub Pages.
3. Tell Pages your custom domain is `www.thrivingtogetherpt.com`.
4. Update DNS so the domain points at GitHub instead of Wix.
5. Wait for HTTPS to be issued, verify the live site works.
6. Cancel Wix.

The DNS step is the only one that's visible to the public. Everything before
it is silent: GitHub Pages will be running in parallel with Wix on a
github.io URL, and the live `www.thrivingtogetherpt.com` will keep loading
the Wix site until you flip DNS.

## Before you start: lower the DNS TTL

Whatever provider hosts your DNS (probably Wix unless you've moved it),
lower the **TTL** on the existing `www` and apex (`@`) records to **300
seconds** (5 minutes). Do this **at least 24 hours before the cutover**.
That way, if anything goes wrong after the DNS flip, the rollback will
propagate in 5 minutes instead of a day.

If you can't be bothered, the migration still works — rollback is just slower.

## Step 1 — Create the GitHub repo and push

```
# from this directory
gh repo create thriving-together --public --source . --remote origin
git push -u origin main
```

If you don't have the `gh` CLI, do it in the browser:
- New repo at github.com → name it `thriving-together` → don't initialize
  with anything → copy the `git remote add origin ...` snippet → run that
  + `git push -u origin main`.

The repo can be public or private — GitHub Pages works on both for free
plans. Public is simpler.

## Step 2 — Enable GitHub Pages

In the repo's web UI: **Settings → Pages**.
- **Source:** Deploy from a branch
- **Branch:** `main` / `(root)`
- Click **Save**.

GitHub will build the site and give you a URL like
`https://<your-username>.github.io/thriving-together/`. Open it. The site
should be live there with broken styling (because of the path prefix) —
that's expected. The custom-domain step fixes the path.

## Step 3 — Set the custom domain in Pages

Same Settings → Pages screen:
- **Custom domain:** `www.thrivingtogetherpt.com` → Save.
- GitHub will run a DNS check. It will fail right now — you haven't
  updated DNS yet. That's fine.

This step writes a `CNAME` file to the repo. **A `CNAME` file already
exists in this repo** containing `www.thrivingtogetherpt.com`, so the
custom domain should auto-populate when you enable Pages.

## Step 4 — Update DNS

This is the public-facing flip. Where you do this depends on where
your DNS lives.

### If your domain is registered through Wix

You have two options:

**Option A: Transfer the domain out of Wix** (recommended long-term).
Move it to a real registrar like Cloudflare, Namecheap, or Porkbun. This
removes Wix from the picture entirely. It takes ~5 days and you keep your
ownership of the name. After transfer, follow Option B's DNS instructions
at the new registrar.

**Option B: Keep the domain at Wix but point DNS away from Wix.**
In Wix dashboard → **Domains** → click your domain → **Advanced** →
**Edit DNS records**. You'll add the records below. Wix may complain;
that's expected.

### If your domain is registered elsewhere

Log into your registrar (GoDaddy, Cloudflare, Google Domains, etc.) and
edit DNS records for `thrivingtogetherpt.com`.

### The records to set

Replace any existing `A`, `AAAA`, or `CNAME` records on `@` and `www`
with these:

| Type    | Host  | Value                                     | TTL  |
|---------|-------|-------------------------------------------|------|
| A       | @     | 185.199.108.153                           | 300  |
| A       | @     | 185.199.109.153                           | 300  |
| A       | @     | 185.199.110.153                           | 300  |
| A       | @     | 185.199.111.153                           | 300  |
| AAAA    | @     | 2606:50c0:8000::153                       | 300  |
| AAAA    | @     | 2606:50c0:8001::153                       | 300  |
| AAAA    | @     | 2606:50c0:8002::153                       | 300  |
| AAAA    | @     | 2606:50c0:8003::153                       | 300  |
| CNAME   | www   | `<your-github-username>.github.io.`       | 300  |

The four `A` IPs and four `AAAA` IPs are GitHub Pages' published anycast
addresses. They make the apex (`thrivingtogetherpt.com`) redirect to
`www.thrivingtogetherpt.com`. The `CNAME` on `www` is what actually serves
the site. Replace `<your-github-username>` with your real GitHub username
(no angle brackets, and **keep the trailing dot** if your DNS UI requires
fully-qualified names).

If your registrar doesn't allow `AAAA` records, skip them — IPv6 is
optional. The site will still work over IPv4.

### MX records (email)

If you have email hosted on the domain (Google Workspace, Wix Mailbox,
etc.), **leave the MX records alone**. They're independent of the web
records above. Only touch `A` / `AAAA` / `CNAME` for `@` and `www`.

If you currently use **Wix Mailbox** for email at `@thrivingtogetherpt.com`,
that ends when Wix is cancelled. Move that elsewhere first (Google
Workspace, Fastmail, etc.) or accept losing it.

## Step 5 — Wait, then verify

DNS propagation usually completes in a few minutes if you lowered the TTL,
otherwise up to a few hours.

Check progress with:

```
dig +short www.thrivingtogetherpt.com CNAME
dig +short thrivingtogetherpt.com A
```

The CNAME should resolve to `<username>.github.io.` and the A records
should be the 185.199.108-111.153 set above.

Once DNS resolves, go back to **Settings → Pages**. The DNS check should
turn green. Tick **Enforce HTTPS** (it might take 10–20 minutes after
DNS resolves before this checkbox is enabled — GitHub provisions a
Let's Encrypt cert in the background).

Then visit `https://www.thrivingtogetherpt.com` and click around all
three pages. Hard-refresh (`cmd+shift+R`) if caching is being weird.

## Step 6 — Cancel Wix

Only after the live site has been working from GitHub for a day or two:

1. Confirm the domain is no longer pointing at Wix
   (`dig www.thrivingtogetherpt.com` shows GitHub IPs).
2. Confirm email is not running through Wix (or has been moved).
3. Wix dashboard → **Subscriptions** → cancel the website plan.
4. If the domain is still registered through Wix and you don't want to
   transfer it, you'll keep paying Wix the **domain renewal fee** even
   after cancelling the website plan. Transferring out is the only way
   to fully exit.

## Rolling back (if something goes wrong)

You don't need to rebuild Wix. Just **revert the DNS change**: set the
`A` / `AAAA` / `CNAME` records back to their original Wix values. With a
300s TTL the public site is back on Wix in 5 minutes. The Wix subscription
must still be active for this to work, which is why **don't cancel Wix
until you've been live on GitHub for at least a day**.

## Known limitations of the snapshot

These were intentional decisions during the migration; documenting them
so they're not surprises later.

- **No contact form.** Replaced with a simple email + WhatsApp link card
  on `contact-1.html`. GitHub Pages can't process form POSTs, so a real
  form would need a third-party handler (Formspree, Basin, etc.).
- **No Google Map embeds.** The Wix-rendered Google Map iframes were
  removed; they only worked under Wix's runtime. If you want a map back,
  use Google's standard embed code from maps.google.com → Share → Embed
  a map, and paste the `<iframe>` into the relevant HTML file.
- **No Wix Chat bubble.** Removed for the same reason.
- **HTML is Wix-generated and verbose.** It's editable by hand for text
  changes (search for the visible string and replace it), but bigger
  layout changes are painful. If the site grows beyond a handful of
  pages, plan a clean rebuild on a static site generator (11ty, Astro,
  Hugo) using this snapshot as content reference.
- **No CMS / no edit-in-browser.** All content changes happen by editing
  files in this repo and pushing. There's no Wix-style dashboard.
