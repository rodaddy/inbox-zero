# Workflow: Update a voice profile

Run this when the owner says the drafts sound wrong — "too stiff", "too
chatty", "that's not how I'd say it" — or when they ask for an update outright.

## Steps

1. **Find out whether this is a profile problem or a draft problem.** One
   question, before touching anything:

   > "Is it every draft in that voice, or was that one just off? If it's every
   > one, I'll rebuild the profile. If it's one, tell me what you'd have written
   > instead and I'll note it as a preference."

   A single bad draft is a preference, recorded in `USER-CONFIG.md`
   (`voice preference (external): shorter, no closing pleasantry`). Rebuilding
   a whole profile from one complaint throws away a profile the owner already
   approved.

2. **Confirm the selection before reading anything.** Never silently re-read
   the previous window:

   > "Same folder and date range as last time — Sent, since February — or
   > something different? If your writing's changed, a tighter recent window
   > gives a truer read."

   Update `voice/selections.md` with the new selection and the new date.

3. **Keep the old profile.** Rename the existing file to
   `voice/<name>.md.previous-<YYYYMMDD>` before writing the new one. Never
   overwrite. An owner who preferred the old one has to be able to say so, and
   "I liked the last one better" is a normal thing to say.

4. **Rebuild** via `build-profile.md`, unchanged: same floor, same no-quoting
   rule, same read-versus-usable counts.

5. **Reset the review flag.** The new profile is `reviewed by owner: no`, even
   though the previous one was approved. Approval attaches to a specific file,
   not to a voice. Run `review-and-approve.md`.

6. **Fold in the recorded preferences.** Any `voice preference (...)` lines the
   owner has stated since the last build get written into the new profile as
   their own section, marked as coming from the owner rather than the sample:

   ```
   ## Owner's own corrections
   source: stated by owner, 2026-08-14
   - Shorter. Cut the closing pleasantry.
   - Never open with "I hope this finds you well".
   ```

   The owner's stated corrections outrank anything read from the sample, in
   this file and in every draft written from it.

## Rules

- Never rebuild without confirming the selection. The old selection may include
  a folder the owner has since decided is off-limits.
- Exclusions carry forward automatically and are never dropped on a rebuild.
  If the owner wants an exclusion lifted, they say so and you record the change
  with its date.
- A rebuilt profile is unapproved until read. There is no "they approved the
  last one" shortcut.
- If the rebuild's sample now falls below the floor of 15 usable messages,
  refuse per `SKILL.md` and keep the previous approved profile in place. Say
  that plainly: "I'd rather keep the one you already approved than replace it
  with something built on nine emails."
