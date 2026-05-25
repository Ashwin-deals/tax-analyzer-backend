from __future__ import annotations

import logging
import re
import uuid
import hashlib
import hmac
import secrets
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from app.core.config import settings
from utils.classification_rules import apply_stored_classification_guard, normalize_category
from utils.constants import CATEGORY_GST, CATEGORY_NORMAL, CATEGORY_POSSIBLE_GST, CATEGORY_TDS

try:
    from pymongo import ASCENDING, DESCENDING, MongoClient
    from pymongo.errors import DuplicateKeyError, PyMongoError
except ImportError:  # pragma: no cover - exercised in environments without pymongo installed.
    ASCENDING = DESCENDING = MongoClient = None
    DuplicateKeyError = PyMongoError = Exception

try:
    import certifi
except ImportError:  # pragma: no cover - certifi is declared in requirements.
    certifi = None


logger = logging.getLogger(__name__)

COLLECTIONS = (
    "users",
    "businesses",
    "email_settings",
    "statement_uploads",
    "transactions",
    "review_items",
    "corrections",
)

DEFAULT_USER_EMAIL = "demo@finscan.local"
DEFAULT_USERNAME = "demo"
DEFAULT_BUSINESS_NAME = "Farm2Bag"
PASSWORD_HASH_ALGORITHM = "pbkdf2_sha256"
PASSWORD_HASH_ITERATIONS = 210_000
REQUIRED_DATABASE_NAME = "taxAnalyzer"
MONTH_LABELS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


class AuthError(Exception):
    """Raised when authentication or registration cannot be completed."""


