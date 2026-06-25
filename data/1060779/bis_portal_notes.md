# BIS Portal Scrape — BIN 1060779 (310 W 144 Street, Manhattan)

Source: https://a810-bisweb.nyc.gov/bisweb/PropertyProfileOverviewServlet?bin=1060779
Captured: 2026-06-24

This file captures information visible in the BIS HTML portal that is **not** in
the Socrata Open Data API (`ic3t-wcy2`, `bs8b-p36w`). The portal exposes a few
fields — chiefly the pre-1985 paper records and the LNO/Action ledger — that
the structured datasets either miss entirely or expose only with poor coverage.

## Property profile
- Address: 310 West 144 Street, Manhattan, NY 10030 (also lists 310-312)
- BIN: 1060779
- Block 2044, Lot 20
- BBL: 1020440020
- Tax block/lot: 2044 / 20
- Cross streets: Frederick Douglass Boulevard, Bradhurst Avenue
- DOF Building Classification: **D1 - ELEVATOR APT**
- HPD Multiple Dwelling: **Yes**
- Community Board: 110 (Manhattan CB10)
- Census Tract: 259
- Buildings on lot: 1
- Additional BINs for building: NONE
- Landmark Status: not landmarked
- Vacant: NO
- Condo: NO
- Special District: UNKNOWN

## Tallies (totals / open) from portal sidebar
- Complaints: 45 / 0
- DOB violations: 33 / 0
- OATH/ECB violations: 58 / 0
- Jobs/Filings: 11
- ARA / LAA Jobs: 1
- Total Jobs: 12
- Actions: 24

Nothing currently open. Building is in compliance posture as of this scrape.

## The only Certificate of Occupancy on record before 2025
- **CO 980, dated 1918-08-26**
- Legal use: "non-fireproof, basement & 4 story garage"
- Floors:
  - Basement: boiler room
  - 1st floor & floors above: garage, 15 employees in the entire building
- Owner of record at issuance: Euphemia G. Coffin
- Source: BIS legacy CO PDF (M000000980.PDF), copied to this directory as `co_980_1918.pdf`
- This CO is *paper-only* and does **not** appear in the digital `bs8b-p36w`
  dataset (Socrata pull returned `[]`). The BIS portal exposes it as a
  scanned PDF only.

## Letter of No Objection 4281 (LNO 4281)
- Date: 2017-09-27
- Use status: APPROVED
- Use: PARKING GARAGE UG#8
- Floors: ONE THROUGH FOUR
- Comments: "PARKING GARAGE FOR 130 CARS"

This LNO **re-affirms the garage use 99 years after the original CO** and
post-dates the 2009-2011 conversion approvals. It is a strong signal that
the residential conversion never actually took place in 2010-2017 —
the building was still operating as a 130-car parking garage in 2017.

## Pre-1985 paper "Actions" ledger from BIS portal
These are the legacy paper records prior to the digital DOB era. Most have
file date "00/00/YYYY" because only the year was preserved when records were
digitized into the ledger.

| Record | Type | Date |
|---|---|---|
| NB 100-02* | NEW BUILDING | 1902 |
| UB* 1750-08 | (UB unknown — likely Use Building?) | 1908 |
| ELV 111-18 | ELEVATOR | 1918 |
| ALT 334-18 | ALTERATION | 1918 |
| P 205-18 | PLUMBING | 1918 |
| **CO 980** | **CERTIFICATE OF OCCUPANCY** | **1918** |
| ESA 2782-27 | ELECTRIC SIGN APP | 1927 |
| ESA 431-29 | ELECTRIC SIGN APP | 1929 |
| ESA 2776-37 | ELECTRIC SIGN APP | 1937 |
| SR 1636-38 | SPECIAL REPORT | 1938 |
| GAS 504-46 | GAS | 1946 |
| ALT 556-48 | ALTERATION | 1948 |
| SR 4068-48 | SPECIAL REPORT | 1948 |
| UB* 92-48 | (UB unknown) | 1948 |
| ESA 80-51 | ELECTRIC SIGN APP | 1951 |
| COM 1289-53 | COMPLAINTS | 1953 |
| BN 1944-54E | BUILDING NOTICE | 1954 |
| COM 71-55 | COMPLAINTS | 1955 |
| COM 3494-64 | COMPLAINTS | 1964 |
| BN 2930-76 | BUILDING NOTICE | 1976 |
| V* 010980E1244F5 | DOB VIOLATION (CLOSED 2011-09-28) | 1980 |
| V* 010980E1244-511F5 | DOB VIOLATION (CLOSED 2011-09-28) | 1980 |
| EA 468/03SO062403#1514 | ELEVATOR APP | 2003-05-02 |

## Notes for corpus / coverage tracking
- The Socrata BIS jobs dataset (`ic3t-wcy2`) covers only filings the portal
  classifies as "Jobs/Filings" — i.e. permitted construction work since the
  digital era. It does **not** include the Actions ledger above.
- Pre-1985 paper records (NB 100-02, the 1918 ALT/P/ELV cluster, etc.) are
  visible only as ledger entries. Their underlying drawings would need to
  be requested as physical microfilm from 280 Broadway. They are listed
  here for completeness but treated as **out of corpus** for this build.
- The 1918 CO 980 PDF is in corpus (`co_980_1918.pdf`) and ingested.
- The 2017 LNO 4281 is **not** an entity in our schema currently. Decision:
  treat it as a `legal_use_event` attached to BIN, separate from CO and
  separate from Job. Captured in the portal notes for now; will land in
  the schema as an `interim_use_records` table.
