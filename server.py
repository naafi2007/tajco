#!/usr/bin/env python3
"""Taj Opticals storefront and owner dashboard.

A dependency-free, single-process starter application using Python's standard
library and SQLite. For a multi-instance production deployment, move the
SQLite database and uploads to managed PostgreSQL/object storage as described
in README.md.
"""
from __future__ import annotations

import base64
import csv
import datetime as dt
import hashlib
import hmac
import html
import io
import ipaddress
import json
import mimetypes
import os
import re
import secrets
import shutil
import socket
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from email import policy
from email.parser import BytesParser
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
DEFAULT_DB = ROOT / "data" / "taj-opticals.sqlite3"
DEFAULT_UPLOADS = ROOT / "uploads"
MAX_REQUEST_BYTES = 125 * 1024 * 1024
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_VIDEO_BYTES = 100 * 1024 * 1024
PBKDF2_ROUNDS = 310_000
SESSION_DAYS = 7
VIDEO_API = "https://api.dev.runwayml.com/v1"
VIDEO_API_VERSION = "2024-11-06"

CATEGORY_SEED = [
    ("eyeglasses", "Eyeglasses", 10),
    ("sunglasses", "Sunglasses", 20),
    ("contact-lenses", "Contact Lenses", 30),
    ("accessories", "Accessories", 40),
]
POLICY_SEED = [
    ("about", "About Us", "A Clearer Vision. A Better You.", "At TAJ OPTICALS, we believe eyewear should feel good, look right, and fit your budget. We’re committed to quality, affordability, comfort and personal style—guided by our promise: High Quality, Low Cost.\n\nServing Coimbatore since 1950, we invite you to explore eyeglasses, sunglasses, contact lenses and accessories. Contact us to confirm current availability and prices.", "published"),
    ("contact", "Contact Us", "We’re here to help", "Phone: 9597378385\nAddress: 18/109, Channa Complex, Big Bazaar Street, Coimbatore, Tamil Nadu – 641001, India.\nFor WhatsApp ordering, use the verified WhatsApp number shown by the store.", "published"),
    ("faq", "Frequently Asked Questions", "Frequently asked questions", "This information is being reviewed by the store owner. Please contact Taj Opticals directly for current product availability, prescriptions, payment and order questions.", "draft"),
    ("delivery", "Delivery Information", "Delivery at a glance", "Delivery is calculated from the product subtotal before delivery charges. The current threshold and standard charge are controlled by the store settings above. Please contact the store to confirm any other delivery details.", "published"),
    ("returns", "Return and Exchange Policy", "Return and exchange policy", "Draft for owner review. This page does not confirm a return, exchange, refund, warranty or cancellation commitment. Please contact Taj Opticals before ordering to confirm the current policy.", "draft"),
    ("privacy", "Privacy Policy", "Privacy policy", "Draft for owner review. Please contact Taj Opticals with questions about information shared when enquiring or ordering. The owner should review and approve this page before relying on it as a formal privacy notice.", "draft"),
    ("terms", "Terms and Conditions", "Terms and conditions", "Draft for owner review. Product availability, pricing and order details should be confirmed directly with Taj Opticals. The owner should review and approve these terms before relying on this page as a formal agreement.", "draft"),
]

class APIError(Exception):
    def __init__(self, status: int, message: str, details: Any = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.details = details


def db_path() -> Path:
    return Path(os.environ.get("TAJ_DB_PATH", str(DEFAULT_DB))).expanduser().resolve()


def uploads_path() -> Path:
    return Path(os.environ.get("TAJ_UPLOAD_DIR", str(DEFAULT_UPLOADS))).expanduser().resolve()


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def india_today() -> str:
    return dt.datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()


class ClosingConnection(sqlite3.Connection):
    """SQLite transaction context that also closes the handle on exit."""
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def connect_db() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15, factory=ClosingConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def ensure_schema() -> None:
    uploads_path().mkdir(parents=True, exist_ok=True)
    with connect_db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS admins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_salt TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY,
            csrf_hash TEXT NOT NULL,
            admin_id INTEGER NOT NULL REFERENCES admins(id) ON DELETE CASCADE,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS sessions_expiry_idx ON sessions(expires_at);
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT NOT NULL UNIQUE COLLATE NOCASE,
            name TEXT NOT NULL UNIQUE COLLATE NOCASE,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS brands (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE COLLATE NOCASE,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sku TEXT NOT NULL UNIQUE COLLATE NOCASE,
            slug TEXT NOT NULL UNIQUE COLLATE NOCASE,
            name TEXT NOT NULL,
            category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE RESTRICT,
            brand_id INTEGER REFERENCES brands(id) ON DELETE SET NULL,
            description TEXT NOT NULL DEFAULT '',
            selling_price_paise INTEGER NOT NULL CHECK (selling_price_paise >= 0),
            original_price_paise INTEGER CHECK (original_price_paise IS NULL OR original_price_paise >= 0),
            stock_qty INTEGER NOT NULL DEFAULT 0 CHECK (stock_qty >= 0),
            status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('active','draft','out_of_stock')),
            featured INTEGER NOT NULL DEFAULT 0 CHECK (featured IN (0,1)),
            color TEXT NOT NULL DEFAULT '',
            frame_shape TEXT NOT NULL DEFAULT '',
            frame_size TEXT NOT NULL DEFAULT '',
            material TEXT NOT NULL DEFAULT '',
            specifications_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS products_category_status_idx ON products(category_id, status, created_at DESC);
        CREATE INDEX IF NOT EXISTS products_status_price_idx ON products(status, selling_price_paise);
        CREATE INDEX IF NOT EXISTS products_featured_idx ON products(featured, status);
        CREATE TABLE IF NOT EXISTS media (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
            kind TEXT NOT NULL CHECK (kind IN ('image','video')),
            storage_key TEXT,
            external_url TEXT,
            original_name TEXT NOT NULL DEFAULT '',
            mime_type TEXT NOT NULL DEFAULT '',
            byte_size INTEGER NOT NULL DEFAULT 0,
            sort_order INTEGER NOT NULL DEFAULT 0,
            is_cover INTEGER NOT NULL DEFAULT 0 CHECK (is_cover IN (0,1)),
            created_at TEXT NOT NULL,
            CHECK (storage_key IS NOT NULL OR external_url IS NOT NULL)
        );
        CREATE INDEX IF NOT EXISTS media_product_idx ON media(product_id, kind, sort_order);
        CREATE TABLE IF NOT EXISTS banners (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            headline TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            image_url TEXT NOT NULL DEFAULT '',
            product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
            start_date TEXT,
            end_date TEXT,
            cta_text TEXT NOT NULL DEFAULT 'Shop now',
            status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('active','draft')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS banners_status_dates_idx ON banners(status, start_date, end_date);
        CREATE TABLE IF NOT EXISTS policies (
            slug TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            heading TEXT NOT NULL,
            body TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('published','draft')),
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ai_assets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id INTEGER NOT NULL REFERENCES admins(id) ON DELETE CASCADE,
            storage_key TEXT NOT NULL,
            original_name TEXT NOT NULL,
            mime_type TEXT NOT NULL,
            byte_size INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS video_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_key TEXT NOT NULL UNIQUE,
            product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
            provider TEXT NOT NULL DEFAULT 'runway',
            provider_task_id TEXT,
            status TEXT NOT NULL DEFAULT 'submitting',
            progress INTEGER,
            prompt TEXT NOT NULL,
            settings_json TEXT NOT NULL,
            source_media_json TEXT NOT NULL DEFAULT '[]',
            output_url TEXT,
            error_message TEXT,
            saved_media_id INTEGER REFERENCES media(id) ON DELETE SET NULL,
            last_checked_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS video_jobs_product_idx ON video_jobs(product_id, created_at DESC);
        """)
        now = utc_now()
        for slug, name, order in CATEGORY_SEED:
            conn.execute("INSERT OR IGNORE INTO categories(slug,name,sort_order,created_at) VALUES(?,?,?,?)", (slug, name, order, now))
        for key, value in default_settings().items():
            conn.execute("INSERT OR IGNORE INTO settings(key,value_json,updated_at) VALUES(?,?,?)", (key, json.dumps(value, ensure_ascii=False), now))
        for slug, title, heading, body, status in POLICY_SEED:
            conn.execute("INSERT OR IGNORE INTO policies(slug,title,heading,body,status,updated_at) VALUES(?,?,?,?,?,?)", (slug, title, heading, body, status, now))
        conn.commit()


def default_settings() -> dict[str, Any]:
    return {
        "business_name": "TAJ OPTICALS",
        "tagline": "High Quality, Low Cost",
        "address": "18/109, Channa Complex, Big Bazaar Street, Coimbatore, Tamil Nadu – 641001, India.",
        "maps_url": "https://maps.google.com/?q=10.993682,76.964188",
        "contact_phone": "9597378385",
        "whatsapp_number": "",
        "whatsapp_verified": False,
        "logo_url": "",
        "free_delivery_threshold_paise": 170000,
        "standard_delivery_charge_paise": 5000,
    }


def get_setting(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value_json FROM settings WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row["value_json"])
    except (TypeError, json.JSONDecodeError):
        return default


def set_setting(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute("INSERT INTO settings(key,value_json,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at", (key, json.dumps(value, ensure_ascii=False), utc_now()))


def money_to_paise(value: Any, *, optional: bool = False) -> int | None:
    if optional and (value is None or str(value).strip() == ""):
        return None
    if isinstance(value, bool):
        raise ValueError("Enter a valid price in rupees.")
    try:
        amount = Decimal(str(value).strip().replace(",", "").replace("₹", ""))
    except (InvalidOperation, ValueError, AttributeError):
        raise ValueError("Enter a valid price in rupees.")
    if not amount.is_finite() or amount < 0 or amount > Decimal("999999999"):
        raise ValueError("Price must be a non-negative amount within the supported limit.")
    rounded = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return int(rounded * 100)


def calculate_delivery(subtotal_paise: int, threshold_paise: int = 170000, delivery_paise: int = 5000) -> dict[str, int | bool]:
    subtotal = max(0, int(subtotal_paise))
    threshold = max(0, int(threshold_paise))
    fee = max(0, int(delivery_paise))
    free = subtotal >= threshold
    charge = 0 if free or subtotal == 0 else fee
    return {
        "product_subtotal_paise": subtotal,
        "delivery_charge_paise": charge,
        "total_paise": subtotal + charge,
        "free_delivery": free and subtotal > 0,
        "remaining_for_free_delivery_paise": max(0, threshold - subtotal),
        "free_delivery_threshold_paise": threshold,
    }


def slugify(text: str) -> str:
    out = re.sub(r"[^a-z0-9]+", "-", str(text).strip().lower()).strip("-")
    return out[:80] or "product"


def text_value(value: Any, max_len: int = 5000, *, allow_empty: bool = True) -> str:
    if value is None:
        value = ""
    value = str(value).strip()
    if not allow_empty and not value:
        raise ValueError("This field is required.")
    if len(value) > max_len:
        raise ValueError(f"This field must be {max_len} characters or fewer.")
    return value


def parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


def resolve_category(conn: sqlite3.Connection, value: Any) -> sqlite3.Row | None:
    if value is None or str(value).strip() == "":
        return None
    raw = str(value).strip()
    if raw.isdigit():
        return conn.execute("SELECT * FROM categories WHERE id=?", (int(raw),)).fetchone()
    return conn.execute("SELECT * FROM categories WHERE slug=? OR name=? COLLATE NOCASE", (raw, raw)).fetchone()


def validate_product(conn: sqlite3.Connection, data: dict[str, Any], current_id: int | None = None) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("Product data must be an object.")
    sku = text_value(data.get("sku"), 80, allow_empty=False)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,79}", sku):
        raise ValueError("SKU may contain letters, numbers, dots, underscores, slashes and hyphens.")
    duplicate = conn.execute("SELECT id FROM products WHERE sku=? COLLATE NOCASE AND (? IS NULL OR id<>?)", (sku, current_id, current_id)).fetchone()
    if duplicate:
        raise ValueError(f"SKU {sku} already exists.")
    name = text_value(data.get("name"), 180, allow_empty=False)
    category = resolve_category(conn, data.get("category_id", data.get("category")))
    if not category:
        raise ValueError("Choose a category that exists in the catalogue.")
    try:
        price = money_to_paise(data.get("selling_price_inr", data.get("price_inr", data.get("price"))))
        original = money_to_paise(data.get("original_price_inr", data.get("original_price")), optional=True)
    except ValueError as exc:
        raise ValueError(str(exc))
    if original is not None and original < price:
        raise ValueError("Original price must be at least the selling price.")
    stock_raw = data.get("stock_qty", 0)
    try:
        if isinstance(stock_raw, bool) or not re.fullmatch(r"\d+", str(stock_raw).strip()):
            raise ValueError
        stock = int(str(stock_raw).strip())
    except (ValueError, TypeError):
        raise ValueError("Stock quantity must be a whole number of zero or more.")
    if stock > 2_147_483_647:
        raise ValueError("Stock quantity is too large.")
    status = str(data.get("status", "draft")).strip().lower()
    if status not in {"active", "draft", "out_of_stock"}:
        raise ValueError("Status must be active, draft or out_of_stock.")
    brand = text_value(data.get("brand"), 120)
    description = text_value(data.get("description"), 12000)
    color = text_value(data.get("color"), 120)
    frame_shape = text_value(data.get("frame_shape"), 120)
    frame_size = text_value(data.get("frame_size"), 120)
    material = text_value(data.get("material"), 120)
    specs = data.get("specifications", {})
    if isinstance(specs, str):
        if specs.strip():
            try:
                specs = json.loads(specs)
            except json.JSONDecodeError:
                raise ValueError("Specifications must be valid JSON or blank.")
        else:
            specs = {}
    if not isinstance(specs, dict):
        raise ValueError("Specifications must be an object.")
    clean_specs = {}
    for key, val in specs.items():
        safe_key = text_value(key, 60, allow_empty=False)
        if re.fullmatch(r"[A-Za-z0-9 _-]+", safe_key) is None:
            raise ValueError("Specification names may use letters, numbers, spaces, underscores and hyphens.")
        clean_specs[safe_key] = text_value(val, 500)
    for key in ("pack_size", "sphere", "cylinder", "axis", "base_curve", "diameter"):
        if data.get(key) not in (None, ""):
            clean_specs[key] = text_value(data.get(key), 120)
    featured = parse_bool(data.get("featured", False))
    return {
        "sku": sku,
        "name": name,
        "category_id": int(category["id"]),
        "brand": brand,
        "description": description,
        "selling_price_paise": int(price),
        "original_price_paise": original,
        "stock_qty": stock,
        "status": status,
        "featured": int(featured),
        "color": color,
        "frame_shape": frame_shape,
        "frame_size": frame_size,
        "material": material,
        "specifications_json": json.dumps(clean_specs, ensure_ascii=False),
    }


def create_admin_account(email: str, password: str) -> None:
    email = email.strip().lower()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise ValueError("Enter a valid email address.")
    if len(password) < 12 or len(password) > 256:
        raise ValueError("Admin password must be between 12 and 256 characters.")
    ensure_schema()
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    with connect_db() as conn:
        if conn.execute("SELECT 1 FROM admins LIMIT 1").fetchone():
            raise ValueError("An admin account already exists; use the admin panel to sign in.")
        conn.execute("INSERT INTO admins(email,password_salt,password_hash,created_at) VALUES(?,?,?,?)", (email, salt.hex(), digest.hex(), utc_now()))
        conn.commit()


def verify_password(password: str, salt_hex: str, hash_hex: str) -> bool:
    try:
        test = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), PBKDF2_ROUNDS).hex()
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(test, hash_hex)


def public_settings(conn: sqlite3.Connection) -> dict[str, Any]:
    settings = {k: get_setting(conn, k, v) for k, v in default_settings().items()}
    verified = bool(settings.get("whatsapp_verified")) and bool(settings.get("whatsapp_number"))
    return {
        "business_name": settings["business_name"],
        "tagline": settings["tagline"],
        "address": settings["address"],
        "maps_url": settings["maps_url"],
        "contact_phone": settings["contact_phone"],
        "whatsapp_number": settings["whatsapp_number"] if verified else "",
        "whatsapp_ready": verified,
        "logo_url": settings["logo_url"],
        "delivery": {
            "free_delivery_threshold_paise": int(settings["free_delivery_threshold_paise"]),
            "standard_delivery_charge_paise": int(settings["standard_delivery_charge_paise"]),
        },
    }


def media_dict(row: sqlite3.Row) -> dict[str, Any]:
    url = row["external_url"] or ("/uploads/" + urllib.parse.quote(row["storage_key"], safe=""))
    return {
        "id": row["id"], "kind": row["kind"], "url": url,
        "filename": row["original_name"], "mime_type": row["mime_type"],
        "byte_size": row["byte_size"], "sort_order": row["sort_order"],
        "is_cover": bool(row["is_cover"]),
    }


def product_dict(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    media_rows = conn.execute("SELECT * FROM media WHERE product_id=? ORDER BY is_cover DESC, sort_order, id", (row["id"],)).fetchall()
    try:
        specifications = json.loads(row["specifications_json"] or "{}")
    except json.JSONDecodeError:
        specifications = {}
    selling = int(row["selling_price_paise"])
    original = row["original_price_paise"]
    return {
        "id": row["id"], "sku": row["sku"], "slug": row["slug"],
        "name": row["name"], "category_id": row["category_id"],
        "category": {"id": row["category_id"], "name": row["category_name"], "slug": row["category_slug"]},
        "brand": row["brand_name"] or "", "description": row["description"],
        "selling_price_paise": selling, "selling_price_inr": selling / 100,
        "original_price_paise": original, "original_price_inr": (int(original) / 100 if original is not None else None),
        "discount_percent": (max(0, round((int(original) - selling) * 100 / int(original))) if original and int(original) > 0 and int(original) > selling else None),
        "stock_qty": int(row["stock_qty"]), "status": row["status"],
        "available": row["status"] == "active" and int(row["stock_qty"]) > 0,
        "featured": bool(row["featured"]), "color": row["color"],
        "frame_shape": row["frame_shape"], "frame_size": row["frame_size"],
        "material": row["material"], "specifications": specifications,
        "media": [media_dict(m) for m in media_rows],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def product_select_sql(where: str = "") -> str:
    return """SELECT p.*, c.name AS category_name, c.slug AS category_slug, b.name AS brand_name
              FROM products p JOIN categories c ON c.id=p.category_id
              LEFT JOIN brands b ON b.id=p.brand_id """ + (" WHERE " + where if where else "")


def get_product(conn: sqlite3.Connection, identifier: str, *, active_only: bool = False) -> sqlite3.Row | None:
    ident = urllib.parse.unquote(identifier)
    if ident.isdigit():
        query = product_select_sql("p.id=?" + (" AND p.status='active'" if active_only else ""))
        return conn.execute(query, (int(ident),)).fetchone()
    query = product_select_sql("p.slug=?" + (" AND p.status='active'" if active_only else ""))
    return conn.execute(query, (ident,)).fetchone()


def product_brand_id(conn: sqlite3.Connection, name: str) -> int | None:
    if not name:
        return None
    now = utc_now()
    conn.execute("INSERT OR IGNORE INTO brands(name,created_at) VALUES(?,?)", (name, now))
    row = conn.execute("SELECT id FROM brands WHERE name=? COLLATE NOCASE", (name,)).fetchone()
    return int(row["id"]) if row else None


def insert_product(conn: sqlite3.Connection, clean: dict[str, Any]) -> int:
    brand_id = product_brand_id(conn, clean["brand"])
    slug_base = slugify(clean["name"])
    slug = slug_base
    suffix = 2
    while conn.execute("SELECT 1 FROM products WHERE slug=? COLLATE NOCASE", (slug,)).fetchone():
        slug = f"{slug_base}-{suffix}"
        suffix += 1
    now = utc_now()
    cursor = conn.execute("""INSERT INTO products(sku,slug,name,category_id,brand_id,description,selling_price_paise,original_price_paise,stock_qty,status,featured,color,frame_shape,frame_size,material,specifications_json,created_at,updated_at)
                             VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
        clean["sku"], slug, clean["name"], clean["category_id"], brand_id,
        clean["description"], clean["selling_price_paise"], clean["original_price_paise"],
        clean["stock_qty"], clean["status"], clean["featured"], clean["color"],
        clean["frame_shape"], clean["frame_size"], clean["material"],
        clean["specifications_json"], now, now,
    ))
    return int(cursor.lastrowid)


