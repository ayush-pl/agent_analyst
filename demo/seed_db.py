"""Create a small, deterministic e-commerce SQLite database for the demo.

Tables: customers, products, orders, order_items. Two years of orders (2025-2026)
with seasonality and regional differences, so questions have interesting answers.

    python demo/seed_db.py            # writes demo/shop.db
    python demo/seed_db.py --path x.db
"""

from __future__ import annotations

import argparse
import random
import sqlite3
from datetime import date, timedelta
from pathlib import Path

DEFAULT_PATH = Path(__file__).with_name("shop.db")

REGIONS = ["North America", "Europe", "Asia Pacific", "Latin America"]
SEGMENTS = ["Consumer", "Small Business", "Enterprise"]
PRODUCTS = [
    # name, category, unit_price, unit_cost
    ("Aurora Laptop 14", "Electronics", 1299.0, 910.0),
    ("Aurora Laptop 16", "Electronics", 1699.0, 1190.0),
    ("Pulse Wireless Earbuds", "Electronics", 149.0, 62.0),
    ("Pulse Smartwatch", "Electronics", 299.0, 140.0),
    ("Nimbus 4K Monitor", "Electronics", 449.0, 300.0),
    ("Ergo Office Chair", "Furniture", 389.0, 210.0),
    ("Standing Desk Pro", "Furniture", 699.0, 420.0),
    ("Oak Bookshelf", "Furniture", 229.0, 120.0),
    ("Trail Running Shoes", "Sports", 129.0, 55.0),
    ("Yoga Mat Plus", "Sports", 49.0, 14.0),
    ("Carbon Road Bike", "Sports", 2199.0, 1500.0),
    ("Barista Espresso Machine", "Home", 549.0, 330.0),
    ("Cast Iron Cookware Set", "Home", 199.0, 85.0),
    ("Smart Air Purifier", "Home", 259.0, 130.0),
    ("Linen Bedding Set", "Home", 159.0, 60.0),
]
FIRST = ["Ava", "Liam", "Mia", "Noah", "Zoe", "Ethan", "Ivy", "Lucas", "Aria", "Kai",
         "Nora", "Omar", "Priya", "Chen", "Sofia", "Mateo", "Yuki", "Lena", "Ravi", "Elif"]
LAST = ["Smith", "Garcia", "Kim", "Patel", "Müller", "Rossi", "Silva", "Tanaka",
        "Novak", "Okafor", "Jensen", "Haddad", "Dubois", "Ivanov", "Costa"]

SCHEMA = """
CREATE TABLE customers (
    customer_id   INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    email         TEXT NOT NULL UNIQUE,
    region        TEXT NOT NULL,
    segment       TEXT NOT NULL,
    signup_date   DATE NOT NULL
);
CREATE TABLE products (
    product_id    INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    category      TEXT NOT NULL,
    unit_price    REAL NOT NULL,
    unit_cost     REAL NOT NULL
);
CREATE TABLE orders (
    order_id      INTEGER PRIMARY KEY,
    customer_id   INTEGER NOT NULL REFERENCES customers(customer_id),
    order_date    DATE NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('completed', 'returned', 'cancelled')),
    channel       TEXT NOT NULL CHECK (channel IN ('web', 'mobile', 'retail'))
);
CREATE TABLE order_items (
    order_item_id INTEGER PRIMARY KEY,
    order_id      INTEGER NOT NULL REFERENCES orders(order_id),
    product_id    INTEGER NOT NULL REFERENCES products(product_id),
    quantity      INTEGER NOT NULL,
    unit_price    REAL NOT NULL,
    discount      REAL NOT NULL DEFAULT 0
);
"""


def seed(path: Path = DEFAULT_PATH, seed_value: int = 7) -> Path:
    rng = random.Random(seed_value)
    path = Path(path)
    if path.exists():
        path.unlink()
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)

    con.executemany(
        "INSERT INTO products VALUES (?, ?, ?, ?, ?)",
        [(i + 1, *p) for i, p in enumerate(PRODUCTS)],
    )

    customers = []
    for cid in range(1, 401):
        name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
        region = rng.choices(REGIONS, weights=[40, 30, 20, 10])[0]
        segment = rng.choices(SEGMENTS, weights=[60, 28, 12])[0]
        signup = date(2024, 6, 1) + timedelta(days=rng.randint(0, 700))
        customers.append((cid, name, f"user{cid}@example.com", region, segment, signup.isoformat()))
    con.executemany("INSERT INTO customers VALUES (?, ?, ?, ?, ?, ?)", customers)

    region_boost = {"North America": 1.0, "Europe": 0.95, "Asia Pacific": 1.25, "Latin America": 0.8}
    orders, items = [], []
    oid = iid = 0
    day = date(2025, 1, 1)
    end = date(2026, 10, 31)
    while day <= end:
        # seasonality: November/December peak, mild growth over time
        season = 1.8 if day.month in (11, 12) else 1.0
        growth = 1 + (day - date(2025, 1, 1)).days / 1500
        for _ in range(rng.randint(2, int(6 * season * growth) + 2)):
            cust = rng.choice(customers)
            if date.fromisoformat(cust[5]) > day:
                continue
            oid += 1
            status = rng.choices(["completed", "returned", "cancelled"], weights=[88, 7, 5])[0]
            channel = rng.choices(["web", "mobile", "retail"], weights=[50, 35, 15])[0]
            orders.append((oid, cust[0], day.isoformat(), status, channel))
            for _ in range(rng.choices([1, 2, 3], weights=[65, 25, 10])[0]):
                iid += 1
                pid = rng.randint(1, len(PRODUCTS))
                qty = 1 if PRODUCTS[pid - 1][2] > 500 else rng.choice([1, 1, 2, 3])
                qty = max(1, round(qty * region_boost[cust[3]]))
                discount = rng.choice([0, 0, 0, 0.05, 0.1, 0.15])
                items.append((iid, oid, pid, qty, PRODUCTS[pid - 1][2], discount))
        day += timedelta(days=1)

    con.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?)", orders)
    con.executemany("INSERT INTO order_items VALUES (?, ?, ?, ?, ?, ?)", items)
    con.commit()
    con.close()
    return path


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--path", type=Path, default=DEFAULT_PATH)
    args = ap.parse_args()
    p = seed(args.path)
    con = sqlite3.connect(p)
    counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("customers", "products", "orders", "order_items")}
    print(f"Seeded {p}: {counts}")
