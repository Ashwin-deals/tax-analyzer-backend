# ─────────────────────────────────────────────────────────────────────────────
#  utils/constants.py  —  Single source of truth for keywords, weights, paths
# ─────────────────────────────────────────────────────────────────────────────

# ── Internal column aliases ───────────────────────────────────────────────────
# Added to the DataFrame by normalize_columns(); dropped before export.
INTERNAL_DESCRIPTION_COL = "_description"
INTERNAL_DEBIT_COL       = "_debit"
INTERNAL_CREDIT_COL      = "_credit"
INTERNAL_DATE_COL        = "_date"
INTERNAL_COLS = [INTERNAL_DESCRIPTION_COL, INTERNAL_DEBIT_COL,
                 INTERNAL_CREDIT_COL, INTERNAL_DATE_COL]

# ── Column name candidates (case-insensitive auto-detection) ──────────────────
DESCRIPTION_COLUMN_CANDIDATES = [
    "particulars", "narration", "description", "remarks",
    "transaction details", "details", "transaction narration", "reference",
    # ICICI / IDFC style
    "transaction remarks", "tran remarks", "txn remarks",
    # Other bank formats
    "chq / ref no", "chq/ref", "transaction description", "beneficiary",
    "payment details", "memo", "note",
]
DEBIT_COLUMN_CANDIDATES = [
    "debit", "withdrawal", "withdrawal amt.", "withdrawal amt",
    "debit amt", "debit amt.", "dr amount", "dr amt",
    # Variants with parenthetical suffixes or typos (e.g. ICICI 'Withdra wal (Dr)')
    "withdrawal (dr)", "withdra wal (dr)", "withdrawl (dr)", "withdraw (dr)",
    "debit (dr)", "dr)", "amount (dr)", "wdl (dr)",
    # Other banks
    "amount debited", "debit amount",
]
CREDIT_COLUMN_CANDIDATES = [
    "credit", "deposit", "deposit amt.", "deposit amt",
    "credit amt", "credit amt.", "cr amount", "cr amt",
    # Variants with parenthetical suffixes (e.g. ICICI 'Deposit (Cr)')
    "deposit (cr)", "credit (cr)", "cr)", "amount (cr)",
    # Other banks
    "amount credited", "credit amount",
]
DATE_COLUMN_CANDIDATES = [
    "transaction date", "date", "value date", "txn date", "posting date",
]

# ── Categories (TAX_CATEGORY) ──────────────────────────────────────────────────
# Final tax interpretation values. Flow/business semantics live in FLOW_TYPE,
# not in the tax category.
CATEGORY_TDS          = "TDS"
CATEGORY_GST          = "GST"
CATEGORY_POSSIBLE_GST = "POSSIBLE_GST"
CATEGORY_NORMAL       = "NORMAL"

# Legacy label retained only so older evaluation files can be read safely.
# The scorer should not emit this as a final TAX_CATEGORY.
CATEGORY_UNCERTAIN    = "UNCERTAIN"
TAX_CATEGORY_ORDER    = [CATEGORY_GST, CATEGORY_POSSIBLE_GST, CATEGORY_TDS, CATEGORY_NORMAL]

# Thresholds for explicit classifications, ambiguous POSSIBLE_GST, and review
# routing. Competing signals are handled through Review_Recommended instead of
# emitting a separate UNCERTAIN tax category.
SCORE_HIGH_THRESHOLD     = 8    # ≥ 8  → HIGH confidence, direct classification
SCORE_MEDIUM_THRESHOLD   = 3    # ≥ 3  → MEDIUM confidence, classifiable
SCORE_UNCERTAIN_CUTOFF   = 4    # competing signals above this threshold trigger review
SCORE_CLOSE_CALL_MARGIN  = 2    # TDS/GST within this margin -> Review_Recommended

# ── TDS signal weights ────────────────────────────────────────────────────────
SCORE_TDS_KEYWORD        = 10
SCORE_TDS_SECTION_CODE   = 8
SCORE_TDS_TXTYPE_BLKNEFT = 4
SCORE_TDS_QUARTER_END    = 1    # Reduced: timing alone can't elevate category

# ── GST signal weights ────────────────────────────────────────────────────────
SCORE_GST_KEYWORD        = 10
SCORE_GST_GSTIN_PATTERN  = 9
SCORE_GST_WEAK_HINT      = 4    # Weak hint (invoice, settlement, accounting)
SCORE_GST_GATEWAY        = 3    # Gateway/commercial infra -> POSSIBLE_GST when tax is ambiguous
SCORE_GST_CMS_CARDPMT    = 2    # Card/CMS is behavioral context, not a final tax category
SCORE_GST_UPI_DEBIT      = 1    # Reduced: UPI debit alone is insufficient for tax ambiguity
SCORE_GST_NONROUND_AMT   = 2

