"""
scripts/generate_complex_eval.py
──────────────────────────────────
Generates a large, hand-crafted evaluation dataset (150 rows) covering
real-world bank narration patterns, edge cases, and adversarial inputs.

Run from project root:
    python3 scripts/generate_complex_eval.py
"""

from pathlib import Path
import sys

_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_root))

import pandas as pd

OUTPUT = _root / "data/input/evaluation_sample.xlsx"

# (Actual_Category, Particulars, Debit, Credit, Date)
CASES = [

    # ════════════════════════════════════════════════════════════════════════
    # TDS — Clear signals
    # ════════════════════════════════════════════════════════════════════════
    ("TDS", "BLKNEFT/BLKPAY_00000099/TDS PAYMENT Q4 FY2025/NSDLPAY",              5000,   None, "31-Mar-2026"),
    ("TDS", "NEFT/TAX DEDUCTED AT SOURCE 194J PROFESSIONAL FEES",                 3200,   None, "31-Mar-2026"),
    ("TDS", "RTGS/INCOME TAX PAYMENT AY2025-26 CHALLAN 280",                     18000,   None, "15-Mar-2026"),
    ("TDS", "TDS DEDUCTION 194C CONTRACTOR PAYMENT MARCH 2026",                   1500,   None, "31-Mar-2026"),
    ("TDS", "NEFT/INCOME TAX DEPT NSDL TCS 206C SCRAP SALES",                     750,   None, "30-Jun-2025"),
    ("TDS", "BLKNEFT/TDS RECOVERY FY2024-25 SEC 195 NRI PAYMENT",                4200,   None, "30-Sep-2025"),
    ("TDS", "RTGS/IT DEMAND NOTICE AY2024-25 PAYMENT",                           9800,   None, "15-Dec-2025"),
    ("TDS", "NEFT/TDS CREDIT 192 SALARY DEDUCTION FEB 2026",                     6000,   None, "28-Feb-2026"),
    ("TDS", "NEFT/TDS DEDUCTION 194A INTEREST INCOME FY2026 Q1",                 2400,   None, "05-Apr-2026"),
    ("TDS", "TDS PAYMENT CHALLAN 281 194H COMMISSION BROKERAGE",                 1100,   None, "30-Jun-2025"),
    ("TDS", "BLKNEFT/TCS DEDUCTION 206C SCRAP SALE PROCEEDS",                     450,   None, "30-Sep-2025"),
    ("TDS", "NEFT/TAX DEDUCTED SOURCE RENT 194I QUARTERLY",                      5500,   None, "31-Dec-2025"),
    ("TDS", "IT REFUND AY2023-24 CREDIT INCOME TAX DEPT",                        None,   8000, "10-Apr-2026"),
    ("TDS", "TDS CREDIT FY2025-26 Q4 PROFESSIONAL SERVICES 194J",               None,   2100, "15-Apr-2026"),
    ("TDS", "NEFT/TDS RECOVERY OLD DUES 194C SUB-CONTRACTOR",                    1800,   None, "20-Apr-2026"),
    ("TDS", "RTGS/INCOME TAX ADVANCE TAX INSTALMENT Q2",                        25000,   None, "15-Sep-2025"),

    # ════════════════════════════════════════════════════════════════════════
    # TDS — Section codes with letter suffix
    # ════════════════════════════════════════════════════════════════════════
    ("TDS", "PAYMENT 194A INTEREST ON FD HDFC BANK",                              980,   None, "30-Jun-2025"),
    ("TDS", "TDS 194B WINNINGS LOTTERY ONLINE PORTAL",                            500,   None, "15-Mar-2026"),
    ("TDS", "NEFT/DEDUCTION 194G LOTTERY COMMISSION AGENT",                       220,   None, "31-Jan-2026"),
    ("TDS", "PAYMENT AGAINST 194N CASH WITHDRAWAL THRESHOLD",                    3000,   None, "28-Feb-2026"),
    ("TDS", "TDS ON 194K DIVIDEND MUTUAL FUND REDEMPTION",                        650,   None, "31-Mar-2026"),

    # ════════════════════════════════════════════════════════════════════════
    # GST — Explicit keywords
    # ════════════════════════════════════════════════════════════════════════
    ("GST", "NEFT/GST CHALLAN PMT-06 MARCH 2026 CGST SGST",                     18000,   None, "20-Mar-2026"),
    ("GST", "NEFT/IGST PAYMENT IMPORT DUTY INVOICE INV-2026-001",               12000,   None, "10-Apr-2026"),
    ("GST", "GST REFUND SGST Q4 FY2025 CREDIT TO ACCOUNT",                       None,   4500, "05-Apr-2026"),
    ("GST", "TAX INVOICE PAYMENT VENDOR ABC PVT LTD INV-5678",                   6300,   None, "12-Apr-2026"),
    ("GST", "SERVICE TAX PAYMENT PRE-GST VENDOR LEGACY SYSTEM",                  2100,   None, "01-Apr-2026"),
    ("GST", "NEFT/GST PAYMENT UTGST UNION TERRITORY SUPPLIER",                   3300,   None, "14-Apr-2026"),
    ("GST", "GST REVERSAL CREDIT NOTE VENDOR RETURN INV-4321",                   None,   1500, "16-Apr-2026"),
    ("GST", "PAYMENT PROFORMA INVOICE SUPPLIER ADVANCE 30PCT",                   9000,   None, "18-Apr-2026"),
    ("GST", "E-INVOICE PAYMENT PORTAL GST NETWORK NSDL",                         7700,   None, "19-Apr-2026"),
    ("GST", "NEFT/CGST PAYABLE FY2026 MARCH QUARTER RETURN",                    11000,   None, "25-Mar-2026"),
    ("GST", "IGST REFUND FROM CUSTOMS DEPT FOR EXPORT GOODS",                    None,   6200, "22-Apr-2026"),
    ("GST", "GST CHALLAN APRIL 2026 QUARTERLY PAYMENT GSTIN",                   14000,   None, "30-Apr-2026"),
    ("GST", "BILL PAYMENT ELECTRICITY TNEB CONSUMER 1234567",                    3200,   None, "05-Apr-2026"),
    ("GST", "INVOICE SETTLEMENT OFFICE SUPPLIES VENDOR APRIL",                   1800,   None, "07-Apr-2026"),
    ("GST", "EINVOICE UPLOAD GSTIN 33ABCDE1234F1Z5 PAYMENT",                     4400,   None, "09-Apr-2026"),

    # ════════════════════════════════════════════════════════════════════════
    # GST — Merchant / payment gateway
    # ════════════════════════════════════════════════════════════════════════
    ("GST", "CMS_IFT CARD PMT MID-90096587 SETDT-01042026IDFC",                  3772,   None, "01-Apr-2026"),
    ("GST", "CMS_IFT CARD PMT MID-90096587 SETDT-15042026IDFC",                  1200,   None, "15-Apr-2026"),
    ("GST", "UPI/DR/865747524558/SWIGGY/ICIC/upiswig/Payment",                    450,   None, "05-Apr-2026"),
    ("GST", "UPI/DR/937654321098/AMAZON SELLER SVCS/HDFC/amzn/Order",            1299,   None, "09-Apr-2026"),
    ("GST", "POS TXN RELIANCE FRESH STORE CHENNAI 600020",                         875,   None, "11-Apr-2026"),
    ("GST", "CARD PAYMENT FLIPKART INTERNET SERVICES 3004",                      4599,   None, "13-Apr-2026"),
    ("GST", "UPI/DR/736251897430/ZOMATO MEDIA PVT/HDFC/zomato/Food",              680,   None, "15-Apr-2026"),
    ("GST", "UPI/DR/612834756190/PHONEPE MERCHANT/UTIB/ppemerchant/Pay",          999,   None, "17-Apr-2026"),
    ("GST", "NEFT/AXISCN1313348991/RAZORPAY PAYMENTS PVT LTD ESCROW",            None,  22000, "07-Apr-2026"),
    ("GST", "NEFT/AXISCN1326699579/RAZORPAY PAYMENTS PVT LTD PAYOUT",            None,  15000, "26-Apr-2026"),
    ("GST", "UPI/DR/556677889900/TAXMANN PVT LTD/HDFC/taxmann/Books",             850,   None, "25-Apr-2026"),
    ("GST", "UPI/DR/445566778899/PAYU PAYMENTS PVT LTD/ICIC/payu/Sub",          1500,   None, "27-Apr-2026"),
    ("GST", "UPI/DR/334455667788/GOOGLE PAY MERCHANT/SBIN/gpay/Txn",              360,   None, "28-Apr-2026"),
    ("GST", "CARD PAYMENT AMAZON PRIME ANNUAL SUBSCRIPTION",                     1499,   None, "29-Apr-2026"),
    ("GST", "POS PURCHASE BIGBASKET DELIVERY ONLINE ORDER",                      2340,   None, "30-Apr-2026"),
    ("GST", "UPI/DR/223344556677/MYNTRA DESIGNS/HDFC/myntra/Fashion",            3799,   None, "02-May-2026"),
    ("GST", "CARD PMT HDFC CREDIT CARD 4512XXXX APRIL BILL",                    18500,   None, "05-May-2026"),
    ("GST", "VENDOR PAYMENT INVOICE OFFICE RENT GST INCLUDED",                   45000,   None, "01-Apr-2026"),
    ("GST", "PURCHASE ORDER PAYMENT STATIONERY VENDOR APRIL 26",                  3200,   None, "03-Apr-2026"),
    ("GST", "MERCHANT PAYMENT FUEL STATION IOCL CHENNAI",                        4800,   None, "04-Apr-2026"),

    # ════════════════════════════════════════════════════════════════════════
    # NORMAL — Salary, transfers, ATM, FD
    # ════════════════════════════════════════════════════════════════════════
    ("NORMAL", "NEFT RECEIVED SALARY MARCH 2026 FARM2BAG PVT LTD",              None,  85000, "01-Apr-2026"),
    ("NORMAL", "ATM WITHDRAWAL SBI BRANCH ANNA NAGAR CHENNAI",                  5000,   None, "06-Apr-2026"),
    ("NORMAL", "IMPS TRANSFER TO HDFC SAVINGS XXXX5678 PERSONAL",              15000,   None, "08-Apr-2026"),
    ("NORMAL", "NEFT/FIXED DEPOSIT RENEWAL 90 DAYS HDFC 8.5% PA",              50000,   None, "10-Apr-2026"),
    ("NORMAL", "CHEQUE DEPOSIT 002456 ASHWIN KUMAR PERSONAL LOAN REPAY",         None,  25000, "14-Apr-2026"),
    ("NORMAL", "INTEREST CREDIT SAVINGS ACCOUNT QUARTERLY FY2026",               None,    420, "30-Jun-2025"),
    ("NORMAL", "RTGS/PROPERTY SALE PROCEEDS RECEIVED RERA ESCROW",              None, 500000, "20-Apr-2026"),
    ("NORMAL", "NEFT/LOAN EMI RECEIPT HDFC HOME LOAN ACCOUNT 1234",             None,  45000, "05-Apr-2026"),
    ("NORMAL", "NEFT/ADVANCE RENT PAYMENT 12 MONTHS PROPRIETORSHIP",           120000,   None, "29-Apr-2026"),
    ("NORMAL", "UPI/CR/648651946554/ARUNKUMA/KKBK/arunkum/UPI",                 None,   3400, "18-Apr-2026"),
    ("NORMAL", "NEFT/SALARY CREDIT INCOME MARCH 2026 ACCT PAYROLL",             None,  62000, "01-Apr-2026"),
    ("NORMAL", "FD MATURITY PROCEEDS ICICI BANK 180 DAYS",                      None,  53000, "15-Apr-2026"),
    ("NORMAL", "RECURRING DEPOSIT INSTALLMENT MONTHLY SB ACCOUNT",             10000,   None, "01-Apr-2026"),
    ("NORMAL", "ATM CASH WITHDRAWAL HDFC BANK T NAGAR BRANCH",                  3000,   None, "12-Apr-2026"),
    ("NORMAL", "CHEQUE RETURN 003456 DISHONOURED ICICI CHARGES",                 None,   None, "16-Apr-2026"),
    ("NORMAL", "NEFT/INTER-BANK TRANSFER OWN ACCOUNT SBI SAVINGS",             25000,   None, "17-Apr-2026"),
    ("NORMAL", "UPI/CR/122311671947/S PONRAJ/HDFC/ponrajs/UPI",                 None,    800, "28-Apr-2026"),
    ("NORMAL", "UPI/CR/611921913570/B S VIJ/CNRB/georesi/UPI",                  None,   2200, "29-Apr-2026"),
    ("NORMAL", "IMPS-OPM/610619429062/FARM2BAG PVT LTD/BARB0/7920",            None,   7920, "30-Apr-2026"),

    # ════════════════════════════════════════════════════════════════════════
    # NORMAL — Incoming UPI credits (common false-positive zone)
    # ════════════════════════════════════════════════════════════════════════
    ("NORMAL", "UPI/CR/121584710214/SUBRAMAN/HDFC/subrama/UPI",                  None,     47, "01-Apr-2026"),
    ("NORMAL", "UPI/CR/610251994788/JOHN PAUL/SIBL/pjohnpa/UPI",                 None,   1319, "02-Apr-2026"),
    ("NORMAL", "UPI/CR/961519507458/JASMINE B/HDFC/jasminb/Payment",             None,    250, "03-Apr-2026"),
    ("NORMAL", "UPI/CR/122202621945/SURESH K/HDFC/sureshk/UPI",                  None,    500, "04-Apr-2026"),
    ("NORMAL", "UPI/CR/611878541953/RAMYA S/CNRB/ramyase/UPI",                   None,    760, "05-Apr-2026"),
    ("NORMAL", "UPI/CR/100521925784/DEVARAJA/SBIN/9788557/Market payment",       None,   1100, "06-Apr-2026"),
    ("NORMAL", "UPI/CR/611924881968/SUSHMAN /UTIB/sushman/UPI",                  None,   4500, "07-Apr-2026"),
    ("NORMAL", "UPI/CR/611945907922/S RAMESHA/SBIN/ramesha/UPI",                 None,   2700, "08-Apr-2026"),

    # ════════════════════════════════════════════════════════════════════════
    # NORMAL — Hard edge cases (adversarial)
    # ════════════════════════════════════════════════════════════════════════
    # Contains "income" but is payroll, not income tax
    ("NORMAL", "SALARY INCOME CREDIT APRIL 2026 HR PAYROLL SYSTEM",              None,  72000, "01-May-2026"),
    # Contains "tds" in company name — should NOT be classified TDS
    ("NORMAL", "NEFT/TRANSFER TO HDTDS ENTERPRISES PVT LTD CAPITAL",           10000,   None, "23-Apr-2026"),
    # Small amount < ₹1 → NORMAL
    ("NORMAL", "UPI/DR/000000000001/CANTEEN/SBIN/canteen/Lunch",                   0.5,  None, "28-Apr-2026"),
    # Personal gift — should be NORMAL
    ("NORMAL", "UPI/DR/123456789012/PRIYA RAMESH/CNRB/priyar/Birthday gift",    1000,   None, "27-Apr-2026"),
    # P2P transfer
    ("NORMAL", "UPI/P2P/736251890123/RAVI KUMAR/SBIN/transfer/Personal",        2000,   None, "12-Apr-2026"),
    # Interest income from bank
    ("NORMAL", "INTEREST CREDIT SAVINGS ACCOUNT MONTHLY ACCRUAL",                None,    180, "30-Apr-2026"),
    # Loan disbursement — large NORMAL
    ("NORMAL", "NEFT/HOME LOAN DISBURSEMENT HDFC LTD TRANCHE 3",                None, 800000, "15-May-2026"),
    # Insurance premium — NORMAL
    ("NORMAL", "NEFT/LIC PREMIUM PAYMENT POLICY 123456789 ANNUAL",              12000,   None, "01-Apr-2026"),
    # School fee — NORMAL
    ("NORMAL", "NEFT/SCHOOL FEE PAYMENT DPS CHENNAI TERM 2",                    45000,   None, "01-Apr-2026"),
    # Medical reimbursement — NORMAL
    ("NORMAL", "NEFT/MEDICAL REIMBURSEMENT CORPORATE HEALTH CLAIM",              None,   8500, "10-Apr-2026"),

    # ════════════════════════════════════════════════════════════════════════
    # GST — Keyword collision tests
    # ════════════════════════════════════════════════════════════════════════
    # Has SETDT- (negative -5) but also CARD PMT (strong GST +5) → net positive → GST
    ("GST", "CMS_IFT CARD PMT MID-12345678 SETDT-20042026IDFB",                 1200,   None, "20-Apr-2026"),
    # Incoming GST refund (credit, GST keyword present)
    ("GST", "UPI/CR/889900112233/GST REFUND PORTAL NSDL CREDIT",                 None,   5000, "24-Apr-2026"),
    # Has "invoice" and is outgoing → GST
    ("GST", "NEFT/PAYMENT AGAINST INVOICE INV-2026-789 SUPPLIER",               8400,   None, "22-Apr-2026"),
    # Vendor payment with GST mention
    ("GST", "RTGS/VENDOR PAYMENT OFFICE SUPPLIES GST 18PCT INCLUDED",           22000,   None, "10-Apr-2026"),
    # Merchant with non-round amount
    ("GST", "UPI/DR/667788990011/BIGBASKET/HDFC/bigbskt/Grocery",               1247,   None, "11-Apr-2026"),
    # Easebuzz payment gateway
    ("GST", "NEFT/EASEBUZZ PAYMENT GATEWAY SETTLEMENT APRIL",                    None,  18500, "12-Apr-2026"),
    # Billdesk payment
    ("GST", "NEFT/BILLDESK PAYMENT UTILITY ELECTRICITY APRIL",                  4200,   None, "13-Apr-2026"),
    # GSTIN number in narration
    ("GST", "PAYMENT GSTIN 29AABCU9603R1ZP VENDOR KARNATAKA",                   9900,   None, "14-Apr-2026"),

    # ════════════════════════════════════════════════════════════════════════
    # TDS — Edge cases
    # ════════════════════════════════════════════════════════════════════════
    # Incoming TDS refund (credit side)
    ("TDS", "IT REFUND NSDL AY2024-25 INCOME TAX CREDIT",                        None,  12000, "18-Apr-2026"),
    # TDS with both debit and credit context
    ("TDS", "TDS DEDUCTION INTEREST INCOME FD HDFC BANK 194A",                   1200,   None, "30-Jun-2025"),
    # December quarter-end TDS
    ("TDS", "NEFT/TDS PAYMENT DECEMBER QUARTER 194C TRANSPORT",                  3600,   None, "31-Dec-2025"),
    # BLKNEFT bulk TDS
    ("TDS", "BLKNEFT/SALARY TDS BULK PAYMENT MARCH 2026 192",                   42000,   None, "31-Mar-2026"),
    # Small TDS on FD interest
    ("TDS", "TDS ON FD INTEREST 194A HDFC FIXED DEPOSIT",                          98,   None, "30-Sep-2025"),

    # ════════════════════════════════════════════════════════════════════════
    # NORMAL — More salary/transfer variants
    # ════════════════════════════════════════════════════════════════════════
    ("NORMAL", "NEFT/BONUS PAYMENT DIWALI FY2025-26 PAYROLL",                    None,  20000, "01-Nov-2025"),
    ("NORMAL", "NEFT/GRATUITY PAYMENT EMPLOYEE SETTLEMENT",                      None,  80000, "15-Apr-2026"),
    ("NORMAL", "UPI/CR/556677889901/KAVITHA R/SBIN/kavithar/Rent",               None,  15000, "01-Apr-2026"),
    ("NORMAL", "NEFT/DIVIDEND RECEIVED INFOSYS LTD Q3 2025",                     None,   3200, "15-Jan-2026"),
    ("NORMAL", "NEFT/MUTUAL FUND REDEMPTION HDFC EQUITY FUND",                   None,  45000, "20-Apr-2026"),
    ("NORMAL", "NEFT/PPF ACCOUNT TRANSFER POST OFFICE SAVINGS",                 15000,   None, "01-Apr-2026"),
    ("NORMAL", "NEFT/NPS CONTRIBUTION NATIONAL PENSION SCHEME",                 12500,   None, "01-Apr-2026"),
    ("NORMAL", "UPI/DR/778899001122/PETROL BUNK/SBIN/fuel/Fuel fill",           2000,   None, "10-Apr-2026"),
    ("NORMAL", "NEFT/CREDIT CARD BILL PAYMENT AXIS BANK CARD",                  18000,   None, "08-Apr-2026"),
    ("NORMAL", "ATM WITHDRAWAL CANARA BANK VELACHERY BRANCH",                    6000,   None, "20-Apr-2026"),
    ("NORMAL", "NEFT/VEHICLE LOAN EMI HDFC AUTO FINANCE APRIL",                 14500,   None, "05-Apr-2026"),
    ("NORMAL", "NEFT/PERSONAL LOAN EMI KOTAK BANK APRIL 2026",                   8200,   None, "06-Apr-2026"),

]

