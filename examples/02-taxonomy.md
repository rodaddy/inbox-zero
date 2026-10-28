# Worked example 2 — the filled folder list

**SYNTHETIC EXAMPLE. Made-up business, made-up folders.** This is what
`taxonomy.md` looks like after the install interview, for one invented company.
Yours will differ — that is the point of the interview.

**Who:** Priya Raman, Raman Fire & Safety. Sprinkler inspection and extinguisher
service, eleven people, one office admin. Invented for this demonstration.

---

## What she was offered

The installer offered the default seven and asked her to cross out, rename, or
add up to three of her own:

- Action Needed
- Waiting On
- Finance
- Clients
- Vendors
- Newsletters
- Archive-Reference

## What she changed, and what the kit said

**She asked for "Inspections" as an eighth folder.** Accepted — it is under the
twelve-folder ceiling and she gave it a meaning.

**She asked to rename "Clients" to "Accounts."** Accepted, used verbatim. The
kit does not tidy anybody's capitalization or wording.

**She asked to drop "Action Needed"** because she said she would just look at
the flags. The kit refused:

> "I need one folder for 'I'm not sure — look at this' or I'd have to guess, and
> guessing is what you're paying this thing to stop doing. Keep this one or give
> me another name for it."

She renamed it **"Needs Me"** and kept it. That is the fallback folder.

**She asked for a ninth and tenth folder** ("Permits" and "Old Quotes") and then
took them back herself when she saw the list written out. The kit's line was
that a longer list makes the sorting less reliable, not that she was not allowed.

---

## The resulting `taxonomy.md`

```
# Folder taxonomy — the closed set

fallback folder: Needs Me
taxonomy enforced by: connector configuration + skill rules
last changed: 2026-08-17

| folder name | what goes in it |
| --- | --- |
| Needs Me | I have to do or answer something, or the kit could not tell |
| Waiting On | I answered, the ball is with them |
| Finance | Invoices, bills, payments, statements, anything with a dollar figure |
| Accounts | Customer correspondence that is not urgent and is not about money |
| Inspections | Scheduling, reports, and reinspection notices for annual work |
| Vendors | Suppliers, parts orders, backorders, the alarm monitoring company |
| Newsletters | Subscriptions, marketing, trade bulletins, bulk mail |
| Archive-Reference | Keep it, nothing to do |
```

---

## Why the closed list is the security feature

Eight names. The kit can put a message in one of eight places and nowhere else.
It cannot invent a ninth mid-sweep, and on the connector lane the connector
itself refuses a folder that is not in its configuration — so "closed set" is
something the software enforces, not something the AI promises.

That is what caps the damage from a hostile email. The worst it can achieve, if
it talked the AI into anything at all, is putting itself in the wrong one of
eight folders Priya already looks at. See example 1 for one trying.

---

**Reminder: this is an invented company and an invented folder list.** Your
install runs the interview with you and writes your own file. Nothing here is
copied into your kit.
