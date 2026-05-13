import sys
from pathlib import Path
import pandas as pd

# Add src to path so we can import internal modules
sys.path.append(str(Path(__file__).resolve().parent.parent))

from src.loader import load_excel
from src.scorer import score_transaction
from utils.constants import CATEGORY_GST, CATEGORY_TDS, CATEGORY_NORMAL, CATEGORY_POSSIBLE_GST

GOLD_DATASET_PATH = Path(__file__).resolve().parent / "gold_dataset.xlsx"

def evaluate_gold_dataset():
    if not GOLD_DATASET_PATH.exists():
        print(f"❌ Gold dataset not found at {GOLD_DATASET_PATH}")
        return

    print("=======================================================")
    print("  Gold Dataset Evaluation Baseline")
    print("=======================================================\n")
    
    # Load and process
    df = load_excel(GOLD_DATASET_PATH)
    print(f"Loaded {len(df)} transactions from Gold Dataset.\n")
    
    score_results = df.apply(score_transaction, axis=1)
    
    df['predicted_category'] = [r.category for r in score_results]
    df['review_recommended'] = [r.needs_review for r in score_results]
    df['actual_category'] = df['Actual_Category']
    
    gst_compatible = [CATEGORY_GST, CATEGORY_POSSIBLE_GST]

    # Accuracy Tracking (Accept POSSIBLE_GST as valid for GST in legacy labels)
    correct_mask = (df['predicted_category'] == df['actual_category']) | (df['predicted_category'].isin(gst_compatible) & (df['actual_category'] == CATEGORY_GST))
    correct = correct_mask.sum()
    accuracy = correct / len(df)
    
    print("📊 ACCURACY TRACKING")
    print("-------------------------------------------------------")
    print(f"Overall Accuracy: {accuracy:.2%} ({correct}/{len(df)})\n")

    # Precision & Recall
    print("🎯 PRECISION & RECALL")
    print("-------------------------------------------------------")
    for target_cat in [CATEGORY_GST, CATEGORY_TDS]:
        if target_cat == CATEGORY_GST:
            # Treat POSSIBLE_GST as a valid GST-compatible class for legacy labels.
            true_pos = len(df[df['predicted_category'].isin(gst_compatible) & (df['actual_category'] == CATEGORY_GST)])
            false_pos = len(df[df['predicted_category'].isin(gst_compatible) & (df['actual_category'] != CATEGORY_GST)])
            false_neg = len(df[~df['predicted_category'].isin(gst_compatible) & (df['actual_category'] == CATEGORY_GST)])
            true_neg = len(df[~df['predicted_category'].isin(gst_compatible) & (df['actual_category'] != CATEGORY_GST)])
        else:
            true_pos = len(df[(df['predicted_category'] == target_cat) & (df['actual_category'] == target_cat)])
            false_pos = len(df[(df['predicted_category'] == target_cat) & (df['actual_category'] != target_cat)])
            false_neg = len(df[(df['predicted_category'] != target_cat) & (df['actual_category'] == target_cat)])
            true_neg = len(df[(df['predicted_category'] != target_cat) & (df['actual_category'] != target_cat)])
        
        precision = true_pos / (true_pos + false_pos) if (true_pos + false_pos) > 0 else 0
        recall = true_pos / (true_pos + false_neg) if (true_pos + false_neg) > 0 else 0
        fpr = false_pos / (false_pos + true_neg) if (false_pos + true_neg) > 0 else 0
        fnr = false_neg / (false_neg + true_pos) if (false_neg + true_pos) > 0 else 0
        
        print(f"{target_cat} Metrics:")
        print(f"  Precision: {precision:.2%} | Recall: {recall:.2%}")
        print(f"  FPR: {fpr:.2%} | FNR: {fnr:.2%}\n")

    # Confusion Summary
    print("🔍 CONFUSION SUMMARY (Misclassifications)")
    print("-------------------------------------------------------")
    misclassified = df[(df['predicted_category'] != df['actual_category']) & ~(df['predicted_category'].isin([CATEGORY_POSSIBLE_GST]) & (df['actual_category'] == CATEGORY_GST))]
    if not misclassified.empty:
        confusion = misclassified.groupby(['actual_category', 'predicted_category']).size().reset_index(name='count')
        for _, row in confusion.iterrows():
            print(f"  {row['actual_category']} → {row['predicted_category']}: {row['count']}")
    else:
        print("  No misclassifications in Gold Dataset.")
    print("\n")

    # Review Rate Statistics
    print("⚠️ REVIEW RATE STATISTICS")
    print("-------------------------------------------------------")
    reviews = df[df['review_recommended'] == True]
    print(f"Global Review Rate: {len(reviews) / len(df):.2%} ({len(reviews)}/{len(df)})")
    if not misclassified.empty:
        missed_reviews = misclassified[misclassified['review_recommended'] == False]
        print(f"Silent Failures (Misclassified but NOT flagged for review): {len(missed_reviews)}")
    print("\n")

    print("=======================================================")

if __name__ == "__main__":
    evaluate_gold_dataset()
