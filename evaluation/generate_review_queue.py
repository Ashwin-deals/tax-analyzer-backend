import pandas as pd
from pathlib import Path

TRAINING_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "training_dataset.csv"
OUTPUT_PATH = Path(__file__).resolve().parent.parent / "data" / "output" / "smart_review_queue.csv"

def generate_review_queue():
    if not TRAINING_DATA_PATH.exists():
        print(f"❌ No dataset found at {TRAINING_DATA_PATH}")
        return

    df = pd.read_csv(TRAINING_DATA_PATH)
    
    # Exclude rows that are already manually corrected (they have an actual_category)
    unlabeled = df[df['actual_category'].isna() | (df['actual_category'] == "")]

    if unlabeled.empty:
        print("✅ All transactions have been manually reviewed. Nothing to do!")
        return

    # Filter logic:
    # We want to review:
    # 1. review_recommended == True
    # 2. confidence == MEDIUM
    # 3. predicted_category == POSSIBLE_GST
    # 4. classification_mode == HEURISTIC (we want to verify heuristics, not explicit matches or learned ones)
    
    mask = (
        (unlabeled['review_recommended'].astype(str).str.lower() == 'true') |
        (unlabeled['confidence'] == 'MEDIUM') |
        (unlabeled['predicted_category'] == 'POSSIBLE_GST')
    ) & (unlabeled['classification_mode'] == 'HEURISTIC')

    review_queue = unlabeled[mask].copy()

    # Deduplicate to avoid repetitive reviews of the exact same vendor heuristic
    review_queue = review_queue.drop_duplicates(subset=['vendor', 'predicted_category'])

    # Save queue
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    review_queue.to_csv(OUTPUT_PATH, index=False)
    
    print("=======================================================")
    print("  Smart Review Workflow (Human-in-the-Loop)")
    print("=======================================================\n")
    print(f"Original Unlabeled Dataset: {len(unlabeled)} transactions")
    print(f"Filtered Review Queue     : {len(review_queue)} unique vendor patterns")
    print(f"Effort Reduction          : {100 * (1 - len(review_queue)/len(unlabeled)):.1f}%\n")
    print(f"✅ Generated prioritized review queue at:")
    print(f"   {OUTPUT_PATH}")
    print("\nNext Step: Review these transactions, use the UI to submit corrections, and re-run.")

if __name__ == "__main__":
    generate_review_queue()
