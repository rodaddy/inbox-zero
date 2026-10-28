# Workflow: Review and approve a voice profile

A profile is not usable until the owner has read it. This workflow is that
gate. It is short on purpose — the work is the owner's, not yours.

## Steps

1. **Show the whole profile in the chat.** Not a summary, not "here are the
   highlights" — the full text of the file, so the owner reads exactly what is
   written down. A summary is how a line nobody agreed to survives into a file.

2. **Say what it is and what you want from them**, in these words or close:

   > "Here's what I think your outside-the-company voice is. Read it properly —
   > I'd rather you disagree with it now than in a draft next week. It describes
   > patterns and doesn't quote anything you wrote, on purpose, so it's safe to
   > have sitting in a file. Cross out anything you don't want in there. Tell me
   > when it's right and I'll mark it approved. Until then I won't write
   > anything with it."

3. **Take their edits verbatim.** The owner's description of their own voice
   outranks yours in every case, including when the sample says otherwise.
   If they say "I'm not that blunt," change the line — do not argue from the
   sample. It is their name on the mail.

4. **Take their redactions without asking why.** If they want a whole section
   gone, delete it. If a section disappears, note the consequence plainly and
   move on:

   > "Dropped the section on how you say no. Drafts in that situation will play
   > it straight and plain, and you'll probably rewrite them."

5. **Flip the flag when they confirm.** Set in the file:

   ```
   reviewed by owner:   yes
   reviewed on:         2026-08-17
   ```

   Say one line back: "Approved — I'll use that for outside-the-company replies
   from now on. Say 'update my external voice' any time it drifts."

6. **If they do not answer, nothing happens.** No timeout, no "I'll assume this
   is fine." An unreviewed profile stays unreviewed and `draft-replies` keeps
   refusing to use it, which is the correct behaviour and not a bug to work
   around.

## Rules

- Never mark a profile reviewed on the owner's behalf, and never infer approval
  from silence or from a change of subject.
- Never argue an owner out of an edit using the sample as evidence. You read
  forty emails; they have written every one they ever sent.
- If the owner asks to delete a profile entirely, delete the file and record
  the voice as unavailable in `voice/selections.md`. Drafts for that voice then
  fall back to the branding-file house voice, and the drafter says so.