def update_product(conn: sqlite3.Connection, product_id: int, clean: dict[str, Any]) -> None:
    brand_id = product_brand_id(conn, clean["brand"])
    row = conn.execute("SELECT slug FROM products WHERE id=?", (product_id,)).fetchone()
    if not row:
        raise APIError(404, "Product not found.")
    slug = row["slug"]
    # Keep stable links on rename; the owner can retain the original slug.
    conn.execute("""UPDATE products SET sku=?,name=?,category_id=?,brand_id=?,description=?,selling_price_paise=?,original_price_paise=?,stock_qty=?,status=?,featured=?,color=?,frame_shape=?,frame_size=?,material=?,specifications_json=?,updated_at=? WHERE id=?""", (
        clean["sku"], clean["name"], clean["category_id"], brand_id,
        clean["description"], clean["selling_price_paise"], clean["original_price_paise"],
        clean["stock_qty"], clean["status"], clean["featured"], clean["color"],
        clean["frame_shape"], clean["frame_size"], clean["material"],
        clean["specifications_json"], utc_now(), product_id,
    ))


def category_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {"id": row["id"], "slug": row["slug"], "name": row["name"], "sort_order": row["sort_order"]}


def valid_external_url(url: Any) -> str:
    value = text_value(url, 2048, allow_empty=False)
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Use a valid HTTPS media URL.")
    host = parsed.hostname.lower()
    if host in {"localhost", "localhost.localdomain"}:
        raise ValueError("Localhost URLs are not allowed.")
    try:
        ip = ipaddress.ip_address(host)
        if not ip.is_global:
            raise ValueError("Private network URLs are not allowed.")
    except ValueError as exc:
        if "Private network" in str(exc):
            raise
    return value


def sample_csv_text() -> str:
    columns = ["sku", "name", "category", "brand", "description", "selling_price_inr", "original_price_inr", "stock_qty", "status", "featured", "color", "frame_shape", "frame_size", "material", "pack_size", "sphere", "cylinder", "axis", "base_curve", "diameter", "specifications_json"]
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(columns)
    return out.getvalue()


CSV_ALIASES = {
    "sku": {"sku", "product_sku", "product_id"},
    "name": {"name", "product_name", "title"},
    "category": {"category", "category_name", "category_slug"},
    "brand": {"brand", "brand_name"},
    "description": {"description", "product_description"},
    "selling_price_inr": {"selling_price_inr", "price_inr", "price", "selling_price"},
    "original_price_inr": {"original_price_inr", "original_price", "mrp"},
    "stock_qty": {"stock_qty", "stock", "quantity", "inventory"},
    "status": {"status", "publish_status"},
    "featured": {"featured", "is_featured"},
    "color": {"color", "colour"},
    "frame_shape": {"frame_shape", "shape"},
    "frame_size": {"frame_size", "size"},
    "material": {"material"},
    "pack_size": {"pack_size", "pack"},
    "sphere": {"sphere", "power_sphere"},
    "cylinder": {"cylinder", "cyl"},
    "axis": {"axis"},
    "base_curve": {"base_curve", "bc"},
    "diameter": {"diameter", "dia"},
    "specifications_json": {"specifications_json", "specifications", "attributes_json"},
}


