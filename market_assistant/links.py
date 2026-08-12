"""Reliable Torn Item Market deep links.

Telegram / Android sometimes behaves inconsistently with Torn hash routes.  The
most reliable direct item-market URL includes the item id, item name and item
type, with the text fields URL encoded.  Existing code can continue calling
market_link(item_id); register_catalog() supplies the extra metadata.
"""
from __future__ import annotations

from urllib.parse import quote

_ITEMS: dict[int, dict] = {}


def register_catalog(catalog: dict[int, dict] | None) -> None:
    _ITEMS.clear()
    if catalog:
        _ITEMS.update({int(k): v for k, v in catalog.items()})


def market_link(item_id: int) -> str:
    iid = int(item_id)
    base = "https://www.torn.com/page.php?sid=ItemMarket"
    item = _ITEMS.get(iid)
    if not item:
        return f"{base}#/market/view=search&itemID={iid}&sortField=price&sortOrder=ASC"

    name = quote(str(item.get("name", "")), safe="")
    item_type = quote(str(item.get("type", "")), safe="")
    return (
        f"{base}#/market/view=search&itemID={iid}"
        f"&itemName={name}&itemType={item_type}"
        "&sortField=price&sortOrder=ASC"
    )


def fallback_market_link(item_id: int) -> str:
    """Simpler ID-only route to use if Torn's expanded search route misbehaves."""
    iid = int(item_id)
    return (
        "https://www.torn.com/page.php?sid=ItemMarket"
        f"#/market/view=search&itemID={iid}&sortField=price&sortOrder=ASC"
    )
