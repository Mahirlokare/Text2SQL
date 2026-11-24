"""Benchmark: execution accuracy on a gold question/SQL set. Run: python evaluate.py"""
import json
import sqlite3
import pandas as pd
from core.engine import Text2SQLEngine
from core.llm import SQLGenerator

DB = "data/shop.db"
GOLD = [
    ("How many customers are there?", "SELECT COUNT(*) FROM customers"),
    ("How many orders are delivered?", "SELECT COUNT(*) FROM orders WHERE status='delivered'"),
    ("What is the average product price?", "SELECT AVG(price) FROM products"),
    ("List the cities of customers with number of customers per city",
     "SELECT city, COUNT(*) FROM customers GROUP BY city"),
    ("Which 3 products are the most expensive?", "SELECT name FROM products ORDER BY price DESC LIMIT 3"),
    ("Total units sold per category",
     "SELECT c.name, SUM(oi.quantity) FROM order_items oi JOIN products p ON p.id=oi.product_id "
     "JOIN categories c ON c.id=p.category_id GROUP BY c.id"),
    ("How many customers have never placed an order?",
     "SELECT COUNT(*) FROM customers WHERE id NOT IN (SELECT customer_id FROM orders)"),
]


def norm(df: pd.DataFrame):
    return sorted(tuple(round(v, 2) if isinstance(v, float) else v for v in row)
                  for row in df.itertuples(index=False))


def main():
    eng = Text2SQLEngine(DB, SQLGenerator())
    conn = sqlite3.connect(DB)
    ok, rows = 0, []
    for q, gold in GOLD:
        eng.history.clear()
        r = eng.ask(q)
        try:
            good = r.error is None and norm(r.df) == norm(pd.read_sql_query(gold, conn))
        except Exception:
            good = False
        ok += good
        rows.append({"question": q, "pred": r.sql, "correct": good, "attempts": r.attempts})
        print(("✔" if good else "✘"), q)
    print(f"\nExecution accuracy: {ok}/{len(GOLD)} = {ok/len(GOLD):.0%}")
    json.dump(rows, open("data/eval_results.json", "w"), indent=2)


if __name__ == "__main__":
    main()