class RepositoryError(Exception):
    """Raised when MongoDB records cannot be safely changed."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def normalize_username(username: str) -> str:
    return re.sub(r"[^a-z0-9_.-]", "", (username or "").strip().lower())


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        PASSWORD_HASH_ITERATIONS,
    ).hex()
    return f"{PASSWORD_HASH_ALGORITHM}${PASSWORD_HASH_ITERATIONS}${salt}${digest}"


def verify_password(password: str, password_hash: str | None) -> bool:
    if not password_hash:
        return False
    try:
        algorithm, iterations_text, salt, expected = password_hash.split("$", 3)
        iterations = int(iterations_text)
    except ValueError:
        return False
    if algorithm != PASSWORD_HASH_ALGORITHM:
        return False
    actual = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        iterations,
    ).hex()
    return hmac.compare_digest(actual, expected)


def _normalise_transaction_doc(row: dict[str, Any]) -> dict[str, Any]:
    return apply_stored_classification_guard(row, statement_id=row.get("statement_id"), final_stage=True)


def _transaction_category(row: dict[str, Any]) -> str:
    return normalize_category(row.get("tax_category") or row.get("classification") or row.get("category"))


def _exact_tax_category(row: dict[str, Any]) -> str | None:
    value = row.get("tax_category")
    if value is None:
        return None
    category = str(value).strip().upper()
    if category in {CATEGORY_GST, CATEGORY_POSSIBLE_GST, CATEGORY_TDS, CATEGORY_NORMAL}:
        return category
    return None


def _transaction_review_recommended(row: dict[str, Any]) -> bool:
    value = row.get("review_recommended")
    if isinstance(value, bool):
        return value
    if value is not None:
        text = str(value).strip().lower()
        if text in {"true", "yes", "y", "1", "review", "review_needed", "review needed"}:
            return True
        if text in {"false", "no", "n", "0", "cleared", "none"}:
            return False
    return str(row.get("review_status") or "").strip().lower() in {"pending", "review", "review_needed", "review needed"}


def _exact_review_recommended(row: dict[str, Any]) -> bool:
    value = row.get("review_recommended")
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in {"true", "yes", "y", "1"}


def _has_transaction_narration(row: dict[str, Any]) -> bool:
    return bool(
        str(
            row.get("narration")
            or row.get("description")
            or row.get("remarks")
            or row.get("transaction_remarks")
            or row.get("raw_row_text")
            or row.get("raw_extracted_row")
            or ""
        ).strip()
    )


def _mongo_uri_without_database(uri: str) -> str:
    parsed = urlsplit(uri)
    if not parsed.scheme.startswith("mongodb"):
        return uri
    return urlunsplit((parsed.scheme, parsed.netloc, "/", parsed.query, parsed.fragment))


def _s3_url(bucket: str | None, region: str | None, key: str | None) -> str | None:
    if not bucket or not key:
        return None
    region = region or settings.aws_region
    encoded_key = quote(str(key), safe="/")
    if region and region != "us-east-1":
        return f"https://{bucket}.s3.{region}.amazonaws.com/{encoded_key}"
    return f"https://{bucket}.s3.amazonaws.com/{encoded_key}"


def _clean_doc(doc: dict[str, Any] | None) -> dict[str, Any] | None:
    if not doc:
        return None
    cleaned = dict(doc)
    cleaned.pop("_id", None)
    return cleaned


def _date_key(value: Any) -> str:
    if value is None:
        return "Unknown"
    text = str(value).strip()
    if not text or text.lower() in {"nan", "nat", "none"}:
        return "Unknown"

    if isinstance(value, (int, float)) or re.fullmatch(r"\d+(?:\.\d+)?", text):
        try:
            serial = float(value)
            if 20_000 <= serial <= 80_000:
                return (datetime(1899, 12, 30) + timedelta(days=serial)).date().isoformat()
        except (TypeError, ValueError, OverflowError):
            pass

    normalized = re.sub(r"\s+", " ", text.replace(",", " ")).strip()
    iso_candidate = normalized.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(iso_candidate).date().isoformat()
    except ValueError:
        pass

    date_fragment = re.search(
        r"\b\d{1,4}[-/.]\d{1,2}[-/.]\d{2,4}\b|\b\d{1,2}[- ](?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*[- ]\d{2,4}\b",
        normalized,
        re.IGNORECASE,
    )
    candidates = [normalized]
    if date_fragment:
        candidates.insert(0, re.sub(r"sept", "Sep", date_fragment.group(0), flags=re.IGNORECASE))

    formats = (
        "%d-%m-%Y",
        "%d/%m/%Y",
        "%d.%m.%Y",
        "%d-%m-%y",
        "%d/%m/%y",
        "%d.%m.%y",
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%Y.%m.%d",
        "%m/%d/%Y",
        "%m-%d-%Y",
        "%d %b %Y",
        "%d %B %Y",
        "%d-%b-%Y",
        "%d-%B-%Y",
        "%d %b %y",
        "%d %B %y",
        "%d-%b-%y",
        "%d-%B-%y",
    )
    for candidate in candidates:
        for fmt in formats:
            try:
                return datetime.strptime(candidate[:24], fmt).date().isoformat()
            except ValueError:
                continue

    for fmt in ("%Y-%m-%d %H:%M:%S", "%d-%m-%Y %H:%M:%S", "%d/%m/%Y %H:%M:%S"):
        try:
            return datetime.strptime(normalized[:24], fmt).date().isoformat()
        except ValueError:
            continue
    return normalized[:10]


def _date_label(value) -> str:
    return f"{value.day} {MONTH_LABELS[value.month - 1]} {value.year}"


def _period_name(period_key: str, period: str) -> str:
    try:
        if period == "week":
            match = re.fullmatch(r"(\d{4})-W(\d{2})", period_key)
            if not match:
                return "Unknown"
            start = datetime.fromisocalendar(int(match.group(1)), int(match.group(2)), 1).date()
            end = start + timedelta(days=6)
            if start.year == end.year:
                return f"{start.day} {MONTH_LABELS[start.month - 1]} - {_date_label(end)}"
            return f"{_date_label(start)} - {_date_label(end)}"
        parsed = datetime.strptime(period_key, "%Y-%m").date()
    except ValueError:
        return "Unknown"
    return f"{MONTH_LABELS[parsed.month - 1]} {parsed.year}"


def _period_key(date_text: str, period: str) -> str:
    try:
        parsed = datetime.fromisoformat(date_text).date()
    except ValueError:
        return "Unknown"
    if period == "week":
        year, week, _ = parsed.isocalendar()
        return f"{year}-W{week:02d}"
    return f"{parsed.year}-{parsed.month:02d}"


def _party_name(narration: Any) -> str:
    text = re.sub(r"\s+", " ", str(narration or "")).strip()
    if not text:
        return "Unknown"
    parts = [part.strip() for part in re.split(r"[/|:-]", text) if part.strip()]
    stop_words = {
        "upi",
        "neft",
        "rtgs",
        "imps",
        "cr",
        "dr",
        "payment",
        "transfer",
        "txn",
        "transaction",
    }
    for part in parts:
        normalized = re.sub(r"[^a-zA-Z0-9 &.]", "", part).strip()
        if len(normalized) >= 3 and normalized.lower() not in stop_words and not normalized.isdigit():
            return normalized[:80].title()
    return text[:80].title()


class DataRepository:
    def __init__(self) -> None:
        self._client = None
        self._db = None
        self._available = False
        self._memory: dict[str, dict[str, dict[str, Any]]] = {name: {} for name in COLLECTIONS}

        if settings.mongodb_database != REQUIRED_DATABASE_NAME:
            raise RuntimeError(f"FinScan MongoDB database must be '{REQUIRED_DATABASE_NAME}'")
        if not settings.mongodb_uri:
            logger.info("MONGODB_URI is not configured; using in-memory data store for this process.")
            return
        if MongoClient is None:
            logger.warning("pymongo is not installed; using in-memory data store until dependencies are installed.")
            return

        try:
            sanitized_uri = _mongo_uri_without_database(settings.mongodb_uri)
            client_options = {"serverSelectionTimeoutMS": 1500}
            if certifi is not None:
                client_options["tlsCAFile"] = certifi.where()
            self._client = MongoClient(sanitized_uri, **client_options)
            self._client.admin.command("ping")
            self._db = self._client[REQUIRED_DATABASE_NAME]
            self._available = True
            logger.info("MongoDB connected successfully.")
            logger.info("MongoDB database selected: %s", REQUIRED_DATABASE_NAME)
            self.initialize_collections()
        except Exception as exc:  # pragma: no cover - depends on external MongoDB availability.
            logger.warning("MongoDB unavailable; using in-memory data store: %s", exc)
            self._client = None
            self._db = None
            self._available = False

    @property
    def is_available(self) -> bool:
        return self._available

    def initialize_collections(self) -> None:
        if not self._available or self._db is None:
            logger.info("MongoDB collections initialized in in-memory store: %s", ", ".join(COLLECTIONS))
            return
        try:
            existing = set(self._db.list_collection_names())
            created: list[str] = []
            for collection in COLLECTIONS:
                if collection not in existing:
                    self._db.create_collection(collection)
                    created.append(collection)
            self.ensure_indexes()
            logger.info(
                "MongoDB collections initialized for %s: %s",
                REQUIRED_DATABASE_NAME,
                ", ".join(COLLECTIONS),
            )
            if created:
                logger.info("MongoDB collections created: %s", ", ".join(created))
        except PyMongoError as exc:
            logger.warning("Could not initialize MongoDB collections: %s", exc)

    def ensure_indexes(self) -> None:
        if not self._available or self._db is None:
            return
        try:
            self._db.users.create_index([("email", ASCENDING)], unique=True)
            self._db.users.create_index([("username", ASCENDING)], unique=True, sparse=True)
            self._db.businesses.create_index([("user_id", ASCENDING), ("name", ASCENDING)])
            self._db.email_settings.create_index([("user_id", ASCENDING)], unique=True)
            self._db.email_settings.create_index([("auto_fetch_enabled", ASCENDING), ("fetch_frequency", ASCENDING)])
            self._db.statement_uploads.create_index([("user_id", ASCENDING), ("uploaded_at", DESCENDING)])
            self._db.statement_uploads.create_index([("business_id", ASCENDING), ("uploaded_at", DESCENDING)])
            self._db.transactions.create_index([("user_id", ASCENDING), ("statement_id", ASCENDING)])
            self._db.transactions.create_index([("business_id", ASCENDING), ("statement_id", ASCENDING)])
            self._db.review_items.create_index([("business_id", ASCENDING), ("status", ASCENDING)])
            self._db.corrections.create_index([("business_id", ASCENDING), ("created_at", DESCENDING)])
        except PyMongoError as exc:
            logger.warning("Could not ensure MongoDB indexes: %s", exc)

    def register_user(
        self,
        name: str,
        username: str,
        email: str,
        password: str,
        business_name: str,
    ) -> dict[str, Any]:
        normalized_email = normalize_email(email)
        normalized_username = normalize_username(username)
        display_name = " ".join((name or "").split())
        business_display_name = " ".join((business_name or "").split())

        if not display_name:
            raise AuthError("Name is required")
        if not normalized_username or len(normalized_username) < 3:
            raise AuthError("Username must be at least 3 characters")
        if not normalized_email or "@" not in normalized_email:
            raise AuthError("A valid email address is required")
        if not password or len(password) < 8:
            raise AuthError("Password must be at least 8 characters")
        if not business_display_name:
            raise AuthError("Business name is required")
        if self.find_user_by_identifier(normalized_email):
            raise AuthError("Email is already registered")
        if self.find_user_by_identifier(normalized_username):
            raise AuthError("Username is already registered")

        user = {
            "user_id": uuid.uuid4().hex,
            "email": normalized_email,
            "username": normalized_username,
            "name": display_name,
            "password_hash": hash_password(password),
            "auth_provider": "password",
            "created_at": utc_now(),
            "updated_at": utc_now(),
        }
        try:
            self._insert_one("users", user, key="user_id")
        except AuthError:
            raise

        business = self.create_business(
            user_id=user["user_id"],
            name=business_display_name,
            industry="Business",
        )
        return self._auth_response(user, business, auth_mode="password")

    def login(self, identifier: str, password: str | None = None) -> dict[str, Any]:
        user = self.find_user_by_identifier(identifier)
        if not user or not verify_password(password or "", user.get("password_hash")):
            raise AuthError("Invalid username/email or password")
        businesses = self._find_many("businesses", {"user_id": user["user_id"]})
        business = businesses[0] if businesses else self.create_business(user["user_id"], DEFAULT_BUSINESS_NAME)
        return self._auth_response(user, business, auth_mode="password")

    def _auth_response(self, user: dict[str, Any], business: dict[str, Any], auth_mode: str) -> dict[str, Any]:
        return {
            "token": f"session-{user['user_id']}",
            "authMode": auth_mode,
            "session": {
                "type": "placeholder",
                "userId": user["user_id"],
                "createdAt": utc_now(),
            },
            "user": self.public_user(user),
            "business": self.public_business(business),
        }

    def ensure_default_profile(self) -> dict[str, Any]:
        existing = self._find_one("users", {"email": DEFAULT_USER_EMAIL})
        if existing:
            user = existing
        else:
            system_username = DEFAULT_USERNAME
            while self._find_one("users", {"username": system_username}):
                system_username = f"{DEFAULT_USERNAME}-{uuid.uuid4().hex[:4]}"
            user = {
                "user_id": uuid.uuid4().hex,
                "email": DEFAULT_USER_EMAIL,
                "username": system_username,
                "name": "Demo User",
                "password_hash": None,
                "auth_provider": "system",
                "login_disabled": True,
                "created_at": utc_now(),
                "updated_at": utc_now(),
            }
            self._insert_one("users", user, key="user_id")
        business = self.ensure_business_for_user(user["user_id"], DEFAULT_BUSINESS_NAME)
        return {
            "token": None,
            "authMode": "system",
            "user": self.public_user(user),
            "business": self.public_business(business),
        }

    def find_user_by_identifier(self, identifier: str) -> dict[str, Any] | None:
        raw_identifier = (identifier or "").strip()
        if not raw_identifier:
            return None
        normalized_email = normalize_email(raw_identifier)
        normalized_username = normalize_username(raw_identifier)
        if "@" in raw_identifier:
            return self._find_one("users", {"email": normalized_email})
        return self._find_one("users", {"username": normalized_username})

    def upsert_user(self, email: str, name: str | None = None) -> dict[str, Any]:
        normalized_email = normalize_email(email or DEFAULT_USER_EMAIL)
        existing = self._find_one("users", {"email": normalized_email})
        if existing:
            return existing
        normalized_username = normalize_username(normalized_email.split("@")[0]) or uuid.uuid4().hex[:8]
        while self._find_one("users", {"username": normalized_username}):
            normalized_username = f"{normalized_username}-{uuid.uuid4().hex[:4]}"
        doc = {
            "user_id": uuid.uuid4().hex,
            "email": normalized_email,
            "username": normalized_username,
            "name": name or normalized_email.split("@")[0].replace(".", " ").title(),
            "password_hash": None,
            "auth_provider": "system",
            "login_disabled": True,
            "created_at": utc_now(),
            "updated_at": utc_now(),
        }
        self._insert_one("users", doc, key="user_id")
        return doc

    def ensure_business_for_user(self, user_id: str, name: str = DEFAULT_BUSINESS_NAME) -> dict[str, Any]:
        existing = self._find_one("businesses", {"user_id": user_id, "name": name})
        if existing:
            return existing
        return self.create_business(user_id=user_id, name=name)

    def create_business(self, user_id: str, name: str, industry: str | None = None) -> dict[str, Any]:
        doc = {
            "business_id": uuid.uuid4().hex,
            "user_id": user_id,
            "name": (name or DEFAULT_BUSINESS_NAME).strip(),
            "industry": industry or "Food and agriculture",
            "created_at": utc_now(),
            "updated_at": utc_now(),
        }
        self._insert_one("businesses", doc, key="business_id")
        return doc

    def list_businesses(self, user_id: str) -> list[dict[str, Any]]:
        return [self.public_business(doc) for doc in self._find_many("businesses", {"user_id": user_id})]

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        return _clean_doc(self._find_one("users", {"user_id": user_id}))

    def get_business(self, business_id: str) -> dict[str, Any] | None:
        doc = self._find_one("businesses", {"business_id": business_id})
        return self.public_business(doc) if doc else None

    def get_email_settings_record(self, user_id: str) -> dict[str, Any] | None:
        return self._find_one("email_settings", {"user_id": user_id})

    def get_email_settings(self, user_id: str) -> dict[str, Any]:
        return self.public_email_settings(self.get_email_settings_record(user_id))

    def upsert_email_settings(self, user_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        existing = self.get_email_settings_record(user_id)
        now = utc_now()
        payload = {
            "user_id": user_id,
            "updated_at": now,
            **updates,
        }
        if existing:
            self._update_one("email_settings", {"user_id": user_id}, {"$set": payload})
        else:
            payload = {
                "email_settings_id": uuid.uuid4().hex,
                "created_at": now,
                "last_fetch_at": None,
                "last_fetch_status": None,
                "last_fetch_error": None,
                **payload,
            }
            self._insert_one("email_settings", payload, key="email_settings_id")
        return self.get_email_settings_record(user_id) or payload

    def update_email_fetch_status(self, user_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        existing = self.get_email_settings_record(user_id)
        if not existing:
            return None
        self._update_one(
            "email_settings",
            {"user_id": user_id},
            {
                "$set": {
                    "updated_at": utc_now(),
                    **updates,
                }
            },
        )
        return self.get_email_settings_record(user_id)

    def list_enabled_email_settings(self) -> list[dict[str, Any]]:
        return self._find_many("email_settings", {"auto_fetch_enabled": True})

    def create_statement_upload(self, metadata: dict[str, Any]) -> dict[str, Any]:
        s3_bucket = metadata.get("s3Bucket") or metadata.get("s3_bucket") or metadata.get("originalS3Bucket") or metadata.get("original_s3_bucket")
        s3_key = metadata.get("s3Key") or metadata.get("s3_key") or metadata.get("originalS3Key") or metadata.get("original_s3_key")
        s3_region = metadata.get("s3Region") or metadata.get("s3_region") or metadata.get("originalS3Region") or metadata.get("original_s3_region")
        s3_url = metadata.get("s3Url") or metadata.get("s3_url") or _s3_url(s3_bucket, s3_region, s3_key)
        doc = {
            "statement_id": metadata["statementId"],
            "business_id": metadata["businessId"],
            "user_id": metadata.get("userId"),
            "username": metadata.get("username"),
            "business_name": metadata.get("businessName"),
            "filename": metadata["filename"],
            "original_filename": metadata.get("originalFilename") or metadata["filename"],
            "uploaded_at": metadata["uploadedAt"],
            "processing_status": metadata.get("status", "uploaded"),
            "original_s3_key": s3_key,
            "original_s3_bucket": s3_bucket,
            "original_s3_region": s3_region,
            "s3_key": s3_key,
            "s3_bucket": s3_bucket,
            "s3_url": s3_url,
            "storage_provider": metadata.get("storageProvider") or metadata.get("storage_provider") or "s3",
            "source": metadata.get("source", "manual"),
            "source_metadata": metadata.get("sourceMetadata") or metadata.get("source_metadata") or {},
            "is_password_protected": bool(metadata.get("isPasswordProtected") or metadata.get("is_password_protected")),
            "unlock_status": metadata.get("unlockStatus") or metadata.get("unlock_status") or "unknown",
            "encrypted_statement_password": metadata.get("encryptedStatementPassword") or metadata.get("encrypted_statement_password"),
            "generated_report_keys": metadata.get("generatedReportKeys") or metadata.get("generated_report_keys") or [],
            "total_transactions": 0,
            "total_credits": 0.0,
            "total_debits": 0.0,
            "created_at": utc_now(),
            "updated_at": utc_now(),
        }
        self._insert_one("statement_uploads", doc, key="statement_id")
        return self.public_statement(doc)

    def update_statement_upload(self, statement_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        db_updates = {
            "updated_at": utc_now(),
            **updates,
        }
        self._update_one("statement_uploads", {"statement_id": statement_id}, {"$set": db_updates})
        doc = self._find_one("statement_uploads", {"statement_id": statement_id})
        return self.public_statement(doc) if doc else None

    def get_statement_upload(self, statement_id: str) -> dict[str, Any] | None:
        doc = self._find_one("statement_uploads", {"statement_id": statement_id})
        return self.public_statement(doc) if doc else None

    def get_statement_upload_record(self, statement_id: str) -> dict[str, Any] | None:
        return self._find_one("statement_uploads", {"statement_id": statement_id})

    def list_statement_uploads(self, business_id: str, user_id: str | None = None) -> list[dict[str, Any]]:
        query = {"business_id": business_id}
        if user_id:
            query["user_id"] = user_id
        docs = self._find_many("statement_uploads", query)
        docs.sort(key=lambda item: item.get("uploaded_at", ""), reverse=True)
        return [self.public_statement(doc) for doc in docs]

    def list_user_statement_records(self, user_id: str) -> list[dict[str, Any]]:
        business_ids = [
            doc.get("business_id")
            for doc in self._find_many("businesses", {"user_id": user_id})
            if doc.get("business_id")
        ]
        by_statement_id: dict[str, dict[str, Any]] = {}
        for doc in self._find_many("statement_uploads", {"user_id": user_id}):
            by_statement_id[doc.get("statement_id") or uuid.uuid4().hex] = doc
        for business_id in business_ids:
            for doc in self._find_many("statement_uploads", {"business_id": business_id}):
                by_statement_id[doc.get("statement_id") or uuid.uuid4().hex] = doc
        docs = list(by_statement_id.values())
        docs.sort(key=lambda item: item.get("uploaded_at", ""), reverse=True)
        return docs

    def delete_statement_records(self, statement_id: str, user_id: str | None = None) -> dict[str, int]:
        statement = self._find_one("statement_uploads", {"statement_id": statement_id})
        if not statement:
            raise RepositoryError("Statement not found")
        if user_id and statement.get("user_id") != user_id:
            raise RepositoryError("Statement does not belong to this user")

        transaction_ids = [
            doc.get("transaction_id")
            for doc in self._find_many("transactions", {"statement_id": statement_id})
            if doc.get("transaction_id")
        ]

        if self._available and self._db is not None:
            try:
                owner_query = {"statement_id": statement_id}
                if user_id:
                    owner_query["user_id"] = user_id
                linked_query = [{"statement_id": statement_id}]
                if transaction_ids:
                    linked_query.append({"transaction_id": {"$in": transaction_ids}})
                correction_query = {"$or": linked_query}
                review_query = {"$or": linked_query}

                transaction_query = {"statement_id": statement_id}
                deleted = {
                    "corrections": int(self._db.corrections.delete_many(correction_query).deleted_count),
                    "review_items": int(self._db.review_items.delete_many(review_query).deleted_count),
                    "transactions": int(self._db.transactions.delete_many(transaction_query).deleted_count),
                    "statement_uploads": int(self._db.statement_uploads.delete_one(owner_query).deleted_count),
                }
                if deleted["statement_uploads"] != 1:
                    raise RepositoryError("Statement metadata could not be deleted")
                return deleted
            except PyMongoError as exc:
                raise RepositoryError(f"Could not delete statement records: {exc}") from exc

        deleted = {
            "corrections": self._delete_by_predicate(
                "corrections",
                lambda doc: doc.get("statement_id") == statement_id or doc.get("transaction_id") in transaction_ids,
            ),
            "review_items": self._delete_by_predicate(
                "review_items",
                lambda doc: doc.get("statement_id") == statement_id or doc.get("transaction_id") in transaction_ids,
            ),
            "transactions": self._delete_by_predicate(
                "transactions",
                lambda doc: doc.get("statement_id") == statement_id,
            ),
            "statement_uploads": self._delete_by_predicate(
                "statement_uploads",
                lambda doc: doc.get("statement_id") == statement_id and (not user_id or doc.get("user_id") == user_id),
            ),
        }
        if deleted["statement_uploads"] != 1:
            raise RepositoryError("Statement metadata could not be deleted")
        return deleted

    def delete_user_account_records(self, user_id: str) -> dict[str, int]:
        user = self._find_one("users", {"user_id": user_id})
        if not user:
            raise RepositoryError("User not found")

        business_ids = {
            doc.get("business_id")
            for doc in self._find_many("businesses", {"user_id": user_id})
            if doc.get("business_id")
        }
        statement_ids = {doc.get("statement_id") for doc in self.list_user_statement_records(user_id) if doc.get("statement_id")}

        transaction_docs: dict[str, dict[str, Any]] = {}
        for doc in self._find_many("transactions", {"user_id": user_id}):
            transaction_docs[doc.get("transaction_id") or uuid.uuid4().hex] = doc
        for statement_id in statement_ids:
            for doc in self._find_many("transactions", {"statement_id": statement_id}):
                transaction_docs[doc.get("transaction_id") or uuid.uuid4().hex] = doc
        for business_id in business_ids:
            for doc in self._find_many("transactions", {"business_id": business_id}):
                transaction_docs[doc.get("transaction_id") or uuid.uuid4().hex] = doc

        transaction_ids = {
            doc.get("transaction_id")
            for doc in transaction_docs.values()
            if doc.get("transaction_id")
        }

        if self._available and self._db is not None:
            try:
                linked_clauses: list[dict[str, Any]] = [{"user_id": user_id}]
                if business_ids:
                    linked_clauses.append({"business_id": {"$in": list(business_ids)}})
                if statement_ids:
                    linked_clauses.append({"statement_id": {"$in": list(statement_ids)}})
                if transaction_ids:
                    linked_clauses.append({"transaction_id": {"$in": list(transaction_ids)}})
                linked_query = {"$or": linked_clauses}

                statement_clauses: list[dict[str, Any]] = [{"user_id": user_id}]
                if business_ids:
                    statement_clauses.append({"business_id": {"$in": list(business_ids)}})

                deleted = {
                    "corrections": int(self._db.corrections.delete_many(linked_query).deleted_count),
                    "review_items": int(self._db.review_items.delete_many(linked_query).deleted_count),
                    "transactions": int(self._db.transactions.delete_many(linked_query).deleted_count),
                    "statement_uploads": int(self._db.statement_uploads.delete_many({"$or": statement_clauses}).deleted_count),
                    "email_settings": int(self._db.email_settings.delete_many({"user_id": user_id}).deleted_count),
                    "businesses": int(self._db.businesses.delete_many({"user_id": user_id}).deleted_count),
                    "users": int(self._db.users.delete_one({"user_id": user_id}).deleted_count),
                }
                if deleted["users"] != 1:
                    raise RepositoryError("User account could not be deleted")
                return deleted
            except PyMongoError as exc:
                raise RepositoryError(f"Could not delete user account records: {exc}") from exc

        business_id_set = set(business_ids)
        statement_id_set = set(statement_ids)
        transaction_id_set = set(transaction_ids)

        def is_linked(doc: dict[str, Any]) -> bool:
            return (
                doc.get("user_id") == user_id
                or doc.get("business_id") in business_id_set
                or doc.get("statement_id") in statement_id_set
                or doc.get("transaction_id") in transaction_id_set
            )

        deleted = {
            "corrections": self._delete_by_predicate("corrections", is_linked),
            "review_items": self._delete_by_predicate("review_items", is_linked),
            "transactions": self._delete_by_predicate("transactions", is_linked),
            "statement_uploads": self._delete_by_predicate(
                "statement_uploads",
                lambda doc: doc.get("user_id") == user_id or doc.get("business_id") in business_id_set,
            ),
            "email_settings": self._delete_by_predicate("email_settings", lambda doc: doc.get("user_id") == user_id),
            "businesses": self._delete_by_predicate("businesses", lambda doc: doc.get("user_id") == user_id),
            "users": self._delete_by_predicate("users", lambda doc: doc.get("user_id") == user_id),
        }
        if deleted["users"] != 1:
            raise RepositoryError("User account could not be deleted")
        return deleted

    def replace_transactions(self, business_id: str, statement_id: str, rows: list[dict[str, Any]]) -> None:
        self._delete_many("transactions", {"statement_id": statement_id})
        self._delete_many("review_items", {"statement_id": statement_id})
        if rows:
            self._insert_many("transactions", rows, key="transaction_id")

        review_items = [
            {
                "review_item_id": uuid.uuid4().hex,
                "business_id": business_id,
                "user_id": row.get("user_id"),
                "statement_id": statement_id,
                "transaction_id": row["transaction_id"],
                "status": "pending",
                "reason": row.get("reason") or "Review recommended by classifier",
                "created_at": utc_now(),
                "updated_at": utc_now(),
            }
            for row in rows
            if row.get("review_status") == "pending"
        ]
        if review_items:
            self._insert_many("review_items", review_items, key="review_item_id")

    def list_transactions(self, statement_id: str) -> list[dict[str, Any]]:
        docs = self._find_many("transactions", {"statement_id": statement_id})
        docs.sort(key=lambda item: item.get("row_index", 0))
        return [_normalise_transaction_doc(_clean_doc(doc)) for doc in docs]

    def list_business_transactions(self, business_id: str, user_id: str | None = None) -> list[dict[str, Any]]:
        query = {"business_id": business_id}
        if user_id:
            query["user_id"] = user_id
        docs = self._find_many("transactions", query)
        docs.sort(key=lambda item: (str(item.get("transaction_date") or ""), item.get("row_index", 0)))
        return [_normalise_transaction_doc(_clean_doc(doc)) for doc in docs]

    def list_analyzed_user_statement_records(self, user_id: str) -> list[dict[str, Any]]:
        statements = []
        for statement in self.list_user_statement_records(user_id):
            if statement.get("deleted_at") or statement.get("deleted") is True:
                continue
            status = statement.get("processing_status") or statement.get("status")
            if str(status or "").strip().lower() == "analyzed":
                statements.append(statement)
        return statements

    def _update_transaction_tax_fields(self, row: dict[str, Any], updates: dict[str, Any]) -> None:
        if not updates:
            return
        query = {}
        if row.get("transaction_id"):
            query = {"transaction_id": row["transaction_id"]}
        elif row.get("statement_id") and row.get("row_index") is not None:
            query = {"statement_id": row["statement_id"], "row_index": row["row_index"]}
        if not query:
            return
        self._update_one("transactions", query, {"$set": {"updated_at": utc_now(), **updates}})
        row.update(updates)

    def _reconcile_transaction_tax_fields(self, row: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        updates: dict[str, Any] = {}
        exact_category = _exact_tax_category(row)
        recalculated = False

        if exact_category:
            category = exact_category
            if row.get("classification") != category:
                updates["classification"] = category
        elif _has_transaction_narration(row):
            guarded = _normalise_transaction_doc(row)
            category = normalize_category(guarded.get("classification"))
            recalculated = True
            updates.update(
                {
                    "tax_category": category,
                    "classification": category,
                    "confidence": guarded.get("confidence"),
                    "review_status": guarded.get("review_status"),
                    "review_recommended": bool(guarded.get("review_recommended")),
                    "reason": guarded.get("reason"),
                    "normalized_particulars": guarded.get("normalized_particulars"),
                    "classification_source": guarded.get("classification_source"),
                    "final_override_applied": guarded.get("final_override_applied"),
                }
            )
        elif row.get("classification") is not None or row.get("category") is not None:
            category = _transaction_category(row)
            recalculated = True
            updates["tax_category"] = category
            updates["classification"] = category
        else:
            return row, recalculated

        review_recommended = (
            bool(updates["review_recommended"])
            if "review_recommended" in updates
            else _transaction_review_recommended(row)
        )
        if not isinstance(row.get("review_recommended"), bool):
            updates["review_recommended"] = review_recommended
        if row.get("review_status") not in {"pending", "cleared"}:
            updates["review_status"] = "pending" if review_recommended else "cleared"

        if updates:
            self._update_transaction_tax_fields(row, updates)
        return row, recalculated

    def tax_signal_summary(self, user_id: str) -> dict[str, Any]:
        statements = self.list_analyzed_user_statement_records(user_id)
        statement_ids = {
            statement.get("statement_id")
            for statement in statements
            if statement.get("statement_id")
        }

        counts = Counter({CATEGORY_GST: 0, CATEGORY_POSSIBLE_GST: 0, CATEGORY_TDS: 0, CATEGORY_NORMAL: 0})
        pending_review_count = 0
        raw_transaction_count = 0
        counted_transaction_count = 0
        invalid_category_count = 0
        reconciled_transaction_count = 0

        for statement_id in statement_ids:
            for row in self._find_many("transactions", {"statement_id": statement_id}):
                if row.get("statement_id") not in statement_ids:
                    continue
                raw_transaction_count += 1
                row, reconciled = self._reconcile_transaction_tax_fields(row)
                if reconciled:
                    reconciled_transaction_count += 1
                category = _exact_tax_category(row)
                if not category:
                    invalid_category_count += 1
                    continue
                counts[category] += 1
                if _exact_review_recommended(row):
                    pending_review_count += 1
                counted_transaction_count += 1

        category_total = int(
            counts.get(CATEGORY_GST, 0)
            + counts.get(CATEGORY_POSSIBLE_GST, 0)
            + counts.get(CATEGORY_TDS, 0)
            + counts.get(CATEGORY_NORMAL, 0)
        )
        logger.info(
            "FinScan tax summary debug user_id=%s analyzed_statements=%s raw_transactions=%s counted_transactions=%s reconciled_transactions=%s invalid_tax_category=%s counts=%s pending_review=%s",
            user_id,
            len(statements),
            raw_transaction_count,
            counted_transaction_count,
            reconciled_transaction_count,
            invalid_category_count,
            dict(counts),
            pending_review_count,
        )

        return {
            "userId": user_id,
            "statementCount": len(statements),
            "totalTransactions": category_total,
            "transactionCount": category_total,
            "rawTransactionCount": raw_transaction_count,
            "reconciledTransactionCount": reconciled_transaction_count,
            "invalidTaxCategoryCount": invalid_category_count,
            "taxCounts": {
                CATEGORY_GST: int(counts.get(CATEGORY_GST, 0)),
                CATEGORY_POSSIBLE_GST: int(counts.get(CATEGORY_POSSIBLE_GST, 0)),
                CATEGORY_TDS: int(counts.get(CATEGORY_TDS, 0)),
                CATEGORY_NORMAL: int(counts.get(CATEGORY_NORMAL, 0)),
            },
            "categoryTotal": category_total,
            "countsMatchTransactions": category_total == counted_transaction_count,
            "pendingReviewCount": int(pending_review_count),
            "source": "saved_transactions",
            "updatedAt": utc_now(),
        }

    def dashboard_analytics(self, business_id: str, user_id: str | None = None) -> dict[str, Any]:
        transactions = self.list_business_transactions(business_id, user_id=user_id)
        statements = self.list_statement_uploads(business_id, user_id=user_id)

        daily: dict[str, dict[str, float]] = defaultdict(lambda: {"credits": 0.0, "debits": 0.0, "net": 0.0})
        tax_counts = Counter()
        parties: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"name": "", "count": 0, "credits": 0.0, "debits": 0.0, "total": 0.0}
        )
        pending_review_count = 0

        for row in transactions:
            credit = float(row.get("credit") or 0)
            debit = float(row.get("debit") or 0)
            date = _date_key(row.get("transaction_date"))
            daily[date]["credits"] += credit
            daily[date]["debits"] += debit
            daily[date]["net"] += credit - debit
            tax_counts[_transaction_category(row)] += 1
            if row.get("review_status") == "pending":
                pending_review_count += 1

            party = _party_name(row.get("narration"))
            parties[party]["name"] = party
            parties[party]["count"] += 1
            parties[party]["credits"] += credit
            parties[party]["debits"] += debit
            parties[party]["total"] += credit + debit

        daily_rows = [
            {"date": date, **{key: round(value, 2) for key, value in values.items()}}
            for date, values in sorted(daily.items())
            if date != "Unknown"
        ]
        unknown = daily.get("Unknown")
        if unknown:
            daily_rows.append({"date": "Unknown", **{key: round(value, 2) for key, value in unknown.items()}})

        weekly_rows = self._rollup_cashflow(daily_rows, "week")
        monthly_rows = self._rollup_cashflow(daily_rows, "month")
        top_parties = sorted(parties.values(), key=lambda item: item["total"], reverse=True)[:8]

        return {
            "businessId": business_id,
            "userId": user_id,
            "statementCount": len(statements),
            "totalTransactions": len(transactions),
            "totalCredits": round(sum(float(row.get("credit") or 0) for row in transactions), 2),
            "totalDebits": round(sum(float(row.get("debit") or 0) for row in transactions), 2),
            "netCashflow": round(
                sum(float(row.get("credit") or 0) for row in transactions)
                - sum(float(row.get("debit") or 0) for row in transactions),
                2,
            ),
            "dailyCashflow": daily_rows[-30:],
            "weeklyCashflow": weekly_rows[-12:],
            "monthlyCashflow": monthly_rows[-12:],
            "taxCounts": {
                CATEGORY_GST: int(tax_counts.get(CATEGORY_GST, 0)),
                CATEGORY_TDS: int(tax_counts.get(CATEGORY_TDS, 0)),
                CATEGORY_POSSIBLE_GST: int(tax_counts.get(CATEGORY_POSSIBLE_GST, 0)),
                CATEGORY_NORMAL: int(tax_counts.get(CATEGORY_NORMAL, 0)),
            },
            "pendingReviewCount": pending_review_count,
            "topParties": [
                {
                    "name": item["name"],
                    "count": int(item["count"]),
                    "credits": round(float(item["credits"]), 2),
                    "debits": round(float(item["debits"]), 2),
                    "total": round(float(item["total"]), 2),
                }
                for item in top_parties
            ],
            "recentStatements": statements[:5],
        }

    def _rollup_cashflow(self, daily_rows: list[dict[str, Any]], period: str) -> list[dict[str, Any]]:
        grouped: dict[str, dict[str, float]] = defaultdict(lambda: {"credits": 0.0, "debits": 0.0, "net": 0.0})
        for row in daily_rows:
            key = _period_key(row["date"], period)
            grouped[key]["credits"] += float(row.get("credits") or 0)
            grouped[key]["debits"] += float(row.get("debits") or 0)
            grouped[key]["net"] += float(row.get("net") or 0)
        return [
            {
                "period": key,
                "periodName": _period_name(key, period) if key != "Unknown" else "Unknown",
                **{field: round(value, 2) for field, value in values.items()},
            }
            for key, values in sorted(grouped.items())
        ]

    def public_user(self, doc: dict[str, Any] | None) -> dict[str, Any]:
        doc = _clean_doc(doc) or {}
        return {
            "userId": doc.get("user_id"),
            "email": doc.get("email"),
            "username": doc.get("username"),
            "name": doc.get("name"),
            "authProvider": doc.get("auth_provider", "password"),
        }

    def public_business(self, doc: dict[str, Any] | None) -> dict[str, Any]:
        doc = _clean_doc(doc) or {}
        return {
            "businessId": doc.get("business_id"),
            "userId": doc.get("user_id"),
            "name": doc.get("name"),
            "industry": doc.get("industry"),
            "createdAt": doc.get("created_at"),
        }

    def public_email_settings(self, doc: dict[str, Any] | None) -> dict[str, Any]:
        doc = _clean_doc(doc) or {}
        has_password = bool(doc.get("encrypted_gmail_app_password") or doc.get("encrypted_app_password"))
        has_statement_password = bool(doc.get("encrypted_statement_password"))
        return {
            "enabled": bool(doc.get("auto_fetch_enabled")),
            "enableAutoFetch": bool(doc.get("auto_fetch_enabled")),
            "gmailAddress": doc.get("gmail_address") or "",
            "gmailAppPassword": "********" if has_password else "",
            "hasAppPassword": has_password,
            "fetchFrequency": doc.get("fetch_frequency") or "6h",
            "fetchTime": doc.get("fetch_time") or "09:00",
            "fetchDay": doc.get("fetch_day") or "monday",
            "nextFetchAt": doc.get("next_fetch_at"),
            "businessId": doc.get("business_id"),
            "statementPasswordType": doc.get("statement_password_type") or "none",
            "statementPassword": "********" if has_statement_password else "",
            "hasStatementPassword": has_statement_password,
            "lastFetchAt": doc.get("last_fetch_at"),
            "lastFetchStatus": doc.get("last_fetch_status"),
            "lastFetchError": doc.get("last_fetch_error"),
            "lastTestAt": doc.get("last_test_at"),
            "updatedAt": doc.get("updated_at"),
        }

    def public_statement(self, doc: dict[str, Any] | None) -> dict[str, Any]:
        doc = _clean_doc(doc) or {}
        return {
            "statementId": doc.get("statement_id"),
            "businessId": doc.get("business_id"),
            "userId": doc.get("user_id"),
            "username": doc.get("username"),
            "businessName": doc.get("business_name"),
            "filename": doc.get("filename"),
            "originalFilename": doc.get("original_filename") or doc.get("filename"),
            "uploadDate": doc.get("uploaded_at"),
            "uploadedAt": doc.get("uploaded_at"),
            "analyzedAt": doc.get("analyzed_at"),
            "totalTransactions": int(doc.get("total_transactions") or 0),
            "totalCredits": float(doc.get("total_credits") or 0),
            "totalDebits": float(doc.get("total_debits") or 0),
            "processingStatus": doc.get("processing_status", "uploaded"),
            "status": doc.get("processing_status", "uploaded"),
            "storageProvider": doc.get("storage_provider"),
            "source": doc.get("source", "manual"),
            "passwordRequired": bool(doc.get("password_required")),
            "passwordError": doc.get("password_error"),
            "isPasswordProtected": bool(doc.get("is_password_protected")),
            "unlockStatus": doc.get("unlock_status") or "unknown",
            "storedInS3": bool(doc.get("s3_key") or doc.get("original_s3_key")),
            "s3Bucket": doc.get("s3_bucket") or doc.get("original_s3_bucket"),
            "s3Key": doc.get("s3_key") or doc.get("original_s3_key"),
            "s3Url": doc.get("s3_url") or _s3_url(
                doc.get("s3_bucket") or doc.get("original_s3_bucket"),
                doc.get("original_s3_region"),
                doc.get("s3_key") or doc.get("original_s3_key"),
            ),
            "summary": doc.get("summary", {}),
        }

    def _find_one(self, collection: str, query: dict[str, Any]) -> dict[str, Any] | None:
        if self._available and self._db is not None:
            try:
                return _clean_doc(self._db[collection].find_one(query))
            except PyMongoError as exc:
                logger.warning("MongoDB find_one failed for %s: %s", collection, exc)
        for doc in self._memory[collection].values():
            if self._matches(doc, query):
                return dict(doc)
        return None

    def _find_many(self, collection: str, query: dict[str, Any]) -> list[dict[str, Any]]:
        if self._available and self._db is not None:
            try:
                return [_clean_doc(doc) for doc in self._db[collection].find(query)]
            except PyMongoError as exc:
                logger.warning("MongoDB find failed for %s: %s", collection, exc)
        return [dict(doc) for doc in self._memory[collection].values() if self._matches(doc, query)]

    def _insert_one(self, collection: str, doc: dict[str, Any], key: str) -> None:
        if self._available and self._db is not None:
            try:
                self._db[collection].insert_one(dict(doc))
                return
            except DuplicateKeyError as exc:
                raise AuthError("User already exists") from exc
            except PyMongoError as exc:
                logger.warning("MongoDB insert failed for %s: %s", collection, exc)
        if collection == "users":
            if any(existing.get("email") == doc.get("email") for existing in self._memory[collection].values()):
                raise AuthError("Email is already registered")
            if any(existing.get("username") == doc.get("username") for existing in self._memory[collection].values()):
                raise AuthError("Username is already registered")
        self._memory[collection][doc[key]] = dict(doc)

    def _insert_many(self, collection: str, docs: list[dict[str, Any]], key: str) -> None:
        if self._available and self._db is not None:
            try:
                self._db[collection].insert_many([dict(doc) for doc in docs], ordered=False)
                return
            except PyMongoError as exc:
                logger.warning("MongoDB bulk insert failed for %s: %s", collection, exc)
        for doc in docs:
            self._memory[collection][doc[key]] = dict(doc)

    def _update_one(self, collection: str, query: dict[str, Any], update: dict[str, Any]) -> None:
        if self._available and self._db is not None:
            try:
                self._db[collection].update_one(query, update)
                return
            except PyMongoError as exc:
                logger.warning("MongoDB update failed for %s: %s", collection, exc)
        update_set = update.get("$set", {})
        for key, doc in self._memory[collection].items():
            if self._matches(doc, query):
                self._memory[collection][key] = {**doc, **update_set}
                return

    def _delete_many(self, collection: str, query: dict[str, Any]) -> None:
        if self._available and self._db is not None:
            try:
                self._db[collection].delete_many(query)
                return
            except PyMongoError as exc:
                logger.warning("MongoDB delete failed for %s: %s", collection, exc)
        matching = [key for key, doc in self._memory[collection].items() if self._matches(doc, query)]
        for key in matching:
            self._memory[collection].pop(key, None)

    def _delete_by_predicate(self, collection: str, predicate) -> int:
        matching = [key for key, doc in self._memory[collection].items() if predicate(doc)]
        for key in matching:
            self._memory[collection].pop(key, None)
        return len(matching)

    def _matches(self, doc: dict[str, Any], query: dict[str, Any]) -> bool:
        return all(doc.get(key) == value for key, value in query.items())


repository = DataRepository()
