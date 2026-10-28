# docs/security

Security exhibits that ship with the kit. Each file is a statement the
buyer can check against the source it cites, not a policy.

- `write-paths.md` -- the mutation statement: every write the connector can
  perform, in the mailbox, on disk, in the keychain, and on the wire, each
  with the path and line it lives at. Ships in the enhanced tier only; the
  basic tier withholds it because it documents the drafting and voice
  writes that tier does not carry (`scripts/packaging/tiers.json`).
