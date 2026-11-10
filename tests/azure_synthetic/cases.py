"""The 10 made-up cases (SYNTHETIC TEST DATA: every organisation, person, number and address is invented).

Each case: what the client sent (form values written into a CCLA form), what Register shows after the processor ended
the case (snapshot), how the request arrived (Case Source) and the result a correct reviewer must give.

Traps (one of each kind, none may come out Correct):
  value_mismatch  case 1  - Register's new postcode differs from the form's          -> Incorrect
  wrong_signer    case 2  - the 2nd authorising trustee is not on the account        -> Incorrect
  missing_page    case 8  - page 3 (first trustee + signature) is not in the file    -> Incorrect or Inconclusive
  illegible_key   case 9  - the new organisation name is crossed out / unreadable    -> Inconclusive
"""
from __future__ import annotations

import hashlib

SUB_ADDRESS, SUB_SIGNATORY, SUB_NAME = "Change of Address", "Change of Signatory", "Change of Organisation Name"
SUB_CORRESPONDENT, SUB_UPDATE = "Change of Correspondent", "Account Instructions Update"
SUB_WORKTYPES = (SUB_ADDRESS, SUB_SIGNATORY, SUB_NAME, SUB_CORRESPONDENT, SUB_UPDATE)

DOC_TITLE = {
    "change-address-form": "Change of address form",
    "email-instruction-authority-form": "Email instructions authority form",
    "change-organisation-name": "Change of organisation name form",
    "change-signatory-form": "Change of authorised signatories form",
    "change-correspondent-form": "Change of correspondent form",
    "coif-charities-funds-mandate-form": "COIF mandate form",
    "coif-charities-funds-amendment-income-and-withdrawal-payments-form": "COIF income and withdrawal amendment form",
}


def iid(n: int) -> str:
    return hashlib.md5(f"synthetic-azure-reviewer-case-{n}".encode()).hexdigest()


def person(pid, title, first, last, dob, position, email, mobile, address, postcode, role="Trustee",
           signatory=True, correspondent=False, middle="", phone=""):
    return {"id": pid, "title": title, "first": first, "middle": middle, "last": last, "dob": dob, "position": position,
            "email": email, "mobile": mobile, "phone": phone, "address": address, "postcode": postcode, "role": role,
            "signatory": signatory, "correspondent": correspondent}


def block(prefix, p, signed):
    """Form values of a trustee/signatory block for person p."""
    return {f"{prefix}_title": p["title"], f"{prefix}_forename": p["first"], f"{prefix}_surname": p["last"],
            f"{prefix}_date_of_birth": p["dob"], f"{prefix}_position": p["position"], f"{prefix}_email": p["email"],
            f"{prefix}_home_address": "\n".join(p["address"]), f"{prefix}_postcode": p["postcode"],
            f"{prefix}_signature": "signed", f"{prefix}_signed_date": signed}


def register(account, client, name, charity, address, postcode, email, phone, parties, email_authority="No",
             bank=None, income="Add income to the balance of the account", fund="COIF Charities Deposit Fund",
             previous_name="", effective="12/03/2019", portal_admin=""):
    """Register after the processor ended the case.  Everything a SYNTHETIC rule asks about is on it (charity number,
    previous name after a rename, office address, address effective date), so a clean case can be fully confirmed."""
    custom = {"Email Instruction Authority": email_authority, "Charity Commission Number": charity}
    if previous_name:
        custom["Previous Account Name"] = previous_name
    if portal_admin:
        custom["Portal Administrator"] = portal_admin
    custom["Record type"] = "SYNTHETIC TEST DATA"
    return {"account": account, "client": client, "name": name, "charity": charity, "address": address,
            "postcode": postcode, "email": email, "phone": phone, "parties": parties, "fund": fund,
            "opened": "12/03/2019", "income": income, "bank": bank, "effective": effective, "custom": custom}


