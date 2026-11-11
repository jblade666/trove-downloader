# trove-downloader

Download and sync files from public file-share links to a local folder or an SFTP
server. It only downloads: it doesn't manage or upload anything on the remote side.

Runs on Linux, macOS and Windows with Python 3.10 or newer.

## What it does

- Downloads a whole shared folder, one subfolder of it, or a single shared file, from
  [ten sites](#supported-sites), through any link shortener that redirects.
- Skips files already present with the right size, and resumes partial downloads.
- Keeps file and folder dates, and only writes a date that differs.
- Keeps a list of permanent sites in `sites.yaml`, and brings them all up to date
  with one `trove sync`, recording how each one is doing.
- Follows a stable link to a small document (a PDF or a web page) that holds the
  current share link, for sharers who change links often.
- Writes to a local folder, to an SFTP server, or through a local mount of that
  server when it's reachable.
- Reports local files that are no longer on the server. It deletes them only when
  asked, and refuses when that looks unsafe.
- Writes nothing with `--dry-run`, including dates.
- Rewrites names other systems can't store, and keeps every file inside the output
  folder even when a remote name tries to leave it.

## Getting started

### 1. Install

trove isn't on PyPI. Install it from a clone of this repository with
[pipx](https://pipx.pypa.io), which puts the `trove` command on your PATH in its own
environment.

**Linux**

```bash
sudo apt install pipx git        # or your distribution's equivalent
pipx ensurepath                  # then open a new terminal
git clone https://github.com/jblade666/trove-downloader.git
cd trove-downloader
pipx install .
```

**macOS**

```bash
brew install pipx git
pipx ensurepath                  # then open a new terminal
git clone https://github.com/jblade666/trove-downloader.git
cd trove-downloader
pipx install .
```

**Windows** (PowerShell, with Python from python.org or the Microsoft Store)

```powershell
py -m pip install --user pipx
py -m pipx ensurepath            # then open a new terminal
git clone https://github.com/jblade666/trove-downloader.git
cd trove-downloader
pipx install .
```

Check that it works with `trove --version`. To update later, `git pull` in the clone,
then `pipx install --force .`.

#### Optional extras

Two extras add packages that only some sites need:

| Extra | Needed for |
|---|---|
| `browser` | ufile and workupload, and gofile when its signing salt changes |
| `kregfile` | kregfile rooms |

```bash
pipx install --force ".[browser,kregfile]"
```

The `browser` extra also needs a Chromium download, run with trove's own Python:

```bash
# Linux and macOS
"$(pipx environment --value PIPX_LOCAL_VENVS)/trove-downloader/bin/python" -m playwright install chromium
```

```powershell
# Windows
& "$(pipx environment --value PIPX_LOCAL_VENVS)\trove-downloader\Scripts\python.exe" -m playwright install chromium
```

### 2. Download your first link

trove has no default destination, so the first runs name one with `-o`. The folder
must already exist. Quote links, because `#` starts a comment in most shells.

```bash
mkdir -p ~/Downloads/troves
trove get "https://mega.nz/folder/AbCdEfGh#ExampleFolderKey123" -o ~/Downloads/troves --dry-run
trove get "https://mega.nz/folder/AbCdEfGh#ExampleFolderKey123" -o ~/Downloads/troves
```

On Windows, use a path such as `-o "D:\Troves"`.

The dry run lists what would download and writes nothing. The real run puts the share
in a subfolder named after it, such as `~/Downloads/troves/Example Share`. Run it again
later and only new or changed files download.

### 3. Create a config file

A config file names your destinations, so you can write `-o library:/troves` rather
than a full path, and set a default so `-o` is optional. trove looks for it here:

| System | Default config path |
|---|---|
| Linux | `~/.config/trove-downloader/config.yaml` |
| macOS | `~/Library/Application Support/trove-downloader/config.yaml` |
| Windows | `%LOCALAPPDATA%\trove-downloader\config.yaml` |

`$TROVE_CONFIG` or `--config PATH` points somewhere else. `trove doctor` prints the
path it uses and checks every destination in it. The smallest useful config is:

```yaml
default_output: "library:/{provider}"     # {provider} becomes mega, mediafire, ...

destinations:
  library:
    path: "~/Downloads/troves"            # on Windows, e.g. "D:/Troves"
```

[Destinations](#destinations) has more setups, including SFTP.

### 4. Add permanent sites

Links you want to keep up to date go in `sites.yaml`, beside the config:

```yaml
sites:
  - name: "Example Trove"
    url: "https://mega.nz/folder/AbCdEfGh#ExampleFolderKey123"
```

Then `trove sync --dry-run` shows what every site would fetch. `trove sync` fetches
it, and `trove sites` reports how each site did. [Permanent sites](#permanent-sites)
covers shorteners, fallbacks, passwords and links kept inside a PDF.

## Supported sites

| Provider | Links |
|---|---|
| `allsync` | Allsync and other Nextcloud public shares (`https://host/s/TOKEN`) |
| `gofile` | gofile.io folders and files (`https://gofile.io/d/ID`, or a bare `ID`) |
| `koofr` | Koofr public links (`https://app.koofr.net/links/UUID`, or a bare UUID) |
| `kregfile` | kregfile rooms on any host (`https://host:port/r/ROOM`); needs the `kregfile` extra |
| `mediafire` | MediaFire public folders and files (`mediafire.com/folder/KEY`, `/file/KEY`) |
| `mega` | MEGA public folders and files (`mega.nz/folder/ID#KEY`, `/file/ID#KEY`, the older `#F!` and `#!` forms, or a bare `ID#KEY`), with no MEGAcmd and no account |
| `ufile` | ufile.io folders and files (`ufile.io/f/SLUG`, `ufile.io/FILESLUG`, or a bare `f/SLUG` or `FILESLUG`); needs the `browser` extra and, to download, a desktop |
| `workupload` | workupload.com file links (`workupload.com/file/ID`, or a bare `ID`); needs the `browser` extra and a desktop |
| `wormhole` | wormhole.app rooms (`https://wormhole.app/ROOM#KEY`, or a bare `ROOM#KEY`), decrypted as they download |
| `yandex` | Yandex Disk public shares (`yadi.sk/d/ID`, `disk.yandex.*/d/ID`, `/i/ID` for one file) |

Any link shortener that redirects (rebrand.ly, TinyURL and the like) is followed to
a supported link, keeping the `#key` that MEGA and wormhole links carry. A shortener
that shows a page instead of redirecting isn't followed; use the link it leads to.

## Downloading one link

`trove get` syncs any links you give it. Every example below works with `-o` (a base
folder; the share's name becomes a subfolder) or `--dest` (the exact folder). All
links here are made up.

### Folders and files on each site

```bash
# MEGA: a folder, a subfolder of it, and a single file
trove get "https://mega.nz/folder/AbCdEfGh#ExampleFolderKey123" -o library:/mega
trove get "https://mega.nz/folder/AbCdEfGh#ExampleFolderKey123/folder/XyZ12345" -o library:/mega
trove get "https://mega.nz/file/QrStUvWx#ExampleFileKey4567890" --dest library:/mega/Singles

# MediaFire: a folder and a single file
trove get "https://www.mediafire.com/folder/abc123def456g/Example_Folder" -o library:/mediafire
trove get "https://www.mediafire.com/file/xyz789uvw012h/example.pdf/file" --dest library:/mediafire/Singles

# gofile: folders and files both use /d/ID
trove get "https://gofile.io/d/AbC123" -o library:/gofile

# Yandex Disk: a folder and a single file
trove get "https://yadi.sk/d/ExampleFolderId" -o library:/yandex
trove get "https://disk.yandex.com/i/ExampleFileId" --dest library:/yandex/Singles

# Koofr, and a Nextcloud share with a password
trove get "https://app.koofr.net/links/00000000-1111-2222-3333-444444444444" -o library:/koofr
trove get "https://cloud.example.com/s/ExampleToken123" --password-file secret.txt -o library:/allsync

# wormhole, kregfile, ufile, workupload
trove get "https://wormhole.app/ExampleRoom#ExampleRoomKey" -o library:/wormhole
trove get "https://files.example.com:8080/r/ExampleRoom" -o library:/kregfile
trove get "https://ufile.io/f/example1" -o library:/ufile
trove get "https://workupload.com/file/ExAmPlE1" --dest library:/workupload

# A shortener, and several links in one run
trove get "https://rebrand.ly/example-trove" -o library:/troves
trove get --from-file links.txt -o library:/troves --pause 5
```

A single-file link with `-o` lands in a folder named after the file, such as
`Singles/example/example.pdf`. With `--dest`, the file lands directly in that folder.
If other files share that folder, add `--no-orphans`, or they're reported as orphans.

A password can also come from `--password`, but a file keeps it out of your shell
history.

### Choosing what and where

| Option | Meaning |
|---|---|
| `-o DEST` | Base destination; the share's name becomes a subfolder |
| `--dest DEST` | The exact folder to mirror into |
| `--name NAME` | Subfolder under `-o` instead of the share's name |
| `-p`, `--path SUB` | Only this subfolder of the share |
| `--only GLOB` | Only fetch matching files (repeatable, case-insensitive) |
| `--provider NAME` | Force a provider, for a bare ID or an unusual host |
| `--fallback URL` | Another link to try if the first fails (repeatable) |

With `-o` and `--path`, files land where a full sync would put them. With `--dest`
and `--path`, the subfolder's contents land directly in that folder.

A bare ID, like `AbC123` or MEGA's `ID#KEY`, could belong to any host, so trove never
guesses which: it needs `--provider`, or a configured site that uses it. For MEGA, the
key's length tells a folder from a file.

When a link (or its bare ID) belongs to a configured site, `get` syncs it where that
site does: its `output`, `folder`, `dest` and password, with anything you pass on the
command line winning. A site behind a shortener is found from the share its last
sync reached. Sites with a `path` are left out, since the link names the whole share.
`--no-site` skips the lookup.

## Destinations

`-o`, `--dest`, `default_output` and a site's `output` or `dest` all accept:

- `name:/sub/path`, a destination named in the config
- `sftp://user@host:port/path`
- any local path

`{provider}` in any of them becomes the provider's name. A local base folder must
already exist, so an unmounted drive can't silently fill your local disk.

### Local storage only

```yaml
default_output: "library:/{provider}"

destinations:
  library:
    path: "~/Troves"          # Linux and macOS; Windows: "D:/Troves"
```

An external or network drive works the same way: point `path` at its mount point,
such as `/mnt/troves`, `/Volumes/Troves` or `T:/`.

### SFTP only

```yaml
default_output: "server:/files/{provider}"

destinations:
  server:
    url: "sftp://user@files.example.com:22/"
    identity_file: "~/.ssh/id_ed25519"   # optional; an SSH agent works too
    known_hosts: "~/.ssh/known_hosts"    # optional
```

trove authenticates with an SSH key or agent first. Failing that, it looks for a
password in `$TROVE_SFTP_PASSWORD`, then in `password_file`, then in the OS keyring.
`trove set-password server` stores one in the keyring: Credential Manager on Windows,
Keychain on macOS, Secret Service (GNOME Keyring or KWallet) on Linux. A headless
Linux server often has no keyring, so use a key, the variable or `password_file`
there. Passwords are refused inside URLs.

The host key must be known. Connect once with `ssh` to record it, or point
`known_hosts` at a file that has it.

### SFTP with a local mount (a NAS, for example)

A NAS often serves the same folders over SFTP and over SMB or NFS. List the local
mounts and trove writes through them whenever they're reachable, which is usually
much faster. Otherwise it falls back to SFTP, so the same config works at home and
away.

```yaml
default_output: "nas:/files/{provider}"

destinations:
  nas:
    url: "sftp://user@nas.example.lan:22/"
    password_file: "secrets/nas.txt"     # relative paths are beside the config
    keyring_service: "trove-downloader"  # optional; the default
    mounts:                              # server folder: where it's mounted here
      "/files": "Z:/"                    # Windows mapped drive
      # "/files": "/Volumes/files"       # macOS
      # "/files": "/mnt/nas/files"       # Linux
```

`--remote on` always uses SFTP, and `--remote off` insists on the mount. The state
file records folders in their SFTP form either way. A mount must show the server's
files exactly, as an SMB share of the same folder does.

### Several destinations

A config can name as many destinations as you like, and each site picks one:

```yaml
destinations:
  nas:
    url: "sftp://user@nas.example.lan:22/"
  scratch:
    path: "/data/downloads"
```

## Config

```yaml
default_output: "nas:/files/{provider}"   # used when a run or site names no output
keep_markers: ["(kept)"]                  # names containing these are never orphans

destinations:
  # ... as above

sites_file: "sites.yaml"   # optional; this is the default, beside the config
state_file: "state.json"   # optional; this is the default, beside the config

sites: []                  # optional; sites can also go here, ahead of sites_file's
```

Keep the config out of version control, since links and paths are personal.
Relative paths in it are resolved against its own folder.

## Permanent sites

Links you sync repeatedly go in one list, whatever site they're on. It's a separate
file so it can move between machines, while each machine keeps its own destinations
and credentials. trove never rewrites it, so your comments and order stay put.

### Every field

```yaml
sites:
  - name: "Example"                     # unique; the state file tracks sites by name
    url: "https://rebrand.ly/example"   # a shortener or a direct link, on any provider
    fallback: ["https://mega.nz/folder/AbCdEfGh#ExampleFolderKey123"]
    password_file: "secrets/example.txt"
    output: "scratch:/other"            # optional, overrides default_output
    folder: "Group/Example"             # optional, instead of the share's own name
    dest: "nas:/exact/folder"           # optional, the exact folder (instead of output)
    path: "Some/Subfolder"              # optional, only this part of the share
    rollover: "Archive"                 # optional, see below
    provider: "allsync"                 # optional, instead of detecting it
    link_in_document: ["mega.nz"]       # optional, see below
    note: "free text"                   # optional, for your own records
    enabled: true                       # false retires a site but keeps it listed
```

Only `name` and `url` are required.

### Recipes

```yaml
sites:
  # A plain MEGA folder, into default_output.
  - name: "Old School Maps"
    url: "https://mega.nz/folder/AbCdEfGh#ExampleFolderKey123"

  # A rebrand.ly link the sharer repoints when a folder is taken down, with the
  # direct link it leads to today as a fallback in case the shortener dies.
  - name: "Sci-Fi Trove"
    url: "https://rebrand.ly/example-scifi"
    fallback: ["https://mega.nz/folder/IjKlMnOp#ExampleFolderKey456"]
    output: "nas:/files/mega/Curated"

  # A copy that already exists under another folder name: pin it with dest, or
  # the next sync downloads everything again into a second folder.
  - name: "Fantasy Archive"
    url: "https://www.mediafire.com/folder/abc123def456g/Fantasy"
    dest: "nas:/files/mediafire/Some Uploader/Fantasy Archive"

  # Only one subfolder of a large share.
  - name: "Big Trove - Maps only"
    url: "https://gofile.io/d/AbC123"
    path: "Maps"
    folder: "Big Trove Maps"

  # A password-protected Nextcloud share.
  - name: "Club Share"
    url: "https://cloud.example.com/s/ExampleToken123"
    password_file: "secrets/club.txt"

  # A site whose link died, kept for the record. Naming it in full still runs it.
  - name: "Retired Trove"
    url: "https://mega.nz/folder/QrStUvWx#ExampleFolderKey789"
    enabled: false
    note: "Taken down 2025-01; local copy is the only one."
```

The provider is worked out from where the link leads, so a shortener needs no special
setup and can change target over time. `trove sync` tries the `url`, then each
`fallback`. It warns when a fallback points somewhere else, which means the shortener
was repointed and the fallback is out of date.

### A link kept inside a PDF or a web page

Some archives (OSRchive is one) keep a stable link to a small PDF, often hosted on
MEGA, and put the current share link inside it. When the share is taken down they
upload a new one and edit the PDF, so the PDF's link never changes. Others keep a
one-link web page. With `link_in_document`, trove follows `url` to that document,
reads it, and uses the first link in it on one of the listed hosts (subdomains
included):

```yaml
sites:
  # The PDF is a MEGA file; the share link inside it points at a MEGA folder.
  - name: "OSR Archive"
    url: "https://mega.nz/file/PdFfIlE1#ExamplePdfKey1234567890"
    link_in_document: ["mega.nz"]
    fallback: ["https://mega.nz/folder/StUvWxYz#ExampleFolderKey000"]
    output: "nas:/files/mega/Archives"

  # A shortener that leads to a web page holding the current link, on either host.
  - name: "Page-Published Trove"
    url: "https://rebrand.ly/example-current"
    link_in_document: ["mega.nz", "mediafire.com"]
    fallback: ["https://www.mediafire.com/folder/zyx987wvu654t"]
```

A PDF is read from the share it's in, so MEGA's decryption applies; a web page is read
where the link lands. The link found then goes through the normal path, shorteners
included. If any step fails, trove moves on to `fallback` as usual, so keep the last
known share link there; trove warns when it no longer matches.

### Running syncs

```bash
trove sync --dry-run              # what every enabled site would fetch
trove sync                        # fetch it
trove sync "Sci-Fi*" "Maps"       # only sites matching these (substring or wildcard)
trove sync --provider mega        # only sites on one provider (repeatable)
trove sync --recent-first         # most recently changed sites first
trove sites                       # each site and how its last run went
trove sites --ages                # when each site last gained content
trove sites --gone                # only sites whose link looks dead
```

Each run records what it found in the state file, and `trove sites` reports it. A dead
link is reported as `LINK GONE`, separately from a temporary failure, and is never
retired automatically. A failed link is usually worth a look, since your local copy
may be the only one left. To retire a site, set `enabled: false`.

Runs warn when a site's link moves to a different share or provider, or the share is
renamed, because either can change where files land. Set `dest`, or `output` and
`folder`, on a site to pin its location.

Two sites can sync into one folder, for example two remote folders with the same
name. Once a run has recorded that, `trove sync` checks their orphans together at
the end, so neither one's files count as the other's orphans. If one of them fails
to list, nothing in that folder is deleted. The first run that finds such a pair
warns instead, since it judged them separately. A run that leaves one of them out,
by name or by `--provider`, doesn't check that folder at all.

`--provider` picks sites by the provider they sync from: the site's `provider`, else
the one that recognizes its link. A shortener or document link only says where it
leads once a run has recorded that, so until then such a site is left out, and the
run says how many were.

`rollover` is for servers that move old top-level folders into an archive folder. The
local copy is moved there too, rather than downloaded again with the old copy left
as orphans. A folder whose archived copy already exists locally is left alone.

Only one `trove sync` runs at a time per site list. To run it on a schedule, see
[`docs/scheduling.md`](docs/scheduling.md) for Task Scheduler, cron, systemd and
launchd.

## Commands and options

```bash
trove get URL [URL...] [options]     # sync links
trove get --from-file links.txt      # or read links from a file ('-' for stdin)
trove sync [NAME...] [options]       # sync sites from the config
trove sites [--brief|--gone|--ages]  # list configured sites
trove doctor                         # check the config, destinations and accounts
trove set-password NAME              # store an SFTP password in the OS keyring
trove set-password mediafire         # store a MediaFire account login in the OS keyring
```

Options for both `get` and `sync`:

| Option | Meaning |
|---|---|
| `--dry-run` | Show what would happen and write nothing |
| `--dates-only` | Only fix dates |
| `--verify` | Also re-download files that changed, by the server's recorded tag or, where the site gives hashes, by reading each file back (slow over SFTP) |
| `--seed-manifest` | Record current tags for existing files, for later `--verify` runs |
| `--rollover DIR` | (`get`) Archive folder the server moves old top-level folders into |
| `--password`, `--password-file` | (`get`) Share password |
| `--pause SECONDS` | (`get`) Wait between links, for sites that ban fast clients |
| `--no-orphans` | Skip the orphan check |
| `--delete-orphans` | Delete orphans and folders left empty |
| `--delete-max-pct PCT` | Refuse to delete more than this share of local files (default 25) |
| `--keep-marker TEXT` | Names containing this are never orphans (repeatable) |
| `--remote auto\|on\|off` | For an SFTP destination with a mount: which one to use |
| `--identity-file`, `--sftp-password-file`, `--known-hosts` | SFTP details |
| `--json FILE` | Write a report of every share as JSON (`-` for stdout) |

`--delete-orphans` refuses when the server lists no files, when the run covers only
part of a share, or when it would remove too much. Remote folders do get emptied,
and your copy may be the only one left.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Everything succeeded |
| 1 | Something failed; other shares and files were still attempted |
| 2 | Usage or config problem |
| 3 | The run stopped early, because everything remaining would fail too |
| 4 | Another `trove sync` was already running, so this one did nothing |

These codes are stable, as is the report `--json FILE` writes. Scripts that find
links can hand them to trove from the command line or from Python; see
[`docs/helpers.md`](docs/helpers.md).

## Accounts

trove needs no account for any site. Two can use one:

**MediaFire.** Anonymous MediaFire downloads are slow. With an account (premium is
much faster), `trove set-password mediafire` stores its email and password in the OS
keyring, and downloads then use the API's direct links. `$TROVE_MEDIAFIRE_EMAIL` and
`$TROVE_MEDIAFIRE_PASSWORD` take precedence over the keyring. Listing stays
anonymous, so a dry run or a run with nothing to fetch never logs in. A refused login
stops the run. `trove doctor` reports whether the account works and whether it's
premium.

**gofile.** gofile needs an account token, which a guest account has. trove uses the
one it cached, else your gofile login in Firefox, else a new guest account, and never
prints it.

## Notes on each site

**MEGA.** Links keep their key after `#`, and it never appears in output. Downloads are
checked against MEGA's MAC as they stream, and resume where they stopped; a resumed
file can only be size-checked. When the anonymous transfer quota runs out, the run
stops, keeping what it fetched, and says how long until it resets; run it again then.
A storage server that sends nothing is retried with a fresh link before the run
stops. Names are written the way MEGAcmd wrote them (a reserved character becomes
`%xx`), and existing files are found even when stored in other casing, Unicode form,
or with trailing dots and spaces, so a tree written by MEGAcmd or MEGAsync is reused
rather than fetched again.

**MediaFire.** Names are written with a reserved character as `-`. Without an account,
each download goes through MediaFire's download page.

**gofile.** gofile signs requests with a salt it changes every few months; when a
listing fails the way a stale salt does, trove reads the new one from gofile's site in
a headless browser, once per run, which needs the `browser` extra. A rate limit that
won't clear, or gofile being down, stops the run rather than syncing part of a tree.
Two files with one name in a folder each get ` (ID)` added.

**ufile.** ufile only allows downloads through its own pages, behind a reCAPTCHA that
may need solving by hand, so trove opens a visible browser window for them and they
can't run on a headless server. Listing and `--dry-run` work anywhere. ufile shows
sizes rounded, so a file already there counts as present without a size check. A file
whose free hosting period ended is premium-only, and fails with that message.

**workupload.** workupload checks the browser before showing a file, and releases each
download only after an ad. trove opens its pages in a visible window and lets the page
pass its own check, then reads the file's name, exact size and SHA-256 by itself, so
listings, dry runs and skips need nothing from you. When a file needs downloading,
trove waits while you click Download in that window and dismiss the ad, then checks
the file against the SHA-256 before keeping it. So workupload sites don't suit
unattended runs.

**wormhole.** A link's key is the part after `#`, so the full link is needed, and it
never appears in output. A room folder is named after the room ID. Rooms have a
download limit, so trove asks for download access only when a file really needs
fetching: a dry run or a run that skips everything doesn't count. Rooms the sender
could only share peer-to-peer aren't supported. Wormhole files can't resume, so an
interrupted file starts over.

**kregfile.** Rooms are temporary, so files expire from the server and show up as
orphans. Two uploads can share a name; the later one gets ` [KEY]` added, and each
keeps its name in `.kregfile-manifest.json`. Many kregfile hosts leave the
intermediate certificate out of their chain; trove fetches it from the certificate
and caches the result, rather than turning verification off.

**Yandex Disk.** Popular shares can hit a download limit, which fails that file and
not the run.

**Koofr.** A share password travels in every request's URL, so trove scrubs it from
anything it prints or saves.

## Responsible use

trove downloads what a share's owner has made public through a link. It doesn't
get around passwords, logins, paywalls or download limits; it counts a site's
limits the way the site does and stops when a quota runs out. Some sites ask you
to solve a CAPTCHA or to click through an ad before a download, and trove leaves
those to you.

Make sure you have the right to download and keep what you sync. Respect each
site's terms and the rights of whoever made the files. Keep runs modest: trove
spaces out requests and backs off when a site asks it to, so don't run several
copies against one site, or lower the delays.

## License

MIT; see `LICENSE`.

## Development

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"      # Windows: .venv\Scripts\pip
.venv/bin/pytest
```

See `CLAUDE.md` for the design and the rules worth keeping.
