"""The 5 made-up Sub Worktype rules files the synthetic cases are reviewed against.

SYNTHETIC: written for testing only.  They are NOT FNZ or CCLA procedure; the real rules files are the user's own and
live in their guidance folder on the VDI.
"""

HEAD = ("> SYNTHETIC TEST RULES - made up for testing the reviewer on fake cases. Not FNZ or CCLA procedure.\n\n")

COMMON = """
## How to read the evidence (all Sub Worktypes)
- **Formats**: CCLA account numbers are 2 letters + exactly 7 digits (e.g. CO1234567). UK postcodes have a fixed
  letter/digit pattern; read a handwritten 0/O or 1/I by its place in that pattern, and ignore spacing. Dates are
  dd/mm/yyyy. Phone numbers are compared by their digits only.
- **Identity**: a person on the form is the Register person when the printed name matches AND at least one more
  detail written for them on the form (date of birth, phone number, email address or home address) matches that same
  Register person. Where the form only asks for a name and a phone number (email instructions authority form), that
  phone number is the second detail.
- **Signatures** are checked for presence only: handwriting in the signature box counts as signed (it is not
  compared with a specimen). An empty box is not signed.

## Checks for every case of this Sub Worktype
- **Form complete**: the client's form is the right CCLA form and every page of it is in the case (page numbers
  "Page n of N" run without a gap). A missing page means the instruction cannot be confirmed.
- **Account identified**: the account name and account number(s) on the form match Register (Account Summary).
- **Signed by authorised people**: the form is signed by at least two trustees/executive directors (or authorised
  signatories), each one identified (see Identity) as a Register Interested Party with Signatory = Yes. Anyone signing
  who is not on Register = Not satisfied.
- **Case source**: if the case source is Email, Register must show Email Instruction Authority = Yes (Flags and
  Custom Details). Post needs no check.
- **Readable evidence**: a value the reviewer cannot read with confidence is never treated as a match; the case is then
  Inconclusive unless something else already makes it Incorrect.
"""

RULES = {
    "Change of Address": HEAD + """# Change of Address (SYNTHETIC)

The client asks to change the organisation's correspondence address.
""" + COMMON + """
## Specific checks
- **New address applied**: Register's correspondence address and postcode equal the form's NEW address and postcode
  exactly (every line and the postcode, letter by letter).
- **Effective date**: Register's Address Effective From (Contact Details) equals the form's "With effect from" date.
- **Old address**: the form's old address is informational only; no check.
""",
    "Change of Signatory": HEAD + """# Change of Signatory (SYNTHETIC)

The client removes and/or adds authorised signatories.
""" + COMMON + """
## Specific checks
- **Removed**: each person in Section 2 (removal) is no longer an Interested Party with Signatory = Yes on Register.
- **Added**: each new signatory in Section 3 is on Register with Signatory = Yes, and their name and date of birth on
  Register match the form.
- **Authorised by existing trustees**: the two trustees signing Section 4 must both be on Register (and must not be
  the people being removed).
""",
    "Change of Organisation Name": HEAD + """# Change of Organisation Name (SYNTHETIC)

The client changes the organisation's name.
""" + COMMON + """
## Specific checks
- **Previous name**: the form's previous client name equals Register's Previous Account Name (Flags and Custom
  Details).
- **New name applied**: Register's account name equals the form's NEW client name exactly.
- **Charity reference**: Register's Charity Commission Number (Account Details / Custom Details) equals the form's
  new charity reference.
""",
    "Change of Correspondent": HEAD + """# Change of Correspondent (SYNTHETIC)

The client appoints a new correspondent.
""" + COMMON + """
## Specific checks
- **New correspondent recorded**: the person in Section 2 is an Interested Party on Register with Correspondent = Yes;
  title, forename, surname and date of birth match the form.
- **Correspondence address**: Register's correspondence address and postcode equal the form's "Address for
  correspondence" and its postcode.
- **New correspondent signed** Section 2.
""",
    "Account Instructions Update": HEAD + """# Account Instructions Update (SYNTHETIC)

One of: an Email instructions authority form, a COIF mandate form, or a COIF income and withdrawal payments amendment
form. Apply the checks for the form in the case.
""" + COMMON + """
## Specific checks - Email instructions authority form
- Register shows Email Instruction Authority = Yes after the case.
- Client name and Client ID number(s) match Register; both signatories are on Register with Signatory = Yes.

## Specific checks - COIF income and withdrawal payments amendment form
- **Bank details**: Register's nominated bank account (bank name, account name, sort code, account number) equals
  Section 3 of the form exactly.
- **Income option**: Register's income distribution option equals the option ticked in Section 2.
- **Trustees' authorisation**: Section 4 is signed by two trustees named on Register (both trustee pages present).

## Specific checks - COIF mandate form
- Charity name, registration number (Register's Charity Commission Number) and office address and postcode
  (Register's Office Address / Office Postcode, Contact Details) match Register.
- Nominated bank account on Register equals Section 5 of the form.
- The correspondent (Section 6) is on Register with Correspondent = Yes (identified by name + email address) and is
  Register's Portal Administrator; the authorised signatories (Section 7) are on Register with Signatory = Yes; names
  and dates of birth match.
- The trustees' declaration (Section 10) is signed by two trustees named on Register.
""",
}