def parse_csv_import(conn: sqlite3.Connection, csv_text: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if len(csv_text.encode("utf-8")) > 10 * 1024 * 1024:
        raise APIError(413, "CSV must be 10 MB or smaller.")
    try:
        reader = csv.DictReader(io.StringIO(csv_text))
        if not reader.fieldnames:
            raise APIError(400, "CSV is missing a header row.")
        normalized = {re.sub(r"[^a-z0-9]+", "_", h.strip().lower()).strip("_"): h for h in reader.fieldnames if h}
        mapping = {}
        for target, aliases in CSV_ALIASES.items():
            for alias in aliases:
                if alias in normalized:
                    mapping[target] = normalized[alias]
                    break
        required = {"sku", "name", "category", "selling_price_inr"}
        if not required.issubset(mapping):
            missing = sorted(required - set(mapping))
            raise APIError(400, "CSV is missing required columns: " + ", ".join(missing))
        rows: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        seen: set[str] = set()
        existing = {r["sku"].lower() for r in conn.execute("SELECT sku FROM products")}
        for line_no, raw in enumerate(reader, start=2):
            if not any((v or "").strip() for v in raw.values() if isinstance(v, str)):
                continue
            item = {target: (raw.get(source) or "").strip() for target, source in mapping.items()}
            sku_key = item.get("sku", "").lower()
            if sku_key:
                if sku_key in seen or sku_key in existing:
                    errors.append({"row": line_no, "sku": item.get("sku", ""), "message": "Duplicate SKU in this file or existing catalogue."})
                    seen.add(sku_key)
                    continue
                seen.add(sku_key)
            try:
                specs = {}
                for k in ("pack_size", "sphere", "cylinder", "axis", "base_curve", "diameter"):
                    if item.get(k):
                        specs[k] = item[k]
                if item.get("specifications_json"):
                    supplied = json.loads(item["specifications_json"])
                    if not isinstance(supplied, dict):
                        raise ValueError("Specifications JSON must be an object.")
                    specs.update(supplied)
                item["specifications"] = specs
                item["status"] = item.get("status") or "draft"
                item["stock_qty"] = item.get("stock_qty") or "0"
                clean = validate_product(conn, item)
                rows.append({"row": line_no, "sku": clean["sku"], "name": clean["name"], "category": resolve_category(conn, item.get("category"))["name"], "price_paise": clean["selling_price_paise"], "status": clean["status"], "clean": clean})
            except (ValueError, TypeError, KeyError) as exc:
                errors.append({"row": line_no, "sku": item.get("sku", ""), "message": str(exc)})
        return rows, errors
    except csv.Error as exc:
        raise APIError(400, "Could not read CSV: " + str(exc))


def format_policy(row: sqlite3.Row) -> dict[str, Any]:
    return {"slug": row["slug"], "title": row["title"], "heading": row["heading"], "body": row["body"], "status": row["status"], "updated_at": row["updated_at"]}


def banner_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {"id": row["id"], "headline": row["headline"], "description": row["description"], "image_url": row["image_url"], "product_id": row["product_id"], "start_date": row["start_date"], "end_date": row["end_date"], "cta_text": row["cta_text"], "status": row["status"], "created_at": row["created_at"], "updated_at": row["updated_at"]}


def serialize_video_job(row: sqlite3.Row) -> dict[str, Any]:
    try:
        settings = json.loads(row["settings_json"] or "{}")
    except json.JSONDecodeError:
        settings = {}
    try:
        source_media = json.loads(row["source_media_json"] or "[]")
    except json.JSONDecodeError:
        source_media = []
    return {"id": row["id"], "request_key": row["request_key"], "product_id": row["product_id"], "provider": row["provider"], "provider_task_id": row["provider_task_id"], "status": row["status"], "progress": row["progress"], "prompt": row["prompt"], "settings": settings, "source_media": source_media, "output_url": row["output_url"], "error_message": row["error_message"], "saved_media_id": row["saved_media_id"], "created_at": row["created_at"], "updated_at": row["updated_at"]}


LOGIN_ATTEMPTS: dict[str, list[float]] = {}
LOGIN_LOCK = threading.Lock()

class TajHandler(BaseHTTPRequestHandler):
    server_version = "TajOpticals/1.0"
    sys_version = ""

    def log_message(self, fmt: str, *args: Any) -> None:
        # Keep request logs useful without dumping request bodies or credentials.
        try:
            super().log_message(fmt, *args)
        except Exception:
            pass

    def send_bytes(self, body: bytes, content_type: str, status: int = 200, extra_headers: dict[str, Any] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if extra_headers:
            for key, value in extra_headers.items():
                values = value if isinstance(value, (list, tuple)) else [value]
                for item in values:
                    self.send_header(key, str(item))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def send_json(self, payload: Any, status: int = 200, extra_headers: dict[str, str] | None = None) -> None:
        data = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        headers = {"Cache-Control": "no-store"}
        if extra_headers:
            headers.update(extra_headers)
        self.send_bytes(data, "application/json; charset=utf-8", status, headers)

    def error_json(self, status: int, message: str, details: Any = None) -> None:
        body: dict[str, Any] = {"error": message}
        if details is not None:
            body["details"] = details
        self.send_json(body, status)

    def read_body(self, max_bytes: int = MAX_REQUEST_BYTES) -> bytes:
        raw = self.headers.get("Content-Length")
        if not raw:
            raise APIError(411, "Content-Length is required.")
        try:
            length = int(raw)
        except ValueError:
            raise APIError(400, "Invalid Content-Length.")
        if length < 0 or length > max_bytes:
            raise APIError(413, "Request body is too large.")
        body = self.rfile.read(length)
        if len(body) != length:
            raise APIError(400, "Request body was incomplete.")
        return body

    def read_json(self) -> dict[str, Any]:
        body = self.read_body()
        try:
            data = json.loads(body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise APIError(400, "Request must contain valid JSON.")
        if not isinstance(data, dict):
            raise APIError(400, "Request JSON must be an object.")
        return data

    def parse_parts(self, body: bytes) -> list[tuple[str, str | None, str, bytes]]:
        content_type = self.headers.get("Content-Type", "")
        if not content_type.lower().startswith("multipart/form-data"):
            raise APIError(415, "Expected a multipart form upload.")
        message = BytesParser(policy=policy.default).parsebytes(b"Content-Type: " + content_type.encode("latin-1") + b"\r\nMIME-Version: 1.0\r\n\r\n" + body)
        if not message.is_multipart():
            raise APIError(400, "Could not parse the upload.")
        parts = []
        for part in message.iter_parts():
            if part.get_content_disposition() != "form-data":
                continue
            name = part.get_param("name", header="content-disposition") or ""
            filename = part.get_filename()
            mime = part.get_content_type()
            payload = part.get_payload(decode=True) or b""
            parts.append((name, filename, mime, payload))
        return parts

    def get_admin(self, require_csrf: bool = False) -> tuple[sqlite3.Row, str]:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            raise APIError(401, "Admin sign-in is required.")
        morsel = cookie.get("taj_session")
        if not morsel:
            raise APIError(401, "Admin sign-in is required.")
        token = morsel.value
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with connect_db() as conn:
            session = conn.execute("SELECT s.*,a.email FROM sessions s JOIN admins a ON a.id=s.admin_id WHERE s.token_hash=?", (token_hash,)).fetchone()
            if not session or session["expires_at"] < utc_now():
                if session:
                    conn.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))
                raise APIError(401, "Admin session expired. Please sign in again.")
            if require_csrf:
                supplied = self.headers.get("X-CSRF-Token", "")
                given_hash = hashlib.sha256(supplied.encode("utf-8")).hexdigest()
                if not supplied or not hmac.compare_digest(given_hash, session["csrf_hash"]):
                    raise APIError(403, "Security token is missing or expired. Refresh the admin page and try again.")
            return session, token_hash

    def _dispatch(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        if path.startswith("/api/"):
            self.handle_api(path, query)
            return
        if path.startswith("/uploads/"):
            self.serve_upload(path)
            return
        if path.startswith("/static/"):
            self.serve_static(path)
            return
        self.serve_index()

    def do_GET(self) -> None:
        try:
            self._dispatch()
        except APIError as exc:
            self.error_json(exc.status, exc.message, exc.details)
        except BrokenPipeError:
            pass
        except Exception as exc:
            print("GET request failed:", type(exc).__name__, str(exc), file=sys.stderr)
            self.error_json(500, "Something went wrong. Please try again.")

    def do_POST(self) -> None:
        try:
            self._dispatch()
        except APIError as exc:
            self.error_json(exc.status, exc.message, exc.details)
        except sqlite3.IntegrityError as exc:
            self.error_json(409, "A record with the same unique value already exists.")
        except BrokenPipeError:
            pass
        except Exception as exc:
            print("POST request failed:", type(exc).__name__, str(exc), file=sys.stderr)
            self.error_json(500, "Something went wrong. Please try again.")

    def do_PUT(self) -> None:
        self.do_POST()

    def do_PATCH(self) -> None:
        self.do_POST()

    def do_DELETE(self) -> None:
        self.do_POST()

    def do_HEAD(self) -> None:
        try:
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.path.startswith("/static/") or parsed.path == "/":
                target = self.static_target(parsed.path)
                if target and target.is_file():
                    self.send_response(200)
                    self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
                    self.send_header("Content-Length", str(target.stat().st_size))
                    self.end_headers()
                    return
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
        except Exception:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def serve_index(self) -> None:
        target = STATIC_DIR / "index.html"
        body = target.read_bytes()
        csp = "default-src 'self'; img-src 'self' data: blob: https:; media-src 'self' blob: https:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; font-src 'self' data:; object-src 'none'; base-uri 'self'; frame-ancestors 'self'; form-action 'self' https://wa.me"
        self.send_bytes(body, "text/html; charset=utf-8", 200, {"Cache-Control": "no-cache", "Content-Security-Policy": csp})

    def static_target(self, path: str) -> Path | None:
        rel = path.removeprefix("/static/") if path.startswith("/static/") else "index.html"
        candidate = (STATIC_DIR / urllib.parse.unquote(rel)).resolve()
        try:
            candidate.relative_to(STATIC_DIR.resolve())
        except ValueError:
            return None
        return candidate

    def serve_static(self, path: str) -> None:
        target = self.static_target(path)
        if not target or not target.is_file():
            self.send_bytes(b"Not found", "text/plain; charset=utf-8", 404)
            return
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        cache = "no-cache" if target.suffix.lower() in {".html", ".js", ".css"} else "public, max-age=86400"
        self.send_bytes(target.read_bytes(), content_type, 200, {"Cache-Control": cache})

    def serve_upload(self, path: str) -> None:
        name = urllib.parse.unquote(path.removeprefix("/uploads/"))
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,180}", name):
            self.send_bytes(b"Not found", "text/plain; charset=utf-8", 404)
            return
        target = (uploads_path() / name).resolve()
        try:
            target.relative_to(uploads_path())
        except ValueError:
            self.send_bytes(b"Not found", "text/plain; charset=utf-8", 404)
            return
        if not target.is_file():
            self.send_bytes(b"Not found", "text/plain; charset=utf-8", 404)
            return
        public_asset = False
        with connect_db() as conn:
            public_asset = bool(conn.execute("SELECT 1 FROM media m JOIN products p ON p.id=m.product_id WHERE m.storage_key=? AND p.status='active' LIMIT 1", (name,)).fetchone())
            if not public_asset:
                public_asset = bool(conn.execute("SELECT 1 FROM banners WHERE status='active' AND image_url=? LIMIT 1", ("/uploads/" + name,)).fetchone())
            if not public_asset:
                public_asset = get_setting(conn, "logo_url", "") == "/uploads/" + name
        if not public_asset:
            try:
                self.get_admin()
            except APIError:
                self.send_bytes(b"Not found", "text/plain; charset=utf-8", 404)
                return
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        size = target.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Cache-Control", "public, max-age=3600")
        self.send_header("Content-Disposition", "inline; filename=\"" + target.name.replace('"', '') + "\"")
        self.end_headers()
        try:
            with target.open("rb") as media_file:
                while True:
                    chunk = media_file.read(128 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            return

    def handle_api(self, path: str, query: dict[str, list[str]]) -> None:
        method = self.command.upper()
        segments = [urllib.parse.unquote(x) for x in path.strip("/").split("/")]
        if path == "/api/health" and method == "GET":
            with connect_db() as conn:
                admin = conn.execute("SELECT 1 FROM admins LIMIT 1").fetchone() is not None
            self.send_json({"ok": True, "database": "connected", "admin_configured": admin})
            return
        if path == "/api/store" and method == "GET":
            with connect_db() as conn:
                self.send_json(public_settings(conn))
            return
        if path == "/api/categories" and method == "GET":
            with connect_db() as conn:
                rows = conn.execute("SELECT c.*, (SELECT COUNT(*) FROM products p WHERE p.category_id=c.id AND p.status='active') AS product_count FROM categories c ORDER BY sort_order,name").fetchall()
                self.send_json([{**category_dict(r), "product_count": r["product_count"]} for r in rows])
            return
        if path == "/api/brands" and method == "GET":
            with connect_db() as conn:
                rows = conn.execute("SELECT DISTINCT b.id,b.name FROM brands b JOIN products p ON p.brand_id=b.id WHERE p.status='active' ORDER BY b.name").fetchall()
                self.send_json([dict(r) for r in rows])
            return
        if path == "/api/products" and method == "GET":
            self.public_products(query)
            return
        if len(segments) == 3 and segments[:2] == ["api", "products"] and method == "GET":
            with connect_db() as conn:
                row = get_product(conn, segments[2], active_only=True)
                if not row:
                    raise APIError(404, "Product not found.")
                self.send_json(product_dict(conn, row))
            return
        if path == "/api/banners" and method == "GET":
            today = india_today()
            with connect_db() as conn:
                rows = conn.execute("SELECT * FROM banners WHERE status='active' AND (start_date IS NULL OR start_date<=?) AND (end_date IS NULL OR end_date>=?) ORDER BY created_at DESC", (today, today)).fetchall()
                self.send_json([banner_dict(r) for r in rows])
            return
        if path == "/api/policies" and method == "GET":
            with connect_db() as conn:
                rows = conn.execute("SELECT * FROM policies ORDER BY slug").fetchall()
                self.send_json([format_policy(r) for r in rows])
            return
        if path == "/api/quote" and method == "POST":
            self.public_quote()
            return
        if path == "/api/admin/login" and method == "POST":
            self.admin_login()
            return
        if path == "/api/admin/password" and method == "POST":
            session, token_hash = self.require_admin_mutation()
            self.change_admin_password(session, token_hash)
            return
        if path == "/api/admin/session" and method == "GET":
            session, _ = self.get_admin()
            with connect_db() as conn:
                row = conn.execute("SELECT email FROM admins WHERE id=?", (session["admin_id"],)).fetchone()
            csrf = self.admin_csrf_value(session)
            headers = {"Set-Cookie": self._csrf_cookie} if getattr(self, "_csrf_cookie", None) else None
            self.send_json({"admin": {"email": row["email"]}, "csrf_token": csrf}, 200, headers)
            return
        if path == "/api/admin/logout" and method == "POST":
            self.admin_logout()
            return
        if path == "/api/admin/dashboard" and method == "GET":
            self.admin_dashboard()
            return
        if path == "/api/admin/settings" and method == "GET":
            session, _ = self.get_admin()
            with connect_db() as conn:
                values = {k: get_setting(conn, k, v) for k, v in default_settings().items()}
            csrf = self.admin_csrf_value(session)
            headers = {"Set-Cookie": self._csrf_cookie} if getattr(self, "_csrf_cookie", None) else None
            self.send_json({"settings": values, "csrf_token": csrf}, 200, headers)
            return
        if path == "/api/admin/settings" and method in {"PUT", "PATCH", "POST"}:
            self.require_admin_mutation()
            self.update_settings()
            return
        if path == "/api/admin/products" and method == "GET":
            self.require_admin()
            self.admin_products(query)
            return
        if path == "/api/admin/products" and method == "POST":
            self.require_admin_mutation()
            self.create_product()
            return
        if len(segments) == 4 and segments[:3] == ["api", "admin", "products"] and segments[3].isdigit():
            product_id = self.int_id(segments[3])
            if method in {"PUT", "PATCH"}:
                self.require_admin_mutation()
                self.edit_product(product_id)
                return
            if method == "DELETE":
                self.require_admin_mutation()
                self.delete_product(product_id)
                return
        if path == "/api/admin/categories" and method == "GET":
            self.require_admin()
            self.admin_categories()
            return
        if path == "/api/admin/categories" and method == "POST":
            self.require_admin_mutation()
            self.create_category()
            return
        if len(segments) == 4 and segments[:3] == ["api", "admin", "categories"]:
            cat_id = self.int_id(segments[3])
            if method in {"PUT", "PATCH"}:
                self.require_admin_mutation()
                self.edit_category(cat_id)
                return
            if method == "DELETE":
                self.require_admin_mutation()
                self.delete_category(cat_id)
                return
        if path == "/api/admin/brands" and method == "GET":
            self.require_admin()
            with connect_db() as conn:
                rows = conn.execute("SELECT b.id,b.name,(SELECT COUNT(*) FROM products p WHERE p.brand_id=b.id) AS product_count FROM brands b ORDER BY b.name").fetchall()
                self.send_json([dict(r) for r in rows])
            return
        if path == "/api/admin/brands" and method == "POST":
            self.require_admin_mutation()
            self.create_brand()
            return
        if len(segments) == 4 and segments[:3] == ["api", "admin", "brands"]:
            brand_id = self.int_id(segments[3])
            if method in {"PUT", "PATCH"}:
                self.require_admin_mutation()
                self.edit_brand(brand_id)
                return
            if method == "DELETE":
                self.require_admin_mutation()
                self.delete_brand(brand_id)
                return
        if path == "/api/admin/banners" and method == "GET":
            self.require_admin()
            with connect_db() as conn:
                rows = conn.execute("SELECT * FROM banners ORDER BY created_at DESC").fetchall()
                self.send_json([banner_dict(r) for r in rows])
            return
        if path == "/api/admin/banners" and method == "POST":
            self.require_admin_mutation()
            self.create_banner()
            return
        if len(segments) == 4 and segments[:3] == ["api", "admin", "banners"]:
            banner_id = self.int_id(segments[3])
            if method in {"PUT", "PATCH"}:
                self.require_admin_mutation()
                self.edit_banner(banner_id)
                return
            if method == "DELETE":
                self.require_admin_mutation()
                self.delete_banner(banner_id)
                return
        if path == "/api/admin/policies" and method == "GET":
            self.require_admin()
            with connect_db() as conn:
                rows = conn.execute("SELECT * FROM policies ORDER BY slug").fetchall()
                self.send_json([format_policy(r) for r in rows])
            return
        if len(segments) == 4 and segments[:3] == ["api", "admin", "policies"]:
            slug = segments[3]
            if method in {"PUT", "PATCH", "POST"}:
                self.require_admin_mutation()
                self.edit_policy(slug)
                return
        if path == "/api/admin/products/import/preview" and method == "POST":
            self.require_admin_mutation()
            self.csv_preview()
            return
        if path == "/api/admin/products/import" and method == "POST":
            self.require_admin_mutation()
            self.csv_import()
            return
        if path == "/api/admin/products/export.csv" and method == "GET":
            self.require_admin()
            self.csv_export()
            return
        if path == "/api/admin/products/sample.csv" and method == "GET":
            self.require_admin()
            self.send_bytes(sample_csv_text().encode("utf-8-sig"), "text/csv; charset=utf-8", 200, {"Content-Disposition": "attachment; filename= taj-opticals-product-template.csv", "Cache-Control": "no-store"})
            return
        if len(segments) == 5 and segments[:3] == ["api", "admin", "products"] and segments[4] == "media" and method == "POST":
            self.require_admin_mutation()
            self.upload_product_media(self.int_id(segments[3]))
            return
        if len(segments) == 6 and segments[:3] == ["api", "admin", "products"] and segments[4] == "media" and segments[5] == "url" and method == "POST":
            self.require_admin_mutation()
            self.add_external_media(self.int_id(segments[3]))
            return
        if len(segments) == 6 and segments[:3] == ["api", "admin", "products"] and segments[4] == "media" and segments[5] == "reorder" and method == "POST":
            self.require_admin_mutation()
            self.reorder_media(self.int_id(segments[3]))
            return
        if len(segments) == 4 and segments[:3] == ["api", "admin", "media"] and method == "DELETE":
            self.require_admin_mutation()
            self.delete_media(self.int_id(segments[3]))
            return
        if path == "/api/admin/assets/images" and method == "POST":
            self.require_admin_mutation()
            self.upload_generic_image()
            return
        if path == "/api/admin/ai-video-assets" and method == "POST":
            session, _ = self.require_admin_mutation()
            self.upload_ai_asset(session)
            return
        if path == "/api/admin/ai-video-assets" and method == "GET":
            session, _ = self.require_admin()
            self.list_ai_assets(session)
            return
        if path == "/api/admin/video-jobs" and method == "GET":
            self.require_admin()
            self.list_video_jobs(query)
            return
        if path == "/api/admin/video-jobs" and method == "POST":
            self.require_admin_mutation()
            self.create_video_job()
            return
        if len(segments) == 4 and segments[:3] == ["api", "admin", "video-jobs"]:
            job_id = self.int_id(segments[3])
            if method == "GET":
                self.require_admin()
                self.refresh_video_job(job_id)
                return
        if len(segments) == 5 and segments[:3] == ["api", "admin", "video-jobs"] and segments[4] == "save-to-gallery" and method == "POST":
            self.require_admin_mutation()
            self.save_video_to_gallery(self.int_id(segments[3]))
            return
        if path == "/api/admin/video-capabilities" and method == "GET":
            self.require_admin()
            self.send_json({"provider": "Runway API", "configured": bool(os.environ.get("RUNWAYML_API_SECRET")), "model": os.environ.get("RUNWAY_MODEL", "gen4.5"), "duration_options": [2,3,4,5,6,8,10], "audio_supported": False, "setup_message": "Set RUNWAYML_API_SECRET on the server to enable paid video generation. Runway API use consumes provider credits; the app will not generate until an administrator explicitly confirms a paid request." if not os.environ.get("RUNWAYML_API_SECRET") else "Runway API credentials are present. Generation consumes provider credits and requires explicit confirmation."})
            return
        raise APIError(404, "API route not found.")

    def require_admin(self) -> tuple[sqlite3.Row, str]:
        return self.get_admin(require_csrf=False)

    def require_admin_mutation(self) -> tuple[sqlite3.Row, str]:
        return self.get_admin(require_csrf=True)

    def admin_csrf_value(self, session: sqlite3.Row) -> str:
        # During login a CSRF secret is generated and a non-HttpOnly same-site cookie is set.
        # The browser reads that cookie and submits it in X-CSRF-Token.
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            cookie = SimpleCookie()
        c = cookie.get("taj_csrf")
        if c:
            digest = hashlib.sha256(c.value.encode("utf-8")).hexdigest()
            if hmac.compare_digest(digest, session["csrf_hash"]):
                return c.value
        # Existing session is valid but its non-HttpOnly token may have been removed; rotate safely.
        csrf = secrets.token_urlsafe(32)
        with connect_db() as conn:
            conn.execute("UPDATE sessions SET csrf_hash=? WHERE token_hash=?", (hashlib.sha256(csrf.encode()).hexdigest(), session["token_hash"]))
            conn.commit()
        session_token = cookie.get("taj_session").value if cookie.get("taj_session") else ""
        secure = "; Secure" if (os.environ.get("COOKIE_SECURE") == "1" or self.headers.get("X-Forwarded-Proto", "").lower() == "https") else ""
        self._csrf_cookie = "taj_csrf=" + csrf + "; Path=/; SameSite=Lax; Max-Age=" + str(SESSION_DAYS * 86400) + secure
        # The handler adds this Set-Cookie header to the JSON response in the caller.
        return csrf

    def int_id(self, value: str) -> int:
        try:
            result = int(value)
            if result < 1:
                raise ValueError
            return result
        except (ValueError, TypeError):
            raise APIError(400, "Invalid record identifier.")

    def public_products(self, query: dict[str, list[str]]) -> None:
        q = (query.get("q") or query.get("search") or [""])[0].strip()
        category = (query.get("category") or [""])[0].strip()
        brand = (query.get("brand") or [""])[0].strip()
        sort = (query.get("sort") or ["newest"])[0]
        try:
            page = max(1, int((query.get("page") or ["1"])[0]))
            limit = min(48, max(1, int((query.get("limit") or ["12"])[0])))
        except ValueError:
            raise APIError(400, "Page and limit must be whole numbers.")
        params: list[Any] = []
        where = ["p.status='active'"]
        if q:
            where.append("(p.name LIKE ? OR p.sku LIKE ? OR p.description LIKE ? OR COALESCE(b.name,'') LIKE ?)")
            needle = "%" + q[:100] + "%"
            params.extend([needle, needle, needle, needle])
        if category:
            where.append("(c.slug=? OR CAST(c.id AS TEXT)=?)")
            params.extend([category, category])
        if brand:
            where.append("b.name=? COLLATE NOCASE")
            params.append(brand)
        if (query.get("featured") or [""])[0].strip().lower() in {"1", "true", "yes"}:
            where.append("p.featured=1")
        for key, operator in (("min_price", ">="), ("max_price", "<=")):
            if query.get(key):
                try:
                    amount = money_to_paise(query[key][0])
                except ValueError as exc:
                    raise APIError(400, str(exc))
                where.append(f"p.selling_price_paise {operator} ?")
                params.append(amount)
        order = {
            "price_asc": "p.selling_price_paise ASC, p.id DESC",
            "price_desc": "p.selling_price_paise DESC, p.id DESC",
            "newest": "p.created_at DESC, p.id DESC",
            "name": "p.name COLLATE NOCASE ASC",
        }.get(sort, "p.created_at DESC, p.id DESC")
        with connect_db() as conn:
            where_sql = " AND ".join(where)
            total = int(conn.execute("SELECT COUNT(*) FROM products p JOIN categories c ON c.id=p.category_id LEFT JOIN brands b ON b.id=p.brand_id WHERE " + where_sql, params).fetchone()[0])
            rows = conn.execute(product_select_sql(where_sql) + f" ORDER BY {order} LIMIT ? OFFSET ?", [*params, limit, (page - 1) * limit]).fetchall()
            data = [product_dict(conn, r) for r in rows]
        self.send_json({"products": data, "page": page, "limit": limit, "total": total, "pages": (total + limit - 1) // limit})

    def public_quote(self) -> None:
        data = self.read_json()
        items = data.get("items")
        if not isinstance(items, list) or len(items) > 60:
            raise APIError(400, "Add at least one valid cart item.")
        quantities: dict[int, int] = {}
        with connect_db() as conn:
            for item in items:
                if not isinstance(item, dict):
                    raise APIError(400, "Invalid cart item.")
                try:
                    product_id = int(item.get("product_id", item.get("id")))
                    qty_raw = item.get("quantity", item.get("qty", 1))
                    if isinstance(qty_raw, bool) or not re.fullmatch(r"\d+", str(qty_raw)):
                        raise ValueError
                    qty = int(qty_raw)
                except (ValueError, TypeError):
                    raise APIError(400, "Product and quantity must be valid whole numbers.")
                if product_id < 1 or qty < 1 or qty > 100:
                    raise APIError(400, "Quantity must be between 1 and 100.")
                quantities[product_id] = quantities.get(product_id, 0) + qty
            if not quantities:
                quote = calculate_delivery(0, int(get_setting(conn, "free_delivery_threshold_paise", 170000)), int(get_setting(conn, "standard_delivery_charge_paise", 5000)))
                self.send_json({"items": [], **quote})
                return
            result_items = []
            subtotal = 0
            for product_id, qty in quantities.items():
                row = conn.execute(product_select_sql("p.id=? AND p.status='active'"), (product_id,)).fetchone()
                if not row:
                    raise APIError(409, "A cart item is no longer available. Refresh the catalogue and try again.")
                if int(row["stock_qty"]) <= 0:
                    raise APIError(409, f"{row['name']} is out of stock.")
                if qty > int(row["stock_qty"]):
                    raise APIError(409, f"Only {row['stock_qty']} unit(s) of {row['name']} are currently in stock.")
                line_total = int(row["selling_price_paise"]) * qty
                subtotal += line_total
                cover = conn.execute("SELECT external_url,storage_key FROM media WHERE product_id=? AND kind='image' ORDER BY is_cover DESC,sort_order,id LIMIT 1", (row["id"],)).fetchone()
                image_url = (cover["external_url"] or ("/uploads/" + urllib.parse.quote(cover["storage_key"], safe=""))) if cover else ""
                result_items.append({"product_id": row["id"], "name": row["name"], "sku": row["sku"], "slug": row["slug"], "quantity": qty, "unit_price_paise": int(row["selling_price_paise"]), "line_subtotal_paise": line_total, "url": "/product/" + urllib.parse.quote(row["slug"], safe="-"), "image_url": image_url})
            quote = calculate_delivery(subtotal, int(get_setting(conn, "free_delivery_threshold_paise", 170000)), int(get_setting(conn, "standard_delivery_charge_paise", 5000)))
        self.send_json({"items": result_items, **quote})

    def admin_login(self) -> None:
        data = self.read_json()
        email = str(data.get("email", "")).strip().lower()
        password = str(data.get("password", ""))
        ip = self.client_address[0] if self.client_address else "unknown"
        now = time.time()
        with LOGIN_LOCK:
            attempts = [stamp for stamp in LOGIN_ATTEMPTS.get(ip, []) if now - stamp < 900]
            LOGIN_ATTEMPTS[ip] = attempts
            if len(attempts) >= 10:
                raise APIError(429, "Too many sign-in attempts. Please wait 15 minutes and try again.")
        with connect_db() as conn:
            row = conn.execute("SELECT * FROM admins WHERE email=? COLLATE NOCASE", (email,)).fetchone()
        if not row or not verify_password(password, row["password_salt"], row["password_hash"]):
            with LOGIN_LOCK:
                LOGIN_ATTEMPTS.setdefault(ip, []).append(now)
            raise APIError(401, "Email or password is incorrect.")
        with LOGIN_LOCK:
            LOGIN_ATTEMPTS.pop(ip, None)
        token = secrets.token_urlsafe(36)
        csrf = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        csrf_hash = hashlib.sha256(csrf.encode()).hexdigest()
        expiry = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=SESSION_DAYS)).replace(microsecond=0).isoformat()
        with connect_db() as conn:
            conn.execute("DELETE FROM sessions WHERE expires_at<?", (utc_now(),))
            conn.execute("INSERT INTO sessions(token_hash,csrf_hash,admin_id,expires_at,created_at) VALUES(?,?,?,?,?)", (token_hash, csrf_hash, row["id"], expiry, utc_now()))
            conn.commit()
        secure = "; Secure" if (os.environ.get("COOKIE_SECURE") == "1" or self.headers.get("X-Forwarded-Proto", "").lower() == "https") else ""
        headers = {
            "Set-Cookie": [
                "taj_session=" + token + "; Path=/; HttpOnly; SameSite=Lax; Max-Age=" + str(SESSION_DAYS * 86400) + secure,
                "taj_csrf=" + csrf + "; Path=/; SameSite=Lax; Max-Age=" + str(SESSION_DAYS * 86400) + secure,
            ],
            "Cache-Control": "no-store",
        }
        self.send_json({"admin": {"email": row["email"]}, "csrf_token": csrf}, 200, headers)

    def change_admin_password(self, session: sqlite3.Row, current_token_hash: str) -> None:
        data = self.read_json()
        current_password = str(data.get("current_password", ""))
        new_password = str(data.get("new_password", ""))
        confirm_password = str(data.get("confirm_password", ""))
        if len(new_password) < 12 or len(new_password) > 256:
            raise APIError(400, "New password must be between 12 and 256 characters.")
        if new_password != confirm_password:
            raise APIError(400, "New password confirmation does not match.")
        if current_password == new_password:
            raise APIError(400, "Choose a password different from the current one.")
        with connect_db() as conn:
            admin = conn.execute("SELECT * FROM admins WHERE id=?", (session["admin_id"],)).fetchone()
            if not admin or not verify_password(current_password, admin["password_salt"], admin["password_hash"]):
                raise APIError(403, "Current password is incorrect.")
            salt = secrets.token_bytes(16)
            digest = hashlib.pbkdf2_hmac("sha256", new_password.encode("utf-8"), salt, PBKDF2_ROUNDS)
            conn.execute("UPDATE admins SET password_salt=?,password_hash=? WHERE id=?", (salt.hex(), digest.hex(), session["admin_id"]))
            conn.execute("DELETE FROM sessions WHERE admin_id=? AND token_hash<>?", (session["admin_id"], current_token_hash))
            conn.commit()
        self.send_json({"ok": True, "message": "Password updated. Other admin sessions have been signed out."})

    def admin_logout(self) -> None:
        _, token_hash = self.require_admin_mutation()
        with connect_db() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))
            conn.commit()
        self.send_json({"ok": True}, 200, {"Set-Cookie": ["taj_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0", "taj_csrf=; Path=/; SameSite=Lax; Max-Age=0"]})

    def admin_dashboard(self) -> None:
        self.require_admin()
        with connect_db() as conn:
            data = {
                "products": int(conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]),
                "active_products": int(conn.execute("SELECT COUNT(*) FROM products WHERE status='active'").fetchone()[0]),
                "draft_products": int(conn.execute("SELECT COUNT(*) FROM products WHERE status='draft'").fetchone()[0]),
                "out_of_stock": int(conn.execute("SELECT COUNT(*) FROM products WHERE stock_qty=0 OR status='out_of_stock'").fetchone()[0]),
                "categories": int(conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0]),
                "processing_video_jobs": int(conn.execute("SELECT COUNT(*) FROM video_jobs WHERE status IN ('submitting','processing','queued')").fetchone()[0]),
                "recent_products": [product_dict(conn, r) for r in conn.execute(product_select_sql() + " ORDER BY p.created_at DESC LIMIT 5").fetchall()],
            }
        self.send_json(data)

    def admin_products(self, query: dict[str, list[str]]) -> None:
        q = (query.get("q") or [""])[0].strip()
        status = (query.get("status") or [""])[0].strip()
        where, params = [], []
        if q:
            where.append("(p.name LIKE ? OR p.sku LIKE ? OR COALESCE(b.name,'') LIKE ?)")
            needle = "%" + q[:100] + "%"
            params.extend([needle, needle, needle])
        if status in {"active", "draft", "out_of_stock"}:
            where.append("p.status=?")
            params.append(status)
        with connect_db() as conn:
            rows = conn.execute(product_select_sql(" AND ".join(where)) + " ORDER BY p.updated_at DESC", params).fetchall()
            self.send_json([product_dict(conn, r) for r in rows])

    def create_product(self) -> None:
        data = self.read_json()
        with connect_db() as conn:
            try:
                clean = validate_product(conn, data)
                product_id = insert_product(conn, clean)
                conn.commit()
            except ValueError as exc:
                raise APIError(400, str(exc))
            row = conn.execute(product_select_sql("p.id=?"), (product_id,)).fetchone()
            self.send_json(product_dict(conn, row), 201)

    def edit_product(self, product_id: int) -> None:
        data = self.read_json()
        with connect_db() as conn:
            if not conn.execute("SELECT 1 FROM products WHERE id=?", (product_id,)).fetchone():
                raise APIError(404, "Product not found.")
            try:
                clean = validate_product(conn, data, current_id=product_id)
                update_product(conn, product_id, clean)
                conn.commit()
            except ValueError as exc:
                raise APIError(400, str(exc))
            row = conn.execute(product_select_sql("p.id=?"), (product_id,)).fetchone()
            self.send_json(product_dict(conn, row))

    def delete_product(self, product_id: int) -> None:
        with connect_db() as conn:
            row = conn.execute("SELECT id FROM products WHERE id=?", (product_id,)).fetchone()
            if not row:
                raise APIError(404, "Product not found.")
            media = conn.execute("SELECT storage_key FROM media WHERE product_id=?", (product_id,)).fetchall()
            conn.execute("DELETE FROM products WHERE id=?", (product_id,))
            conn.commit()
        for m in media:
            if m["storage_key"]:
                try:
                    (uploads_path() / m["storage_key"]).unlink(missing_ok=True)
                except OSError:
                    pass
        self.send_json({"ok": True})

    def admin_categories(self) -> None:
        with connect_db() as conn:
            rows = conn.execute("SELECT c.*, (SELECT COUNT(*) FROM products p WHERE p.category_id=c.id) AS product_count FROM categories c ORDER BY sort_order,name").fetchall()
            self.send_json([{**category_dict(r), "product_count": r["product_count"]} for r in rows])

    def create_category(self) -> None:
        data = self.read_json()
        name = text_value(data.get("name"), 80, allow_empty=False)
        slug = text_value(data.get("slug") or slugify(name), 80, allow_empty=False)
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug.lower()):
            raise APIError(400, "Category slug may contain lowercase letters, numbers and hyphens.")
        try:
            sort_order = int(data.get("sort_order", 100))
        except (TypeError, ValueError):
            raise APIError(400, "Sort order must be a whole number.")
        with connect_db() as conn:
            cursor = conn.execute("INSERT INTO categories(slug,name,sort_order,created_at) VALUES(?,?,?,?)", (slug.lower(), name, sort_order, utc_now()))
            conn.commit()
            row = conn.execute("SELECT * FROM categories WHERE id=?", (cursor.lastrowid,)).fetchone()
            self.send_json(category_dict(row), 201)

    def edit_category(self, cat_id: int) -> None:
        data = self.read_json()
        name = text_value(data.get("name"), 80, allow_empty=False)
        slug = text_value(data.get("slug") or slugify(name), 80, allow_empty=False).lower()
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
            raise APIError(400, "Category slug may contain lowercase letters, numbers and hyphens.")
        try:
            sort_order = int(data.get("sort_order", 100))
        except (TypeError, ValueError):
            raise APIError(400, "Sort order must be a whole number.")
        with connect_db() as conn:
            if not conn.execute("SELECT 1 FROM categories WHERE id=?", (cat_id,)).fetchone():
                raise APIError(404, "Category not found.")
            conn.execute("UPDATE categories SET name=?,slug=?,sort_order=? WHERE id=?", (name, slug, sort_order, cat_id))
            conn.commit()
            row = conn.execute("SELECT * FROM categories WHERE id=?", (cat_id,)).fetchone()
            self.send_json(category_dict(row))

    def delete_category(self, cat_id: int) -> None:
        with connect_db() as conn:
            row = conn.execute("SELECT id FROM categories WHERE id=?", (cat_id,)).fetchone()
            if not row:
                raise APIError(404, "Category not found.")
            if conn.execute("SELECT 1 FROM products WHERE category_id=? LIMIT 1", (cat_id,)).fetchone():
                raise APIError(409, "This category still has products. Reassign or archive those products first.")
            conn.execute("DELETE FROM categories WHERE id=?", (cat_id,))
            conn.commit()
        self.send_json({"ok": True})

    def create_brand(self) -> None:
        data = self.read_json()
        try:
            name = text_value(data.get("name"), 120, allow_empty=False)
        except ValueError as exc:
            raise APIError(400, str(exc))
        with connect_db() as conn:
            cur = conn.execute("INSERT INTO brands(name,created_at) VALUES(?,?)", (name, utc_now()))
            conn.commit()
            row = conn.execute("SELECT id,name,(SELECT COUNT(*) FROM products WHERE brand_id=brands.id) AS product_count FROM brands WHERE id=?", (cur.lastrowid,)).fetchone()
            self.send_json(dict(row), 201)

    def edit_brand(self, brand_id: int) -> None:
        data = self.read_json()
        try:
            name = text_value(data.get("name"), 120, allow_empty=False)
        except ValueError as exc:
            raise APIError(400, str(exc))
        with connect_db() as conn:
            if not conn.execute("SELECT 1 FROM brands WHERE id=?", (brand_id,)).fetchone():
                raise APIError(404, "Brand not found.")
            conn.execute("UPDATE brands SET name=? WHERE id=?", (name, brand_id))
            conn.commit()
            row = conn.execute("SELECT id,name,(SELECT COUNT(*) FROM products WHERE brand_id=brands.id) AS product_count FROM brands WHERE id=?", (brand_id,)).fetchone()
            self.send_json(dict(row))

    def delete_brand(self, brand_id: int) -> None:
        with connect_db() as conn:
            row = conn.execute("SELECT id FROM brands WHERE id=?", (brand_id,)).fetchone()
            if not row:
                raise APIError(404, "Brand not found.")
            if conn.execute("SELECT 1 FROM products WHERE brand_id=? LIMIT 1", (brand_id,)).fetchone():
                raise APIError(409, "This brand is assigned to one or more products. Update those products first.")
            conn.execute("DELETE FROM brands WHERE id=?", (brand_id,))
            conn.commit()
        self.send_json({"ok": True})

    def update_settings(self) -> None:
        data = self.read_json()
        fields = {"business_name": (120, False), "tagline": (240, True), "address": (500, False), "maps_url": (1000, False), "contact_phone": (40, True), "logo_url": (2048, True)}
        clean: dict[str, Any] = {}
        try:
            for key, (limit, optional) in fields.items():
                if key in data:
                    clean[key] = text_value(data[key], limit, allow_empty=optional)
            if "maps_url" in clean:
                parsed = urllib.parse.urlparse(clean["maps_url"])
                if parsed.scheme != "https" or parsed.netloc not in {"maps.google.com", "www.google.com", "maps.app.goo.gl"}:
                    raise ValueError("Use the supplied Google Maps HTTPS link or another Google Maps link.")
            if "contact_phone" in clean and clean["contact_phone"] and not re.fullmatch(r"[+0-9() .-]{7,40}", clean["contact_phone"]):
                raise ValueError("Enter a valid contact phone number.")
            if "logo_url" in clean and clean["logo_url"] and not clean["logo_url"].startswith("/uploads/"):
                clean["logo_url"] = valid_external_url(clean["logo_url"])
            if "whatsapp_number" in data or "whatsapp_verified" in data:
                number = re.sub(r"\D", "", str(data.get("whatsapp_number", "")))
                verified = parse_bool(data.get("whatsapp_verified", False))
                if verified and not (11 <= len(number) <= 15):
                    raise ValueError("Enter an international WhatsApp number including country code (11 to 15 digits), then confirm it is registered on WhatsApp.")
                clean["whatsapp_number"] = number if verified else ""
                clean["whatsapp_verified"] = bool(verified and number)
            if "free_delivery_threshold_inr" in data:
                threshold = money_to_paise(data["free_delivery_threshold_inr"])
                if threshold is None or threshold <= 0:
                    raise ValueError("Free-delivery threshold must be greater than ₹0.")
                clean["free_delivery_threshold_paise"] = threshold
            if "standard_delivery_charge_inr" in data:
                charge = money_to_paise(data["standard_delivery_charge_inr"])
                if charge is None:
                    raise ValueError("Enter a valid delivery charge.")
                clean["standard_delivery_charge_paise"] = charge
        except ValueError as exc:
            raise APIError(400, str(exc))
        allowed = set(default_settings())
        with connect_db() as conn:
            for key, value in clean.items():
                if key in allowed:
                    set_setting(conn, key, value)
            conn.commit()
            settings = {k: get_setting(conn, k, v) for k, v in default_settings().items()}
        self.send_json({"settings": settings, "public": public_settings_from_values(settings)})

    def csv_preview(self) -> None:
        data = self.read_json()
        csv_text = data.get("csv", "")
        if not isinstance(csv_text, str):
            raise APIError(400, "CSV data is required.")
        with connect_db() as conn:
            rows, errors = parse_csv_import(conn, csv_text)
        self.send_json({"valid_count": len(rows), "error_count": len(errors), "preview": [{k: v for k, v in row.items() if k != "clean"} for row in rows[:20]], "errors": errors})

    def csv_import(self) -> None:
        data = self.read_json()
        csv_text = data.get("csv", "")
        if not isinstance(csv_text, str):
            raise APIError(400, "CSV data is required.")
        with connect_db() as conn:
            rows, errors = parse_csv_import(conn, csv_text)
            inserted = 0
            for row in rows:
                try:
                    insert_product(conn, row["clean"])
                    inserted += 1
                except sqlite3.IntegrityError:
                    errors.append({"row": row["row"], "sku": row["sku"], "message": "SKU was duplicated during import and was not added."})
            conn.commit()
        self.send_json({"imported": inserted, "failed": len(errors), "errors": errors, "message": f"Imported {inserted} product(s); {len(errors)} row(s) need attention."})

    def csv_export(self) -> None:
        with connect_db() as conn:
            rows = conn.execute(product_select_sql() + " ORDER BY p.id").fetchall()
            out = io.StringIO()
            writer = csv.writer(out)
            writer.writerow(["sku","name","category","brand","description","selling_price_inr","original_price_inr","stock_qty","status","featured","color","frame_shape","frame_size","material","specifications_json"])
            for row in rows:
                product = product_dict(conn, row)
                writer.writerow([product["sku"], product["name"], product["category"]["name"], product["brand"], product["description"], f"{product['selling_price_paise']/100:.2f}", (f"{product['original_price_paise']/100:.2f}" if product["original_price_paise"] is not None else ""), product["stock_qty"], product["status"], str(product["featured"]).lower(), product["color"], product["frame_shape"], product["frame_size"], product["material"], json.dumps(product["specifications"], ensure_ascii=False)])
        self.send_bytes(out.getvalue().encode("utf-8-sig"), "text/csv; charset=utf-8", 200, {"Content-Disposition": "attachment; filename=taj-opticals-products.csv", "Cache-Control": "no-store"})

    def upload_file_parts(self, parts: list[tuple[str, str | None, str, bytes]], *, expected_kind: str, max_files: int = 12) -> list[tuple[str, str, str, bytes, str]]:
        file_parts = [p for p in parts if p[1] and p[3]]
        if not file_parts:
            raise APIError(400, "Choose at least one file to upload.")
        if len(file_parts) > max_files:
            raise APIError(400, f"Upload no more than {max_files} files at a time.")
        results = []
        for _, filename, declared_mime, payload in file_parts:
            original = Path(filename or "upload").name[:180]
            ext = Path(original).suffix.lower()
            image_exts = {".jpg", ".jpeg", ".png", ".webp"}
            video_exts = {".mp4", ".webm"}
            if ext in image_exts:
                kind = "image"
                if ext in {".jpg", ".jpeg"} and not payload.startswith(b"\xff\xd8\xff"):
                    raise APIError(400, f"{original}: file does not look like a valid JPEG.")
                if ext == ".png" and not payload.startswith(b"\x89PNG\r\n\x1a\n"):
                    raise APIError(400, f"{original}: file does not look like a valid PNG.")
                if ext == ".webp" and not (payload.startswith(b"RIFF") and payload[8:12] == b"WEBP"):
                    raise APIError(400, f"{original}: file does not look like a valid WebP image.")
                mime = {".jpg":"image/jpeg", ".jpeg":"image/jpeg", ".png":"image/png", ".webp":"image/webp"}[ext]
                limit = MAX_IMAGE_BYTES
            elif ext in video_exts:
                kind = "video"
                if ext == ".mp4" and (len(payload) < 12 or payload[4:8] != b"ftyp"):
                    raise APIError(400, f"{original}: file does not look like a valid MP4 video.")
                if ext == ".webm" and not payload.startswith(b"\x1a\x45\xdf\xa3"):
                    raise APIError(400, f"{original}: file does not look like a valid WebM video.")
                mime = "video/mp4" if ext == ".mp4" else "video/webm"
                limit = MAX_VIDEO_BYTES
            else:
                raise APIError(415, f"{original}: supported formats are JPG, PNG, WebP, MP4 and WebM.")
            if expected_kind != "any" and kind != expected_kind:
                raise APIError(415, f"{original}: choose a {expected_kind} file.")
            if len(payload) > limit:
                raise APIError(413, f"{original}: file exceeds the {limit // (1024*1024)} MB limit.")
            results.append((original, kind, mime, payload, ext))
        return results

    def write_upload(self, payload: bytes, ext: str) -> str:
        key = secrets.token_hex(20) + ext
        path = uploads_path() / key
        path.write_bytes(payload)
        return key

    def upload_product_media(self, product_id: int) -> None:
        body = self.read_body()
        parts = self.parse_parts(body)
        kind_value = next((p[3].decode("utf-8", "ignore").strip().lower() for p in parts if p[0] == "kind" and not p[1]), "any")
        if kind_value not in {"image", "video", "any"}:
            raise APIError(400, "Media kind must be image or video.")
        uploads = self.upload_file_parts(parts, expected_kind=kind_value, max_files=12)
        with connect_db() as conn:
            if not conn.execute("SELECT 1 FROM products WHERE id=?", (product_id,)).fetchone():
                raise APIError(404, "Product not found.")
            saved = []
            for original, kind, mime, payload, ext in uploads:
                key = self.write_upload(payload, ext)
                sort = conn.execute("SELECT COALESCE(MAX(sort_order),-1)+1 FROM media WHERE product_id=? AND kind=?", (product_id, kind)).fetchone()[0]
                cover = 1 if kind == "image" and not conn.execute("SELECT 1 FROM media WHERE product_id=? AND kind='image' AND is_cover=1", (product_id,)).fetchone() else 0
                cursor = conn.execute("INSERT INTO media(product_id,kind,storage_key,external_url,original_name,mime_type,byte_size,sort_order,is_cover,created_at) VALUES(?,?,?,NULL,?,?,?,?,?,?)", (product_id, kind, key, original, mime, len(payload), sort, cover, utc_now()))
                saved.append(cursor.lastrowid)
            conn.commit()
            rows = conn.execute("SELECT * FROM media WHERE id IN (" + ",".join("?" for _ in saved) + ") ORDER BY sort_order", saved).fetchall()
            self.send_json([media_dict(r) for r in rows], 201)

    def add_external_media(self, product_id: int) -> None:
        data = self.read_json()
        try:
            url = valid_external_url(data.get("url"))
            kind = str(data.get("kind", "video")).lower()
            if kind not in {"image", "video"}:
                raise ValueError("Media type must be image or video.")
            if kind == "image" and not re.search(r"\.(jpg|jpeg|png|webp)(?:[?#].*)?$", url, re.I):
                raise ValueError("External image URL should end in JPG, PNG or WebP.")
            if kind == "video" and not re.search(r"\.(mp4|webm)(?:[?#].*)?$", url, re.I):
                raise ValueError("External video URL should end in MP4 or WebM.")
        except ValueError as exc:
            raise APIError(400, str(exc))
        with connect_db() as conn:
            if not conn.execute("SELECT 1 FROM products WHERE id=?", (product_id,)).fetchone():
                raise APIError(404, "Product not found.")
            order = conn.execute("SELECT COALESCE(MAX(sort_order),-1)+1 FROM media WHERE product_id=? AND kind=?", (product_id, kind)).fetchone()[0]
            cursor = conn.execute("INSERT INTO media(product_id,kind,external_url,original_name,mime_type,byte_size,sort_order,is_cover,created_at) VALUES(?,?,?,?,'',0,?,0,?)", (product_id, kind, url, "External media", order, utc_now()))
            conn.commit()
            row = conn.execute("SELECT * FROM media WHERE id=?", (cursor.lastrowid,)).fetchone()
            self.send_json(media_dict(row), 201)

    def reorder_media(self, product_id: int) -> None:
        data = self.read_json()
        ids = data.get("ids")
        cover_id = data.get("cover_id")
        if not isinstance(ids, list) or len(ids) > 100:
            raise APIError(400, "Provide a list of media identifiers.")
        try:
            ids = [int(x) for x in ids]
            cover_id = int(cover_id) if cover_id is not None else None
        except (TypeError, ValueError):
            raise APIError(400, "Media identifiers must be whole numbers.")
        with connect_db() as conn:
            rows = conn.execute("SELECT id,kind FROM media WHERE product_id=?", (product_id,)).fetchall()
            by_id = {r["id"]: r["kind"] for r in rows}
            if any(i not in by_id for i in ids) or set(ids) != set(by_id):
                raise APIError(400, "The media list does not match this product.")
            if cover_id is not None and (cover_id not in by_id or by_id[cover_id] != "image"):
                raise APIError(400, "Cover image must be an image belonging to this product.")
            for index, media_id in enumerate(ids):
                conn.execute("UPDATE media SET sort_order=? WHERE id=?", (index, media_id))
                if by_id[media_id] == "image":
                    conn.execute("UPDATE media SET is_cover=? WHERE id=?", (1 if media_id == cover_id else 0, media_id))
            if cover_id is None:
                first = conn.execute("SELECT id FROM media WHERE product_id=? AND kind='image' ORDER BY sort_order LIMIT 1", (product_id,)).fetchone()
                conn.execute("UPDATE media SET is_cover=0 WHERE product_id=? AND kind='image'", (product_id,))
                if first:
                    conn.execute("UPDATE media SET is_cover=1 WHERE id=?", (first["id"],))
            conn.commit()
            updated = conn.execute("SELECT * FROM media WHERE product_id=? ORDER BY kind,sort_order", (product_id,)).fetchall()
            self.send_json([media_dict(r) for r in updated])

    def delete_media(self, media_id: int) -> None:
        with connect_db() as conn:
            row = conn.execute("SELECT * FROM media WHERE id=?", (media_id,)).fetchone()
            if not row:
                raise APIError(404, "Media not found.")
            conn.execute("DELETE FROM media WHERE id=?", (media_id,))
            if row["kind"] == "image" and row["is_cover"]:
                next_row = conn.execute("SELECT id FROM media WHERE product_id=? AND kind='image' ORDER BY sort_order LIMIT 1", (row["product_id"],)).fetchone()
                if next_row:
                    conn.execute("UPDATE media SET is_cover=1 WHERE id=?", (next_row["id"],))
            conn.commit()
        if row["storage_key"]:
            (uploads_path() / row["storage_key"]).unlink(missing_ok=True)
        self.send_json({"ok": True})

    def upload_generic_image(self) -> None:
        parts = self.parse_parts(self.read_body())
        uploads = self.upload_file_parts(parts, expected_kind="image", max_files=1)
        original, kind, mime, payload, ext = uploads[0]
        key = self.write_upload(payload, ext)
        self.send_json({"url": "/uploads/" + urllib.parse.quote(key, safe=""), "filename": original, "mime_type": mime}, 201)

    def upload_ai_asset(self, session: sqlite3.Row) -> None:
        parts = self.parse_parts(self.read_body())
        uploads = self.upload_file_parts(parts, expected_kind="image", max_files=6)
        saved = []
        with connect_db() as conn:
            for original, kind, mime, payload, ext in uploads:
                key = self.write_upload(payload, ext)
                cursor = conn.execute("INSERT INTO ai_assets(admin_id,storage_key,original_name,mime_type,byte_size,created_at) VALUES(?,?,?,?,?,?)", (session["admin_id"], key, original, mime, len(payload), utc_now()))
                saved.append({"id": cursor.lastrowid, "url": "/uploads/" + urllib.parse.quote(key, safe=""), "filename": original, "mime_type": mime})
            conn.commit()
        self.send_json(saved, 201)

    def list_ai_assets(self, session: sqlite3.Row) -> None:
        with connect_db() as conn:
            rows = conn.execute("SELECT * FROM ai_assets WHERE admin_id=? ORDER BY created_at DESC LIMIT 50", (session["admin_id"],)).fetchall()
            self.send_json([{ "id": r["id"], "url": "/uploads/" + urllib.parse.quote(r["storage_key"], safe=""), "filename": r["original_name"], "mime_type": r["mime_type"], "byte_size": r["byte_size"] } for r in rows])

    def create_banner(self) -> None:
        data = self.read_json()
        clean = self.validate_banner(data)
        now = utc_now()
        with connect_db() as conn:
            cur = conn.execute("INSERT INTO banners(headline,description,image_url,product_id,start_date,end_date,cta_text,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (*clean, now, now))
            conn.commit()
            row = conn.execute("SELECT * FROM banners WHERE id=?", (cur.lastrowid,)).fetchone()
            self.send_json(banner_dict(row), 201)

    def validate_banner(self, data: dict[str, Any]) -> tuple[Any, ...]:
        try:
            headline = text_value(data.get("headline"), 160, allow_empty=False)
            description = text_value(data.get("description"), 800)
            image_url = text_value(data.get("image_url"), 2048)
            if image_url and not (image_url.startswith("/uploads/") or valid_external_url(image_url)):
                raise ValueError("Banner image must be an uploaded file or HTTPS URL.")
            product_id = data.get("product_id") or None
            if product_id is not None:
                product_id = int(product_id)
            start_date = text_value(data.get("start_date"), 10) or None
            end_date = text_value(data.get("end_date"), 10) or None
            for date_value in (start_date, end_date):
                if date_value:
                    dt.date.fromisoformat(date_value)
            if start_date and end_date and start_date > end_date:
                raise ValueError("End date cannot be before start date.")
            cta = text_value(data.get("cta_text", "Shop now"), 80) or "Shop now"
            status = "active" if data.get("status") == "active" else "draft"
            if status == "active" and not image_url:
                raise ValueError("Upload a banner image before publishing this promotion.")
            if product_id is not None:
                with connect_db() as conn:
                    linked = conn.execute("SELECT status FROM products WHERE id=?", (product_id,)).fetchone()
                if not linked:
                    raise ValueError("Choose a product that exists in the catalogue.")
                if status == "active" and linked["status"] != "active":
                    raise ValueError("Publish the associated product before activating this promotion.")
            return headline, description, image_url, product_id, start_date, end_date, cta, status
        except (ValueError, TypeError) as exc:
            raise APIError(400, str(exc))

    def edit_banner(self, banner_id: int) -> None:
        data = self.read_json()
        clean = self.validate_banner(data)
        with connect_db() as conn:
            if not conn.execute("SELECT 1 FROM banners WHERE id=?", (banner_id,)).fetchone():
                raise APIError(404, "Promotion not found.")
            conn.execute("UPDATE banners SET headline=?,description=?,image_url=?,product_id=?,start_date=?,end_date=?,cta_text=?,status=?,updated_at=? WHERE id=?", (*clean, utc_now(), banner_id))
            conn.commit()
            row = conn.execute("SELECT * FROM banners WHERE id=?", (banner_id,)).fetchone()
            self.send_json(banner_dict(row))

    def delete_banner(self, banner_id: int) -> None:
        with connect_db() as conn:
            if not conn.execute("DELETE FROM banners WHERE id=?", (banner_id,)).rowcount:
                raise APIError(404, "Promotion not found.")
            conn.commit()
        self.send_json({"ok": True})

    def edit_policy(self, slug: str) -> None:
        data = self.read_json()
        try:
            title = text_value(data.get("title"), 120, allow_empty=False)
            heading = text_value(data.get("heading"), 180, allow_empty=False)
            body = text_value(data.get("body"), 12000, allow_empty=False)
            status = "published" if data.get("status") == "published" else "draft"
        except ValueError as exc:
            raise APIError(400, str(exc))
        with connect_db() as conn:
            if not conn.execute("SELECT 1 FROM policies WHERE slug=?", (slug,)).fetchone():
                raise APIError(404, "Page not found.")
            conn.execute("UPDATE policies SET title=?,heading=?,body=?,status=?,updated_at=? WHERE slug=?", (title, heading, body, status, utc_now(), slug))
            conn.commit()
            row = conn.execute("SELECT * FROM policies WHERE slug=?", (slug,)).fetchone()
            self.send_json(format_policy(row))

    def list_video_jobs(self, query: dict[str, list[str]]) -> None:
        product_id = (query.get("product_id") or [""])[0]
        with connect_db() as conn:
            if product_id.isdigit():
                rows = conn.execute("SELECT * FROM video_jobs WHERE product_id=? ORDER BY created_at DESC LIMIT 50", (int(product_id),)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM video_jobs ORDER BY created_at DESC LIMIT 50").fetchall()
            self.send_json([serialize_video_job(r) for r in rows])

    def create_video_job(self) -> None:
        data = self.read_json()
        if not os.environ.get("RUNWAYML_API_SECRET"):
            raise APIError(503, "AI video generation is not configured. Set RUNWAYML_API_SECRET on the server after creating a Runway Dev account. No generation was submitted and no credits were used.")
        if not parse_bool(data.get("confirm_paid_generation")):
            raise APIError(400, "Confirm that you understand this request may consume paid provider credits.")
        try:
            product_id = int(data.get("product_id"))
            duration = int(data.get("duration", 5))
            if duration not in {2, 3, 4, 5, 6, 8, 10}:
                raise ValueError("Choose a supported duration from 2 to 10 seconds.")
            formats = {"vertical": "720:1280", "square": "960:960", "landscape": "1280:720"}
            format_key = str(data.get("format", "vertical"))
            if format_key not in formats:
                raise ValueError("Choose vertical, square or landscape format.")
            styles = {"premium_luxury": "premium luxury editorial lighting", "modern_fashion": "modern fashion campaign, clean studio motion", "minimal": "minimal soft-light product showcase", "festive": "warm festive seasonal atmosphere, tasteful and uncluttered", "cinematic": "cinematic product showcase with gentle camera movement"}
            style_key = str(data.get("style", "cinematic"))
            if style_key not in styles:
                raise ValueError("Choose a supported visual style.")
            headline = text_value(data.get("headline"), 160)
            description = text_value(data.get("description"), 800)
            request_key = text_value(data.get("request_key"), 80, allow_empty=False)
            if not re.fullmatch(r"[A-Za-z0-9-]{16,80}", request_key):
                raise ValueError("Invalid idempotency key.")
            image_source = data.get("image_source") or {}
            if not isinstance(image_source, dict):
                raise ValueError("Select a product image or uploaded AI image.")
            source_type = image_source.get("type")
            source_id = int(image_source.get("id"))
        except (ValueError, TypeError) as exc:
            raise APIError(400, str(exc))
        with connect_db() as conn:
            existing = conn.execute("SELECT * FROM video_jobs WHERE request_key=?", (request_key,)).fetchone()
            if existing:
                self.send_json(serialize_video_job(existing), 200)
                return
            product = conn.execute(product_select_sql("p.id=?"), (product_id,)).fetchone()
            if not product:
                raise APIError(404, "Product not found.")
            if source_type == "product_media":
                media = conn.execute("SELECT * FROM media WHERE id=? AND product_id=? AND kind='image'", (source_id, product_id)).fetchone()
                if not media or not media["storage_key"]:
                    raise APIError(400, "Choose an uploaded product image. External URLs cannot be used as an AI source image.")
                source_path = uploads_path() / media["storage_key"]
                original = media["original_name"]
                mime = media["mime_type"]
                source_media = [{"type": "product_media", "id": source_id, "filename": original}]
            elif source_type == "ai_asset":
                media = conn.execute("SELECT * FROM ai_assets WHERE id=?", (source_id,)).fetchone()
                if not media:
                    raise APIError(400, "Uploaded AI source image was not found.")
                source_path = uploads_path() / media["storage_key"]
                original = media["original_name"]
                mime = media["mime_type"]
                source_media = [{"type": "ai_asset", "id": source_id, "filename": original}]
            else:
                raise APIError(400, "Choose a product image or upload an image in AI Video Studio.")
            if not source_path.is_file() or source_path.stat().st_size > MAX_IMAGE_BYTES:
                raise APIError(400, "The selected image is missing or exceeds the generation upload limit.")
            image_bytes = source_path.read_bytes()
            mime = mime if mime in {"image/jpeg", "image/png", "image/webp"} else mimetypes.guess_type(source_path.name)[0] or "image/jpeg"
            data_uri = f"data:{mime};base64," + base64.b64encode(image_bytes).decode("ascii")
            price_label = f"₹{int(product['selling_price_paise']) / 100:,.2f}"
            # Prompt text is limited to verified product identity plus owner-entered wording; AI is told not to invent claims.
            copy_text = " ".join(x for x in (headline, description) if x)
            prompt = f"Create a short {styles[style_key]} for the optical product {product['name']}. Keep the supplied product's shape, materials, colour and details faithful to the reference image. Use subtle camera movement and polished studio lighting. Do not add text, prices, logos, discounts, medical claims, extra product features, or people. Owner-approved copy for context only: {copy_text or 'none'}. Product price in the database for owner reference: {price_label}. Do not render or alter this price."
            settings_obj = {"format": format_key, "ratio": formats[format_key], "duration": duration, "style": style_key, "headline": headline, "description": description, "model": os.environ.get("RUNWAY_MODEL", "gen4.5"), "audio_supported": False}
            now = utc_now()
            try:
                cursor = conn.execute("INSERT INTO video_jobs(request_key,product_id,provider,status,prompt,settings_json,source_media_json,created_at,updated_at) VALUES(?,?,'runway','submitting',?,?,?,?,?)", (request_key, product_id, prompt, json.dumps(settings_obj, ensure_ascii=False), json.dumps(source_media, ensure_ascii=False), now, now))
                job_id = cursor.lastrowid
                conn.commit()
            except sqlite3.IntegrityError:
                row = conn.execute("SELECT * FROM video_jobs WHERE request_key=?", (request_key,)).fetchone()
                self.send_json(serialize_video_job(row), 200)
                return
        # Upstream call is intentionally made only after explicit paid-generation confirmation and only once per request key.
        try:
            task_response = self.runway_request("POST", "/image_to_video", {"model": os.environ.get("RUNWAY_MODEL", "gen4.5"), "promptImage": data_uri, "promptText": prompt, "ratio": settings_obj["ratio"], "duration": duration}, timeout=50)
            task_id = task_response.get("id") or task_response.get("task_id")
            if not task_id:
                raise RuntimeError("Provider did not return a task identifier.")
            with connect_db() as conn:
                conn.execute("UPDATE video_jobs SET provider_task_id=?,status='processing',progress=?,updated_at=? WHERE id=?", (task_id, task_response.get("progress"), utc_now(), job_id))
                conn.commit()
                row = conn.execute("SELECT * FROM video_jobs WHERE id=?", (job_id,)).fetchone()
            self.send_json(serialize_video_job(row), 201)
        except Exception as exc:
            safe_message = str(exc)[:400]
            with connect_db() as conn:
                conn.execute("UPDATE video_jobs SET status='failed',error_message=?,updated_at=? WHERE id=?", (safe_message, utc_now(), job_id))
                conn.commit()
            raise APIError(502, "The video provider did not accept the request. Check the provider dashboard before retrying; an upstream timeout may have an unknown charge status.")

    def runway_request(self, method: str, path: str, payload: dict[str, Any] | None = None, timeout: int = 30) -> dict[str, Any]:
        secret = os.environ.get("RUNWAYML_API_SECRET", "")
        if not secret:
            raise APIError(503, "Runway credentials are not configured.")
        url = VIDEO_API + path
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Authorization": "Bearer " + secret, "X-Runway-Version": VIDEO_API_VERSION, "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = response.read(2 * 1024 * 1024)
                decoded = json.loads(data.decode("utf-8"))
                if not isinstance(decoded, dict):
                    raise RuntimeError("Provider returned an invalid response.")
                return decoded
        except urllib.error.HTTPError as exc:
            details = exc.read(4096).decode("utf-8", "replace")
            raise RuntimeError(f"Runway API returned HTTP {exc.code}: {details}")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError("Runway API request failed: " + str(exc))

    def refresh_video_job(self, job_id: int) -> None:
        with connect_db() as conn:
            row = conn.execute("SELECT * FROM video_jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise APIError(404, "Video job not found.")
            should_check = row["provider_task_id"] and row["status"] in {"submitting", "queued", "processing"}
            recent = False
            if row["last_checked_at"]:
                try:
                    checked = dt.datetime.fromisoformat(row["last_checked_at"])
                    recent = (dt.datetime.now(dt.timezone.utc) - checked).total_seconds() < 5
                except ValueError:
                    pass
            if not should_check or recent or not os.environ.get("RUNWAYML_API_SECRET"):
                self.send_json(serialize_video_job(row))
                return
            task_id = row["provider_task_id"]
            conn.execute("UPDATE video_jobs SET last_checked_at=? WHERE id=?", (utc_now(), job_id))
            conn.commit()
        try:
            task = self.runway_request("GET", "/tasks/" + urllib.parse.quote(task_id, safe=""), None, timeout=25)
            provider_status = str(task.get("status", "processing")).lower()
            mapping = {"pending": "queued", "running": "processing", "processing": "processing", "succeeded": "completed", "completed": "completed", "failed": "failed", "canceled": "failed", "cancelled": "failed"}
            status = mapping.get(provider_status, "processing")
            output = task.get("output")
            output_url = output[0] if isinstance(output, list) and output else task.get("output_url")
            if isinstance(output_url, dict):
                output_url = output_url.get("url")
            if output_url and not isinstance(output_url, str):
                output_url = None
            progress = task.get("progress")
            try:
                progress = max(0, min(100, int(progress))) if progress is not None else None
            except (TypeError, ValueError):
                progress = None
            error = task.get("failure") or task.get("error")
            if isinstance(error, dict):
                error = error.get("message") or "Video generation failed."
            error = str(error)[:400] if error else None
            with connect_db() as conn:
                conn.execute("UPDATE video_jobs SET status=?,progress=?,output_url=?,error_message=?,updated_at=? WHERE id=?", (status, progress, output_url if status == "completed" else None, error, utc_now(), job_id))
                conn.commit()
                row = conn.execute("SELECT * FROM video_jobs WHERE id=?", (job_id,)).fetchone()
            self.send_json(serialize_video_job(row))
        except Exception as exc:
            with connect_db() as conn:
                row = conn.execute("SELECT * FROM video_jobs WHERE id=?", (job_id,)).fetchone()
            self.send_json({**serialize_video_job(row), "poll_warning": "Could not refresh provider status just now; try again shortly."})

    def safe_download_video(self, url: str) -> tuple[bytes, str]:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise APIError(400, "Provider returned an invalid HTTPS video URL.")
        host = parsed.hostname.lower()
        if not (host == "runwayml.com" or host.endswith(".runwayml.com") or host == "runwayusercontent.com" or host.endswith(".runwayusercontent.com")):
            raise APIError(400, "The provider output host is not on the allowed download list.")
        # Resolve and reject private-network destinations before fetching.
        try:
            for info in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM):
                ip = ipaddress.ip_address(info[4][0])
                if not ip.is_global:
                    raise APIError(400, "The provider output resolved to a private network.")
        except socket.gaierror:
            raise APIError(502, "Could not resolve the provider video URL.")
        request = urllib.request.Request(url, headers={"User-Agent": "TajOpticals/1.0"})
        try:
            with urllib.request.urlopen(request, timeout=50) as response:
                final = urllib.parse.urlparse(response.geturl())
                if final.scheme != "https" or not final.hostname or not (final.hostname.endswith(".runwayml.com") or final.hostname.endswith(".runwayusercontent.com") or final.hostname in {"runwayml.com", "runwayusercontent.com"}):
                    raise APIError(400, "Provider redirected to an unapproved host.")
                ctype = response.headers.get_content_type()
                if ctype not in {"video/mp4", "application/octet-stream"}:
                    raise APIError(415, "Provider output is not an MP4 video.")
                length = response.headers.get("Content-Length")
                if length and int(length) > MAX_VIDEO_BYTES:
                    raise APIError(413, "Generated video exceeds the 100 MB storage limit.")
                payload = response.read(MAX_VIDEO_BYTES + 1)
                if len(payload) > MAX_VIDEO_BYTES or len(payload) < 12 or payload[4:8] != b"ftyp":
                    raise APIError(415, "Downloaded output is not a supported MP4 video under 100 MB.")
                return payload, "video/mp4"
        except APIError:
            raise
        except Exception as exc:
            raise APIError(502, "Could not download generated video: " + str(exc)[:250])

    def save_video_to_gallery(self, job_id: int) -> None:
        with connect_db() as conn:
            row = conn.execute("SELECT * FROM video_jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise APIError(404, "Video job not found.")
            if row["status"] != "completed" or not row["output_url"]:
                raise APIError(409, "The video is not ready to save.")
            if row["saved_media_id"]:
                media = conn.execute("SELECT * FROM media WHERE id=?", (row["saved_media_id"],)).fetchone()
                self.send_json({"media": media_dict(media), "already_saved": True})
                return
            url = row["output_url"]
            product_id = row["product_id"]
        payload, mime = self.safe_download_video(url)
        key = self.write_upload(payload, ".mp4")
        with connect_db() as conn:
            order = conn.execute("SELECT COALESCE(MAX(sort_order),-1)+1 FROM media WHERE product_id=? AND kind='video'", (product_id,)).fetchone()[0]
            cur = conn.execute("INSERT INTO media(product_id,kind,storage_key,original_name,mime_type,byte_size,sort_order,is_cover,created_at) VALUES(?,'video',?,?,?,?,?,0,?)", (product_id, key, f"AI promo {job_id}.mp4", mime, len(payload), order, utc_now()))
            media_id = cur.lastrowid
            conn.execute("UPDATE video_jobs SET saved_media_id=?,updated_at=? WHERE id=?", (media_id, utc_now(), job_id))
            conn.commit()
            media = conn.execute("SELECT * FROM media WHERE id=?", (media_id,)).fetchone()
        self.send_json({"media": media_dict(media), "already_saved": False}, 201)


def public_settings_from_values(values: dict[str, Any]) -> dict[str, Any]:
    verified = bool(values.get("whatsapp_verified")) and bool(values.get("whatsapp_number"))
    return {"business_name": values.get("business_name", "TAJ OPTICALS"), "tagline": values.get("tagline", "High Quality, Low Cost"), "address": values.get("address", ""), "maps_url": values.get("maps_url", ""), "contact_phone": values.get("contact_phone", ""), "whatsapp_number": values.get("whatsapp_number", "") if verified else "", "whatsapp_ready": verified, "logo_url": values.get("logo_url", ""), "delivery": {"free_delivery_threshold_paise": int(values.get("free_delivery_threshold_paise", 170000)), "standard_delivery_charge_paise": int(values.get("standard_delivery_charge_paise", 5000))}}


def run_server(host: str = "0.0.0.0", port: int = 8000) -> ThreadingHTTPServer:
    ensure_schema()
    httpd = ThreadingHTTPServer((host, port), TajHandler)
    httpd.daemon_threads = True
    print(f"Taj Opticals listening on http://{host}:{port} — database: {db_path()}", flush=True)
    return httpd


def cli_create_admin() -> None:
    import getpass
    email = os.environ.get("ADMIN_EMAIL") or input("Admin email: ").strip()
    password = os.environ.get("ADMIN_PASSWORD") or getpass.getpass("New admin password (12+ characters): ")
    if not os.environ.get("ADMIN_PASSWORD"):
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            raise SystemExit("Passwords did not match.")
    try:
        create_admin_account(email, password)
    except (ValueError, sqlite3.IntegrityError) as exc:
        raise SystemExit(str(exc))
    print(f"Admin account created for {email}. Sign in at /admin.")


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] in {"create-admin", "setup-admin"}:
        ensure_schema()
        cli_create_admin()
        return
    if len(sys.argv) > 1 and sys.argv[1] == "init-db":
        ensure_schema()
        print("Database schema initialized at", db_path())
        return
    port = int(os.environ.get("PORT", "8000"))
    server = run_server("0.0.0.0", port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Shutting down Taj Opticals server")
    finally:
        server.server_close()

if __name__ == "__main__":
    main()
