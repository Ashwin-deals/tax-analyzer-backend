"""
scripts/generate_evaluation_sample.py
──────────────────────────────────────
Picks a stratified sample from the existing bank statement, pre-populates
a Predicted_Suggestion column (so you can validate it), and adds a blank
Actual_Category column ready for manual labelling.

Run from project root:
    python3 scripts/generate_evaluation_sample.py
    python3 scripts/generate_evaluation_sample.py --size 100
    python3 scripts/generate_evaluation_sample.py --size 0   # all rows
"""

import argparse
from pathlib import Path
import sys

_project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_project_root))

import pandas as pd
from src.loader import load_statement
from src.scorer import score_transaction
from utils.constants import INTERNAL_COLS, TAX_CATEGORY_ORDER

DEFAULT_INPUT  = "data/input/bank_statement.xlsx"
DEFAULT_OUTPUT = "data/input/evaluation_sample.xlsx"
DEFAULT_SIZE   = 60


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a stratified evaluation sample for manual labelling."
    )
    parser.add_argument("--input",  default=DEFAULT_INPUT)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--size", type=int, default=DEFAULT_SIZE,
        help=f"Rows to sample (default {DEFAULT_SIZE}). Use 0 for all rows.",
    )
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    input_path  = (_project_root / args.input).resolve()
    output_path = (_project_root / args.output).resolve()

    print(f"Loading: {input_path}")
    df = load_statement(input_path)

    # Run classifier to add suggestion column
    score_results = df.apply(score_transaction, axis=1)
    df["Predicted_Suggestion"] = [r.category for r in score_results]

    # Drop internal alias columns
    df.drop(columns=[c for c in INTERNAL_COLS if c in df.columns], inplace=True)

    # ── Stratified sampling ───────────────────────────────────────────────────
    if args.size > 0 and args.size < len(df):
        parts = []
        per_cat = max(1, args.size // 4)

        for cat in TAX_CATEGORY_ORDER:
            cat_df = df[df["Predicted_Suggestion"] == cat]
            n = min(per_cat, len(cat_df))
            if n > 0:
                parts.append(cat_df.sample(n=n, random_state=args.seed))

        result = pd.concat(parts).sample(frac=1, random_state=args.seed).reset_index(drop=True)

        # Top up to reach target size if needed
        if len(result) < args.size:
            remaining = df[~df.index.isin(result.index)]
            top_up    = min(args.size - len(result), len(remaining))
            if top_up > 0:
                result = pd.concat([
                    result,
                    remaining.sample(n=top_up, random_state=args.seed),
                ]).reset_index(drop=True)
    else:
        result = df.reset_index(drop=True)

    # Add blank Actual_Category as first column for easy Excel filling
    result.insert(0, "Actual_Category", "")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    result.to_excel(str(output_path), index=False)

    dist = result["Predicted_Suggestion"].value_counts().to_dict()
    print(f"\n✅ Evaluation sample written → {output_path}")
    print(f"   Rows: {len(result)}")
    print(f"   Predicted distribution: {dist}")
    print(f"\n⚠️  Next steps:")
    print(f"   1. Open '{output_path.name}' in Excel")
    print(f"   2. Fill the 'Actual_Category' column for each row (GST / POSSIBLE_GST / TDS / NORMAL)")
    print(f"   3. Save and run: python3 src/evaluator.py")


if __name__ == "__main__":
    main()
