"""
scripts/generate_sample_data.py
────────────────────────────────
Generates a realistic sample bank_statement.xlsx for testing.

Run from the project root:
    python scripts/generate_sample_data.py
"""

from pathlib import Path
import pandas as pd
import random

OUTPUT_PATH = Path("data/input/bank_statement.xlsx")

SAMPLE_TRANSACTIONS = [
    # TDS rows
    ("01/04/2024", "TDS Deduction 194J Professional Fees Q4",         5000,  None),
    ("05/04/2024", "TAX DEDUCTED AT SOURCE 194C Contractor Payment",  2000,  None),
    ("10/04/2024", "TDS Recovery FY2023-24 Sec 195",                  1500,  None),
    ("15/04/2024", "INCOME TAX REFUND AY2023-24",                     None,  8000),
    ("20/04/2024", "TDS Credit 192 Salary Mar 2024",                  3000,  None),
    ("25/04/2024", "TCS Collected 206C Scrap Sale",                    750,  None),

    # GST rows
    ("02/04/2024", "NEFT/GST Payment April 2024 CGST",               18000,  None),
    ("03/04/2024", "IGST on Import Invoice #INV-2024-001",            12000,  None),
    ("07/04/2024", "GST Challan PMT-06 March Filing",                  9000,  None),
    ("08/04/2024", "Tax Invoice #5567 Vendor ABC Pvt Ltd",             6300,  None),
    ("12/04/2024", "Card Payment Amazon.in Order 405-XYZ",              None,  None),   # merchant
    ("13/04/2024", "POS Transaction Reliance Fresh Store",             1200,  None),   # merchant
    ("18/04/2024", "Bill Payment BSNL Broadband April",                 999,  None),   # merchant
    ("22/04/2024", "GST Refund SGST Q4 FY2024",                        None,  4500),
    ("27/04/2024", "CMS_VENDOR/SWIGGY BUSINESS SETTLEMENT",            None,  3200),   # merchant

    # NORMAL rows
    ("04/04/2024", "NEFT Received Salary March 2024",                  None, 85000),
    ("06/04/2024", "ATM Withdrawal SBI Connaught Place",              10000,  None),
    ("09/04/2024", "Transfer to Savings Account XXXX1234",             5000,  None),
    ("11/04/2024", "UPI/P2P Ravi Kumar 9876543210",                    2000,  None),
    ("14/04/2024", "IMPS Transfer to HDFC XXXX5678",                  15000,  None),
    ("16/04/2024", "Fixed Deposit Renewal 90 days",                   50000,  None),
    ("19/04/2024", "Cheque Deposit 001234 Ashwin Traders",             None, 25000),
    ("21/04/2024", "NEFT Transfer Personal Account",                   3000,  None),
    ("23/04/2024", "Interest Credit Savings Account",                  None,   420),
    ("28/04/2024", "Opening Balance Brought Forward",                  None, 10000),
]

random.shuffle(SAMPLE_TRANSACTIONS)

rows = []
for i, (date, particulars, debit, credit) in enumerate(SAMPLE_TRANSACTIONS, start=1):
    balance = random.uniform(10000, 500000)
    rows.append({
        "Sr. No.":     i,
        "Date":        date,
        "Particulars": particulars,
        "Chq./Ref.No": f"REF{random.randint(100000, 999999)}",
        "Debit":       debit,
        "Credit":      credit,
        "Balance":     round(balance, 2),
    })

df = pd.DataFrame(rows)
OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
df.to_excel(OUTPUT_PATH, index=False)
print(f"✅ Sample data written to {OUTPUT_PATH.resolve()}")
print(f"   Rows: {len(df)}")
