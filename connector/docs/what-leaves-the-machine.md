# What leaves the machine

Status: WRITTEN 2026-08-24, corrected 2026-08-24 after a source re-read.
Source of truth for the data boundary is the code:
`src/inbox_zero_connector/classifiers/openai_compatible.py`
(`_message_facts`, `build_prompt`, `_tool_schema`) and
`src/inbox_zero_connector/models/mail.py` (no body field exists).

## Classification payload

Two things travel in every classifier request: facts about the message, and
the mailbox's own filing criteria.

**Per message, exactly three fields** — the sender address, the subject line,
and the first 500 characters of the provider's preview snippet. **Never the
body, never attachments, never headers beyond those three.** That list is
enumerated by name in `_message_facts`, so a new field on the message model
cannot start travelling without an edit there. This is the guarantee the
product is sold on and it holds.

**Per request, the mailbox's taxonomy** — the folder NAMES and their
DESCRIPTIONS. `build_prompt` prepends `taxonomy.describe()` to every request,
and `_tool_schema` sends the folder names a second time as a JSON-Schema
`enum` so a compliant provider constrains the model to the installed
vocabulary. Those descriptions are written during the install interview to say
how this owner sorts their mail, so if a folder is named or described after a
client, a matter, or a deal, that text reaches the endpoint on every call.
Name folders accordingly. This is configuration, not mail content — but it is
egress, and an inventory that omitted it would be wrong.

**What does NOT travel:** the mailbox owner's own domain. An earlier draft
plumbed `owner_domain` into the prompt, but no construction path ever supplied
it, so the clause never rendered in any deployment. The parameter has since
been REMOVED rather than left as an unset default (issue #166, closed):
`build_prompt(message, taxonomy)` takes no domain argument, and
`classifiers/factory.py` (`from_settings`) constructs the classifier without
one. Whether the domain SHOULD travel is an open wiring decision; this
document will be corrected in the same change that makes it.

Messages settled by a deterministic rule never reach the classifier at all,
and no network call is made for them. Those are: provider-flagged important,
no sender, Gmail Promotions, and — the category an owner builds themselves —
the LEARNED RULES recorded by `record_correction`, replayed as data by
`services/triage/decide.py`. Every correction an owner makes is one more class
of message that is settled locally from then on. A run with no classifier
configured (`classifier.base_url` null) makes no network call at all and files
everything unsettled into Review.

## Where it goes

The endpoint is the client's. `classifier.base_url` in
`config/connector-config.json` defaults to null (no classifier, rules
only); set it to any OpenAI-compatible chat-completions endpoint the client
owns or pays for, with the key in `secrets/config.json` as
`credentials.classifier_api_key`. rTech-hosted classification (the Bifrost
gateway) is not a default and never a silent fallback: it is provided only
on explicit request, paid, under NDA. The rTech beta installs that ship a
Bifrost endpoint pre-filled are the standing exception, and their zips say
so.

## Google sign-in: the unverified-app screen

The connector's shipped Google client (GCP project `rtech-inbox-zero`) is an
External client in Testing and is not yet verified by Google. On
`auth google` the browser shows "Google hasn't verified this app". This is
expected, not an error. Click-through:

1. Click **Advanced** at the bottom-left of the warning.
2. Click the **Go to ... (unsafe)** link that appears.
3. On the permissions screen, tick the Gmail permission if it is shown as a
   checkbox, then click **Continue**.

Only addresses added as test users on the client can get past that screen;
everyone else sees "access blocked". While the client is in Testing the
grant expires after 7 days and `auth google` must be re-run. Google
verification (which removes both limits) is tracked on
rTech's tracking issue.
