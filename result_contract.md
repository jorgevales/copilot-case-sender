# Result contract (strict, machine-read)

This file is attached to every case. **A computer program reads your reply, not a person.** Any reply that does not follow this file exactly is rejected and you will be asked again. This file controls only the final answer. Do the analysis required by `daily_review_instructions.md` and the sub-worktype rules file first, then write the answer exactly as below.

---

## 1. Your whole reply is ONE line of JSON

Your reply must be **exactly one line**: one JSON object, and nothing else.

- The first character of your reply is `{` and the last character is `}`.
- Your reply starts with exactly: `{"COPILOT_RESULT":"`
- **Nothing before it:** no "Working" section, no heading, no greeting, no summary, no explanation, no "Here is the result".
- **Nothing after it:** no notes, no sources, no references, no citations, no footnotes, no follow-up questions or suggestions.
- **No formatting:** no code block, no backticks (`` ` ``), no "json" label, no bold, no bullet points, no tables, no markdown of any kind.
- **No line breaks** anywhere in the reply, not even inside text values.

Put all your reasoning inside the `JUSTIFICATION` value. There is no other place for it.

---

## 2. The exact template

Write the keys in exactly this order, spelled exactly like this, and no other keys:

`{"COPILOT_RESULT":"…","DISCREPANCY_TYPE":"…","HEADLINE":"…","CASE_END_DATETIME":"…","REGISTER_SNAPSHOT_DATETIME":"…","CLIENT_REQUEST":"…","JUSTIFICATION":"…","A_B_COMPARISONS":[{"ITEM":"…","Customer_expectation":"…","CUSTOMER_SOURCE":"…","System_data":"…","SYSTEM_SOURCE":"…","MATCH":false,"EXPLANATION":"…"}],"RULE_CHECKS":[{"RULE":"…","STATUS":"…","EVIDENCE":"…","NOTE":"…"}]}`

A complete example with made-up values (your values come from your own review):

{"COPILOT_RESULT":"Incorrect","DISCREPANCY_TYPE":"Potential processor error","HEADLINE":"Person A was not set as a signatory on Register.","CASE_END_DATETIME":"12/10/2026 02:17 PM","REGISTER_SNAPSHOT_DATETIME":"2026-10-14T12:23:38+00:00","CLIENT_REQUEST":"Add Person A as a signatory on the account (signed client form added 10/10/2026; provenance: Client-supplied).","JUSTIFICATION":"Potential processor error: the client form requests Person A as signatory but Register does not show Person A ticked as Signatory. All required rule checks were satisfied.","A_B_COMPARISONS":[{"ITEM":"Person A - Signatory flag","Customer_expectation":"Ticked","CUSTOMER_SOURCE":"Signed client form added 10/10/2026; provenance: Client-supplied","System_data":"Unticked","SYSTEM_SOURCE":"Account Summary > Interested Parties > Person A > Signatory","MATCH":false,"EXPLANATION":"The signed client form asks for Person A to be a signatory, but Register shows the Signatory box for Person A unticked."},{"ITEM":"Person A - Surname","Customer_expectation":"Smith","CUSTOMER_SOURCE":"Signed client form added 10/10/2026; provenance: Client-supplied","System_data":"Smith","SYSTEM_SOURCE":"Account Summary > Interested Parties > Person A > Surname","MATCH":true,"EXPLANATION":"Same value."}],"RULE_CHECKS":[{"RULE":"Instruction signed by the required signatories","STATUS":"Satisfied","EVIDENCE":"Signed client form added 10/10/2026; provenance: Client-supplied","NOTE":"Required signatures present"}]}

The message you received ends with the same template, with this case's own date/time values already in place.

---

## 3. How to fill each key (follow these steps in order)

**Step 1 - `A_B_COMPARISONS`** (a list with **at least one** row, always). **One row per field** of the client's request compared with Register: if the request covers 22 fields, write 22 rows. Never put two fields in one row. Each row has exactly these seven keys, in this order:

| Key | What to write |
|---|---|
| `ITEM` | What is compared (field, and party if relevant). |
| `Customer_expectation` | The value the client's established request requires, or `Not established from case evidence`. |
| `CUSTOMER_SOURCE` | The document or Audit Trail entry and when it was added, then `provenance: Client-supplied`, `provenance: Processor-created`, `provenance: Client Services` or `provenance: Provenance unclear`. Add `; Client Services override: <message and date>` when one applied. |
| `System_data` | The Register value, or exactly one of: `Blank on Register`, `Not available in Register snapshot`, `Not captured`, `Not retrieved`. |
| `SYSTEM_SOURCE` | The Register JSON location, parts joined with ` > `. Example: `Account Summary > Interested Parties > Person A > Signatory`. |
| `MATCH` | `true` (they match), `false` (they do not match) or `null` (they cannot be compared). Written with **no quotes**. |
| `EXPLANATION` | **About this field only.** `MATCH` `false`: what exactly differs (the client's value versus the Register value) and what a reviewer should check. `MATCH` `null`: why this field cannot be compared. `MATCH` `true`: a few words, e.g. `Same value.` Do not repeat the field name at the start: the program shows it as `{ITEM}: {EXPLANATION}`. |

**A value missing from the snapshot is never a mismatch.** If the Register JSON does not contain the field, or its section says `Not available in Register snapshot`, write `System_data` `Not available in Register snapshot` and `MATCH` `null`. Use `Blank on Register` (a real, comparable value) only when the field is present and empty, or its section says `Blank on Register`.

If nothing could be compared at all, still write one row showing what you tried, with `MATCH` `null`.

**Step 2 - `RULE_CHECKS`** (a list). One row for **every** required check in the sub-worktype rules file, including checks the processor did not perform. Each row has exactly these four keys, in this order:

| Key | What to write |
|---|---|
| `RULE` | The check, named as in the sub-worktype rules file. |
| `STATUS` | Exactly one of: `Satisfied`, `Not satisfied`, `Not determinable`, `Not applicable`. |
| `EVIDENCE` | What established the status, with its source and provenance label. |
| `NOTE` | A short explanation. **Never empty.** For `Satisfied` a brief confirmation is enough. |

Write `"RULE_CHECKS":[]` **only** when no sub-worktype rules file was attached. In that case `COPILOT_RESULT` must be `Inconclusive`.

**Step 3 - `COPILOT_RESULT`.** Go down this list and stop at the first line that is true:

1. Any row has `"MATCH":false`, **or** any rule check has `"STATUS":"Not satisfied"` → `"Incorrect"`.
2. Any row has `"MATCH":null`, **or** any rule check has `"STATUS":"Not determinable"`, **or** `RULE_CHECKS` is `[]`, **or** the client's request cannot be established, **or** the request clearly belongs to a different sub-worktype → `"Inconclusive"`.
3. Otherwise (every `MATCH` is `true` and every STATUS is `Satisfied` or `Not applicable`) → `"Correct"`.

Never write `Incorrect` unless line 1 is true. Never write `Correct` if line 1 or line 2 is true.

**Step 4 - `DISCREPANCY_TYPE`.**

- No row has `"MATCH":false` → `"None"`. This is also true when the result is `Incorrect` only because of a rule check.
- At least one row has `"MATCH":false` → read the **Case age statement** in the message:

| Case age statement in the message | `DISCREPANCY_TYPE` |
|---|---|
| `Case completed within the last 3 business days` | `Potential processor error` |
| `Case completed more than 3 business days` | `Possible discrepancy due to aged case` |
| `Not provided` (or missing): business-day gap 3 or less, or also `Not provided` | `Potential processor error` |
| `Not provided` (or missing): business-day gap more than 3 | `Possible discrepancy due to aged case` |

Never calculate business days yourself.

**Step 5 - `CASE_END_DATETIME` and `REGISTER_SNAPSHOT_DATETIME`.** Copy them **character for character** from the message ("Case end date/time" and "Register snapshot date/time"). If the message shows `Not provided`, write `Not provided`. Do not reformat, convert or shorten them.

**Step 6 - `HEADLINE`.** The outcome in at most 20 words, e.g. `2 of 22 fields differ on Register: signatory and postcode.` or `All 22 requested fields match Register.`

**Step 7 - `CLIENT_REQUEST`.** One sentence: the client's request as established, with its source and provenance label. If it cannot be established, write `Not established from case evidence` followed by the reason.

**Step 8 - `JUSTIFICATION`.** Your argument for the result: what you compared, what you checked, and what is missing or uncertain. Its **first words** depend on `DISCREPANCY_TYPE`:

| `DISCREPANCY_TYPE` | `JUSTIFICATION` must start with | and must also contain |
|---|---|---|
| `Potential processor error` | `Potential processor error:` | - |
| `Possible discrepancy due to aged case` | `Possible discrepancy due to aged case:` | the exact sentence `This case was completed more than 3 business days before the Register snapshot was captured.` |
| `None` | anything | - |

If the case age was not provided, say so in `JUSTIFICATION`. When a `MATCH` is `null` or a STATUS is `Not determinable`, say why in `JUSTIFICATION`.

---

## 4. Text rules (the program rejects anything else)

- **Valid JSON:** double quotes around every key and every text value, a comma between items, no trailing commas, no comments.
- **Never a double quote inside a text value.** Use single quotes (`'`) instead. Never use a backslash (`\`).
- **No line breaks or tabs** inside values. Separate points with `;`.
- **No empty text values.** Every text value has at least one word; use the fixed phrases above where they apply.
- **`true`, `false`, `null`** are written without quotes and appear only as `MATCH` values.
- **No markdown** inside values (no `**`, no `#`, no bullet characters, no links).
- **Personal details:** include a date of birth, NI number, address, phone number, email or other personal detail **only** when that field is itself being compared. Refer to people by name or role only as far as the comparison needs.

---

## 5. Final check before you send

1. My reply is one single line, starts with `{"COPILOT_RESULT":"` and ends with `}`, with nothing before or after it and no code block.
2. It has exactly these nine keys in this order: `COPILOT_RESULT`, `DISCREPANCY_TYPE`, `HEADLINE`, `CASE_END_DATETIME`, `REGISTER_SNAPSHOT_DATETIME`, `CLIENT_REQUEST`, `JUSTIFICATION`, `A_B_COMPARISONS`, `RULE_CHECKS`.
3. `A_B_COMPARISONS` has one row per field compared (at least one); every row has the seven keys; every `MATCH` is `true`, `false` or `null` without quotes; every row has its own `EXPLANATION`.
4. `RULE_CHECKS` has one row per required check, each with the four keys and a non-empty `NOTE`.
5. `COPILOT_RESULT` follows Step 3, `DISCREPANCY_TYPE` follows Step 4, and `JUSTIFICATION` starts as Step 8 requires.
6. `CASE_END_DATETIME` and `REGISTER_SNAPSHOT_DATETIME` are copied exactly from the message.
7. No double quotes inside text values, no line breaks, no empty values.

---

## 6. If you are asked to resend

The program will tell you which problems it found. Reply again with **only** the corrected single JSON line, as in Section 1. Fix exactly the problems listed and keep the rest of your review the same. Do not redo the review and do not add any text around the JSON.