def _cases():
    out = []

    # 1 ------------------------------------------------------------------ Change of Address, typed, VALUE MISMATCH
    eleanor = person("IP1001", "MRS", "ELEANOR", "PRICE", "14/02/1961", "CHAIR", "E.PRICE@STALDHELMTRUST.ORG.UK", "07700900101",
                     ["2 ORCHARD CLOSE", "WEST HADDON", "NORTHAMPTON"], "NN6 7AQ")
    david = person("IP1002", "MR", "DAVID", "ASHBY", "03/09/1958", "TREASURER", "D.ASHBY@STALDHELMTRUST.ORG.UK", "07700900102",
                   ["9 MILL LANE", "GUILSBOROUGH", "NORTHAMPTON"], "NN6 8PZ", correspondent=True)
    out.append(dict(
        n=1, sub=SUB_ADDRESS, source="Post", style="font", trap="value_mismatch", expected=["Incorrect"],
        form="change-address-form",
        values={"account_name": "ST ALDHELM PARISH TRUST", "account_number_1": "CO4000101",
                "old_address": "THE VESTRY\nCHURCH LANE\nWEST HADDON", "old_postcode": "NN6 7AD",
                "new_address": "14 CHAPEL ROW\nWEST HADDON\nNORTHAMPTON", "new_postcode": "NN6 7AP",
                "effective_date": "01/11/2026", "email": "ADMIN@STALDHELMTRUST.ORG.UK",
                **block("trustee_1", eleanor, "28/10/2026"), **block("trustee_2", david, "28/10/2026")},
        register=register("CO4000101", "7000101", "ST ALDHELM PARISH TRUST", "1180011",
                          ["14 CHAPEL ROW", "WEST HADDON", "NORTHAMPTON"], "NN6 7PA",  # <- the trap: 7AP on the form
                          "ADMIN@STALDHELMTRUST.ORG.UK", "01604 900101", [eleanor, david], effective="01/11/2026"),
        updated="Correspondence address", received="03/11/2026", end="05/11/2026 02:41 PM"))

    # 2 ------------------------------------------------------------------ Change of Signatory, typed, WRONG SIGNER
    samuel = person("IP2001", "MR", "SAMUEL", "REID", "21/06/1966", "CHAIR", "S.REID@HARBOURLIGHT.ORG.UK", "07700900201",
                    ["41 QUAY STREET", "FALMOUTH"], "TR11 3HH", correspondent=True)
    anne = person("IP2002", "MRS", "ANNE", "COLLINS", "09/01/1971", "TRUSTEE", "A.COLLINS@HARBOURLIGHT.ORG.UK", "07700900202",
                  ["3 BEACON TERRACE", "FALMOUTH"], "TR11 2BJ")
    priya = person("IP2003", "MS", "PRIYA", "NAIR", "30/11/1984", "TREASURER", "P.NAIR@HARBOURLIGHT.ORG.UK", "07700900203",
                   ["17 WODEHOUSE TERRACE", "FALMOUTH"], "TR11 3EP")
    martin = person("IPX999", "MR", "MARTIN", "HOLLOWAY", "12/04/1969", "TRUSTEE", "M.HOLLOWAY@EXAMPLE.ORG", "07700900299",
                    ["8 ARWENACK AVENUE", "FALMOUTH"], "TR11 3JW")   # NOT on Register: the trap
    out.append(dict(
        n=2, sub=SUB_SIGNATORY, source="Post", style="font", trap="wrong_signer", expected=["Incorrect"],
        form="change-signatory-form",
        values={"todays_date": "27/10/2026", "organisation_name": "HARBOUR LIGHT YOUTH PROJECT", "account_number_1": "CO4000102",
                "removed_1_title": "MR", "removed_1_forename": "GRAHAM", "removed_1_surname": "TULLY",
                **block("new_signatory_1", priya, "27/10/2026"),
                **block("trustee_1", samuel, "27/10/2026"), **block("trustee_2", martin, "27/10/2026")},
        register=register("CO4000102", "7000102", "HARBOUR LIGHT YOUTH PROJECT", "1180012", ["41 QUAY STREET", "FALMOUTH"],
                          "TR11 3HH", "INFO@HARBOURLIGHT.ORG.UK", "01326 900102", [samuel, anne, priya]),
        updated="Interested Parties: GRAHAM TULLY removed, PRIYA NAIR added as signatory", received="02/11/2026",
        end="05/11/2026 11:12 AM"))

    # 3 ------------------------------------------------------------------ Change of Organisation Name, typed, correct
    alan = person("IP3001", "MR", "ALAN", "FORBES", "05/05/1955", "CHAIR", "A.FORBES@MEADOWBANKHC.ORG.UK", "07700900301",
                  ["1 MEADOW BANK", "KENDAL"], "LA9 4DX", correspondent=True)
    helen = person("IP3002", "DR", "HELEN", "MARSH", "18/08/1963", "SECRETARY", "H.MARSH@MEADOWBANKHC.ORG.UK", "07700900302",
                   ["22 STRICKLAND GATE", "KENDAL"], "LA9 4ND")
    out.append(dict(
        n=3, sub=SUB_NAME, source="Email", style="font", trap=None, expected=["Correct"], form="change-organisation-name",
        values={"previous_client_name": "MEADOWBANK ALMSHOUSE CHARITY", "client_id_numbers": "CO4000103",
                "new_client_name": "MEADOWBANK HOUSING CHARITY", "old_charity_reference": "1180013",
                "new_charity_reference": "1180013",
                **block("trustee_1", alan, "29/10/2026"),
                **{k: v for k, v in block("trustee_2", helen, "29/10/2026").items() if not k.endswith(("home_address", "postcode"))}},
        register=register("CO4000103", "7000103", "MEADOWBANK HOUSING CHARITY", "1180013", ["1 MEADOW BANK", "KENDAL"],
                          "LA9 4DX", "OFFICE@MEADOWBANKHC.ORG.UK", "01539 900103", [alan, helen], email_authority="Yes",
                          previous_name="MEADOWBANK ALMSHOUSE CHARITY"),
        email_from=alan["email"], updated="Account name", received="30/10/2026", end="04/11/2026 09:30 AM"))

    # 4 ------------------------------------------------------------------ Change of Correspondent, typed, correct
    peter = person("IP4001", "MR", "PETER", "WALSH", "11/12/1960", "CHAIR", "P.WALSH@RIVERSIDEFOODBANK.ORG.UK", "07700900401",
                   ["5 KINGS PLACE", "BEDFORD"], "MK40 1HG")
    linda = person("IP4002", "MRS", "LINDA", "CHO", "24/03/1974", "TREASURER", "L.CHO@RIVERSIDEFOODBANK.ORG.UK", "07700900402",
                   ["12 ASHBURNHAM ROAD", "BEDFORD"], "MK40 1DS")
    ruth = person("IP4003", "MS", "RUTH", "ADEYEMI", "22/07/1979", "SECRETARY", "R.ADEYEMI@RIVERSIDEFOODBANK.ORG.UK", "07700900403",
                  ["40 GOLDINGTON ROAD", "BEDFORD"], "MK40 3LH", role="Correspondent", signatory=False, correspondent=True)
    out.append(dict(
        n=4, sub=SUB_CORRESPONDENT, source="Post", style="font", trap=None, expected=["Correct"], form="change-correspondent-form",
        values={"todays_date": "26/10/2026", "organisation_name": "RIVERSIDE FOODBANK TRUST", "account_number_1": "CO4000104",
                "new_correspondent_title": "MS", "new_correspondent_forename": "RUTH", "new_correspondent_surname": "ADEYEMI",
                "new_correspondent_date_of_birth": "22/07/1979", "new_correspondent_position": "SECRETARY",
                "new_correspondent_mobile": "07700900403", "new_correspondent_email": "R.ADEYEMI@RIVERSIDEFOODBANK.ORG.UK",
                "new_correspondent_home_address": "40 GOLDINGTON ROAD\nBEDFORD", "new_correspondent_postcode": "MK40 3LH",
                "correspondence_address": "UNIT 3 MILL YARD\nRIVERSIDE ROAD\nBEDFORD", "correspondence_postcode": "MK40 1QT",
                "new_correspondent_signature": "signed",
                **block("trustee_1", peter, "26/10/2026"), **block("trustee_2", linda, "26/10/2026")},
        register=register("CO4000104", "7000104", "RIVERSIDE FOODBANK TRUST", "1180014",
                          ["UNIT 3 MILL YARD", "RIVERSIDE ROAD", "BEDFORD"], "MK40 1QT", "R.ADEYEMI@RIVERSIDEFOODBANK.ORG.UK",
                          "01234 900104", [peter, linda, ruth]),
        updated="Correspondent: RUTH ADEYEMI; correspondence address", received="29/10/2026", end="03/11/2026 04:05 PM"))

    # 5 ------------------------------------------------------------------ Email instruction authority, typed, correct
    james = person("IP5001", "MR", "JAMES", "BARLOW", "02/02/1959", "CHAIR", "J.BARLOW@KESTRELHILL.ORG.UK", "07700900501",
                   ["HILL HOUSE", "KESTREL HILL", "HEXHAM"], "NE46 1AB", correspondent=True, phone="01434 900501")
    fiona = person("IP5002", "MRS", "FIONA", "GRANT", "16/10/1968", "TREASURER", "F.GRANT@KESTRELHILL.ORG.UK", "07700900502",
                   ["4 PRIESTPOPPLE", "HEXHAM"], "NE46 1PS", phone="01434 900502")
    out.append(dict(
        n=5, sub=SUB_UPDATE, source="Post", style="font", trap=None, expected=["Correct"], form="email-instruction-authority-form",
        values={"client_name": "KESTREL HILL HOSPICE FUND", "client_id_numbers": "CO4000105",
                "signatory_1_signature": "signed", "signatory_1_name": "JAMES BARLOW", "signatory_1_phone": "01434 900501",
                "signatory_2_signature": "signed", "signatory_2_name": "FIONA GRANT", "signatory_2_phone": "01434 900502",
                "signed_date": "30/10/2026"},
        register=register("CO4000105", "7000105", "KESTREL HILL HOSPICE FUND", "1180015", ["HILL HOUSE", "KESTREL HILL", "HEXHAM"],
                          "NE46 1AB", "ACCOUNTS@KESTRELHILL.ORG.UK", "01434 900105", [james, fiona], email_authority="Yes"),
        updated="Custom details: Email Instruction Authority set to Yes", received="31/10/2026", end="04/11/2026 01:20 PM"))

    # 6 ------------------------------------------------------------------ Change of Address, HANDWRITTEN, correct
    clare = person("IP6001", "MS", "CLARE", "BENNETT", "07/07/1972", "CHAIR", "C.BENNETT@NORTHGATECHOIR.ORG.UK", "07700900601",
                   ["15 HOOLE ROAD", "CHESTER"], "CH2 3NH", correspondent=True)
    owen = person("IP6002", "MR", "OWEN", "DAVIES", "19/01/1965", "TREASURER", "O.DAVIES@NORTHGATECHOIR.ORG.UK", "07700900602",
                  ["2 QUEENS PARK ROAD", "CHESTER"], "CH4 7AD")
    out.append(dict(
        n=6, sub=SUB_ADDRESS, source="Email", style="hand", writer=1, trap=None, expected=["Correct"], form="change-address-form",
        values={"account_name": "NORTHGATE COMMUNITY CHOIR", "account_number_1": "CO4000106",
                "old_address": "21 BRIDGE STREET\nCHESTER", "old_postcode": "CH1 1NQ",
                "new_address": "7 ABBEY MEWS\nNORTHGATE\nCHESTER", "new_postcode": "CH1 4HG", "effective_date": "02/11/2026",
                "email": "HELLO@NORTHGATECHOIR.ORG.UK",
                **block("trustee_1", clare, "30/10/2026"), **block("trustee_2", owen, "30/10/2026")},
        register=register("CO4000106", "7000106", "NORTHGATE COMMUNITY CHOIR", "1180016", ["7 ABBEY MEWS", "NORTHGATE", "CHESTER"],
                          "CH1 4HG", "HELLO@NORTHGATECHOIR.ORG.UK", "01244 900106", [clare, owen], email_authority="Yes",
                          effective="02/11/2026"),
        email_from=clare["email"], updated="Correspondence address", received="31/10/2026", end="05/11/2026 10:02 AM"))

    # 7 ------------------------------------------------------------------ Change of Signatory, HANDWRITTEN, correct
    richard = person("IP7001", "MR", "RICHARD", "HALE", "28/02/1957", "CHAIR", "R.HALE@WILLOWLANERESCUE.ORG.UK", "07700900701",
                     ["WILLOW LANE FARM", "MALTON"], "YO17 6UF", correspondent=True)
    grace = person("IP7002", "MS", "GRACE", "LOWE", "13/06/1980", "TRUSTEE", "G.LOWE@WILLOWLANERESCUE.ORG.UK", "07700900702",
                   ["6 YORKERSGATE", "MALTON"], "YO17 7AB")
    sophie = person("IP7003", "MRS", "SOPHIE", "TURNER", "04/04/1986", "TREASURER", "S.TURNER@WILLOWLANERESCUE.ORG.UK", "07700900703",
                    ["19 OLD MALTONGATE", "MALTON"], "YO17 7EG")
    out.append(dict(
        n=7, sub=SUB_SIGNATORY, source="Post", style="hand", writer=2, trap=None, expected=["Correct"], form="change-signatory-form",
        values={"todays_date": "28/10/2026", "organisation_name": "WILLOW LANE ANIMAL RESCUE", "account_number_1": "CO4000107",
                "removed_1_title": "MR", "removed_1_forename": "KEITH", "removed_1_surname": "MORGAN",
                **block("new_signatory_1", sophie, "28/10/2026"),
                **block("trustee_1", richard, "28/10/2026"), **block("trustee_2", grace, "28/10/2026")},
        register=register("CO4000107", "7000107", "WILLOW LANE ANIMAL RESCUE", "1180017", ["WILLOW LANE FARM", "MALTON"],
                          "YO17 6UF", "CONTACT@WILLOWLANERESCUE.ORG.UK", "01653 900107", [richard, grace, sophie]),
        updated="Interested Parties: KEITH MORGAN removed, SOPHIE TURNER added as signatory", received="02/11/2026",
        end="06/11/2026 09:45 AM"))

    # 8 ------------------------------------------------------------------ COIF income/withdrawal, HANDWRITTEN, MISSING PAGE
    nadia = person("IP8001", "MS", "NADIA", "KHAN", "25/09/1976", "CHAIR", "N.KHAN@SEABROOKLITERACY.ORG.UK", "07700900801",
                   ["11 MARINE PARADE", "SEABROOK"], "CT21 5RR", correspondent=True)
    tom = person("IP8002", "MR", "TOM", "FLETCHER", "08/12/1962", "TREASURER", "T.FLETCHER@SEABROOKLITERACY.ORG.UK", "07700900802",
                 ["3 PRINCES PARADE", "HYTHE"], "CT21 6AQ")
    bank8 = {"bank": "BARCLAYS BANK", "account_name": "SEABROOK LITERACY CHARITY", "sort": "20-45-77", "number": "43917265"}
    out.append(dict(
        n=8, sub=SUB_UPDATE, source="Email", style="hand", writer=3, trap="missing_page", expected=["Incorrect", "Inconclusive"],
        form="coif-charities-funds-amendment-income-and-withdrawal-payments-form", drop_pages=[3],
        values={"todays_date": "29/10/2026", "charity_name": "SEABROOK LITERACY CHARITY",
                "account_number_1": "4000108", "deposit_income_add_to_balance": "", "deposit_income_pay_to_deposit_account": "",
                "deposit_income_pay_to_bank": "ticked", "bank_name": "BARCLAYS BANK", "bank_account_name": "SEABROOK LITERACY CHARITY",
                "bank_sort_code": "20-45-77", "bank_account_number": "43917265",
                **block("trustee_1", nadia, "29/10/2026"), **block("trustee_2", tom, "29/10/2026")},
        alternatives={"account_number_1": ["CO4000108"]},
        register=register("CO4000108", "7000108", "SEABROOK LITERACY CHARITY", "1180018", ["11 MARINE PARADE", "SEABROOK"],
                          "CT21 5RR", "ADMIN@SEABROOKLITERACY.ORG.UK", "01303 900108", [nadia, tom], email_authority="Yes",
                          bank=bank8, income="Pay income to the nominated bank account"),
        email_from=nadia["email"], updated="Nominated bank account; income distribution option", received="30/10/2026",
        end="04/11/2026 03:33 PM"))

    # 9 ------------------------------------------------------------------ Change of Organisation Name, HANDWRITTEN, ILLEGIBLE
    victor = person("IP9001", "MR", "VICTOR", "AMBROSE", "15/03/1953", "CHAIR", "V.AMBROSE@ORCHARDVALEARTS.ORG.UK", "07700900901",
                    ["THE GRANARY", "ORCHARD VALE", "LEDBURY"], "HR8 1EE", correspondent=True)
    isla = person("IP9002", "MS", "ISLA", "MCKAY", "02/10/1982", "TREASURER", "I.MCKAY@ORCHARDVALEARTS.ORG.UK", "07700900902",
                  ["27 HOMEND", "LEDBURY"], "HR8 1BN")
    out.append(dict(
        n=9, sub=SUB_NAME, source="Post", style="hand", writer=4, trap="illegible_key", expected=["Inconclusive"],
        form="change-organisation-name", illegible=["new_client_name"],
        values={"previous_client_name": "ORCHARD VALE ARTS TRUST", "client_id_numbers": "CO4000109",
                "new_client_name": "ORCHARD VALE ARTS FOUNDATION", "old_charity_reference": "1180019",
                "new_charity_reference": "1180019",
                **block("trustee_1", victor, "27/10/2026"),
                **{k: v for k, v in block("trustee_2", isla, "27/10/2026").items() if not k.endswith(("home_address", "postcode"))}},
        register=register("CO4000109", "7000109", "ORCHARD VALE ARTS FOUNDATION", "1180019",
                          ["THE GRANARY", "ORCHARD VALE", "LEDBURY"], "HR8 1EE", "INFO@ORCHARDVALEARTS.ORG.UK", "01531 900109",
                          [victor, isla], previous_name="ORCHARD VALE ARTS TRUST"),
        updated="Account name", received="29/10/2026", end="05/11/2026 12:10 PM"))

    # 10 ----------------------------------------------------------------- COIF mandate, HANDWRITTEN, correct
    andrew = person("IP1101", "MR", "ANDREW", "KNIGHT", "20/05/1964", "CHAIR", "A.KNIGHT@THORNFIELDMISSION.ORG.UK", "07700901001",
                    ["8 HESLINGTON LANE", "YORK"], "YO10 5DX")
    maya = person("IP1102", "DR", "MAYA", "PATEL", "17/11/1977", "TREASURER", "M.PATEL@THORNFIELDMISSION.ORG.UK", "07700901002",
                  ["31 FULFORD ROAD", "YORK"], "YO10 4BE")
    june = person("IP1103", "MRS", "JUNE", "ELLIS", "09/09/1969", "ADMINISTRATOR", "J.ELLIS@THORNFIELDMISSION.ORG.UK", "07700901003",
                  ["THE OLD SCHOOL HOUSE", "THORNFIELD ROAD", "YORK"], "YO10 3BP", role="Correspondent", signatory=False,
                  correspondent=True)
    bank10 = {"bank": "LLOYDS BANK", "account_name": "THORNFIELD MISSION SOCIETY", "sort": "30-96-12", "number": "11473920"}
    out.append(dict(
        n=10, sub=SUB_UPDATE, source="Post", style="hand", writer=5, trap=None, expected=["Correct"],
        form="coif-charities-funds-mandate-form",
        values={"todays_date": "26/10/2026", "charity_name": "THORNFIELD MISSION SOCIETY", "account_number_1": "4000110",
                "charity_registration_number": "1180020", "office_address": "THE OLD SCHOOL HOUSE\nTHORNFIELD ROAD\nYORK",
                "office_postcode": "YO10 3BP", "general_email": "OFFICE@THORNFIELDMISSION.ORG.UK", "daytime_phone": "01904 901010",
                "trustee_1_name": "ANDREW KNIGHT", "trustee_1_date_of_birth": "20/05/1964", "trustee_1_position": "CHAIR",
                "trustee_2_name": "MAYA PATEL", "trustee_2_date_of_birth": "17/11/1977", "trustee_2_position": "TREASURER",
                "bank_name": "LLOYDS BANK", "bank_account_name": "THORNFIELD MISSION SOCIETY", "bank_sort_code": "30-96-12",
                "bank_account_number": "11473920",
                "correspondent_title": "MRS", "correspondent_forename": "JUNE", "correspondent_surname": "ELLIS",
                "correspondent_email": "J.ELLIS@THORNFIELDMISSION.ORG.UK", "correspondent_signature": "signed",
                "signatory_1_title": "MR", "signatory_1_forename": "ANDREW", "signatory_1_surname": "KNIGHT",
                "signatory_1_date_of_birth": "20/05/1964", "signatory_1_signature": "signed",
                "signatory_2_title": "DR", "signatory_2_forename": "MAYA", "signatory_2_surname": "PATEL",
                "signatory_2_date_of_birth": "17/11/1977", "signatory_2_signature": "signed",
                "trustee_declaration_1_name": "ANDREW KNIGHT", "trustee_declaration_1_signature": "signed",
                "trustee_declaration_1_date": "26/10/2026", "trustee_declaration_2_name": "MAYA PATEL",
                "trustee_declaration_2_signature": "signed", "trustee_declaration_2_date": "26/10/2026"},
        alternatives={"account_number_1": ["CO4000110"]},
        register=register("CO4000110", "7000110", "THORNFIELD MISSION SOCIETY", "1180020",
                          ["THE OLD SCHOOL HOUSE", "THORNFIELD ROAD", "YORK"], "YO10 3BP", "OFFICE@THORNFIELDMISSION.ORG.UK",
                          "01904 901010", [andrew, maya, june], bank=bank10, portal_admin="MRS JUNE ELLIS"),
        updated="Mandate details confirmed: trustees, signatories, correspondent, nominated bank account", received="27/10/2026",
        end="03/11/2026 10:50 AM"))

    for c in out:
        c["iid"] = iid(c["n"])
        c["account"], c["client"] = c["register"]["account"], c["register"]["client"]
        c.setdefault("writer", 0)
        c.setdefault("drop_pages", [])
        c.setdefault("illegible", [])
        c.setdefault("alternatives", {})
    return out


CASES = _cases()
