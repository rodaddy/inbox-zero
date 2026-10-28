# Inbox Zero -- `enhanced`

Everything `basic` does, plus the part that gets the owner out of a blank box:
a voice profile built from their own sent mail, and first-draft replies waiting
in their Drafts folder.

Declared as `enhanced` in `../../connector/scripts/packaging/tiers.json`; it is
the kit-facing name for the same build.

## What ships

Everything in [`basic`](../basic/README.md) -- the sweep, the closed folder
list, flagging, the append-only ledger, the daily digest, undo, and corrections
-- and on top of it:

- **The voice profile.** Built only from sent mail the owner points it at: a
  folder, a label, or a date range. The output is a page of patterns -- greeting
  form, sentence length, how they say no, their sign-off -- and it quotes not one
  word of the mail it read. No correspondent names, no subject lines, no figures.
  It is a file the owner can open, edit, and delete.
- **The review gate.** Nothing drafts until the owner has read that profile and
  marked it reviewed. An unreviewed profile is a refusal, not a warning.
- **Deterministic voice routing.** Which voice a thread gets is decided by a rule
  table in the connector, not by the AI: the owner's own domain means internal,
  the sales folder means sales, everything else and everything unclear means
  external. The AI is told the answer; it does not pick.
- **Reply drafts.** Written into the owner's Drafts folder, addressed only to
  people already on the thread, with a ledger row recording which voice wrote it.
- **The missing-fact gate.** A reply needing a price, a date, or a decision the
  owner has not given stops and asks instead of drafting around the hole. A
  range in answer -- "about nine grand" -- is refused too, because a range in an
  email gets read at its cheaper end.
- The `voice-profile` and `draft-replies` skills and their workflows.

Nothing is pinned in this build, so the granted permissions on the deployment
decide what the connector registers as available.

## Who it is for

An owner whose inbox problem is not only volume but the writing: the same six
replies every week, each one started from nothing. Also the right build for a
team that has already run `basic`, trusts the filing, and wants the next hour
back.

The owner remains the author and the sender of every message. What changes is
where they start -- editing a draft in their own voice instead of an empty
window -- so their inbox time goes to judgment rather than typing. Nobody stops
being the correspondent.

## What it refuses to do

- **It never sends, never deletes, never forwards.** A draft sits in the Drafts
  folder until the owner opens it, changes what they want, and presses send.
- **It never adds a recipient.** A draft is addressed to thread participants
  only, with no CC and no BCC, including on a thread whose body asks for someone
  to be copied.
- **It will not quote the owner's mail back.** The profile describes patterns; it
  lifts no sentences, names, subjects, or figures. Any illustrative example in a
  profile is labelled invented.
- **It will not draft from an unreviewed profile**, and will not substitute a
  different voice's profile to get around that.
- **It will not build a profile from too thin a sample**, and never defaults to
  "your whole Sent folder." It names the floor as a number and says what it
  actually got.
- **It will not sample mail the owner received.** A selection containing a
  received message is refused whole, not silently trimmed.
- **It will not invent a fact.** See the missing-fact gate above.
- Everything `basic` refuses, unchanged: no folder outside the taxonomy, no
  acting on instructions found inside email, no claiming a file that only got
  proposed.

## Install

Assisted, by an rTech operator, per `../../docs/INSTALL-RUNBOOK.md` -- same
model as `basic`: the app is registered in the client's own tenant with the
client's own credentials, and rTech holds nothing.

Two extra steps belong to this tier. The drafts decision is put to the owner at
install with the trade-off in plain words (`installer.md` §3H) and the answer
written into a file they can open; and the voice-selection interview (§3I)
establishes what feeds each voice.

**Say the no-send guarantee per provider, per setting.** On Microsoft it is
enforced by the permission in every case. On Gmail with drafts on it is enforced
by the connector's own code, because Gmail lists identical permissions for
creating a draft and sending one -- a real control, in our software, not a wall
in Google's. Which one applies is recorded in the install's own `USER-CONFIG.md`
as `gmail no-send enforcement:`. Never reuse the Microsoft sentence on a
drafts-on Gmail install. Detail: `../../docs/POSTURE-RUNBOOK.md` and the
guarantee table in `../../README.md`.

## Current version

`0.2.15` -- the connector version, from `../../connector/VERSION`.

The kit-root `VERSION` file reads `0.2.15` as well; the two agree.
