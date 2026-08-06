from dotenv import load_dotenv
load_dotenv()

import os
import sqlite3
import math
from datetime import datetime, timedelta
from collections import defaultdict
from flask import Flask, render_template, request, redirect, url_for, session, flash
from werkzeug.security import generate_password_hash, check_password_hash
import requests

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-secret-key")
DB_PATH = os.path.join(os.path.dirname(__file__), "expenses.db")

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
CATEGORIES = ["Food", "Transport", "Shopping", "Bills", "Entertainment",
              "Health", "Education", "Business", "Other"]


# ---------- Database ----------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS expenses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        amount REAL NOT NULL,
        category TEXT NOT NULL,
        note TEXT,
        expense_date TEXT NOT NULL)""")
    conn.commit()
    conn.close()


# ---------- AI helpers (one function calls Groq, two functions use it) ----------

def ask_ai(prompt, max_tokens=150):
    """Send a prompt to Groq and return the reply text, or None if it fails."""
    if not GROQ_API_KEY:
        return None
    try:
        r = requests.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            json={"model": "llama-3.1-8b-instant",
                  "messages": [{"role": "user", "content": prompt}],
                  "max_tokens": max_tokens},
            timeout=15,
        )
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception:
        return None


def ai_pick_category(title, note):
    """Auto-categorize an expense from its title/note."""
    prompt = (f"Pick exactly one category from {CATEGORIES} for this expense: "
              f"'{title} {note}'. Reply with only the category name.")
    reply = ask_ai(prompt, max_tokens=10)
    for cat in CATEGORIES:
        if reply and cat.lower() in reply.lower():
            return cat
    return "Other"


def ai_spending_insight(total, by_category):
    """Write a short summary + tip based on this month's spending."""
    if not by_category:
        return "Add some expenses to get an AI insight."
    breakdown = ", ".join(f"{k}: {v:.0f}" for k, v in by_category.items())
    prompt = (f"Total spending this month is {total:.0f}, split as {breakdown}. "
              f"In 2-3 short sentences, point out the biggest category and give "
              f"one practical saving tip. Plain text, no markdown.")
    return ask_ai(prompt) or "AI insight unavailable (check GROQ_API_KEY)."


# ---------- Auth routes ----------

# @app.route("/")
# def home():
#     return redirect(url_for("dashboard" if "user_id" in session else "login"))

@app.route("/")
def home():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    return render_template("index.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]
        conn = get_db()
        if conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            flash("Username already taken.")
        else:
            conn.execute("INSERT INTO users (username, password) VALUES (?, ?)",
                         (username, generate_password_hash(password)))
            conn.commit()
            flash("Account created! Please log in.")
            conn.close()
            return redirect(url_for("login"))
        conn.close()
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]
        conn = get_db()
        user = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        conn.close()
        if user and check_password_hash(user["password"], password):
            session["user_id"], session["username"] = user["id"], user["username"]
            return redirect(url_for("dashboard"))
        flash("Invalid username or password.")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------- Expense CRUD ----------

@app.route("/dashboard", methods=["GET", "POST"])
def dashboard():
    if "user_id" not in session:
        return redirect(url_for("login"))

    if request.method == "POST":
        title = request.form["title"].strip()
        note = request.form.get("note", "").strip()
        category = request.form.get("category") or "Auto (AI)"

        try:
            amount = float(request.form["amount"])
            if not math.isfinite(amount):
                raise ValueError
        except (ValueError, KeyError):
            flash("Amount must be a valid number.")
            return redirect(url_for("dashboard"))

        if not title:
            flash("Please enter what the expense was for.")
            return redirect(url_for("dashboard"))
        if amount <= 0:
            flash("Amount must be greater than 0.")
            return redirect(url_for("dashboard"))

        if category == "Auto (AI)":
            category = ai_pick_category(title, note)

        conn = get_db()
        conn.execute(
            "INSERT INTO expenses (user_id, title, amount, category, note, expense_date) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (session["user_id"], title, amount, category, note,
             request.form.get("expense_date") or datetime.now().strftime("%Y-%m-%d")),
        )
        conn.commit()
        conn.close()
        return redirect(url_for("dashboard"))

    conn = get_db()
    records = conn.execute(
        "SELECT * FROM expenses WHERE user_id=? ORDER BY expense_date DESC, id DESC",
        (session["user_id"],)).fetchall()
    conn.close()

    total = sum(r["amount"] for r in records)
    this_month = datetime.now().strftime("%Y-%m")
    this_month_total = sum(r["amount"] for r in records if r["expense_date"][:7] == this_month)

    return render_template("dashboard.html", username=session["username"],
                            records=records[:6], total=total,
                            this_month_total=this_month_total,
                            entry_count=len(records), categories=CATEGORIES)


