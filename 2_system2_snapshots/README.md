# Part 2 — System 2 snapshots

`system2_snapshots.py` builds one JSON snapshot per account, in bulk, with parallel signed-in requests. Each snapshot
holds the account summary, its linked Interested Parties and their other customer/account links. It is saved as
`RUNTIME_ROOT\pipeline\snapshots\<day>\<ACCOUNT>_Account_Summary_Interested_Parties_and_Links.json`. The
coordinator copies it into every case folder of that account once Part 1 has finished the case. A snapshot made
earlier the same day is reused.

- **Started by** the coordinator, in the background. Its output goes to `...\logs\part2.log`, and its progress shows
  on the sender's console as `system2`.
- **Sign-in**: the System 2 cookies are read straight from the systems Edge over its loopback DevTools endpoint. No
  second Playwright driver is started, and no cookie is ever written to a file. Its check: the `system2_url` page
  shows "logout".
- **Access Denied** (HTTP 401/403, or a page titled Access Denied / Forbidden / Not authorised): after **one** retry
  the whole run **stops** (shared stop signal), so the system is not hammered. Answers already sent to Copilot are
  still collected.
- **Expired sign-in** (a page without "logout", or a sign-in page): this part **pauses**. The sender's console asks
  you to sign in again in the systems Edge and press Enter, then it continues with fresh cookies.
- **Any other failure**: the account is retried once, then reported in `status\part2-failed.json`. Its cases are
  held ("no System 2 snapshot"), never sent without one.
- **For the report**: when an account (or the sign-in check) fails, the page System 2 answered with is kept in
  `RUNTIME_ROOT\logs\captures\<run>\system2-<account>.txt/.html` (HTTP status, title, text; addresses cut before
  `?`). `diagnostics\part2\accounts.json` in the run folder says per account: reused (with the file's time) / saved /
  failed, and how many party pages could not be read.

**Pages**: every System 2 address comes from the config, pasted once in setup. None is written in this code (the
repository is public):
- `system2_url`: the signed-in check;
- `system2_account_page`: with `{account}`;
- `system2_party_marker`: the text in the party profile links;
- `system2_party_links_page`: with `{party}`.

Kept unchanged from the original script: every parser, field label, the 24 account / 40 party worker pools and the
snapshot's JSON layout. Changed: each party/link page is now requested only once (the original could submit
duplicates).
