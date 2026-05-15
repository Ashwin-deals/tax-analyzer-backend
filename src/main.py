"""
src/main.py
───────────
Entry point for the Bank Statement GST & TDS Classifier.

Usage
─────
    # From the project root:
    python -m src.main                                  # uses default path
    python -m src.main data/input/my_statement.xlsx    # custom path
    python -m src.main data/input/my_statement.csv
    python -m src.main data/input/my_statement.pdf
    python -m src.main --help
"""

import argparse
import logging
import sys
from pathlib import Path

# ── Ensure the project root is on sys.path when run directly ─────────────────
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from src.exporter import export_data
from src.email_fetcher import cleanup_downloaded_statements
from src.loader import load_statement
from src.processor import process_transactions
from utils.constants import DEFAULT_INPUT_PATH, DEFAULT_OUTPUT_DIR


# ── Logging setup ─────────────────────────────────────────────────────────────

def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)-8s] %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )


# ── CLI ───────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="tax-analyzer",
        description="Interpret bank statement transactions with flow behavior and tax category.",
    )
    parser.add_argument(
        "input_file",
        nargs="?",
        default=DEFAULT_INPUT_PATH,
        help=f"Path to the bank statement file: xlsx, xls, csv, or pdf (default: {DEFAULT_INPUT_PATH})",
    )
    parser.add_argument(
        "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        help=f"Directory where output files are written (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--no-summary",
        action="store_true",
        help="Skip generating classification_summary.xlsx",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable debug-level logging",
    )
    return parser.parse_args()


# ── Main pipeline ─────────────────────────────────────────────────────────────

def main() -> None:
    args = _parse_args()
    _configure_logging(args.verbose)
    logger = logging.getLogger(__name__)

    print("=" * 55)
    print("  Bank Statement GST & TDS Classifier")
    print("=" * 55)

    # ── Resolve paths relative to project root (works from any CWD) ──────────
    input_path  = _resolve_path(args.input_file)
    output_path = _resolve_path(args.output_dir)

    try:
        # ── Step 1: Load ──────────────────────────────────────────────────────────
        logger.info("Step 1/3 — Loading data from: %s", input_path)
        df = load_statement(input_path)
        print(f"\n📂 Loaded {len(df):,} rows from '{input_path.name}'")

        # ── Step 2: Process / Classify ────────────────────────────────────────────
        logger.info("Step 2/3 — Classifying transactions …")
        result = process_transactions(df)

        gst_count          = len(result.get("GST", []))
        possible_gst_count = len(result.get("POSSIBLE_GST", []))
        tds_count          = len(result.get("TDS", []))
        normal_count       = len(result.get("NORMAL", []))

        # Calculate review needs across all categories
        total_review = sum(df["REVIEW_RECOMMENDED"].sum() for df in result.values() if not df.empty)

        print(f"\n🔍 Classification results:")
        print(f"   • GST          : {gst_count:>5,} transactions")
        print(f"   • POSSIBLE_GST : {possible_gst_count:>5,} transactions")
        print(f"   • TDS          : {tds_count:>5,} transactions")
        print(f"   • NORMAL       : {normal_count:>5,} transactions")
        print(f"   {'─' * 30}")
        print(f"   • TOTAL        : {gst_count + possible_gst_count + tds_count + normal_count:>5,} transactions")
        print(f"\n🚩 Review required for {int(total_review):,} transactions.")

        # ── Step 3: Export ────────────────────────────────────────────────────────
        logger.info("Step 3/3 — Exporting results to: %s", output_path)
        export_data(
            result,
            output_folder=output_path,
            include_summary=not args.no_summary,
        )
    finally:
        _cleanup_email_statement_input(input_path)


def _resolve_path(p: str) -> Path:
    """
    If *p* is a relative path, resolve it against the project root
    (parent of src/) so the script works from any working directory.
    """
    path = Path(p)
    if path.is_absolute():
        return path
    return (_project_root / path).resolve()


def _cleanup_email_statement_input(input_path: Path) -> None:
    email_dir = (_project_root / "data" / "email_statements").resolve()
    try:
        input_path.resolve().relative_to(email_dir)
    except ValueError:
        return
    cleanup_downloaded_statements((input_path,))


if __name__ == "__main__":
    main()
