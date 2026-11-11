# Calling trove from other tools

A script that finds links (a blog or forum scraper, say) shouldn't carry its own
download code. It should hand the links to trove, which skips what's already there,
resumes, dates files, and reports what happened. There are two ways in, with the
same behaviour. What this page describes is stable: within a major version it's only
added to, never changed.

## From the command line

```bash
printf '%s\n' "$LINK1" "$LINK2" | trove get --from-file - -o "nas:/files/{provider}" --json - > report.json
```

- `--from-file -` reads links from stdin, one per line; blank lines and lines
  starting with `#` are ignored. Links on the command line work too.
- Every link in one `trove get` shares one connection per destination. Start one
  `trove get` per batch, not one per link: an SFTP server may refuse an address that
  reconnects quickly.
- `--pause SECONDS` waits between links, for sites that ban clients that are too
  quick (gofile is one).
- `--json FILE` writes the report below. With `--json -` it goes to stdout and all
  progress goes to stderr, so stdout is only the JSON.
- `trove sync --json -` reports on the configured sites the same way.

### Exit codes

| Code | Meaning | What a caller should do |
|---|---|---|
| 0 | Everything succeeded | Nothing |
| 1 | Something failed; everything else still ran | Look at the report's `failed` and `gone` shares |
| 2 | Usage or config problem | Fix the call or the config |
| 3 | The run stopped early (quota, outage, refused login) | Run again later; finished files are skipped |
| 4 | Another `trove sync` was running (sync only) | Try later |

### The report

```json
{
 "version": 1,
 "command": "get",
 "exit_code": 1,
 "dry_run": false,
 "aborted": false,
 "error": null,
 "shares": [
  {
   "link": "https://share.example/s/TOKEN",
   "status": "ok",
   "site": null,
   "error": null,
   "provider": "allsync",
   "share_name": "Some Share",
   "output": "sftp://user@host.example:22/files/allsync/Some Share",
   "files": 120,
   "downloaded": 3,
   "skipped": 117,
   "would_download": 0,
   "dates_changed": 3,
   "errors": 0,
   "orphans": 0,
   "deleted": 0,
   "renamed": 0
  }
 ]
}
```

- `status` is `ok`, `failed`, `gone` (the link looks dead), `aborted` (the run
  stopped at this share) or `not_run` (the run stopped before reaching it).
- `aborted` at the top is true when the run stopped early; every later link is
  `not_run`, in order, so a caller can retry just those.
- `error` at the top is set when the run didn't go ahead at all: a config problem
  (exit code 2), or another sync running (4).
- `site` is the site's name for `trove sync`, and null for `trove get`.
- Links never include their `#fragment`, which can hold a decryption key, and
  passwords are scrubbed.
- Fields may be added in later versions; none will be renamed or removed while
  `version` is 1.

## From Python

```python
from trove import api

report = api.get(links, output="nas:/files/{provider}", pause=3, quiet=True)
if report.aborted:
    print("Stopped early; run again later.")
for share in report.shares:
    print(share.status, share.link, share.downloaded)
```

`api.get(links, *, output=None, dest=None, name=None, path=None, password=None,
only=(), dry_run=False, verify=False, no_orphans=False, pause=0, config=None,
quiet=False)` syncs links like `trove get`. `api.sync(names=(), *, providers=(),
dry_run=False, recent_first=False, no_orphans=False, config=None, quiet=False)` syncs configured
sites like `trove sync`. Both return a `RunReport` whose `shares` are `ShareReport`s
with the fields above, and both raise `api.ConfigError` for a bad config
or bad arguments. `quiet=True` drops the progress output.

`api.recognizes(url)` is true when a provider can sync a link as it stands, which
lets a scraper keep only the links worth handing over. Shorteners aren't recognized,
because only following one tells where it leads; pass those to `get` anyway.

`api.identify(url, *, follow=True)` says where a link leads without listing
anything, as a `LinkInfo` with `link`, `final` (the share link, after any shortener),
`provider`, `identity` and `error`. A shortener is walked one hop at a time and the
share link is never fetched, so `final` keeps its `#fragment`; strip it before
printing. Two links to the same share have the same `identity`, which is how to tell
whether a found link is one you already have. When the link leads nowhere usable,
`error` says why and the rest are None.

`api.sites(*, config=None)` lists the configured sites as `SiteInfo`s, in file order
with disabled ones included: `name`, `url`, `fallback` (a tuple), `enabled`,
`provider` (the one it syncs from, where that's known), `note`, `dest` (the exact
folder the site sets, if any), `link_in_document` (a tuple of hosts; when set, `url`
leads to a document holding the share link, not to the share), and what the last
successful sync recorded,
`identity` and `output` (the folder it synced into).

`api.config_path(config=None)` is the config file trove would use, whether or not it
exists, for a helper that keeps its own records beside it.

`api.open_folder(dest, *, config=None)` opens a destination folder for reading, as a
context manager giving a `Folder`. `dest` is a destination string, an `sftp://` URL,
a local path or a `SiteInfo.output`; a configured mount is used when it's reachable.
`folder.files()` lists the regular files directly in it as `FileInfo`s (`name`,
`size`, `mtime`), and `folder.read(name)` returns one file's bytes.

Both read the user's config for destinations and credentials (`config` names
another file). Nothing else in the package is part of this interface.

## Example: a forum thread scraper

A scraper does what only it can (find the thread, pull the links out of posts) and
hands the rest to trove:

```python
import sys

from trove import api

links = find_links_in_thread()  # the scraper's own job
if not links:
    sys.exit("No links found.")
report = api.get(links, output="nas:/files/{provider}", pause=3)
failed = [s.link for s in report.shares if s.status in ("failed", "gone", "not_run")]
print(f"{len(report.shares) - len(failed)} synced, {len(failed)} not")
sys.exit(report.exit_code)
```

Run it on a schedule as described in [scheduling.md](scheduling.md).
