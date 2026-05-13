import os
import sys
import joblib
import pandas as pd
import numpy as np
from pathlib import Path

# Add root directory to sys.path
sys.path.append(str(Path(__file__).resolve().parent.parent))

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.calibration import CalibratedClassifierCV
from xgboost import XGBClassifier

from utils.constants import CATEGORY_NORMAL, CATEGORY_POSSIBLE_GST

TRAINING_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "training_dataset.csv"
MODEL_DIR = Path(__file__).resolve().parent.parent / "models"
MODEL_PATH = MODEL_DIR / "xgb_model.pkl"
VEC_PATH = MODEL_DIR / "tfidf_vectorizer.pkl"

def load_and_prepare_data():
    if not TRAINING_DATA_PATH.exists():
        print(f"❌ Training dataset not found at {TRAINING_DATA_PATH}")
        return None

    df = pd.read_csv(TRAINING_DATA_PATH)
    
    # Filter for rows that have been manually labeled
    df = df[df['actual_category'].notna() & (df['actual_category'] != "")]
    
    # Map 'GST' to 'POSSIBLE_GST' for training the ambiguous model
    df['actual_category'] = df['actual_category'].replace("GST", CATEGORY_POSSIBLE_GST)
    
    # Filter for Target Classes ONLY (POSSIBLE_GST vs NORMAL)
    target_classes = [CATEGORY_NORMAL, CATEGORY_POSSIBLE_GST]
    df = df[df['actual_category'].isin(target_classes)].copy()
    
    if len(df) < 10:
        print("⚠️ Not enough labelled target classes to train ML model (need >= 10).")
        return None
        
    return df

def extract_features(df, vectorizer=None, is_training=True):
    # Text features
    texts = df['normalized_text'].fillna(df['narration']).fillna("")
    if is_training:
        vectorizer = TfidfVectorizer(max_features=300, ngram_range=(1,2), token_pattern=r'(?u)\b\w+\b')
        X_text = vectorizer.fit_transform(texts).toarray()
    else:
        X_text = vectorizer.transform(texts).toarray()
        
    # Numerical features
    X_num = pd.DataFrame()
    X_num['gst_score'] = df['gst_score'].fillna(0)
    X_num['tds_score'] = df['tds_score'].fillna(0)
    X_num['amount'] = np.log1p(df['debit'].fillna(0) + df['credit'].fillna(0))
    
    # Combine
    X = np.hstack((X_text, X_num.values))
    return X, vectorizer

def train_model():
    print("=======================================================")
    print("  Hybrid ML Layer — Training Pipeline")
    print("=======================================================\n")
    
    df = load_and_prepare_data()
    if df is None:
        return
        
    print(f"Loaded {len(df)} labelled rows for training (NORMAL vs POSSIBLE_GST).")
    
    X, vectorizer = extract_features(df, is_training=True)
    
    # Map classes: NORMAL -> 0, POSSIBLE_GST -> 1
    y = (df['actual_category'] == CATEGORY_POSSIBLE_GST).astype(int).values
    
    # Train XGBoost with Probability Calibration
    print("Training XGBoost Classifier with Isotonic Calibration...")
    base_model = XGBClassifier(
        n_estimators=50,
        max_depth=4,
        learning_rate=0.1,
        eval_metric='logloss'
    )
    model = CalibratedClassifierCV(base_model, method='isotonic', cv=3)
    model.fit(X, y)
    
    # Save artifacts
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, MODEL_PATH)
    joblib.dump(vectorizer, VEC_PATH)
    
    print(f"✅ Model saved to {MODEL_PATH}")
    print(f"✅ Vectorizer saved to {VEC_PATH}\n")
    
    # Simple Train Accuracy
    preds = model.predict(X)
    acc = (preds == y).mean()
    print(f"Training Accuracy (Ambiguous Classes): {acc:.2%}")
    print("=======================================================")
    
if __name__ == "__main__":
    train_model()
