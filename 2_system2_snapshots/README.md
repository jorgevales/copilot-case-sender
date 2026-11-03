# Part 2 — System 2 snapshots

`system2_snapshots.py` builds one JSON snapshot per account, in bulk, with parallel signed-in requests. Each snapshot
holds exactly four blocks: `Account Summary`, `Interested Parties linked to the account`, `Interested Party links to
other account numbers` and `Nine Account Detail Sections`. It is saved as
`RUNTIME_ROOT\pipeline\snapshots\<day>\<ACCOUNT>_Complete_Account_Profile.json` (the name is decided only in
`shared/pipeline.py`). The coordinator copies it into every case folder of that account once Part 1 has finished the
case. A snapshot made earlier the same day is reused only if it reads back complete for that account (four blocks,
nine sections in order, each with a valid result); otherwise it is set aside as `.incomplete.json` and fetched again.
The older three-block snapshots have another name and are never reused.

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
- `system2_party_links_page`: with `{party}`;
- `system2_detail_pages`: the nine account-detail pages, each with `{account}` (setup asks for them one by one; the
  run does not start while one is missing);
- `system2_customer_summary_page`: with `{customer}` where the CSV's Client Number goes; required, and on the same
  host as `system2_url` (the run does not start otherwise). Its address is never printed, logged or written.

**Charity Commission Number** (2026-11-03). The complete account profile may also hold a customer-level
`Client Charity Commission Number` in each matching `Account Summary → metadata → csvMatches` row:
- read from the separately configured customer summary page, for the row's **CSV Client Number as given** (trimmed,
  leading zeros kept; no account-summary fallback, a row with a blank Client Number gets no request);
- requested **once per Client Number per run**, however many rows or accounts share it, in the same 40-worker pool and
  through the same session, retry, pause, Access-Denied stop and stop signal as every other page;
- the answer must stay on the configured page (a redirect is rejected, never merged). The page is not checked
  against the customer number (it does not show one);
- read from the visible `Additional Info` panel, visible row `Charity Commission Number` (spacing, case and a trailing
  colon ignored; hidden panels / rows / cells, buttons and editable controls ignored; a static display wins), as text
  exactly as shown;
- the key goes **right after `Client Number`**, on every row of that customer and only there; nothing else in a row
  changes and no row is added. `""` only when the field is on the page and blank; when the field is not shown or the
  page was not captured, the key is left out;
- `metadata.customerAdditionalInfo` records each Client Number's result: `captureStatus` (`Captured` /
  `Blank on Register` / `Not available` / `Not captured (...)`), `completeness` (`outcome`, `panelFound`,
  `fieldFound`) and `sourceReference: "configured customer summary page"` (no address);
- **optional**: a page not captured never holds a case. The snapshot is saved, the console says so, and a same-day
  re-run fetches that account again (also any snapshot made before this change);
- read back after writing: rows unchanged in number and order, the key's place, exact text and customer, else the
  file is set aside as `.incomplete.json`;
- `diagnostics\part2\accounts.json` holds counts only (`customer_summary_pages_requested` / `_reused` /
  `charity_numbers_extracted` / `_confirmed_blank` / `charity_number_fields_not_shown` / `customer_summary_pages_failed`),
  never a number, a customer or an address. Private routes stay in the git-ignored config.

**Nine account-detail pages** (2026-11-03): `01 Personal Details`, `02 Contact Details`, `03 Correspondence Details`,
`04 Trading Details`, `05 Banking Details`, `06 Other Details`, `07 Suspended Activity`, `08 Future Dated
Instructions`, `09 Flags and Custom Details`. The readers follow the user's sanitised parser reference (element ids,
headings and wording of the real pages):
- requested for each account whose summary was read, in the SAME 40-worker pool as the party/link pages (no more
  requests at once than before), through the same session, retry, pause, Access-Denied stop and stop signal; pending
  pages are cancelled on a stop; results always in the order above;
- each page must stay on its configured address (a redirect = `Request failed`) and, when it shows an account code,
  show the requested account (another one = `Account validation failed`);
- only visible values: hidden attribute, `hidden` / `hide` / `d-none` classes, `display:none`, `visibility:hidden`
  are left out; a folded (`collapse`) section is visible. A drop-down gives its chosen option, never its option list;
  a static display wins over its control unless it is empty; a tick box is `true` / `false`, or `null` when the control
  is not on the page; repeated labels become lists; action labels and columns are dropped; tables keep columns, rows and
  blank cells in order, with no-records text under `message`;
- each section is `{captureStatus, data, completeness}`. Outcome: `Extracted successfully`, `Confirmed empty`,
  `Not found`, `Extraction failed`, `Account validation failed`, `Request failed` or `Unexpected page layout` (none of
  the page's own landmarks found). An empty result is never success: `Confirmed empty` needs proof - Future Dated
  Instructions: the page says "No FDT Instructions linked to Account."; Banking: no `BANK ACCOUNT n` row on a page with
  the main form, the account header and a "Banking Details" title. Optional parts not shown (e.g. Interest
  Distribution Options) are listed under `optionalSectionsNotShown`, only once the page was read;
- **any section not `Extracted successfully` or `Confirmed empty` fails the account**: nothing is written, its cases are
  held with the page names, and the page is kept in `captures\<run>\system2-<account>-<page>.txt/.html`;
- the file is written under a staging name, read back (four blocks, nine sections in order, valid results, the
  account, party / link / contact / bank-account / instruction / table-row counts) and only then given its real name;
- `diagnostics\part2\accounts.json` adds counts only: sections extracted / confirmed empty / not found / failed, the
  failed page names, bank accounts and instruction rows written, file size and time - never a value.

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
