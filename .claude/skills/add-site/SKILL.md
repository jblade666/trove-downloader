---
name: add-site
description: Add a share link to trove's permanent sites list (sites.yaml) so `trove sync` keeps it up to date. Use when the user gives a new share link or shortener to track ("add this trove", "add this site", "track this MEGA folder"), including one already copied by hand or another tool.
---

# Add a permanent site

`sites.yaml` is the user's file, beside the config (`trove doctor` prints the config path;
`sites_file` in the config can move it). It's outside this repo and trove never rewrites
it, so edit it by hand, keep its comments and order, and never commit it or quote its
links here. See "Permanent sites" in README.md for every field.

## 1. Collect the links

- **A shortener link** goes in `url`; the direct share link goes in
  `fallback`. The shortener lets trove follow a repoint, even to another host; the fallback
  keeps working if the shortener dies, and trove warns when they disagree.
- **Only a direct link**: it's the `url`, with no fallback.
- **A link to a small document that holds the current link**: `url` is the document,
  `link_in_document` lists the hosts, and the last known share link is the `fallback`.

Check that the shortener leads to the share the user gave, without fetching the final
URL (that would drop the `#key`). On Windows write this to a scratch file rather than
`python -c`:

```python
import sys
from trove.http import resolve_shortener
from trove.providers import is_provider_url
final = resolve_shortener(sys.argv[1], is_provider_url)
want = sys.argv[2]
print(final.split("#")[0], "| same share:", final.split("#")[0].rstrip("/") == want.split("#")[0].rstrip("/"),
      "| same key:", final.partition("#")[2] == want.partition("#")[2])
```

Never print a `#fragment`: it's a decryption key. Strip it from anything you show.

## 2. Check it isn't already there

Search `sites.yaml` for the shortener's slug and the share's id (the part after
`/folder/`, `/d/` and so on), including commented-out and `enabled: false` entries. If it's
there, update that entry instead, and say what changed.

## 3. Decide where it lands

Ask the user for the destination unless they gave it. Then match how nearby sites
are written:

- `output: "nas:/files/Group"`: a base folder, with the share's own name as a
  subfolder. This is the usual choice, and also what MEGAsync and most tools do.
- `dest: "nas:/files/Group/Exact Name"`: the exact folder. Use it when an existing copy's
  folder isn't named like the share, or to pin it against a later rename.
- `folder:` with `output:` changes only the subfolder's name.

If a copy already exists (from an old tool, MEGAsync, or by hand), the destination must
point at it exactly, or the next sync downloads everything again into a second folder.

## 4. Write the entry

Insert it next to related entries (same group or destination), with a short comment
line if they have one. Back up the file first, to the scratchpad.

```yaml
  # /slug

  - name: "slug"
    url: "https://short.example/slug"
    fallback: ["https://share.example/folder/ID#KEY"]
    output: "nas:/files/Group"
    note: "what's worth remembering: where the first copy came from, what matched"
```

Run `trove sites` and check the new entry shows up with the right url, fallback and
output. That confirms the YAML parsed.

## 5. Dry run

```bash
.venv/Scripts/trove sync "NAME" --dry-run
```

- **An existing copy:** expect 0 to download, 0 dates to update, 0 orphans, and no
  casing differences. Check that the `Output directory:` line is the existing folder.
  If files would download, the folder or names don't match. Find out why before
  syncing for real. A copy made by another tool can differ in dates or sanitized names,
  and a real sync would rewrite the dates or re-download those files.
- **No copy yet:** expect everything to download, into the folder the user expects.

A large share can take minutes to list; run it in the background and summarize the
log rather than reading all of it. Report the counts, and put them in the entry's
`note`.

## 6. Hand back

Say what was added and what the dry run showed. The next `trove sync` includes the site,
and `trove sites` reports how it's doing. Don't run a real sync unless asked.
