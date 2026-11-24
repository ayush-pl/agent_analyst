"""Generate demo/sample_coffee_sales.csv: a deliberately messy real-world-style export.

US dates, "$4.50" prices, a percent column, headers with spaces and symbols; used by the
web demo's "Try a sample file" button and by the CSV tests.
"""

import csv
import random
from datetime import date, timedelta
from pathlib import Path

OUT = Path(__file__).with_name("sample_coffee_sales.csv")
STORES = {"Downtown": 1.4, "Airport": 1.2, "University": 1.0, "Suburb": 0.7}
MENU = [("Latte", "Coffee", 4.50), ("Cappuccino", "Coffee", 4.25), ("Espresso", "Coffee", 3.00),
        ("Cold Brew", "Coffee", 4.75), ("Matcha Latte", "Tea", 5.25), ("Chai Latte", "Tea", 4.95),
        ("Croissant", "Bakery", 3.50), ("Blueberry Muffin", "Bakery", 3.25), ("Bagel", "Bakery", 2.95)]


def main(seed: int = 11) -> None:
    rng = random.Random(seed)
    rows = []
    start = date(2026, 1, 1)
    for day in range(273):                      # Jan 1 - Sep 30 2026
        d = start + timedelta(days=day)
        for store, weight in STORES.items():
            for _ in range(rng.randint(2, int(6 * weight) + 2)):
                name, cat, price = rng.choice(MENU)
                qty = rng.choices([1, 2, 3, 4], weights=[60, 25, 10, 5])[0]
                disc = rng.choice(["0%", "0%", "0%", "10%", "15%"])
                rows.append([d.strftime("%m/%d/%Y"), store, name, cat, qty, f"${price:,.2f}", disc,
                             rng.choice(["Card", "Card", "Mobile App", "Cash"]),
                             rng.choice(["Member", "Guest", "Guest"])])
    with OUT.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Sale Date", "Store", "Item", "Category", "Qty", "Unit Price ($)", "Discount %",
                    "Payment Method", "Customer Type"])
        w.writerows(rows)
    print(f"{len(rows)} rows -> {OUT}")


if __name__ == "__main__":
    main()
