# CLAUDE.md

Guidance for working in this repository.

## What this is

A single CLI, `trove`, that syncs files from public file-share links to a local folder
or an SFTP server. It only downloads. It replaces a family of per-site tools, whose
behaviour each provider must keep matching on existing files.

## This repo may become public

Nothing specific to one person's environment belongs here, in code, docs, tests or
commit history: no hosts, IPs, usernames, drive letters, real share links, passwords,
or real file names in fixtures. Examples use `example.com`-style names. Everything
environment-specific comes from the config file or the command line, and there is no
default destination.

Captured output from real runs stays outside the repo. Test data is synthetic.
`scripts/check_private.py` compares staged changes with a private list kept outside
the repo; install it as a local pre-commit hook. CI runs a secret scan.

## Commands

```bash
.venv/Scripts/python -m pytest         # Windows; .venv/bin/python elsewhere
.venv/Scripts/trove doctor
.venv/Scripts/trove get "URL" -o DEST --dry-run
```

## Architecture

```
cli.py          parses arguments, runs shares, reports exit codes
config.py       config file, destination strings, one connection per SFTP server
engine.py       everything between a listing and the destination tree
fs.py           LocalFS and SFTPFS behind one interface
naming.py       remote name to safe local name
http.py         retries, resumable transfers, shortener walking
model.py        RemoteFile, RemoteFolder, Listing, Transfer, the Share interface
providers/      one module per site, plus the registry and link selection
```

A provider module has `matches(url)` and `open_share(url, password)`, and returns a
`Share`: `list()` gives a `Listing`, and `open_transfer()` gives a byte stream. It knows
nothing about the destination.

## Rules worth keeping

- **Destination paths belong to their filesystem.** Native for `LocalFS`, POSIX for
  `SFTPFS`. Build them with `fs.join`, never `os.path`. Never normalize an `sftp://` URL.
- **List once.** Every SFTP call is a round trip. `Existing` lists each directory once,
  and `scan_local` walks once for case reconciling and orphans.
- **One `trove sync` at a time.** `RunLock` holds an OS file lock beside the state
  file for the whole run, so it's released however the process ends; a second sync
  exits with code 4. On Windows it locks a byte past the text, so the holder's
  details stay readable.
- **One SSH connection per server per process.** OpenSSH's PerSourcePenalties can refuse
  an address that reconnects rapidly. `Destinations` shares the connection, so a batch
  must stay in one process.
- **An up-to-date tree reports 0 dates changed.** Compare with a 1-second tolerance,
  since SFTP keeps whole seconds. Anything else means the comparison is broken.
- **A refused date is a failure.** Some servers refuse date writes on some mounts. It
  doesn't stop the run, but it counts as an error and never as a date changed.
- **A dry run writes nothing,** dates and manifests included.
- **`open_transfer` is lazy.** Some sites count a download when one starts, so a dry run
  or an all-skip run must never reach it.
- **No per-share state in module globals.** A batch runs in one process, and a global
  would carry one share's password into the next. State belongs on the `Share`.
- **Remote `path` is never sanitized; `local` always is.** Mixing them breaks either the
  download URL or the skip check.
- **Sanitizing runs everywhere,** not just on Windows, since a tree may be browsed from
  another system later. A provider's `colon` setting must keep matching the names its
  earlier versions wrote, or every such file re-downloads and appears as an orphan.
- **Remote names differing only by case are separate files.** A case-insensitive share
  can't hold both, and the smaller could overwrite the larger. `resolve_case_twins`
  matches each to an existing file by size, or gives it a free `name (N).ext`.
- **Stable names never reuse a name.** For a flat share with `stable_names`, the
  manifest maps key to local name, and a new key never takes a name it holds, even
  for an expired key: that file may still be there, and the new upload would be
  skipped as present. Names read from the manifest are sanitized like remote ones.
