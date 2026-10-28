# examples

Copy-paste install helpers that ship with the kit. Each tier script calls
`inbox-zero-connector init-config` with that tier's posture, then prints the
scope manifest as install evidence.

- `tier-basic.sh` / `tier-basic.ps1` -- basic tier: file-only, no drafts.
- `tier-ai.sh` / `tier-ai.ps1` -- AI tier: triage plus voice reply drafts.
- `taxonomy-business-personal.json` -- a sample folder taxonomy to adapt.

Run the `.sh` pair on macOS/Linux and the `.ps1` pair on Windows; same flags,
same result.
