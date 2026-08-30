import hmac
import json
import os
import secrets
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("DB_PATH", "/data/orders.db"))
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
MAX_BODY = 2_000_000
POSTS = {}
POSTS_LOCK = threading.Lock()


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path=DB_PATH):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA journal_mode=WAL")
    return db


@contextmanager
def database(path=DB_PATH):
    db = connect(path)
    try:
        with db:
            yield db
    finally:
        db.close()


def init_db(path=DB_PATH):
    with database(path) as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS orders (
          id TEXT PRIMARY KEY,
          created_at TEXT NOT NULL,
          designer TEXT NOT NULL,
          project TEXT NOT NULL,
          note TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'new',
          buyer_note TEXT NOT NULL DEFAULT '',
          reviewed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS order_items (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          order_id TEXT NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
          product_id TEXT NOT NULL,
          name TEXT NOT NULL,
          supplier TEXT NOT NULL DEFAULT '',
          category TEXT NOT NULL DEFAULT '',
          url TEXT NOT NULL,
          image TEXT NOT NULL DEFAULT '',
          source_price TEXT NOT NULL DEFAULT '',
          estimated_price REAL NOT NULL DEFAULT 0,
          quantity INTEGER NOT NULL DEFAULT 1,
          designer_note TEXT NOT NULL DEFAULT '',
          actual_price REAL,
          logistics REAL,
          decision TEXT NOT NULL DEFAULT 'review',
          buyer_note TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS orders_created ON orders(created_at DESC);
        CREATE INDEX IF NOT EXISTS items_order ON order_items(order_id);
        """)


def text(value, limit, required=False):
    value = str(value or "").strip()
    if required and not value:
        raise ValueError("Заполните обязательные поля")
    return value[:limit]


def money(value, nullable=False):
    if value in (None, "") and nullable:
        return None
    try:
        value = round(float(value), 2)
    except (TypeError, ValueError):
        raise ValueError("Некорректная сумма")
    if value < 0 or value > 1_000_000_000:
        raise ValueError("Некорректная сумма")
    return value


def safe_url(value, image=False):
    value = text(value, 2000, not image)
    if not value and image:
        return ""
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (host == "made-in-china.com" or host.endswith(".made-in-china.com")):
        raise ValueError("Некорректная ссылка на товар")
    return value


def create_order(payload, path=DB_PATH):
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list) or not 1 <= len(items) <= 200:
        raise ValueError("В заявке должно быть от 1 до 200 позиций")
    order_id = datetime.now().strftime("%y%m%d") + "-" + secrets.token_hex(3).upper()
    order = (
        order_id, now(), text(payload.get("designer"), 100, True),
        text(payload.get("project"), 160, True), text(payload.get("note"), 2000)
    )
    rows = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Некорректная позиция")
        try:
            quantity = int(item.get("quantity", 1))
        except (TypeError, ValueError):
            raise ValueError("Некорректное количество")
        if not 1 <= quantity <= 10_000:
            raise ValueError("Некорректное количество")
        rows.append((
            order_id, text(item.get("product_id"), 100, True), text(item.get("name"), 500, True),
            text(item.get("supplier"), 300), text(item.get("category"), 100), safe_url(item.get("url")),
            safe_url(item.get("image"), image=True), text(item.get("source_price"), 100),
            money(item.get("estimated_price", 0)), quantity, text(item.get("designer_note"), 1000)
        ))
    with database(path) as db:
        db.execute("INSERT INTO orders(id,created_at,designer,project,note) VALUES(?,?,?,?,?)", order)
        db.executemany("""INSERT INTO order_items(
          order_id,product_id,name,supplier,category,url,image,source_price,estimated_price,quantity,designer_note
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""", rows)
    return order_id


def order_list(path=DB_PATH):
    with database(path) as db:
        return [dict(row) for row in db.execute("""
          SELECT o.*, COUNT(i.id) item_count,
            COALESCE(SUM(i.estimated_price*i.quantity),0) estimated_total,
            COALESCE(SUM(i.actual_price*i.quantity),0) actual_total,
            COALESCE(SUM(i.logistics*i.quantity),0) logistics_total
          FROM orders o LEFT JOIN order_items i ON i.order_id=o.id
          GROUP BY o.id ORDER BY o.created_at DESC
        """)]


def order_detail(order_id, path=DB_PATH):
    with database(path) as db:
        order = db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not order:
            return None
        result = dict(order)
        result["items"] = [dict(row) for row in db.execute("SELECT * FROM order_items WHERE order_id=? ORDER BY id", (order_id,))]
        return result


def update_order(order_id, payload, path=DB_PATH):
    statuses = {"new", "review", "approved", "rejected"}
    decisions = {"review", "viable", "clarify", "reject"}
    status = payload.get("status")
    if status not in statuses:
        raise ValueError("Некорректный статус")
    items = payload.get("items", [])
    if not isinstance(items, list) or len(items) > 200:
        raise ValueError("Некорректные позиции")
    with database(path) as db:
        if not db.execute("SELECT 1 FROM orders WHERE id=?", (order_id,)).fetchone():
            return False
        db.execute("UPDATE orders SET status=?,buyer_note=?,reviewed_at=? WHERE id=?", (
            status, text(payload.get("buyer_note"), 3000), now(), order_id
        ))
        for item in items:
            decision = item.get("decision")
            if decision not in decisions:
                raise ValueError("Некорректное решение")
            db.execute("""UPDATE order_items SET actual_price=?,logistics=?,decision=?,buyer_note=?
              WHERE id=? AND order_id=?""", (
                money(item.get("actual_price"), True), money(item.get("logistics"), True), decision,
                text(item.get("buyer_note"), 1000), int(item.get("id", 0)), order_id
            ))
    return True


class Handler(BaseHTTPRequestHandler):
    server_version = "ChinaRoom/1"

    def send_json(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.security_headers()
        self.end_headers()
        self.wfile.write(body)

    def security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("Некорректный запрос")
        if length < 1 or length > MAX_BODY:
            raise ValueError("Слишком большой запрос")
        try:
            return json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            raise ValueError("Некорректный JSON")

    def is_admin(self):
        supplied = self.headers.get("Authorization", "").removeprefix("Bearer ")
        return bool(ADMIN_PASSWORD) and hmac.compare_digest(supplied, ADMIN_PASSWORD)

    def rate_ok(self):
        ip = self.client_address[0]
        current = time.time()
        with POSTS_LOCK:
            recent = [stamp for stamp in POSTS.get(ip, []) if current - stamp < 60]
            if len(recent) >= 10:
                return False
            recent.append(current)
            POSTS[ip] = recent
        return True

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/health":
            return self.send_json(200, {"ok": True})
        if path.startswith("/api/admin/"):
            if not self.is_admin():
                return self.send_json(401, {"error": "Неверный пароль"})
            if path == "/api/admin/orders":
                return self.send_json(200, {"orders": order_list()})
            if path.startswith("/api/admin/orders/"):
                result = order_detail(path.rsplit("/", 1)[-1])
                return self.send_json(200, result) if result else self.send_json(404, {"error": "Заявка не найдена"})
        if path.startswith("/api/"):
            return self.send_json(404, {"error": "Не найдено"})
        self.serve_static(path)

    def do_POST(self):
        if urlparse(self.path).path != "/api/orders":
            return self.send_json(404, {"error": "Не найдено"})
        if not self.rate_ok():
            return self.send_json(429, {"error": "Слишком много заявок. Повторите через минуту."})
        try:
            order_id = create_order(self.read_json())
            self.send_json(201, {"id": order_id})
        except ValueError as error:
            self.send_json(400, {"error": str(error)})
        except Exception as error:
            self.log_error("order creation failed: %s", error)
            self.send_json(500, {"error": "Не удалось сохранить заявку"})

    def do_PATCH(self):
        path = urlparse(self.path).path
        if not path.startswith("/api/admin/orders/"):
            return self.send_json(404, {"error": "Не найдено"})
        if not self.is_admin():
            return self.send_json(401, {"error": "Неверный пароль"})
        try:
            found = update_order(path.rsplit("/", 1)[-1], self.read_json())
            self.send_json(200, {"ok": True}) if found else self.send_json(404, {"error": "Заявка не найдена"})
        except (ValueError, TypeError) as error:
            self.send_json(400, {"error": str(error)})

    def serve_static(self, url_path):
        name = "index.html" if url_path == "/" else url_path.lstrip("/")
        file = (ROOT / name).resolve()
        if ROOT not in file.parents or not file.is_file():
            return self.send_error(404)
        content_types = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8"}
        body = file.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_types.get(file.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache" if file.suffix == ".html" else "public, max-age=3600")
        self.security_headers()
        self.end_headers()
        self.wfile.write(body)


def self_test():
    import tempfile
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "test.db"
        init_db(path)
        order_id = create_order({
            "designer": "Анна", "project": "Квартира", "note": "Москва",
            "items": [{"product_id": "chairs-1", "name": "Стул", "url": "https://ru.made-in-china.com/product.html", "estimated_price": 50000, "quantity": 2}]
        }, path)
        item_id = order_detail(order_id, path)["items"][0]["id"]
        assert update_order(order_id, {"status": "approved", "buyer_note": "Везём", "items": [{"id": item_id, "actual_price": 48000, "logistics": 7000, "decision": "viable", "buyer_note": "ОК"}]}, path)
        result = order_detail(order_id, path)
        assert result["status"] == "approved" and result["items"][0]["actual_price"] == 48000
        assert order_list(path)[0]["item_count"] == 1
    print("self-test: ok")


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        self_test()
    else:
        init_db()
        if not ADMIN_PASSWORD:
            print("WARNING: ADMIN_PASSWORD is empty; admin API is disabled", flush=True)
        ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "80"))), Handler).serve_forever()
