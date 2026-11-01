"""Generates demo CSVs in ./data. Run: python generate_data.py"""
import os
from datetime import date, timedelta
import numpy as np
import pandas as pd

rng = np.random.default_rng(42)
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(OUT, exist_ok=True)

# average units issued per day
DEMAND = {"O+": 20, "B+": 17, "A+": 12, "AB+": 4, "O-": 2.2, "B-": 1.6, "A-": 1.4, "AB-": 0.6}
DAYS = 365
end = date.today() - timedelta(days=1)
dates = [end - timedelta(days=DAYS - 1 - i) for i in range(DAYS)]

rows, opening = [], []
for grp, mean in DEMAND.items():
    stock = int(round(mean * 12))
    opening.append({"blood_group": grp, "units": stock})
    for i, d in enumerate(dates):
        days_from_end = DAYS - 1 - i
        dem = mean * (0.88 if d.weekday() == 6 else 1.0)
        don = mean * 1.04
        if d.month in (5, 6):            # summer dip
            don *= 0.85
        if d.weekday() in (5, 6):        # weekend camps
            don *= 1.2
        if rng.random() < 0.03:          # festival / exam day with almost no donors
            don *= 0.2
        if days_from_end < 12:           # dengue season surge
            dem *= {"O+": 1.35, "O-": 1.5, "B+": 1.2}.get(grp, 1.0)
        if days_from_end < 21:           # recent donation drought
            don *= {"O-": 0.5, "A-": 0.5, "B+": 0.88, "O+": 0.95}.get(grp, 1.0)
        donated = int(rng.poisson(don))
        issued = int(rng.poisson(dem))
        avail = stock + donated
        issued = min(issued, avail)
        expired = min(int(rng.poisson(stock * 0.008)), avail - issued)
        stock = avail - issued - expired
        rows.append({"date": d.isoformat(), "blood_group": grp,
                     "donated": donated, "issued": issued, "expired": expired})

pd.DataFrame(rows).to_csv(os.path.join(OUT, "stock_log.csv"), index=False)
pd.DataFrame(opening).to_csv(os.path.join(OUT, "opening_stock.csv"), index=False)

# donors
first = ["Aarav", "Riya", "Kabir", "Meera", "Arjun", "Sana", "Vikram", "Ananya", "Rohan", "Isha",
         "Imran", "Priya", "Karan", "Neha", "Rahul", "Divya", "Sameer", "Pooja", "Aditya", "Fatima"]
last = ["Sharma", "Verma", "Khan", "Patel", "Singh", "Gupta", "Nair", "Reddy", "Das", "Joshi", "Mehta", "Ali"]
areas = ["Civil Lines", "Model Town", "Railway Colony", "Sector 12", "Old City", "Lake View", "Market Road"]
groups = list(DEMAND)
weights = np.array([36, 21, 31, 7, 2, 1.5, 1.5, 0.5]); weights = weights / weights.sum()
donors = []
for i in range(400):
    g = rng.choice(["O+", "A+", "B+", "AB+", "O-", "A-", "B-", "AB-"], p=weights)
    donors.append({
        "name": f"{rng.choice(first)} {rng.choice(last)}",
        "blood_group": g,
        "gender": rng.choice(["M", "F"], p=[0.7, 0.3]),
        "phone": "98" + "".join(str(x) for x in rng.integers(0, 10, 8)),
        "area": rng.choice(areas),
        "last_donation": (end - timedelta(days=int(rng.integers(5, 400)))).isoformat(),
    })
pd.DataFrame(donors).to_csv(os.path.join(OUT, "donors.csv"), index=False)

# requests log
hospitals = ["District Hospital", "City Care Hospital", "St. Mary Trauma Centre", "Lifeline Nursing Home", "Govt Medical College"]
reqs = []
for _ in range(60):
    g = rng.choice(groups, p=np.array(list(DEMAND.values())) / sum(DEMAND.values()))
    reqs.append({"req_date": (end - timedelta(days=int(rng.integers(0, 30)))).isoformat(),
                 "hospital": rng.choice(hospitals), "blood_group": g, "units": int(rng.integers(1, 5))})
pd.DataFrame(reqs).sort_values("req_date").to_csv(os.path.join(OUT, "requests.csv"), index=False)
print("Demo data written to", OUT)