- **Content hashes are compared the way the server writes them.** `CONTENT_HASHES`
  holds each `verify_kind` that is a real hash: hex MD5 ignores case, base64
  blake2b doesn't. Only `verify_kind = "etag"` writes tags to the manifest.
- **Loose names keep what's on disk.** With `loose_names`, one walk before anything
  changes maps each remote entry to an existing file or folder whose `loose_key`
  matches (case, NFC, `%xx` escapes, leading spaces, trailing dots and spaces), and
  the entry takes that name. The same walk seeds `Existing` and the orphan check, so
  a large tree is listed once. Names sharing a loose key in one folder are left to
  `resolve_case_twins`.
- **A corrupt download is discarded.** `CorruptDownload` removes the `.part`, so a
  later run can't resume from bad bytes. A short download is a size error instead,
  which is retried.
- **Sites sharing a folder share the orphan check.** `state.json` records each
  site's output folder. In `trove sync`, sites whose recorded folders coincide run
  with `defer_orphans`, and `judge_orphans` checks the folder once against the union
  of their claims. If one didn't list, nothing there is deleted. Sharing is judged
  across every enabled site, so a run that leaves one out (by name or `--provider`)
  skips that folder's check.
- **A MEGA listing is the snapshot plus the action packets since.** The `a:f` answer
  lags; replaying `/sc` from its `sn` until `w` is what matches MEGAcmd.
- **A provider that downloads through something else spools.** ufile's browser saves
  a local temp file, and `open_transfer` streams it, so the engine's `.part` and
  rename still apply. The temp copy is kept until read through, so a failed upload
  resumes from it. `Share.close()` releases such resources after the share.
- **A size that's only shown rounded is no size.** Leave `RemoteFile.size` as None
  rather than compare it, or every file looks changed.
- **`--only` narrows fetching, not what counts as on the server.** Otherwise every
  unselected local file looks like an orphan.
- **A subfolder sync scopes the local scan** to that subfolder, for the same reason.
- **Keep the `--delete-orphans` refusals.** Upstream folders do get emptied or cut down,
  which makes a local copy look like orphans when it may be the only one left.
- **Keys stay out of output.** Print links through `_safe_url`: some keep a decryption
  key in the `#fragment`. Never print passwords. A provider that sends one in the URL
  must scrub the exceptions it raises, because `requests` quotes the URL in its messages
  and those are printed and saved in the state file. Shorteners are walked by hand and
  the final URL is never fetched, because automatic redirects lose the fragment.
- **A failed manifest read stops the share.** Treating it as empty would discard its
  entries on the next save. Only a missing file or invalid JSON counts as empty.
- **Failure levels:** one file's failure is counted and the run moves on. `ShareError`
  moves to the next share. `AbortRun` stops the run, for quota, outage or refused auth.

## Permanent sites

`sites.yaml` is hand-edited and trove never rewrites it, so its comments and order
survive. `state.json` holds what runs learned, by site name. Keep the two apart. A dead
link is reported, never retired automatically. Nothing in a site entry is specific to a
provider, because a shortener can change target; provider-specific behaviour belongs in
the provider.

## Adding a provider

1. Add `providers/<site>.py` and register it in `providers/__init__.py`. Order matters:
   the first match wins, so check that its `matches()` doesn't claim another site's links.
2. Set `colon`, `manifest_name`, `verify_kind` and `can_resume` on the `Share`, matching
   the files the site's old tool already wrote.
3. Capture a dry run from the old tool, outside the repo, before porting.
4. Port. The gate: a dry run over SFTP makes the same decisions as the old tool, with 0
   re-downloads, 0 date changes and 0 new orphans, except where a difference is explained.
5. Add synthetic tests, and update the README's table.

## Testing

Tests are offline and use synthetic data. `tests/conftest.py` has `FakeShare` and `MemFS`,
a case-sensitive POSIX tree that behaves like an SFTP destination. The `dest` fixture runs
an engine test against both a real local folder and `MemFS`.

On Windows, write throwaway scripts to a file rather than using `python -c`, because shell
escaping mangles backslash paths.
