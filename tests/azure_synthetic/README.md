# Synthetic test cases for the Azure reviewer (laptop only)

> **Everything here is made up.** Every organisation, person, account number, address and bank detail is invented
> and marked "SYNTHETIC TEST DATA" on every page. Nothing here is FNZ or CCLA data or procedure. Use it on the laptop
> with your own free-trial Azure, never on the VDI.

## What it is for

Before the Azure reviewer is used on real cases, it is tested on 10 fake cases where the right answer is known:

- **10 cases** covering 7 CCLA forms: the 5 shared forms (change of address, email instructions authority, change of
  organisation name, change of signatories, change of correspondent), the COIF mandate form and the COIF income and
  withdrawal payments amendment form.
- **5 baseline cases** have the form typed into the PDF (like a client filling it on screen).
- **5 handwritten cases** have block capitals written into the boxes from real handwriting samples (EMNIST), saved
  as a 150 dpi scan.
- **4 trap cases**, one of each kind. None of them may come out Correct:

  | # | Trap | What is wrong | Right answer |
  |---|---|---|---|
  | 1 | value mismatch | Register's new postcode is NN6 7PA; the form says NN6 7AP | Incorrect |
  | 2 | wrong signer | the 2nd trustee who signed is not on the account | Incorrect |
  | 8 | missing page | page 3 (first trustee and signature) is not in the file | Incorrect or Inconclusive |
  | 9 | illegible key field | the new organisation name is crossed out and unreadable | Inconclusive |

Account numbers follow the CCLA format: 2 letters + exactly 7 digits (CO4000101 to CO4000110). On the COIF forms,
where "CO" is printed before the box, only the 7 digits are written. Signatures are the signer's name written fast
and slanted with a flourish, using real handwriting strokes, so they show up as handwriting.

Each clean case can be fully confirmed from its own files. Everything its rules file asks about is on the made-up
Register: the Charity Commission Number, the previous name after a rename, the office address, the address
effective date, and daytime phones for the email-authority signatories.

Each case folder looks exactly like one Part 1 + Part 2 leave in the cases folder: the form, `Audit Trail.pdf`,
`Email from client.eml` when the case came by email, and the Register snapshot
`<ACCOUNT>_Complete_Account_Profile.json`. The snapshot is written by **Part 2's own code** from made-up Register
pages, so it has exactly the real shape.

## How to use it

Normally you don't run these by hand: `Run.cmd azure-test` generates the cases, reviews them with Azure and writes
the accuracy report. By hand (laptop, from the project folder):

```
tests\azure_synthetic\.synthetic-venv\Scripts\python.exe -I tests\azure_synthetic\generate.py --out <new empty folder>
tests\azure_synthetic\.synthetic-venv\Scripts\python.exe -I tests\azure_synthetic\score.py --expected <folder>\expected.json --results <results folder> --out <folder>\accuracy-report.md
```

`generate.py` writes `studio.csv` (the BAM Studio report: Request Id, Client Account Number, Client Number, Process
Status, Sub Worktype, End Date, Case Source, plus one Voided row that must be skipped), `cases\`, `guidance\` (5
made-up Sub Worktype rules files) and `expected.json`. The same `--seed` always gives **byte-identical files**, so the
reviewer's Document Intelligence cache is reused and running the test again costs no Azure pages. One full run is
**67 Document Intelligence pages** (the free tier allows 500 a month).

## The pass bar (`score.py`)

All three must hold:

1. **No trap case comes out Correct.** A trap case with no result at all also fails, because nothing shows the trap
   was caught.
2. **At least 95% of the handwritten fields are read right.** The check is strict: the value must be what is written
   on the form, ignoring only capitals and extra spaces. Line breaks or commas between address lines count as spaces.
   The agreed format-safe reading is applied to both sides:
   - phone numbers are compared by their digits only;
   - dates are read as dd/mm/yyyy;
   - postcodes are compared without spaces, and a 0/O or 1/I is read by its place in the UK postcode pattern;
   - account numbers: "C0" is read as "CO" only when exactly 7 digits follow.
   Fields the reviewer flagged as low confidence still count. The report also shows the score over the unflagged
   fields only. The deliberately unreadable field of trap 9 is not part of the score; it must be flagged instead.
3. **Every field below 0.80 confidence is flagged** (LOW_CONFIDENCE).

Signatures count as "signed" or "not signed". A reply of "possibly not signed" counts as not signed.

The report also lists every field that was not right, and how each case was judged.

## Setting it up (once, laptop)

```
python -m venv tests\azure_synthetic\.synthetic-venv
tests\azure_synthetic\.synthetic-venv\Scripts\python.exe -m pip install -r tests\azure_synthetic\requirements-synthetic.txt
tests\azure_synthetic\.synthetic-venv\Scripts\python.exe -I tests\azure_synthetic\download.py
```

`download.py` fetches the public CCLA forms and EMNIST into `_downloads\`. That folder and the venv are git-ignored:
**downloaded forms, handwriting data and generated cases are never committed** (the repository is public).

## Files

| File | What it does |
|---|---|
| `generate.py` | makes the cases (needs the venv + downloads) |
| `cases.py` | the 10 made-up cases: form values, Register after the change, case source, trap, right answer |
| `handwriting.py` | EMNIST block capitals (one consistent "writer" per form), crossed-out text, signature scribbles |
| `register.py` | made-up Register pages read by Part 2's parsers, then Part 2's `write_result` |
| `guidance_rules.py` | the 5 SYNTHETIC Sub Worktype rules files |
| `build_form_maps.py` | writes `3_azure_reviewer/form_maps.json` (the reviewer's field list for the 7 forms) |
| `score.py` | the accuracy report and pass bar (standard Python only) |
| `test_synthetic.py` | tests: the scorer always; the generator only where the venv and downloads exist |

EMNIST (NIST Special Database 19 derivative) is used for research/testing only, as agreed.
