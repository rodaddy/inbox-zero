# `profiles/` — rTech-side build recipes, never shipped

A profile is the recipe `scripts/packaging/build_custom.py` reads to build ONE
client's pre-configured zip. One file per mailbox, named for it.

| Key | Meaning |
|---|---|
| `name` | Profile slug; becomes the zip name `inbox-zero-connector-<name>-<ver>.zip` |
| `zip_tier` | Which tier in `scripts/packaging/tiers.json` to cut (`enhanced`, `basic`) |
| `config` | Overlay merged onto the tracked `config/connector-config.json` |
| `secrets` | Vaultwarden ENTRY NAMES + field — never values |

`config` is a deep overlay, so a profile states only what differs from the
committed defaults; every unnamed setting stays whatever `config/README.md`
documents.

`secrets` leaves are `{"vault": "<entry name>", "field": "<field>"}` — a
POINTER the builder resolves at build time and writes into the zip's
`secrets/config.json`. A plain string leaf is refused by the builder, because a
literal in this folder is a secret committed to git.

Profiles never ship. `tiers.json` `never_ship` carries `profiles/**`, so no
client zip contains this directory.

A Gmail profile resolves both halves of the client's Google OAuth client
from a Vaultwarden entry:
`google_client_id` from its `client_id` field and `google_client_secret` from
its `client_secret` field. Neither value is ever written into this folder.

`TBD-#NN` is a placeholder waiting on that GitHub issue — the build is blocked
on the real value until the issue lands it.

## Install ids — one mailbox per install

Every custom zip carries an `install_id`, and the builder defaults it to the
profile `name` when the profile's `config` overlay does not set one. That id
becomes the last segment of the install's config and data directories and the
suffix on its keychain accounts, so two zips built from two profiles are fully
isolated on one machine without anybody opting in. Set `install_id` explicitly
in the overlay only when a rebuild must keep an EXISTING install's namespace —
changing it points the install at empty directories and an unstored token, so
it is effectively a fresh install.

## Judging a custom zip

`build_custom.py` writes the artifact and then RUNS the judge over it, printing
the `ASSERT WITH:` command first so the operator has a line they can re-run
verbatim. The builder's exit code means "the zip was written AND it passed".

It used to only print that line. #145 is why it no longer does: an unjudged zip
is indistinguishable on disk from a judged one, so the artifact most likely to
reach a client was the one built by whoever did not paste the second command.

```
uv run python scripts/packaging/assert_zip.py --custom <zip> <zip_tier>
```

`--custom` is a flag rather than a tier because the two are orthogonal: a
custom build still cuts the profile's `zip_tier` (`enhanced` or `basic`) and every
rule for that tier still applies. The flag changes these things:

- `secrets/config.json` is ALLOWED. That member is refused by `never_ship` in
  every kit tier, and it is the whole point of a managed install — the client
  never touches configuration.
- That member is then REQUIRED, must parse as JSON, and no leaf string may be
  empty or contain `REPLACE-ME` or `TBD`. An unfilled credential means the
  vault lookup or the `TBD-#NN` block never resolved, and the client would
  install a connector that cannot authenticate.
- `config/connector-config.json` is scanned for those same markers. The
  tracked config ships `REPLACE-ME` for `google_client_id` on purpose — that is
  correct content for a KIT, which the buyer's runbook tells them to fill in —
  and it is fatal in a managed install. **A profile whose `config` overlay does
  not resolve every placeholder cannot build.**

Violations name the KEY PATH only (`credentials.google_client_id`), never the
value. The judge's stdout lands in terminals, CI logs, and handoff notes, so
echoing a leaf would put a live credential in all three.

## Secretless profiles — `secrets` absent means you SAID so

A profile with no `secrets` key (or `"secrets": {}`) builds a zip whose
`secrets/config.json` is `{}`, and the builder stamps a one-line marker into
the ZIP ARCHIVE COMMENT recording that the profile declared itself secretless.
The judge reads that marker.

This exists because `{}` has two causes the bytes cannot distinguish: the
profile genuinely needs no secret — the Microsoft path is a multi-tenant PUBLIC
client, so its client id lives in the connector config and there is nothing to
resolve — or the profile DID declare secrets and every vault read silently
failed. Before #145 both judged clean, and a client 0.2.8 artifact
shipped `{}` with an `OK` receipt.

So: **state it by omission and the build passes; leave a `secrets` block that
resolves to nothing and the build fails.** The marker is not a security control
(anyone can rewrite a zip comment) and does not need to be — it defends against
a silent build failure, not against a forger.

## The zip carries HEAD's bytes, not your working tree's

Every tracked member is read from `git show HEAD:./<path>`, so a custom zip is
a function of a COMMIT and an uncommitted local edit cannot ride into a client
handover. When shipped members differ from `HEAD` the builder prints a
`WARNING:` naming each one and builds anyway — the bytes shipped are correct
either way, and the warning is what tells you the zip does not contain the edit
you are looking at. Commit first if you meant to ship it.

`secrets/config.json` is stored mode `0600` and is sorted INTO the member list
rather than appended after it, so two builds of one profile against one commit
are byte-identical.