rows = []
for actual, particulars, debit, credit, date in CASES:
    rows.append({
        "Actual_Category":  actual,
        "Transaction Date": date,
        "Value Date":       date,
        "Particulars":      particulars,
        "Cheque No.":       "",
        "Debit":            debit,
        "Credit":           credit,
        "Balance":          100000.0,
    })

df = pd.DataFrame(rows)
OUTPUT.parent.mkdir(parents=True, exist_ok=True)
df.to_excel(str(OUTPUT), index=False)

dist = df["Actual_Category"].value_counts().to_dict()
print(f"✅ Complex evaluation dataset written → {OUTPUT}")
print(f"   Total rows : {len(df)}")
print(f"   Distribution: {dist}")

# ── NEW COMPLEX CASES ─────────────────────────────────────────────────────────
NEW_CASES = [
    # TDS — New sections
    ("TDS","NEFT/TDS 194Q GOODS PURCHASE ABOVE 50L CHALLAN",8500,None,"30-Jun-2025"),
    ("TDS","NEFT/194S TDS VIRTUAL DIGITAL ASSET CRYPTO TRANSFER",5000,None,"31-Mar-2026"),
    ("TDS","RTGS/INCOME TAX SELF ASSESSMENT AY2026-27 CHALLAN 280",32000,None,"31-Mar-2026"),
    ("TDS","TCS COLLECTION 206CL MOTOR VEHICLE ABOVE 10L SALE",15000,None,"15-Apr-2026"),
    ("TDS","NEFT/TDS ON INTEREST 194A COOPERATIVE BANK FD Q2",640,None,"30-Sep-2025"),
    ("TDS","BLKNEFT/TDS Q1 FY2026 194C TRANSPORTERS LOGISTICS",7200,None,"30-Jun-2025"),
    ("TDS","NEFT/TDS DEDUCTION 194I RENT COMMERCIAL PROPERTY Q3",22000,None,"31-Dec-2025"),
    ("TDS","RTGS/INCOME TAX DEMAND 143(3) SCRUTINY AY2023-24",45000,None,"15-Jan-2026"),
    ("TDS","NEFT/TDS 194D INSURANCE COMMISSION AGENT QUARTERLY",380,None,"31-Mar-2026"),
    ("TDS","INCOME TAX ADVANCE TAX THIRD INSTALMENT DECEMBER",18000,None,"15-Dec-2025"),
    ("TDS","NEFT/206AB HIGHER TDS DEDUCTION NON-FILER VENDOR",4200,None,"30-Apr-2026"),
    ("TDS","TDS CHALLAN 281 QUARTERLY 194H BROKERAGE MARCH 2026",2100,None,"31-Mar-2026"),
    ("TDS","NEFT/INCOME TAX REFUND AY2025-26 DIRECT CREDIT NSDL",None,18500,"22-Apr-2026"),
    ("TDS","RTGS/TDS PAYMENT 195 NON-RESIDENT NRI FOREIGN PARTY",12000,None,"30-Apr-2026"),
    ("TDS","BLKNEFT/TDS FEB 2026 194J IT SERVICES VENDOR DEDUCT",8800,None,"28-Feb-2026"),
    ("TDS","NSDL TDS CHALLAN MARCH 2026 MULTIPLE DEDUCTEE BULK",15600,None,"31-Mar-2026"),
    ("TDS","RTGS/INCOME TAX PENALTY 271(1)(C) AY2024-25 PAYMENT",25000,None,"31-Dec-2025"),
    ("TDS","IT REFUND CREDITED DIRECT BANK AY2024-25 INCOME TAX",None,9600,"12-Apr-2026"),
    ("TDS","NEFT/CHALLAN 26QB TDS ON PROPERTY SECTION 194IA PAY",150000,None,"15-Apr-2026"),
    ("TDS","NEFT/TDS 194G COMMISSION LOTTERY TICKET VENDOR Q4",850,None,"30-Jun-2025"),
    ("TDS","TDS DEDUCTION 192B SALARY DIRECTOR MARCH PAYROLL",35000,None,"31-Mar-2026"),
    ("TDS","NEFT/TDS RECOVERY 194N CASH WITHDRAWAL ABOVE 1CR",12000,None,"28-Feb-2026"),
    # GST — Advanced patterns
    ("GST","NEFT/IGST SAAS SUBSCRIPTION MICROSOFT AZURE INDIA",12500,None,"01-Apr-2026"),
    ("GST","REVERSE CHARGE IGST LEGAL SERVICES LAWYER FIRM",18000,None,"31-Mar-2026"),
    ("GST","CGST SGST PAYMENT GSTR-3B MARCH 2026 QUARTERLY",24000,None,"20-Mar-2026"),
    ("GST","NEFT/GSTIN 27AADCB2230M1ZT VENDOR MAHARASHTRA SETTLE",9900,None,"15-Apr-2026"),
    ("GST","CARD PAYMENT NETFLIX INDIA SUBSCRIPTION MONTHLY 649",649,None,"01-Apr-2026"),
    ("GST","NEFT/IGST CESS COMPENSATION LUXURY GOODS APRIL 2026",8800,None,"25-Apr-2026"),
    ("GST","UPI/DR/SWIGGY INSTAMART QUICK DELIVERY/ICIC/instamart/Grocery",1847,None,"11-Apr-2026"),
    ("GST","NEFT/EINVOICE IRN HASH VERIFIED VENDOR SETTLEMENT",35000,None,"18-Apr-2026"),
    ("GST","UPI/DR/BLINKIT GROFERS DELIVERY/HDFC/blinkit/Quick",2345,None,"12-Apr-2026"),
    ("GST","CARD PMT SPOTIFY INDIA PREMIUM ANNUAL SUBSCRIPTION",1189,None,"15-Apr-2026"),
    ("GST","NEFT/CGST PAYABLE QUARTERLY RETURN GSTR-3B FY2026",16500,None,"25-Apr-2026"),
    ("GST","NEFT/GST CHALLAN INTEGRATED TAX EXPORT SERVICES APRIL",5500,None,"02-Apr-2026"),
    ("GST","UPI/DR/MEESHO SUPPLY INDIA/HDFC/meesho/Order 1234",1650,None,"20-Apr-2026"),
    ("GST","VENDOR TAX INVOICE CGST 9PCT SGST 9PCT INV-2026-TXN",7200,None,"16-Apr-2026"),
    ("GST","NEFT/IGST PAYABLE IMPORT CUSTOMS FREIGHT FORWARDER",22000,None,"05-Apr-2026"),
    ("GST","CARD PAYMENT HOTSTAR DISNEY PLUS SUBSCRIPTION ANNUAL",1499,None,"22-Apr-2026"),
    ("GST","POS PURCHASE CROMA ELECTRONICS STORE ANNA NAGAR",24990,None,"14-Apr-2026"),
    ("GST","NEFT/GST PORTAL PMT-09 ITC ADJUSTMENT TRANSFER",12000,None,"28-Apr-2026"),
    ("GST","UPI/DR/ZEPTO DELIVERY/HDFC/zepto/Grocery order fast",937,None,"06-Apr-2026"),
    ("GST","CARD PAYMENT ADOBE CREATIVE CLOUD ANNUAL INDIA",4230,None,"10-Apr-2026"),
    ("GST","NEFT/SGST CGST REVERSAL RCM SERVICES IMPORT MARCH",3600,None,"29-Mar-2026"),
    ("GST","VENDOR PAYMENT GSTIN 33ABCDE1234F1Z5 CHENNAI GOODS",15000,None,"23-Apr-2026"),
    ("GST","NEFT/IGST REFUND CUSTOMS EXPORT LUT BOND FY2026",None,38000,"08-Apr-2026"),
    ("GST","CARD PMT APPLE ICLOUD STORAGE SUBSCRIPTION 50GB INR",75,None,"01-May-2026"),
    ("GST","UPI/DR/JUSPAY PAYMENT GATEWAY/HDFC/juspay/Merchant",3500,None,"24-Apr-2026"),
    ("GST","NEFT/CGST SGST ITC CLAIM VENDOR INVOICE MARCH 2026",8900,None,"30-Mar-2026"),
    ("GST","CARD PMT LINKEDIN PREMIUM SUBSCRIPTION INDIA ANNUAL",2499,None,"05-Apr-2026"),
    ("GST","UPI/DR/DUNZO DELIVERY/HDFC/dunzo/Quick commerce",678,None,"19-Apr-2026"),
    ("GST","NEFT/IGST ON SOFTWARE LICENSE SAP INDIA ANNUAL",85000,None,"01-Apr-2026"),
    # NORMAL — Diverse personal/investment
    ("NORMAL","NEFT/SOVEREIGN GOLD BOND SGB TRANCHE VI SUBSCRIPTION",50000,None,"15-Apr-2026"),
    ("NORMAL","NEFT/NATIONAL PENSION SYSTEM NPS TIER1 CONTRIBUTION",12500,None,"01-Apr-2026"),
    ("NORMAL","INWARD REMITTANCE SWIFT USD FOREIGN SALARY CREDIT",None,95000,"05-Apr-2026"),
    ("NORMAL","NEFT/ZERODHA BROKING EQUITY SETTLEMENT T PLUS 2",None,35000,"08-Apr-2026"),
    ("NORMAL","NEFT/MUTUAL FUND SIP AXIS BLUECHIP FUND MONTHLY",5000,None,"01-Apr-2026"),
    ("NORMAL","UPI/CR/VIJAY KUMAR/SBI/vijayk/Festival gift transfer",None,2000,"14-Apr-2026"),
    ("NORMAL","NEFT/EPF CONTRIBUTION EMPLOYEE SHARE PF MARCH 2026",3600,None,"31-Mar-2026"),
    ("NORMAL","NEFT/GOLD LOAN REPAYMENT MUTHOOT FINANCE PRINCIPAL",25000,None,"10-Apr-2026"),
    ("NORMAL","ATM CASH WITHDRAWAL IDFC BANK ADYAR CHENNAI BRANCH",8000,None,"07-Apr-2026"),
    ("NORMAL","NEFT/CHIT FUND MONTHLY CONTRIBUTION SHRIRAM CHITS",10000,None,"05-Apr-2026"),
    ("NORMAL","UPI/CR/PREETHI R/HDFC/preethi/Room rent share apr",None,7500,"01-Apr-2026"),
    ("NORMAL","NEFT/SUKANYA SAMRIDDHI ACCOUNT ANNUAL POST OFFICE",50000,None,"01-Apr-2026"),
    ("NORMAL","NEFT/VEHICLE INSURANCE RENEWAL IFFCO TOKIO ANNUAL",18000,None,"01-Apr-2026"),
    ("NORMAL","ATM WITHDRAWAL AXIS BANK VELACHERY CHENNAI 600042",10000,None,"15-Apr-2026"),
    ("NORMAL","NEFT/RECURRING DEPOSIT CANARA BANK MONTHLY INSTALMENT",5000,None,"01-Apr-2026"),
    ("NORMAL","UPI/CR/SURESH BABU/ICIC/suresh/Grocery split payment",None,450,"10-Apr-2026"),
    ("NORMAL","NEFT/PPF ANNUAL CONTRIBUTION POST OFFICE FY2026",1500000,None,"31-Mar-2026"),
    ("NORMAL","UPI/CR/MEENA S/SBIN/meenas/Birthday money transfer",None,1000,"20-Apr-2026"),
    ("NORMAL","NEFT/HOME LOAN EMI HDFC BANK APRIL 2026 A/C 5678",42000,None,"05-Apr-2026"),
    ("NORMAL","NEFT/CREDIT CARD PAYMENT SBI SIMPLYCASH CARD APR",12500,None,"10-Apr-2026"),
    ("NORMAL","IMPS/PERSONAL TRANSFER OWN ACCOUNT HDFC TO ICICI",20000,None,"12-Apr-2026"),
    ("NORMAL","NEFT/MOTORCYCLE LOAN EMI BAJAJ FINANCE APRIL 2026",4500,None,"07-Apr-2026"),
    ("NORMAL","UPI/DR/PARKING FEE/SBIN/park/Airport long stay",850,None,"03-Apr-2026"),
    ("NORMAL","NEFT/TERM INSURANCE PREMIUM HDFC CLICK2PROTECT PLAN",22000,None,"01-Apr-2026"),
    ("NORMAL","ATM CASH DEPOSIT BRANCH ANNA NAGAR SELF DEPOSIT",None,15000,"09-Apr-2026"),
    ("NORMAL","UPI/CR/RAMKUMAR V/CNRB/ramkum/Share of dinner bill",None,650,"08-Apr-2026"),
    ("NORMAL","NEFT/FIXED DEPOSIT OPENED IDFC BANK 365 DAYS 7.5PCT",100000,None,"01-Apr-2026"),
    ("NORMAL","NEFT/STAFF ADVANCE RECOVERY DEDUCTION SALARY APRIL",5000,None,"30-Apr-2026"),
    ("NORMAL","UPI/CR/ANAND T/HDFC/anandt/Movie ticket reimbursement",None,600,"17-Apr-2026"),
    ("NORMAL","NEFT/TNEB ELECTRICITY BILL PAYMENT CONSUMER 9876543",2800,None,"08-Apr-2026"),
]

extra_rows = []
for actual, particulars, debit, credit, date in NEW_CASES:
    extra_rows.append({
        "Actual_Category": actual, "Transaction Date": date,
        "Value Date": date, "Particulars": particulars,
        "Cheque No.": "", "Debit": debit, "Credit": credit, "Balance": 100000.0,
    })

import pandas as pd
from pathlib import Path
OUTPUT = Path(__file__).resolve().parent.parent / "data/input/evaluation_sample.xlsx"
existing = pd.read_excel(str(OUTPUT))
combined = pd.concat([existing, pd.DataFrame(extra_rows)], ignore_index=True)
combined.to_excel(str(OUTPUT), index=False)
dist = combined["Actual_Category"].value_counts().to_dict()
print(f"✅ Updated evaluation_sample.xlsx")
print(f"   Total rows  : {len(combined)}")
print(f"   Distribution: {dist}")