# Applied symmetrically when a transaction is a pure incoming credit (Fix #5)
PENALTY_INCOMING_TDS = -5
PENALTY_INCOMING_GST = -5

# ── Amount sanity thresholds ──────────────────────────────────────────────────
AMOUNT_IGNORE_BELOW = 1.0          # < ₹1  → skip TDS/GST scoring
AMOUNT_FLAG_ABOVE   = 1_000_000.0  # > ₹10L → flag for review unless strong signal

# ── Soft negative keyword penalties ──────────────────────────────────────────
# Each tuple: (pattern_string, is_regex, penalty_points)
# Penalties are SUBTRACTED from both TDS and GST scores (not a hard cancel).
NEGATIVE_KEYWORDS = [
    # ─ Transfer / incoming signals ──────────────────────────────────────
    (r"upi/cr/",              False, -6),  # UPI incoming credit — money received
    (r"upi/\d{10,}//upi\s*$",True,  -5),  # Truncated UPI narration (tightened: needs //upi at end)
    (r"setdt-",               False, -5),  # Settlement date suffix in card narrations
    (r"imps-opm/",            False, -4),  # Standard IMPS transfer prefix
    (r"/p2p/",                False, -7),  # Peer-to-peer UPI transfer
    (r"\batm[/ ]",            True,  -8),  # ATM withdrawal
    # ─ Personal / non-business payments ──────────────────────────────
    (r"birthday",             False, -6),  # Personal gift transfer
    (r"interest credit",      False, -5),  # Savings interest credit, not GST
    (r"\bsalary\b.{0,10}\bcredit\b", True, -5),  # Salary credit (specific — won't catch 'TDS CREDIT')
    (r"net salary",           False, -8),  # Net-of-TDS salary credit
    (r"salary after tds",     False,-10),  # Explicit salary-after-TDS narration
    (r"personal",             False, -3),  # Personal transfer hint
    (r"rent payment",         False, -5),  # Rent — personal expense, not GST
    (r"\brent\b",             True,  -4),  # Rent keyword alone
    (r"credit card payment",  False, -10), # Credit card payment — kills bill-payment GST kw
    (r"credit card bill",     False, -10), # Credit card bill — kills merchant + bill-payment
    (r"card bill payment",    False, -10), # Explicit card bill phrasing
    (r"card outstanding",     False, -7),  # Outstanding dues transfer
    (r"petrol",               False, -4),  # Petrol — personal expense
    (r"fuel fill",            False, -4),  # Fuel station — personal
    (r"loan emi",             False, -5),  # Loan EMI = normal bank payment
    (r"insurance premium",    False, -4),  # Insurance = normal payment
    (r"school fee",           False, -4),  # School fee = normal payment
    # ─ Statutory / government contributions ──────────────────────────
    (r"epf contribution",     False, -8),  # Employee PF — statutory, not GST/TDS
    (r"esic contribution",    False, -8),  # Employee State Insurance
    (r"\bpf contribution\b",  True,  -7),  # Provident fund
    (r"provident fund",       False, -5),  # PF general mention
]

# ── Company suffix penalties (applied to TDS score ONLY) ─────────────────────
# Reduces TDS when a tax keyword appears inside a company/vendor name.
# NOT applied to GST — so Razorpay Pvt Ltd / merchant gateway credits
# are not penalized just for having 'Pvt Ltd' in the narration.
COMPANY_SUFFIX_PENALTIES = [
    (r"pvt ltd",           False, -5),
    (r"private ltd",       False, -5),
    (r"pvt\. ltd",         True,  -5),
    (r"\bservices\b",      True,  -4),
    (r"\bsolutions\b",     True,  -4),
    (r"\bconsultants\b",   True,  -4),
    (r"\benterprises\b",   True,  -4),
    (r"\btechnologies\b",  True,  -4),
    (r"\baccounting\b",    True,  -3),   # e.g. 'TDS Accounting Services'
]

