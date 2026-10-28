# Workflow: Selection interview

Run this before any sent mail is read, once per voice. It is the step that
makes "we only read what you pointed us at" true rather than a slogan.

## Steps

1. **Read `branding/mail-lane.md` first.** On lane 1 the connector's
   `read_sent_sample` takes the selection you record here. On lanes 2 and 3
   there is no sent-mail access at all — go to the lane-2/3 refusal in
   `SKILL.md` and offer the paste path instead.

2. **Explain the default three voices in one breath**, then ask which they
   want:

   > "I'll learn three separate versions of how you write, because most people
   > write differently to their own team than to a client. The three are: your
   > own colleagues, people outside the company, and new business or quotes. I
   > can do fewer if you'd rather — or add one if you've got a group you write
   > to in a particular way."

   Record the final voice list in `USER-CONFIG.md` as
   `voice profiles: internal, external, sales`.

3. **For each voice, ask the selection question.** One voice at a time, and
   name the voice you are asking about:

   > "First, people outside your company. Which sent mail should I read for
   > that? A folder or label you file that kind of mail into, or a date range —
   > the last six months of your Sent folder, say. I read only what you name
   > here."

   **Ask for a folder/label or a date range. Those are the two forms that
   work.** The connector reads sent mail only and refuses a selection that
   names anything else, so "point me at a few good threads" is not a form to
   offer: a thread holds both sides of a conversation, and the received half is
   mail this tool does not sample. An owner steered to threads gets a refusal
   instead of a profile, which is a worse experience than not offering it.

   If the owner volunteers particular threads anyway, do not refuse them flatly
   — convert:

   > "I can only read the side you wrote, so let me come at it from your Sent
   > folder rather than the threads themselves. Are those the ones you filed
   > under [label], or roughly what months were they? Either of those I can
   > work from."

   If they say "just use my Sent folder," that is a valid selection — but pair
   it with a date range, because "everything I have ever sent" is not a window,
   and say why:

   > "Let's put a date on it — the last six months, say. Writing drifts, and
   > old mail describes somebody you used to be. It also means I'm reading a
   > lot less of your mail than I would otherwise."

4. **Ask the exclusion question. Do not skip it.** This is the question owners
   are glad you asked, and it is where a confidentiality-minded buyer decides
   whether they trust the kit:

   > "Anything I should stay out of? The usual ones are anything to your lawyer
   > or your accountant, anything about staff, or one particular client. I'll
   > write it down and it holds for every run from now on."

   Record exclusions even when the answer is "nothing" — write `(none)`, so a
   later reader can see the question was asked and answered.

5. **Say the size back before reading anything.** Estimate from the selection
   and state it in plain terms:

   > "That's about four months of your Sent folder — probably forty or fifty
   > messages, and I'll only end up using the ones that are real writing rather
   > than forwards and one-word replies. Sound right, or do you want to narrow
   > it?"

   An owner who hears a number gets one last chance to narrow it. An owner who
   never hears one finds out afterwards, which is the wrong order.

6. **Write `voice/selections.md`** in the shape given in `SKILL.md`, one block
   per voice, with fully-spelled-out keys. This file is the record of what the
   owner permitted; `read_sent_sample` is called from it and from nothing else.

7. **Ask about routing only if they added a voice.** The default three route
   themselves (own domain → internal; sales folder → sales; everything else →
   external). A custom voice needs a mechanical rule:

   > "How do I know a message belongs in that voice? Give me something I can
   > check automatically — a folder it gets filed in, or the email domain it's
   > going to. If it's a judgement call, I'll get it wrong sometimes and you
   > won't know when."

   Record the rule where the connector reads it (`connector/README.md` names
   the location) and mirror it into `voice/selections.md`. **A voice with no
   mechanical rule does not get created** — say that plainly and offer to fold
   it into `external` instead.

## Rules

- Never propose a selection the owner did not name. "I'll just read your Sent
  folder" is the failure mode this whole workflow exists to prevent.
- Record selections as a folder/label or a date range. The connector reads sent
  mail only; a selection built from thread message ids normally names received
  mail too and is refused outright, so recording one sets the next run up to
  fail rather than to read something it should not.
- Write date bounds as `YYYY-MM-DD`. Any other form is refused by the connector
  before a request is made.
- Never widen a recorded selection later to improve a result. If the sample is
  too thin, that is the refusal in `SKILL.md`, and the owner decides whether to
  widen it.
- Exclusions are permanent until the owner changes them, and they apply to
  every future run, including updates.
- Nothing in this workflow reads a single message. It only records permission.
