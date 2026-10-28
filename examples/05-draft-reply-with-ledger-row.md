# Worked example 5 — one draft reply, and the row that receipted it

**SYNTHETIC EXAMPLE. Invented people, invented numbers, `example.com`
addresses.** This is the thread from example 1 that most needed an answer,
drafted end to end — including the gate stopping twice.

**The whole point of this example:** the kit stops before it writes, twice, and
that is the feature. A drafted reply is not an answered thread — Priya opens it,
edits it, and sends it. There is no send in this kit at all.

---

## The thread

```
From: Marcus Feld <mfeld@feldpropertygroup.example.com>
To: priya@ramanfiresafety.example.com
Date: Aug 18
Subject: quote for the Bay Ave building

Priya - got your quote. The number's higher than I expected. Can you
break out what the standpipe work costs on its own? Need to decide by
Friday.

Marcus
```

**What Priya said:** "draft a reply to Marcus."

---

## Step 1 — the voice was resolved, not chosen

The kit did not resolve anything and did not call a tool to ask. `get_thread`
returned the thread with the voice already on it — the connector routes it and
hands it over in the same payload as the message bodies — and the kit used what
came back. It did not decide:

> Routing this to your outside-the-company voice — the address isn't on your
> domain and the thread isn't filed under new business.

`feldpropertygroup.example.com` is not `ramanfiresafety.example.com`, and the
thread sits in Needs Me rather than a sales folder, so the rule table returned
`external`. **Ambiguity would have resolved to `external` too** — it is the more
careful voice, and that default is not an AI judgement call.

The `external` profile (example 4) exists and is `reviewed by owner: yes`, so
drafting could proceed. Had it been unreviewed, the run would have stopped:

> "Your external profile is written but you haven't read it yet. I don't write
> mail in your name from a description of you that you haven't agreed with."

---

## Step 2 — the gate stopped it. First time.

Marcus asked for the standpipe work as its own number. The kit does not have
that number and cannot derive it from the total.

> Before I write this: what's the standpipe work on its own, out of the Bay Ave
> quote? He asked for that figure directly, so a reply that talks around it just
> starts another round — and he's deciding Friday.

**What it did not do**, and each of these is a specific rule:

- did not write `[standpipe figure]` and leave her to fill it in — a draft with
  a hole in it gets sent with the hole in it;
- did not write "I'll follow up with a breakdown shortly" — that is a commitment
  to a timeframe she did not choose;
- did not take a percentage of the total and call it an estimate;
- did not reuse a figure from an older thread.

---

## Step 3 — the gate stopped it again. This is where it usually gets beaten.

Priya, between jobs, answered:

> "somewhere around 9 or 10 grand for the standpipe part"

That is a range, and a range in an email is a promise of its lowest number. The
answer was checked mechanically — **a number must be a single figure, not a
range and not an "about"** — and it failed:

> That's a thousand-dollar spread and he's deciding Friday — if I put a range in
> the email he reads the bottom of it and you'll be holding that number. Two ways
> to go: give me the firm figure and I'll write him a real answer, or I write
> the honest holding line — "the standpipe portion runs more than the rest, I'll
> have the exact figure to you by 9am tomorrow" — and you keep it moving without
> committing. Which one?

It did not average the range, did not round it, and did not write
"approximately" over the top of it.

Priya checked her sheet and came back with **$9,400**. That is a single figure,
so the gate opened.

---

## Step 4 — the draft

Written to the external profile: first name and a comma, no greeting word,
answer in the first sentence, short sentences, no bullets even though it lists
items, no exclamation marks, restated ask at the end, one-word sign-off.

```
To:      Marcus Feld <mfeld@feldpropertygroup.example.com>
Subject: Re: quote for the Bay Ave building

Marcus,

The standpipe work is $9,400 of the total.

That covers the hydrostatic test on both risers, the valve
replacements on floors two and three, and the sign-off paperwork for
the marshal. The rest of the quote is the sprinkler inspection and the
extinguisher service, which are annual and separate from this.

If you want to do the standpipe work on its own this year and hold the
rest, that works and I'll requote it that way.

Let me know by Friday either way.

Thanks,
Priya
```

**Note what the draft does not contain:** no "I hope this finds you well," no
"thanks for reaching out," no "please don't hesitate," no exclamation mark, no
bulleted list. Those are on the profile's "things they never do" list, which is
the most useful section in the file.

**Note also the one thing it did not restate.** Marcus called the number "higher
than I expected." The draft does not agree that the quote is high, because Priya
never said it was. Content from the incoming message is the sender's claim, not
the owner's position.

---

## Step 5 — where it went

`create_draft` put it in her Drafts folder, addressed to Marcus and to nobody
else. No CC, no BCC, no additions — recipients come from the thread and from
nowhere else.

> Draft's in your Drafts folder, in your outside-the-company voice. Open it,
> change whatever you want, and send it when it looks right.
>
> Once you've sent it, tell me and I'll move the thread to Waiting On.

**It did not move the thread.** Drafting is not answering; the thread is still
waiting on Priya until she sends it. And it never said "replied," "sent," or
"handled."

---

## Step 6 — the ledger row

One row, written by the connector at the moment the draft was created — not by
the AI, and not as a separate step the AI could skip:

```json
{"action":"create_draft","detail":"reply drafted into Drafts folder","message_id":"AAMkAGI2...Qw","provider":"microsoft","schema":1,"timestamp":"2026-08-18T12:04:19+00:00","voice":"external"}
```

Read that back in plain language:

| field | means |
| --- | --- |
| `action` | a draft was created — the only draft-related action that exists |
| `message_id` | which thread, so it can be found later |
| `provider` | Microsoft 365 on this install |
| `voice` | **`external`** — which voice wrote it |
| `timestamp` | when, in UTC |

**The `voice` field is the point.** Months later, anyone reading the ledger can
see that a draft to an outside contact used the outside voice and not the
internal one. It records the voice NAME only — never the profile text, never the
draft body, never the recipient. A leaked ledger is not a leaked mailbox.

There is no ledger row saying anything was sent, because nothing was and nothing
can be.

---

## What this looks like on a degraded lane

On lane 2 or lane 3, or on an install where drafts are turned off, everything
above is identical except the last two steps. The kit says, before showing the
text:

> "This is a degraded run — I can't put a draft in your mailbox from here, so
> here's the text to paste. Nothing has been written to your mailbox."

Then it shows the same draft. **The gates do not weaken because the lane did:**
the voice is still resolved mechanically, the profile is still required and
still has to be reviewed, and the range still gets refused. What is lost is the
ability to write into the mailbox and the automatic receipt — there is no
mailbox action, so there is no row.

---

**Reminder: Marcus Feld, Priya Raman, the building, and the $9,400 are all
invented.** Your drafts carry your correspondents and your numbers — the ones
you gave the kit yourself, because it refuses to make them up.
