# Workflow — Undo a filing

Triggered by: "undo that last one", "undo the whole sweep", "that should have
gone to Finance", "put that back", "wrong folder".

Undo is the feature that makes the rest of the kit safe to use. Nothing was ever
deleted, so undo is always a move back — never a recovery.

## The ledger is what makes undo possible

Every row records the destination. For a `filed` row you also need the origin.
When you file a message, record the origin in the row so undo is mechanical:
write the origin folder into the `what happened` cell as `filed from <origin>`
when the message came from somewhere other than the inbox. A message swept out
of the inbox reads simply as `filed`, and its origin is the inbox.

**Never edit or remove a ledger row to undo something.** An undo appends a NEW
row with `what happened: moved back`. The record shows both the mistake and the
correction, which is the whole point of an audit ledger.

## "undo that last one"

1. Read the last `filed` or `flagged` row in `ledger/actions.md`.
2. Say what you are about to reverse, in one line: "Putting *Re: certificate for
   Monday* back in your inbox from Action Needed."
3. Reverse it:
   - a `filed` row → `file_message` back to the origin (the inbox unless the row
     names another origin);
   - a `flagged` row → `mark_important` clearing the flag.
4. Append a `moved back` row with `who asked: owner`.
5. Confirm in one line.

If the last row is already a `moved back`, say so and ask which one they mean —
do not undo an undo silently.

## "undo the whole sweep"

1. Find the contiguous block of rows from the most recent sweep (rows with
   `who asked: sweep`, newest first, back to the previous non-sweep row or the
   previous sweep's boundary).
2. Say how many actions you are about to reverse and wait for a yes. This is the
   one place the kit asks before acting, because a bulk reversal is the only
   thing here that is annoying to fix by hand.
3. Reverse them newest first.
4. Append one `moved back` row per reversal.
5. Confirm with the count: "Reversed 34 - everything's back where it was."

## "that should have gone to Finance"

1. **Check the folder is in `taxonomy.md`.** If it is not, this is a taxonomy
   change, not an undo: stop and run `workflows/taxonomy-change.md`. Do not file
   into a folder that is not on the list, and do not create it.
2. File the message to the named folder.
3. Append a row with `who asked: owner`.
4. **Record the preference** in `USER-CONFIG.md` so the same shape of message
   goes there next time. Write it as a plain sentence under a
   `filing preferences:` heading, for example:

   ```
   filing preferences:
   - statements from Chase go to Finance, not Archive-Reference
   ```

5. Say what you learned, in one line: "Got it - Chase statements go to Finance
   from now on."

Preferences are how the taxonomy stops being generic. Always record them; a
correction the kit forgets is a correction the owner has to make again.

## On lanes 2 and 3

The kit did not move anything, so undo is the owner moving it back. Tell them
exactly which message and which folder, then append the `moved back` row once
they confirm they did it. Never write the row first.

## What undo never does

- It never deletes a ledger row.
- It never restores something from a trash or deleted-items folder, because the
  kit never put anything there.
- It never un-sends anything, because nothing was ever sent.
