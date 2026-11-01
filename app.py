import math
import os
import re
import sqlite3
from datetime import date, datetime, timedelta
from functools import wraps

import numpy as np
import pandas as pd
from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE, "bloodbank.db"))
DATA_DIR = os.path.join(BASE, "data")

# Ensure DB directory exists
db_dir = os.path.dirname(DB_PATH)
if db_dir:
    os.makedirs(db_dir, exist_ok=True)

GROUPS = ["O+", "A+", "B+", "AB+", "O-", "A-", "B-", "AB-"]
RARE = {"O-", "A-", "B-", "AB-"}
HORIZON = 14          # forecast days
TARGET_DAYS = 10      # camp planner aims for this many days of cover
CRITICAL_DAYS, LOW_DAYS = 3, 7
DONOR_YIELD = 0.8     # share of invited donors who pass screening

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT UNIQUE NOT NULL,
  email TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'donor',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS blood_stock (
  date TEXT,
  blood_group TEXT,
  donated INTEGER DEFAULT 0,
  issued INTEGER DEFAULT 0,
  expired INTEGER DEFAULT 0,
  PRIMARY KEY(date, blood_group)
);

CREATE TABLE IF NOT EXISTS opening_stock (
  blood_group TEXT PRIMARY KEY,
  units INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS donors (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER,
  name TEXT NOT NULL,
  blood_group TEXT NOT NULL,
  gender TEXT NOT NULL,
  phone TEXT NOT NULL,
  area TEXT NOT NULL,
  last_donation TEXT,
  is_available INTEGER DEFAULT 1,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER,
  req_date TEXT NOT NULL,
  hospital TEXT NOT NULL,
  patient_name TEXT,
  blood_group TEXT NOT NULL,
  units INTEGER NOT NULL,
  urgency TEXT DEFAULT 'Normal',
  status TEXT DEFAULT 'Pending',
  notes TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS inventory_transactions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  trans_date TEXT NOT NULL,
  blood_group TEXT NOT NULL,
  trans_type TEXT NOT NULL,
  units INTEGER NOT NULL,
  reference_id INTEGER,
  remarks TEXT,
  performed_by TEXT NOT NULL
);
"""

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "lifelink-academic-btech-secret-key-2026")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)


@app.after_request
def add_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    return response


# ---------------------------------------------------------------- database
def conn():
    c = sqlite3.connect(DB_PATH, timeout=30.0)
    c.execute("PRAGMA journal_mode=WAL;")
    c.execute("PRAGMA busy_timeout=30000;")
    c.row_factory = sqlite3.Row
    return c


def seed_users(c):
    """Seed default demo accounts if not present."""
    now = datetime.now().isoformat()
    # 1. Admin
    admin = c.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()
    if not admin:
        c.execute(
            "INSERT INTO users (username, email, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?)",
            ("admin", "admin@lifelink.org", generate_password_hash("admin123"), "admin", now)
        )
    # 2. Donor
    donor = c.execute("SELECT id FROM users WHERE username = 'donor1'").fetchone()
    if not donor:
        c.execute(
            "INSERT INTO users (username, email, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?)",
            ("donor1", "donor1@lifelink.org", generate_password_hash("donor123"), "donor", now)
        )
        d_user = c.execute("SELECT id FROM users WHERE username = 'donor1'").fetchone()
        c.execute(
            """INSERT INTO donors (user_id, name, blood_group, gender, phone, area, last_donation, is_available, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (d_user["id"], "Demo Registered Donor", "O+", "M", "9876543210", "Civil Lines",
             (date.today() - timedelta(days=105)).isoformat(), 1, now)
        )
    # 3. Requester
    requester = c.execute("SELECT id FROM users WHERE username = 'requester1'").fetchone()
    if not requester:
        c.execute(
            "INSERT INTO users (username, email, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?)",
            ("requester1", "requester1@lifelink.org", generate_password_hash("requester123"), "requester", now)
        )
    c.commit()


def migrate_db(c):
    """Safely apply column additions if upgrading from existing database."""
    # Check donors columns
    donor_cols = [r[1] for r in c.execute("PRAGMA table_info(donors)").fetchall()]
    if donor_cols and "user_id" not in donor_cols:
        c.execute("ALTER TABLE donors ADD COLUMN user_id INTEGER")
    if donor_cols and "is_available" not in donor_cols:
        c.execute("ALTER TABLE donors ADD COLUMN is_available INTEGER DEFAULT 1")
    if donor_cols and "created_at" not in donor_cols:
        c.execute("ALTER TABLE donors ADD COLUMN created_at TEXT")

    # Check requests columns
    req_cols = [r[1] for r in c.execute("PRAGMA table_info(requests)").fetchall()]
    if req_cols and "user_id" not in req_cols:
        c.execute("ALTER TABLE requests ADD COLUMN user_id INTEGER")
    if req_cols and "patient_name" not in req_cols:
        c.execute("ALTER TABLE requests ADD COLUMN patient_name TEXT")
    if req_cols and "urgency" not in req_cols:
        c.execute("ALTER TABLE requests ADD COLUMN urgency TEXT DEFAULT 'Normal'")
    if req_cols and "status" not in req_cols:
        c.execute("ALTER TABLE requests ADD COLUMN status TEXT DEFAULT 'Fulfilled'")
    if req_cols and "notes" not in req_cols:
        c.execute("ALTER TABLE requests ADD COLUMN notes TEXT")
    if req_cols and "updated_at" not in req_cols:
        c.execute("ALTER TABLE requests ADD COLUMN updated_at TEXT")
    
    # Ensure inventory_transactions table exists
    c.execute("""CREATE TABLE IF NOT EXISTS inventory_transactions (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      trans_date TEXT NOT NULL,
      blood_group TEXT NOT NULL,
      trans_type TEXT NOT NULL,
      units INTEGER NOT NULL,
      reference_id INTEGER,
      remarks TEXT,
      performed_by TEXT NOT NULL
    );""")
    
    seed_users(c)
    c.commit()


def seed(c, wipe=False):
    """Load demo CSVs into SQLite, shifting dates so the data ends yesterday."""
    if wipe:
        for t in ("blood_stock", "opening_stock", "donors", "requests", "inventory_transactions"):
            c.execute(f"DELETE FROM {t}")
        c.commit()

    stock = pd.read_csv(os.path.join(DATA_DIR, "stock_log.csv"), parse_dates=["date"])
    shift = pd.Timestamp(date.today() - timedelta(days=1)) - stock["date"].max()
    stock["date"] = (stock["date"] + shift).dt.strftime("%Y-%m-%d")
    stock.to_sql("blood_stock", c, if_exists="append", index=False)

    pd.read_csv(os.path.join(DATA_DIR, "opening_stock.csv")).to_sql(
        "opening_stock", c, if_exists="append", index=False)

    donors = pd.read_csv(os.path.join(DATA_DIR, "donors.csv"), parse_dates=["last_donation"])
    donors["last_donation"] = (donors["last_donation"] + shift).dt.strftime("%Y-%m-%d")
    donors["is_available"] = 1
    donors["user_id"] = None
    donors["created_at"] = datetime.now().isoformat()
    donors.to_sql("donors", c, if_exists="append", index=False)

    reqs = pd.read_csv(os.path.join(DATA_DIR, "requests.csv"), parse_dates=["req_date"])
    reqs["req_date"] = (reqs["req_date"] + shift).dt.strftime("%Y-%m-%d")
    reqs["user_id"] = None
    reqs["patient_name"] = "Emergency / Inpatient Dept"
    reqs["urgency"] = "Normal"
    reqs["status"] = "Fulfilled"   # Historical requests were already issued
    reqs["notes"] = "Historical logged request"
    reqs["updated_at"] = reqs["req_date"]
    reqs.to_sql("requests", c, if_exists="append", index=False)

    # Seed demo users
    seed_users(c)
    c.commit()


def init_db():
    with conn() as c:
        c.executescript(SCHEMA)
        migrate_db(c)
        if c.execute("SELECT COUNT(*) FROM blood_stock").fetchone()[0] == 0:
            seed(c)


def get_current_stock(c, grp):
    """Calculates instantaneous current inventory for a blood group."""
    op_row = c.execute("SELECT units FROM opening_stock WHERE blood_group = ?", (grp,)).fetchone()
    opening = op_row[0] if op_row else 0
    deltas = c.execute(
        "SELECT SUM(donated), SUM(issued), SUM(expired) FROM blood_stock WHERE blood_group = ?",
        (grp,)
    ).fetchone()
    don, iss, exp = [x or 0 for x in deltas]
    return max(0, int(opening + don - iss - exp))


# ----------------------------------------------------------------- auth
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            if request.path.startswith("/api/"):
                return jsonify(error="Authentication required. Please log in."), 401
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return decorated


def roles_required(*roles):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if "user_id" not in session:
                if request.path.startswith("/api/"):
                    return jsonify(error="Authentication required. Please log in."), 401
                return redirect(url_for("login", next=request.path))
            if session.get("user_role") not in roles:
                if request.path.startswith("/api/"):
                    return jsonify(error="Forbidden: insufficient permissions."), 403
                return render_template("login.html", error="Access restricted to authorized roles.", next=request.path), 403
            return f(*args, **kwargs)
        return decorated
    return decorator


# --------------------------------------------------------------- analytics
def daily_frames(c):
    """One DataFrame per blood group with a running 'stock' column."""
    df = pd.read_sql("SELECT * FROM blood_stock", c, parse_dates=["date"])
    opening = pd.read_sql("SELECT * FROM opening_stock", c).set_index("blood_group")["units"]
    frames = {}
    for grp in GROUPS:
        g = df[df.blood_group == grp].set_index("date").sort_index()
        if g.empty:
            idx = pd.DatetimeIndex([pd.Timestamp(date.today())])
            g = pd.DataFrame({"donated": 0, "issued": 0, "expired": 0}, index=idx)
        idx = pd.date_range(g.index.min(), g.index.max(), freq="D")
        g = g[["donated", "issued", "expired"]].reindex(idx, fill_value=0)
        g["stock"] = int(opening.get(grp, 0)) + (g.donated - g.issued - g.expired).cumsum()
        frames[grp] = g
    return frames


def forecast(g, horizon=HORIZON):
    """Demand = damped linear trend on the last 28 days (NumPy polyfit).
    Donations = mean of the last 14 days. Expirations = mean of the last 28 days.
    Returns daily arrays and projected stock."""
    demand = g["issued"].astype(float)
    base = float(demand.tail(14).mean()) if len(demand.tail(14)) else 0.0
    recent = demand.tail(28).to_numpy()
    slope, level = 0.0, base
    if len(recent) >= 7:
        slope, intercept = np.polyfit(np.arange(len(recent)), recent, 1)
        cap = 0.15 * max(base, 0.1)
        slope = float(np.clip(slope, -cap, cap))
        level = 0.3 * (intercept + slope * (len(recent) - 1)) + 0.7 * base
    steps = np.arange(1, horizon + 1)
    demand_f = np.clip(level + 0.3 * slope * steps, 0, None)
    don_f = np.full(horizon, float(g["donated"].tail(14).mean()) if len(g["donated"]) else 0.0)
    exp_f = np.full(horizon, float(g["expired"].tail(28).mean()) if len(g["expired"]) else 0.0)
    now = float(g["stock"].iloc[-1]) if len(g["stock"]) else 0.0
    raw = now + np.cumsum(don_f - demand_f - exp_f)
    return demand_f, don_f, raw


def demand_spike(g):
    """z-score of the last 3 days' mean demand vs the 60 days before them."""
    s = g["issued"].astype(float)
    base = s.iloc[-63:-3]
    if len(base) < 14 or base.std(ddof=1) == 0:
        return 0.0
    return float((s.tail(3).mean() - base.mean()) / (base.std(ddof=1) / math.sqrt(3)))


def status_for(cover):
    if cover < CRITICAL_DAYS:
        return "critical"
    if cover < LOW_DAYS:
        return "low"
    return "safe"


def build_overview(c):
    frames = daily_frames(c)
    last_day = max(f.index.max() for f in frames.values())
    groups, alerts, camp, surplus = [], [], [], []

    total_expired_30d = 0
    total_donations_30d = 0

    for grp in GROUPS:
        g = frames[grp]
        demand_f, don_f, projected = forecast(g)
        stock = int(g["stock"].iloc[-1])
        daily = max(float(demand_f.mean()), 0.01)
        cover = round(stock / daily, 1)
        status = status_for(cover)
        z = demand_spike(g)
        run_out = None
        hit = np.where(projected <= 0)[0]
        if len(hit):
            run_out = (last_day + timedelta(days=int(hit[0]) + 1)).strftime("%d %b")

        d30 = g.tail(30)
        waste = int(d30["expired"].sum())
        don_sum = int(d30["donated"].sum())
        waste_pct = round(100 * waste / max(don_sum, 1), 1)

        total_expired_30d += waste
        total_donations_30d += don_sum

        groups.append({
            "group": grp, "stock": stock, "cover_days": cover, "status": status,
            "rare": grp in RARE, "demand_daily": round(daily, 1),
            "donation_daily": round(float(don_f[0]), 1), "run_out": run_out,
            "spike_z": round(z, 1), "spike": z >= 2.0,
            "expired_30d": waste, "expired_pct": waste_pct,
        })

        # alerts
        if status != "safe":
            when = f" Projected to run out on {run_out}." if run_out else ""
            alerts.append({"level": status, "group": grp,
                           "title": f"{grp} is {status}",
                           "text": f"{stock} unit{'' if stock == 1 else 's'} in stock, about {cover} days of cover.{when}"})
            if grp in RARE:
                alerts.append({"level": "critical", "group": grp,
                               "title": f"Rare group {grp} needs a donor drive",
                               "text": "Rare groups are hard to replace at short notice. Contact eligible donors now."})
        if z >= 2.0:
            alerts.append({"level": "low", "group": grp,
                           "title": f"Unusual demand for {grp}",
                           "text": f"Last 3 days are {z:.1f} standard errors above the 60-day norm (possible surge)."})
        if waste_pct >= 5:
            alerts.append({"level": "low", "group": grp,
                           "title": f"{grp} expiry loss is high",
                           "text": f"{waste} units expired in 30 days ({waste_pct}% of donations)."})

        # camp planner
        target = TARGET_DAYS * daily
        gap = math.ceil(target - stock)
        if gap > 0:
            camp.append({"group": grp, "gap": gap, "invite": math.ceil(gap / DONOR_YIELD),
                         "cover_days": cover, "status": status, "rare": grp in RARE})

        # surplus that could be shared before it expires
        extra = int(stock - 21 * daily)
        if extra > 5:
            surplus.append({"group": grp, "units": extra})

    camp.sort(key=lambda r: r["cover_days"])
    order = {"critical": 0, "low": 1}
    alerts.sort(key=lambda a: order.get(a["level"], 2))
    for s in surplus:
        alerts.append({"level": "info", "group": s["group"],
                       "title": f"{s['group']} surplus of {s['units']} units",
                       "text": "Offer to nearby hospitals before these units expire."})

    # Count pending requests
    pending_count = c.execute("SELECT COUNT(*) FROM requests WHERE status = 'Pending'").fetchone()[0]

    kpis = {
        "total_units": sum(x["stock"] for x in groups),
        "critical": sum(1 for x in groups if x["status"] == "critical"),
        "low": sum(1 for x in groups if x["status"] == "low"),
        "donors_needed": sum(r["invite"] for r in camp),
        "expired_30d_total": total_expired_30d,
        "avg_wastage_pct": round(100 * total_expired_30d / max(total_donations_30d, 1), 1),
        "pending_requests": pending_count,
        "as_of": last_day.strftime("%d %b %Y"),
    }
    return {"groups": groups, "alerts": alerts, "camp": camp, "kpis": kpis}


# --------------------------------------------------------------------- Navigation
PAGES = [
    ("dashboard", "/", "Dashboard"),
    ("forecast", "/forecast", "Forecast"),
    ("requests", "/requests", "Requests"),
    ("alerts", "/alerts", "Alerts"),
    ("camp", "/camp", "Camp planner"),
    ("donors", "/donors", "Donors"),
    ("log", "/log", "Inventory log"),
]


@app.context_processor
def inject_nav():
    user = None
    if "user_id" in session:
        user = {
            "id": session["user_id"],
            "username": session.get("username"),
            "role": session.get("user_role")
        }
    return {
        "PAGES": PAGES,
        "current_user": user,
        "current_year": date.today().year
    }


# ----------------------------------------------------------------- Page Views
@app.route("/")
def dashboard():
    return render_template("dashboard.html", page="dashboard")


@app.route("/forecast")
def forecast_view():
    return render_template("forecast.html", page="forecast")


@app.route("/requests")
def requests_view():
    return render_template("requests.html", page="requests")


@app.route("/alerts")
def alerts_view():
    return render_template("alerts.html", page="alerts")


@app.route("/camp")
def camp_view():
    return render_template("camp.html", page="camp")


@app.route("/donors")
def donors_view():
    return render_template("donors.html", page="donors")


@app.route("/log")
def log_view():
    return render_template("log.html", page="log")


# ------------------------------------------------------------- Authentication Views
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        next_url = request.form.get("next") or request.args.get("next") or url_for("dashboard")

        with conn() as c:
            u = c.execute("SELECT * FROM users WHERE username = ? OR email = ?", (username, username)).fetchone()
            if u and check_password_hash(u["password_hash"], password):
                session["user_id"] = u["id"]
                session["username"] = u["username"]
                session["user_role"] = u["role"]
                return redirect(next_url)
        return render_template("login.html", error="Invalid username or password.", next=next_url)
    return render_template("login.html", next=request.args.get("next", ""))


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        role = request.form.get("role", "donor").strip().lower()

        if role not in ("donor", "requester"):
            role = "donor"

        if not username or len(username) < 3:
            return render_template("register.html", error="Username must be at least 3 characters.")
        if not email or "@" not in email:
            return render_template("register.html", error="Please provide a valid email address.")
        if not password or len(password) < 6:
            return render_template("register.html", error="Password must be at least 6 characters.")
        if password != confirm:
            return render_template("register.html", error="Passwords do not match.")

        now_str = datetime.now().isoformat()
        with conn() as c:
            existing = c.execute("SELECT id FROM users WHERE username = ? OR email = ?", (username, email)).fetchone()
            if existing:
                return render_template("register.html", error="Username or email is already registered.")

            pwd_hash = generate_password_hash(password)
            c.execute(
                "INSERT INTO users (username, email, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?)",
                (username, email, pwd_hash, role, now_str)
            )
            new_uid = c.execute("SELECT last_insert_rowid()").fetchone()[0]

            # If registered as a donor, optionally create donor record
            if role == "donor":
                bg = request.form.get("blood_group", "O+")
                if bg not in GROUPS:
                    bg = "O+"
                phone = request.form.get("phone", "").strip() or "9800000000"
                area = request.form.get("area", "").strip() or "Civil Lines"
                gender = request.form.get("gender", "M")
                name = request.form.get("full_name", "").strip() or username.capitalize()
                c.execute(
                    """INSERT INTO donors (user_id, name, blood_group, gender, phone, area, last_donation, is_available, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)""",
                    (new_uid, name, bg, gender, phone, area, (date.today() - timedelta(days=120)).isoformat(), now_str)
                )
            c.commit()

        session["user_id"] = new_uid
        session["username"] = username
        session["user_role"] = role
        return redirect(url_for("dashboard"))
    return render_template("register.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("dashboard"))


@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    user_id = session["user_id"]
    with conn() as c:
        user = c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        donor_profile = c.execute("SELECT * FROM donors WHERE user_id = ?", (user_id,)).fetchone()

        if request.method == "POST":
            if user["role"] == "donor":
                phone = request.form.get("phone", "").strip()
                area = request.form.get("area", "").strip()
                bg = request.form.get("blood_group", "").strip()
                is_avail = 1 if request.form.get("is_available") == "1" else 0
                last_don = request.form.get("last_donation", "").strip()

                if bg in GROUPS and phone and area:
                    if donor_profile:
                        c.execute(
                            """UPDATE donors SET phone = ?, area = ?, blood_group = ?, is_available = ?, last_donation = ?
                               WHERE user_id = ?""",
                            (phone, area, bg, is_avail, last_don or None, user_id)
                        )
                    else:
                        c.execute(
                            """INSERT INTO donors (user_id, name, blood_group, gender, phone, area, last_donation, is_available, created_at)
                               VALUES (?, ?, ?, 'M', ?, ?, ?, ?, ?)""",
                            (user_id, user["username"].capitalize(), bg, phone, area, last_don or None, is_avail, datetime.now().isoformat())
                        )
                    c.commit()
                    return render_template("profile.html", user=user, donor=c.execute("SELECT * FROM donors WHERE user_id = ?", (user_id,)).fetchone(), msg="Profile updated successfully!")

        return render_template("profile.html", user=user, donor=donor_profile)


@app.route("/health")
def health_check():
    """Lightweight health check endpoint for cloud load balancers and Render."""
    try:
        with conn() as c:
            c.execute("SELECT 1").fetchone()
        db_status = "connected"
    except Exception as e:
        db_status = f"error: {str(e)}"

    is_healthy = db_status == "connected"
    return jsonify({
        "status": "healthy" if is_healthy else "degraded",
        "database": db_status,
        "service": "LifeLink",
        "timestamp": datetime.now().isoformat()
    }), (200 if is_healthy else 500)


# --------------------------------------------------------------------- API Endpoints
@app.route("/api/me")
def api_me():
    if "user_id" in session:
        return jsonify({
            "authenticated": True,
            "user": {
                "id": session["user_id"],
                "username": session.get("username"),
                "role": session.get("user_role")
            }
        })
    return jsonify({"authenticated": False, "user": None})


@app.route("/api/overview")
def api_overview():
    with conn() as c:
        return jsonify(build_overview(c))


@app.route("/api/forecast/<grp>")
def api_forecast(grp):
    grp = grp.replace(" ", "+").strip()
    if grp not in GROUPS:
        return jsonify(error="Unknown blood group"), 404
    with conn() as c:
        g = daily_frames(c)[grp]
    demand_f, don_f, projected = forecast(g)
    hist = g.tail(30)
    last = g.index.max()
    fdates = [(last + timedelta(days=i + 1)).strftime("%d %b") for i in range(HORIZON)]
    return jsonify({
        "history": {
            "dates": [d.strftime("%d %b") for d in hist.index],
            "stock": hist["stock"].tolist(),
            "donated": hist["donated"].tolist(),
            "issued": hist["issued"].tolist()
        },
        "forecast": {
            "dates": fdates,
            "stock": [round(max(float(x), 0.0), 1) for x in projected],
            "demand": [round(float(x), 1) for x in demand_f],
            "donations": [round(float(x), 1) for x in don_f]
        },
        "methodology": {
            "model": "Damped linear trend regression (NumPy polyfit) + rolling means",
            "horizon_days": HORIZON,
            "target_days": TARGET_DAYS,
            "disclaimer": "Academic forecast model for demonstration; not certified for clinical diagnostic decisions."
        }
    })


@app.route("/api/areas")
def api_areas():
    with conn() as c:
        rows = c.execute("SELECT DISTINCT area FROM donors WHERE area IS NOT NULL AND area != '' ORDER BY area").fetchall()
        return jsonify([r[0] for r in rows])


@app.route("/api/donors")
def api_donors():
    grp = request.args.get("group", "").replace(" ", "+").strip()
    area = request.args.get("area", "").strip()
    search = request.args.get("search", "").strip()

    if grp and grp not in GROUPS and grp.lower() != "all":
        return jsonify(error="Unknown blood group"), 400

    query = "SELECT * FROM donors WHERE 1=1"
    params = []
    if grp and grp in GROUPS:
        query += " AND blood_group = ?"
        params.append(grp)
    if area and area.lower() != "all":
        query += " AND area = ?"
        params.append(area)
    if search:
        query += " AND (name LIKE ? OR phone LIKE ?)"
        params.extend([f"%{search}%", f"%{search}%"])

    today = date.today()
    with conn() as c:
        df = pd.read_sql(query, c, params=params, parse_dates=["last_donation"])
        areas_rows = c.execute("SELECT DISTINCT area FROM donors WHERE area IS NOT NULL AND area != '' ORDER BY area").fetchall()
        areas = [r[0] for r in areas_rows]

    is_admin = session.get("user_role") == "admin"

    if df.empty:
        return jsonify({
            "eligible_total": 0,
            "total_count": 0,
            "donors": [],
            "areas": areas,
            "is_admin": is_admin,
            "disclaimer": "Medical Screening Disclaimer: Automated interval calculations and availability statuses are intended solely for logistical planning. Mandatory clinical screening is required before blood donation."
        })

    gap = df["gender"].map({"M": 90, "F": 120}).fillna(120)       # minimum rest in days
    df["days_since"] = (pd.Timestamp(today) - df["last_donation"]).dt.days
    df["eligible"] = (df["days_since"] >= gap) & (df["is_available"] == 1)

    # Sort: eligible first, then longest rested
    df = df.sort_values(by=["eligible", "days_since"], ascending=[False, False])
    eligible_count = int(df["eligible"].sum())
    total_count = int(len(df))

    out_df = df.head(30).copy()

    # Privacy protection: mask phone numbers for unauthenticated / non-admin viewers
    def mask_phone(p):
        p_str = str(p)
        if is_admin:
            return p_str
        if len(p_str) >= 6:
            return p_str[:2] + "******" + p_str[-2:]
        return "******"

    out_df["phone"] = out_df["phone"].apply(mask_phone)
    out_df["last_donation"] = out_df["last_donation"].dt.strftime("%Y-%m-%d").fillna("Never")

    records = out_df[["id", "name", "blood_group", "gender", "phone", "area", "days_since", "eligible", "is_available"]].to_dict("records")
    return jsonify({
        "eligible_total": eligible_count,
        "total_count": total_count,
        "donors": records,
        "areas": areas,
        "is_admin": is_admin,
        "disclaimer": "Medical Screening Disclaimer: Automated interval calculations and availability statuses are intended solely for logistical planning. Mandatory clinical screening is required before blood donation."
    })


def parse_entry(data, need_hospital=False):
    grp = str(data.get("blood_group", "")).replace(" ", "+").strip()
    try:
        units = int(data.get("units"))
    except (TypeError, ValueError):
        units = 0
    if grp not in GROUPS:
        return None, None, "Choose a valid blood group."
    if not 1 <= units <= 50:
        return None, None, "Units must be between 1 and 50."
    if need_hospital and not str(data.get("hospital", "")).strip():
        return None, None, "Enter the hospital name."
    return grp, units, None


# ------------------------------------------------------------- Request Management APIs
@app.route("/api/requests", methods=["GET", "POST"])
def api_requests():
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        grp, units, err = parse_entry(data, need_hospital=True)
        if err:
            return jsonify(error=err), 400

        hospital = data["hospital"].strip()
        patient_name = data.get("patient_name", "").strip() or "Emergency / Inpatient"
        urgency = data.get("urgency", "Normal").strip().title()
        if urgency not in ("Normal", "Urgent", "Emergency"):
            urgency = "Normal"
        notes = data.get("notes", "").strip()
        user_id = session.get("user_id")

        today_str = date.today().isoformat()
        now_str = datetime.now().isoformat()

        with conn() as c:
            c.execute(
                """INSERT INTO requests (user_id, req_date, hospital, patient_name, blood_group, units, urgency, status, notes, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'Pending', ?, ?)""",
                (user_id, today_str, hospital, patient_name, grp, units, urgency, notes, now_str)
            )
            req_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
            c.commit()

        return jsonify(
            ok=True,
            request_id=req_id,
            message=f"Blood request #{req_id} ({units} units of {grp}) submitted successfully with status Pending."
        ), 201

    # GET requests
    status_filter = request.args.get("status", "").strip()
    role = session.get("user_role")
    user_id = session.get("user_id")

    query = "SELECT * FROM requests WHERE 1=1"
    params = []

    # If requester, only view own requests unless admin
    if role == "requester":
        query += " AND user_id = ?"
        params.append(user_id)
    elif role != "admin":
        # Public or unauthenticated can view non-confidential list or recent
        pass

    if status_filter and status_filter.lower() != "all":
        query += " AND status = ?"
        params.append(status_filter.capitalize())

    query += " ORDER BY id DESC LIMIT 50"

    with conn() as c:
        rows = c.execute(query, params).fetchall()
        req_list = [dict(r) for r in rows]

    return jsonify({
        "requests": req_list,
        "is_admin": role == "admin"
    })


@app.route("/api/requests/<int:req_id>/status", methods=["POST"])
@roles_required("admin")
def api_request_status(req_id):
    data = request.get_json(silent=True) or {}
    new_status = data.get("status", "").strip().capitalize()
    if new_status not in ("Approved", "Fulfilled", "Rejected", "Cancelled"):
        return jsonify(error="Invalid status transition."), 400

    admin_user = session.get("username", "Admin")
    now_str = datetime.now().isoformat()
    today_str = date.today().isoformat()

    with conn() as c:
        c.execute("BEGIN IMMEDIATE")
        req = c.execute("SELECT * FROM requests WHERE id = ?", (req_id,)).fetchone()
        if not req:
            c.rollback()
            return jsonify(error=f"Request #{req_id} not found."), 404

        current_status = req["status"]
        if current_status == "Fulfilled":
            c.rollback()
            return jsonify(error=f"Request #{req_id} has already been fulfilled."), 400
        if current_status in ("Rejected", "Cancelled"):
            c.rollback()
            return jsonify(error=f"Request #{req_id} is already {current_status.lower()}."), 400

        # Handle fulfillment with inventory deduction
        if new_status == "Fulfilled":
            grp = req["blood_group"]
            units = req["units"]
            available = get_current_stock(c, grp)
            if units > available:
                c.rollback()
                return jsonify(error=f"Cannot fulfill: Only {available} unit(s) of {grp} available in stock (requested {units})."), 400

            # Increment issued in blood_stock for today
            c.execute("""INSERT INTO blood_stock(date, blood_group, issued) VALUES(?,?,?)
                         ON CONFLICT(date, blood_group) DO UPDATE SET issued = issued + excluded.issued""",
                      (today_str, grp, units))

            # Record in audit trail
            remarks = f"Request #{req_id} fulfilled for {req['hospital']} (Patient: {req['patient_name'] or 'N/A'})"
            c.execute("""INSERT INTO inventory_transactions(trans_date, blood_group, trans_type, units, reference_id, remarks, performed_by)
                         VALUES (?, ?, 'issuance', ?, ?, ?, ?)""",
                      (today_str, grp, units, req_id, remarks, admin_user))

        # Update request status
        c.execute("UPDATE requests SET status = ?, updated_at = ? WHERE id = ?", (new_status, now_str, req_id))
        c.commit()

    return jsonify(ok=True, message=f"Request #{req_id} status updated to {new_status}.")


# ------------------------------------------------------------- Inventory Operations
@app.route("/api/donation", methods=["POST"])
@login_required
def api_donation():
    data = request.get_json(silent=True) or {}
    grp, units, err = parse_entry(data)
    if err:
        return jsonify(error=err), 400

    user_name = session.get("username", "Staff")
    today_str = date.today().isoformat()
    remarks = data.get("remarks", "Direct camp / facility donation entry").strip()

    with conn() as c:
        c.execute("BEGIN IMMEDIATE")
        c.execute("""INSERT INTO blood_stock(date, blood_group, donated) VALUES(?,?,?)
                     ON CONFLICT(date, blood_group) DO UPDATE SET donated = donated + excluded.donated""",
                  (today_str, grp, units))
        c.execute("""INSERT INTO inventory_transactions(trans_date, blood_group, trans_type, units, remarks, performed_by)
                     VALUES (?, ?, 'donation', ?, ?, ?)""",
                  (today_str, grp, units, remarks, user_name))
        c.commit()

    return jsonify(ok=True, message=f"Successfully logged {units} unit(s) of {grp}.")


@app.route("/api/request", methods=["POST"])
def api_request_legacy():
    """Direct request API with automated or manual triage."""
    data = request.get_json(silent=True) or {}
    grp, units, err = parse_entry(data, need_hospital=True)
    if err:
        return jsonify(error=err), 400

    hospital = data["hospital"].strip()
    user_id = session.get("user_id")
    today_str = date.today().isoformat()
    now_str = datetime.now().isoformat()
    user_role = session.get("user_role")

    with conn() as c:
        # If admin, issue immediately if stock permits
        if user_role == "admin":
            c.execute("BEGIN IMMEDIATE")
            have = get_current_stock(c, grp)
            if units > have:
                c.rollback()
                return jsonify(error=f"Only {have} unit(s) of {grp} currently available in stock."), 400
            
            c.execute(
                """INSERT INTO requests(user_id, req_date, hospital, blood_group, units, status, updated_at)
                   VALUES(?,?,?,?,?,'Fulfilled',?)""",
                (user_id, today_str, hospital, grp, units, now_str)
            )
            req_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
            c.execute("""INSERT INTO blood_stock(date, blood_group, issued) VALUES(?,?,?)
                         ON CONFLICT(date, blood_group) DO UPDATE SET issued = issued + excluded.issued""",
                      (today_str, grp, units))
            c.execute("""INSERT INTO inventory_transactions(trans_date, blood_group, trans_type, units, reference_id, remarks, performed_by)
                         VALUES (?, ?, 'issuance', ?, ?, ?, ?)""",
                      (today_str, grp, units, req_id, f"Direct issue to {hospital}", session.get("username", "Admin")))
            c.commit()
            return jsonify(ok=True, message=f"Directly issued {units} unit(s) of {grp} to {hospital}.")
        else:
            # Requesters submit as Pending
            c.execute(
                """INSERT INTO requests(user_id, req_date, hospital, blood_group, units, status, updated_at)
                   VALUES(?,?,?,?,?,'Pending',?)""",
                (user_id, today_str, hospital, grp, units, now_str)
            )
            req_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
            c.commit()
            return jsonify(ok=True, message=f"Request #{req_id} for {units} unit(s) of {grp} submitted and pending administrator approval.")


@app.route("/api/expiry", methods=["POST"])
@roles_required("admin")
def api_expiry():
    """Records disposal of expired units according to safe biomedical protocols."""
    data = request.get_json(silent=True) or {}
    grp, units, err = parse_entry(data)
    if err:
        return jsonify(error=err), 400

    today_str = date.today().isoformat()
    admin_user = session.get("username", "Admin")
    reason = data.get("reason", "Expired unit disposal per safety protocol").strip()

    with conn() as c:
        c.execute("BEGIN IMMEDIATE")
        have = get_current_stock(c, grp)
        if units > have:
            c.rollback()
            return jsonify(error=f"Cannot record expiry: only {have} unit(s) of {grp} in stock."), 400

        c.execute("""INSERT INTO blood_stock(date, blood_group, expired) VALUES(?,?,?)
                     ON CONFLICT(date, blood_group) DO UPDATE SET expired = expired + excluded.expired""",
                  (today_str, grp, units))
        c.execute("""INSERT INTO inventory_transactions(trans_date, blood_group, trans_type, units, remarks, performed_by)
                     VALUES (?, ?, 'expiry', ?, ?, ?)""",
                  (today_str, grp, units, reason, admin_user))
        c.commit()

    return jsonify(ok=True, message=f"Recorded {units} expired unit(s) of {grp} discarded.")


@app.route("/api/transactions")
@roles_required("admin")
def api_transactions():
    """Returns recent inventory audit transactions."""
    with conn() as c:
        rows = c.execute("SELECT * FROM inventory_transactions ORDER BY id DESC LIMIT 30").fetchall()
        return jsonify([dict(r) for r in rows])


@app.route("/api/reset", methods=["POST"])
@roles_required("admin")
def api_reset():
    """Admin-protected reset of demo datasets."""
    with conn() as c:
        seed(c, wipe=True)
    return jsonify(ok=True, message="Demo data restored successfully.")


# ------------------------------------------------------------- Startup & Init
init_db()

if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG", "0").lower() in ("1", "true")
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=debug)
