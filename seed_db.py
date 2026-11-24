"""Creates a sample e-commerce SQLite database at data/shop.db"""
import os
import random
import sqlite3
from datetime import date, timedelta

random.seed(42)
os.makedirs("data", exist_ok=True)
path = "data/shop.db"
if os.path.exists(path):
    os.remove(path)
c = sqlite3.connect(path)
c.executescript("""
CREATE TABLE customers(id INTEGER PRIMARY KEY, name TEXT, email TEXT, city TEXT, signup_date TEXT);
CREATE TABLE categories(id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE products(id INTEGER PRIMARY KEY, name TEXT, category_id INTEGER, price REAL, stock INTEGER,
  FOREIGN KEY(category_id) REFERENCES categories(id));
CREATE TABLE orders(id INTEGER PRIMARY KEY, customer_id INTEGER, order_date TEXT, status TEXT,
  FOREIGN KEY(customer_id) REFERENCES customers(id));
CREATE TABLE order_items(id INTEGER PRIMARY KEY, order_id INTEGER, product_id INTEGER, quantity INTEGER, unit_price REAL,
  FOREIGN KEY(order_id) REFERENCES orders(id), FOREIGN KEY(product_id) REFERENCES products(id));
""")
cities = ["Mumbai", "Delhi", "Bengaluru", "Chennai", "Pune", "Kolkata", "Hyderabad"]
first = ["Aarav", "Diya", "Rohan", "Isha", "Kabir", "Meera", "Vivaan", "Anaya", "Arjun", "Saanvi"]
last = ["Sharma", "Patel", "Iyer", "Singh", "Reddy", "Das", "Nair", "Gupta"]
cats = ["Electronics", "Books", "Clothing", "Home", "Sports"]
c.executemany("INSERT INTO categories VALUES(?,?)", list(enumerate(cats, 1)))
prods = []
for i in range(1, 41):
    cid = random.randint(1, 5)
    prods.append((i, f"{cats[cid-1]} Item {i}", cid, round(random.uniform(5, 500), 2), random.randint(0, 200)))
c.executemany("INSERT INTO products VALUES(?,?,?,?,?)", prods)
base = date(2024, 1, 1)
for i in range(1, 101):
    n = f"{random.choice(first)} {random.choice(last)}"
    c.execute("INSERT INTO customers VALUES(?,?,?,?,?)",
              (i, n, f"{n.split()[0].lower()}{i}@mail.com", random.choice(cities),
               str(base + timedelta(days=random.randint(0, 365)))))
oi = 1
for o in range(1, 401):
    d = base + timedelta(days=random.randint(0, 700))
    c.execute("INSERT INTO orders VALUES(?,?,?,?)", (o, random.randint(1, 100), str(d),
              random.choice(["delivered", "delivered", "shipped", "cancelled", "pending"])))
    for _ in range(random.randint(1, 4)):
        p = random.choice(prods)
        c.execute("INSERT INTO order_items VALUES(?,?,?,?,?)", (oi, o, p[0], random.randint(1, 5), p[3]))
        oi += 1
c.commit()
print("Created", path)