@app.route("/edit/<int:expense_id>", methods=["GET", "POST"])
def edit_expense(expense_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    conn = get_db()
    expense = conn.execute("SELECT * FROM expenses WHERE id=? AND user_id=?",
                            (expense_id, session["user_id"])).fetchone()
    if not expense:
        conn.close()
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        title = request.form["title"].strip()
        try:
            amount = float(request.form["amount"])
            if not math.isfinite(amount):
                raise ValueError
        except (ValueError, KeyError):
            flash("Amount must be a valid number.")
            conn.close()
            return redirect(url_for("edit_expense", expense_id=expense_id))

        if not title or amount <= 0:
            flash("Please enter a valid title and amount.")
            conn.close()
            return redirect(url_for("edit_expense", expense_id=expense_id))

        expense_date = request.form.get("expense_date") or expense["expense_date"]

        conn.execute(
            "UPDATE expenses SET title=?, amount=?, category=?, note=?, expense_date=? "
            "WHERE id=? AND user_id=?",
            (title, amount, request.form["category"], request.form.get("note", "").strip(),
             expense_date, expense_id, session["user_id"]),
        )
        conn.commit()
        conn.close()
        return redirect(url_for("dashboard"))

    conn.close()
    return render_template("edit.html", expense=expense, categories=CATEGORIES)


@app.route("/delete/<int:expense_id>")
def delete_expense(expense_id):
    if "user_id" not in session:
        return redirect(url_for("login"))
    conn = get_db()
    conn.execute("DELETE FROM expenses WHERE id=? AND user_id=?",
                 (expense_id, session["user_id"]))
    conn.commit()
    conn.close()
    return redirect(url_for("dashboard"))


# ---------- Reports ----------

@app.route("/reports")
def reports():
    if "user_id" not in session:
        return redirect(url_for("login"))

    conn = get_db()
    records = conn.execute("SELECT * FROM expenses WHERE user_id=? ORDER BY expense_date",
                            (session["user_id"],)).fetchall()
    conn.close()

    by_category, by_month = defaultdict(float), defaultdict(float)
    for r in records:
        by_category[r["category"]] += r["amount"]
        by_month[r["expense_date"][:7]] += r["amount"]

    this_month = datetime.now().strftime("%Y-%m")
    last_month = (datetime.now().replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    this_total, last_total = by_month.get(this_month, 0), by_month.get(last_month, 0)
    change_pct = round((this_total - last_total) / last_total * 100, 1) if last_total else None

    this_month_by_category = defaultdict(float)
    for r in records:
        if r["expense_date"][:7] == this_month:
            this_month_by_category[r["category"]] += r["amount"]

    months = sorted(by_month.keys())
    return render_template(
        "reports.html", username=session["username"],
        total=sum(by_category.values()), this_month_total=this_total,
        change_pct=change_pct, change_pct_abs=abs(change_pct) if change_pct is not None else None,
        record_count=len(records),
        category_labels=list(by_category.keys()), category_values=list(by_category.values()),
        category_pairs=sorted(by_category.items(), key=lambda x: -x[1]),
        month_labels=months, month_values=[round(by_month[m], 2) for m in months],
        ai_insight=ai_spending_insight(this_total, this_month_by_category),
    )


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", debug=True)
