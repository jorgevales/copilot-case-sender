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

**Interested Party profile** (2026-11-03; the snapshot missed email, phone and address records). The page System 2
sends holds those records only inside its own setup functions (`AddExistingCustomerInterestedPartyEmails`, `...PhoneNumbers`,
`...Addresses`), which build the table rows in the browser; the tables themselves hold just a header and a `NoEmail` /
`NoPhone` / `NoAddress` placeholder. The old parser therefore wrote "Blank on Register" for emails and addresses and
took the phone placeholder for a record. Now:
- the setup functions' records are **read as text, never run** (a small strict reader: plain text values, numbers,
  the `++LastEmailId` counter; anything else = the section is `Not captured` with the reason). Finished rows a page
  does carry are read too (cell ids matched by prefix inside each row, any suffix; `td.TableHead` headers; no `thead`
  needed) and compared with the script; if they disagree the section is `Not captured` and both are kept;
- every record, in page order, never merged; phone parts as text (leading zeros kept); address lines 1-6 with blanks;
  hidden database references are left out;
- a table is `Blank on Register` only when the setup function lists no record; a placeholder alone proves nothing;
- each section has a `completeness` block: outcome (`Extracted successfully` / `Confirmed empty` / `Not found` /
  `Extraction failed` / `Discrepancy`), source, records detected and written, placeholders left out, notes;
- Personal Details and AML / PEP / Financial Sanctions keep their values and add `controls` (selected label AND value,
  ticked or not, disabled / read-only); hidden dialog defaults are left out; AML / PEP / FS fields are keyed by their
  control IDs (`AML Check Comment` ...), since all three comment boxes are labelled "Financial Sanctions Check Comment";
- the saved signature (`imgSignature`, never the upload preview) is reported present / absent / unreadable; nothing of
  the image is kept or logged;
- each party says whether its separate **Interested Party Link** tab (the links page) was inspected; the links page
  says whether each of its tables was found;
- after writing, the file is read back: if it holds fewer contact records than were read it is renamed
  `....incomplete.json` (never reused) and the account fails (its cases are held).

Kept unchanged from the original script: the account / links parsers, the 24 account / 40 party worker pools and the
snapshot's JSON layout (only added to). Changed: each party/link page is now requested only once (the original could
submit duplicates).
