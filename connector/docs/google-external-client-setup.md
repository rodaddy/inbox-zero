# Google External OAuth Client Setup (Inbox Zero connector)

Status: WRITTEN 2026-08-23, PROPOSED until the first consumer Gmail
receipt.

Purpose: stand up ONE rTech-owned External Google OAuth client so consumer
@gmail.com users can run the Inbox Zero connector without each of them
creating their own Cloud project. Decided by Rico 2026-08-23 (#117).

Project note: `rtech-gws-admin` is the only rTech GCP project observed, and
it is the Workspace-admin project. Prefer a DEDICATED project such as
`rtech-inbox-zero` unless Rico says otherwise; the 100-new-user cap in
section C is per-project and lifetime, so do not spend it on the admin
project.

## A. Rico's console steps

1. Create or select the project.
   https://console.cloud.google.com/projectcreate
   Record the project id; every step below uses it.

2. Enable the Gmail API.
   https://console.cloud.google.com/apis/library/gmail.googleapis.com
   Or: `gcloud services enable gmail.googleapis.com --project <id>`

3. Auth Platform branding.
   https://console.developers.google.com/auth/branding
   Sets App Name, User Support Email, App Logo, App Domain ("specify your
   home page, privacy policy, and terms of service links"), Developer
   Contact Information. Google's guidance: "Choose an app name that
   distinctively represents your business".

4. Audience: set user type External.
   https://console.developers.google.com/auth/audience
   This one page owns BOTH user type (External/Internal) AND publishing
   status (Testing/In production) AND the test-user list. Internal is not
   an option for consumer users: "Projects configured with a user type of
   External are available to any user with a Google Account", while
   Internal requires a Cloud Organization and limits access to its members.

5. Add test users on that same Audience page:
   the owner's test Gmail accounts
   Cap is 100 ("up to 100 test users listed in the OAuth consent screen");
   "A test user consumes a project's test user quota once added to the
   project" - so added users do not free up.

6. Scopes: request gmail.modify only
   (https://www.googleapis.com/auth/gmail.modify). Narrowest scope that
   supports label/read/archive work. Do not add gmail.send.

7. Create the client.
   https://console.developers.google.com/auth/clients
   Create client > Application type = "Desktop app" > name > Create.
   "The Google Cloud console does not require any additional information to
   create OAuth 2.0 credentials for desktop applications."

8. Store the credentials in Vaultwarden, entry
   `rTech - Inbox Zero Google OAuth (External)`, custom fields
   `client_id` and `client_secret`. Do not paste either value into the
   repo, an issue, or chat.

## B. What the agent does after

1. Build a consumer rehearsal copy under
   `~/scratch/gmail-consumer-rehearsal/`
   and point `INBOX_ZERO_CONFIG_DIR` at it, so no rTech-Workspace config or
   token is touched.
2. Pull client_id/client_secret from Vaultwarden into that config dir.
3. Run `auth google` as you@gmail.com and consent through the
   unverified-app screen.
4. Run `watch --once` and confirm a real triage pass on the consumer inbox.
5. Post the receipt (command, exit code, counts, no secret values) to #117.

## C. Testing vs Production facts

- Refresh token expiry in Testing: "A Google Cloud Platform project with an
  OAuth consent screen configured for an external user type and a
  publishing status of 'Testing' is issued a refresh token expiring in 7
  days, unless the only OAuth scopes requested are a subset of name, email
  address, and user profile."
  (https://developers.google.com/identity/protocols/oauth2). Corroborated:
  "Authorizations by a test user will expire seven days from the time of
  consent" (https://support.google.com/cloud/answer/15549945).
  gmail.modify is NOT in that subset, so the 7-day expiry APPLIES. Testing
  mode therefore means re-consent weekly.
- Unverified app screen: appears "if your project's OAuth clients request
  authorization of scopes considered sensitive or restricted before your
  project has completed verification"; Testing status also shows "warning
  messages displayed before authorization."
- In production while unverified: allowed but capped. "Unverified apps that
  are accessing restricted or sensitive scopes have a 100 new-user cap
  restriction", and failure to verify "will result in your project's 100
  new-user cap eventually getting exhausted and Google sign-in being
  disabled for your users." The cap "applies over the entire lifetime of
  the project, and it cannot be reset or changed."
  (https://support.google.com/cloud/answer/13463817)
- Restricted-scope verification path (D2 on #117): homepage "must be hosted
  on a verified domain you own" (ownership via "Google Search Console") and
  "must accurately represent and identify your app or brand"; the privacy
  policy "should be hosted within the domain that hosts your homepage" and
  must disclose "how your app accesses, uses, stores, and/or shares Google
  user data"; scope justification must "only request the narrowest scope(s)
  your app needs to function"; the demo video must show the "end-to-end
  flow of your app including the OAuth grant process".
  (https://support.google.com/cloud/answer/13464321)
- Restricted scope also triggers assessment: "Apps requesting access to
  restricted scopes must meet the additional requirement of secure data
  handling by submitting to an annual security assessment." Google "does
  not charge the developer any fees for security assessment", but "The cost
  for such a service is agreed on between the developer and the assessor"
  (third-party CASA assessor). Re-verification and reassessment are due "at
  least every 12 months after your assessor's Letter of Assessment (LOA)
  approval date." (https://support.google.com/cloud/answer/13463816)
- Google-stated review timelines: Brand Verification "2-3 Business days",
  Sensitive Scope "10 Business days", Restricted Scope "6 weeks" - these
  "estimates are not guaranteed and will vary based on developer
  responsiveness."

## D. Not covered / not stated here

- gmail.modify's exact restricted-vs-sensitive classification was not
  fetched this session; confirm on
  https://developers.google.com/workspace/gmail/api/auth/scopes before
  committing to the assessment path.
- CASA tier durations ("2-3 weeks for tier-2", "4-6 weeks for tier-3") and
  cost range ("$500 to $4,500 USD") came from a search summary, NOT a
  directly fetched Google page. Unconfirmed.
- No verified gcloud/API path exists for creating a Desktop OAuth client;
  treat step 7 as console-only.
- Whether rTech pursues production verification at all is Rico's call and
  is not decided here.
- No pricing, packaging, or client-facing copy is settled by this doc.
