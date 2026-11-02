# Guidance — the instructions Copilot follows

- `result_contract.md` — the strict answer format, attached to every case (when answers are collected).
- **One `.md` file per Sub Worktype**, named after it, e.g. `Change of Address.md`. The match ignores letter case,
  punctuation and spacing, so `change-of-address.md` also works. Each case gets the file of its Sub Worktype; System 1's
  value wins over the CSV's.
- No file for a Sub Worktype = those cases are **held** (never sent with the wrong instructions). The run warns
  about this at the start.

Your real instruction files are private: keep them in the folder you chose as `guidance_dir` in `Run.cmd setup`
(on S:). This folder in the repository only holds the result contract template.
