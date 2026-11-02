# Guidance — the instructions Copilot follows

Every case gets, in this order: **`daily_review_instructions.md`** (how to analyse any case), its **Sub Worktype file**
(below) and **`result_contract.md`** (the strict answer format, when answers are collected). Their paths are given in
`Run.cmd setup`. None of them is kept in this repository (it is public): keep your real files on S:.
An optional `bank_holidays.txt` beside `daily_review_instructions.md` adds England & Wales bank holidays after 2028
(one date per line, `2029-01-01` or `01/01/2029`).

- **One `.md` file per Sub Worktype**, named after it, e.g. `Change of Address.md`. The match ignores letter case,
  punctuation and spacing, so `change-of-address.md` also works. Each case gets the file of its Sub Worktype; System 1's
  value wins over the CSV's.
- No file for a Sub Worktype = those cases are **held** (never sent with the wrong instructions). The run warns
  about this at the start.

Your real instruction files are private: keep them in the folder you chose as `guidance_dir` in `Run.cmd setup`
(on S:). The result contract and the daily instructions may sit there too: they are never taken for a Sub Worktype's
file. This folder in the repository only holds this README.