# ── NORMAL override keywords ──────────────────────────────────────────────────
# If ANY of these match the narration text → immediately classify as NORMAL,
# BEFORE scoring. Replaces the fragile negative-penalty approach for utility
# and statutory payments that are unambiguously personal/business expenses.
#
# IMPORTANT: This override is bypassed when a strong GST/TDS keyword is ALSO
# present (e.g. "TNEB GST PAYMENT" stays GST). Bypass logic is in scorer.py.
#
# Each tuple: (pattern_string, is_regex)
NORMAL_OVERRIDE_KEYWORDS = [
    # ─ Electricity boards ──────────────────────────────────────────────
    (r"\btneb\b",      True),   # Tamil Nadu Electricity Board
    (r"\bbescom\b",   True),   # Bangalore Electricity
    (r"\btsspdcl\b",  True),   # Telangana Southern
    (r"\bkseb\b",     True),   # Kerala State Electricity
    (r"\btangedco\b", True),   # Tamil Nadu Generation
    (r"\bmsedcl\b",   True),   # Maharashtra
    (r"\bwbsedcl\b",  True),   # West Bengal
    (r"\bdvvnl\b",    True),   # UP Paschimanchal
    (r"electricity bill",  False), # Generic electricity bill
    # ─ Gas utilities ───────────────────────────────────────────────────
    (r"\bigl\b",      True),   # Indraprastha Gas
    (r"\bmgl\b",      True),   # Mahanagar Gas
    (r"\badani gas\b",True),   # Adani Gas
    (r"gas bill",     False),  # Generic gas bill
    # ─ Water utilities ─────────────────────────────────────────────────
    (r"\bcmwssb\b",   True),   # Chennai Metro Water
    (r"\bbwssb\b",    True),   # Bangalore Water
    (r"water bill",   False),  # Generic water bill
    # ─ Statutory contributions ─────────────────────────────────────────
    (r"\bepf\b.{0,20}\bcontribution\b", True),  # EPF contribution
    (r"\besic\b.{0,20}\bcontribution\b", True),  # ESIC contribution
]

# ── TDS keywords (whole-word matched in classifier) ───────────────────────────
TDS_KEYWORDS = [
    "tds", "tax deducted", "tax deducted at source",
    "tds deduction", "tds credit", "tds recovery",
    "income tax", "it refund", "it demand", "tcs",
    "interest tax", "bank tds", "tds on interest", "deducted",
    "u/s 194", "u/s 192", "u/s 195", "u/s 206",
]

# TDS section codes — matched with letter suffix (194A) OR next to tds/section
TDS_SECTION_CODES = ["192", "193", "194", "195", "196", "206"]

# ── GST keywords — NO bare "tax" (Fix #12) ────────────────────────────────────
# "tax" alone is too broad; "income tax" is TDS. Only compound GST terms here.
GST_KEYWORDS = [
    "gst", "cgst", "sgst", "igst", "utgst", "gstin",
    "gst payment", "gst refund", "gst credit", "gst reversal",
    "gst challan", "gst payable",
    "goods and service", "goods & service",
    "service tax",      # pre-GST era
]

# ── GST weak hints (→ POSSIBLE_GST) ───────────────────────────────────────────
GST_WEAK_HINTS = [
    "tax invoice",      
    "proforma invoice", "e-invoice", "einvoice",
    "invoice", "bill payment", "settlement", "accounting", "expense"
]

# ── Service/vendor semantic intelligence (→ FLOW_TYPE BUSINESS + POSSIBLE_GST) ─
SERVICE_VENDOR_KEYWORDS = [
    "service", "services", "service charge", "maintenance", "repair",
    "engineering", "engineerin", "consulting", "consultancy", "consultant",
    "professional fee", "professional services", "retainer", "audit",
    "accounting", "bookkeeping", "legal services", "ca services",
    "software", "software license", "license fee", "saas",
    "cloud", "hosting", "server", "domain", "it support", "support fee",
    "vendor payment", "supplier payment", "contractor", "freelancer",
    "agency", "logistics", "courier", "procurement",
]

# ── Merchant / payment-gateway keywords (→ GST) ───────────────────────────────
MERCHANT_KEYWORDS = [
    "card pmt", "card payment", "credit card pmt", "debit card pmt",
    "point of sale", "cms_", "neft/gst", "rtgs/gst",
    "purchase", "vendor payment", "merchant",
    "swiggy", "zomato", "amazon", "flipkart", "myntra",
    "paytm", "phonepe", "gpay", "google pay",
    "razorpay", "payu", "ccavenue", "easebuzz", "billdesk",
]

# ── Category → header colour (openpyxl ARGB) ─────────────────────────────────
CATEGORY_COLOURS = {
    CATEGORY_TDS:          "FFFFC000",  # amber
    CATEGORY_GST:          "FF70AD47",  # green
    CATEGORY_POSSIBLE_GST: "FFA9D08E",  # light green
    CATEGORY_NORMAL:       "FF4472C4",  # blue
    "SUMMARY":             "FF7030A0",  # purple
}

# ── Paths ─────────────────────────────────────────────────────────────────────
DEFAULT_INPUT_PATH = "data/input/bank_statement.xlsx"
DEFAULT_OUTPUT_DIR = "data/output"
OUTPUT_FILENAMES = {
    CATEGORY_GST:          "gst_transactions.xlsx",
    CATEGORY_POSSIBLE_GST: "possible_gst_transactions.xlsx",
    CATEGORY_TDS:          "tds_transactions.xlsx",
    CATEGORY_NORMAL:       "normal_transactions.xlsx",
}
SUMMARY_FILENAME = "classification_summary.xlsx"
