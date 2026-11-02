# Part 3 — Copilot sender

`copilot_case_sender.py` sends each complete case folder to Microsoft 365 Copilot through its own Edge (port
`cdp_port`, default 9333, its own remembered profile). Each case gets one fresh chat, is sent exactly once, and its
strict-JSON answer is read back. The full behaviour is in the top-level `README.md`, sections "Part 3".

In the daily pipeline (`Run.cmd run`) the coordinator starts it in the console with `run --pipeline <run folder>`:
- **cursor question** → Copilot Edge + sign-in → **model question** → **"sign in everywhere, press Enter"**. Part 1
  and Part 2 check their own systems; anything missing is named, and you press Enter again;
- **the queue is live**: a case joins it as soon as the coordinator hands it over (`PipelineFeed`, section 9b), so
  case 1 is sent while System 1 is still downloading case 2;
- **per case**: that case's `<Sub Worktype>.md` + the result contract, then the snapshot, the Audit Trail and the
  documents in Received Documents order. Over `max_attachments`, the last documents are left out and reported;
- **stop** (e.g. System 2 Access Denied): nothing new is sent, and answers already sent are still collected.

`Run.cmd send-only` is the old behaviour: send every folder already in CASES_ROOT with one general review guidance
file (`instructions_path`).
