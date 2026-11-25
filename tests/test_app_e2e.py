import re
import pytest


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OFFLINE_SQL_ONLY", "1")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("SMTP_HOST", raising=False)
    import web
    web._engine = None
    web.USERS_DB = str(tmp_path / "users.db")
    web.init_db()
    web.app.config["TESTING"] = True
    return web.app.test_client()


def test_full_self_contained_signup_confirm_login_and_query(client):
    r = client.post("/signup", data={
        "name": "Mahir",
        "email": "mahir@example.com",
        "password": "password123",
    })
    assert r.status_code == 200
    m = re.search(rb'href="([^"]*/confirm/[^"]+)"', r.data)
    assert m is not None
    confirm_url = m.group(1).decode("utf-8")

    blocked = client.post("/login", data={"email": "mahir@example.com", "password": "password123"})
    assert b"Please confirm your account first" in blocked.data

    conf = client.get(confirm_url, follow_redirects=True)
    assert b"Account confirmed!" in conf.data

    login_r = client.post(
        "/login",
        data={"email": "mahir@example.com", "password": "password123"},
        follow_redirects=True,
    )
    assert b"Hi, Mahir" in login_r.data

    diverse_questions = [
        "Top 5 customers by total spend",
        "Monthly revenue in 2024",
        "Average order value by city",
        "Which category sells the most units?",
        "Cancelled orders per city",
        "How many customers are there?",
        "How many orders are delivered?",
        "What is the average product price?",
        "List the cities of customers with number of customers per city",
        "Which 3 products are the most expensive?",
        "Total units sold per category",
        "How many customers have never placed an order?",
        "Show orders from Mumbai",
        "Products in Books under $100",
        "Products priced between 50 and 200",
        "Cheapest 5 products",
        "Products out of stock",
        "Order count by status",
        "Revenue per category in 2025",
        "Delivered orders in March 2024",
        "Customers who spent more than 500",
        "Top 10 products by revenue",
        "Show all categories",
        "SELECT COUNT(*) AS cnt FROM orders WHERE status = 'shipped'",
    ]
    for q in diverse_questions:
        q_r = client.post("/app", data={"question": q})
        assert q_r.status_code == 200, f"HTTP failed on: {q}"
        assert b'class="flash error"' not in q_r.data, f"Query errored on: {q} -> {q_r.data[:500]}"

    csv_r = client.get("/export.csv")
    assert csv_r.status_code == 200
