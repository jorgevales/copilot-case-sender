# Result contract (strict)

This file is attached to every case. A script reads your answer, so the **format below is mandatory**.

## What to deliver

Finish every answer with **one JSON object written as plain text on a single line**.
**Do NOT use a code block, a code box, backticks or any markdown formatting** for it: write the characters directly in your reply.

Example of the exact shape (shown here on one line, as you must write it):

{"COPILOT_RESULT":"Correct","JUSTIFICATION":"Short argument for the verdict: what you checked and why it matches or not.","A_B_COMPARISONS":[{"ITEM":"what is being compared","Customer_expectation":"what the customer asked for","System_data":"what the system currently shows (live data)","MATCH":true}]}

## Rules

1. The object has **exactly three keys**, spelled exactly like this, and no others:
   `COPILOT_RESULT`, `JUSTIFICATION`, `A_B_COMPARISONS`.
2. `COPILOT_RESULT` is **one word, exactly one of**: `Correct`, `Incorrect`, `Inconclusive`
   (capital first letter, nothing else in the value).
   - `Correct`: the system data matches the customer expectation for **every** item (every `MATCH` is `true`).
   - `Incorrect`: at least one item differs (at least one `MATCH` is `false`).
   - `Inconclusive`: you cannot decide with the data you were given; say what is missing in `JUSTIFICATION`.
3. `JUSTIFICATION` is plain text (one string, not empty) with your argument for the verdict.
4. `A_B_COMPARISONS` is a list. **Each row has exactly these four keys**:
   `ITEM`, `Customer_expectation`, `System_data`, `MATCH`.
   - `ITEM`, `Customer_expectation` and `System_data` are text.
   - `MATCH` is the JSON value `true` or `false` (no quotes).
   - For `Correct` and `Incorrect` list at least one row. When the result is `Incorrect`, the rows that differ
     must show the customer value and the system value side by side. `Inconclusive` may have no rows.
5. Valid JSON only: double quotes, no comments, no trailing commas.
6. Write the JSON object **once**, as plain text, and make it the last thing in your reply.

## If you are asked to resend

You will sometimes be told your answer did not follow this structure. Resend **your exact same answer, unchanged in
content**, as the single-line plain-text JSON object described above. Do not re-do the review and do not change the
verdict.
