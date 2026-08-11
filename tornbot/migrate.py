"""Database migrations for Torn Deal Finder.

Keeps existing user data while upgrading databases created by older bot versions.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in db.execute(f"PRAGMA table_info({table})").fetchall()}


def _add_missing(db: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = _columns(db, table)
    if not existing:
        return
    for name, definition in columns.items():
        if name not in existing:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def migrate_database(path: Path | str) -> None:
    """Upgrade legacy schemas in-place. Safe to run on every startup."""
    db = sqlite3.connect(path)
    try:
        # v7 deal scanner fields. Older releases used different deal columns.
        _add_missing(db, "deals", {
            "listing_key": "TEXT",
            "buy_price": "REAL DEFAULT 0",
            "qty": "INTEGER DEFAULT 1",
            "reference_price": "REAL DEFAULT 0",
            "est_profit": "REAL DEFAULT 0",
            "roi": "REAL DEFAULT 0",
            "discount": "REAL DEFAULT 0",
            "confidence": "REAL DEFAULT 0",
            "score": "REAL DEFAULT 0",
        })
        # Current portfolio schema. Preserve legacy holdings rather than deleting them.
        _add_missing(db, "holdings", {
            "item_name": "TEXT DEFAULT ''",
            "qty": "INTEGER DEFAULT 0",
            "cost_each": "REAL DEFAULT 0",
            "updated_ts": "INTEGER DEFAULT 0",
        })
        _add_missing(db, "price_samples", {
            "reference_price": "REAL DEFAULT 0",
            "cheapest": "REAL DEFAULT 0",
            "api_average": "REAL",
            "confidence": "REAL DEFAULT 0",
            "support": "INTEGER DEFAULT 0",
        })
        # A unique index replaces reliance on a newer CREATE TABLE definition.
        try:
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_deals_listing_key ON deals(listing_key)")
        except sqlite3.IntegrityError:
            # Legacy rows may all have NULL/duplicate placeholders; scanner still works.
            pass
        db.commit()
    finally:
        db.close()
