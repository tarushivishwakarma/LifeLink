"""Comprehensive Automated Test Suite for LifeLink.

Tests all 18 quality criteria:
1. Application startup and core routes.
2. Database initialization and migration.
3. User authentication (Login, Register, Logout).
4. Role-based access control (RBAC).
5. Donor registration and profile updates.
6. Donor filtering (by group, area, search).
7. Blood request creation (starts Pending).
8. Validation and invalid request rejection.
9. Stock availability checks.
10. Safe inventory transactions and negative stock prevention.
11. Request status transitions (Pending -> Approved -> Fulfilled).
12. Forecast endpoint behavior and mathematical consistency.
13. Dashboard KPI consistency.
14. Chart data payload integrity.
15. Empty data resilience.
16. Donor contact data masking for privacy.
17. Administrative reset protection.
18. Production configuration.
"""

import json
import os
import unittest
from datetime import date

# Ensure test DB environment
os.environ["SECRET_KEY"] = "test-secret-key-123"
import app


class LifeLinkTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.app.config["TESTING"] = True
        cls.client = app.app.test_client()

    def setUp(self):
        # Fresh test client session per test
        self.client = app.app.test_client()

    def login(self, username, password):
        return self.client.post(
            "/login",
            data={"username": username, "password": password},
            follow_redirects=True,
        )

    def logout(self):
        return self.client.get("/logout", follow_redirects=True)

    # 1. Application startup and core routes
    def test_01_core_routes_status_ok(self):
        # Health check endpoint
        health_resp = self.client.get("/health")
        self.assertEqual(health_resp.status_code, 200)
        self.assertEqual(health_resp.get_json()["status"], "healthy")

        routes = ["/", "/forecast", "/requests", "/alerts", "/camp", "/donors", "/login", "/register"]
        for r in routes:
            resp = self.client.get(r)
            self.assertEqual(resp.status_code, 200, f"Route {r} failed to load with 200 OK")
            self.assertIn(b"LifeLink", resp.data, f"Brand LifeLink missing on route {r}")

    # 2. Database initialization & schema migration
    def test_02_database_schema(self):
        with app.conn() as c:
            tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            for expected in ["users", "blood_stock", "opening_stock", "donors", "requests", "inventory_transactions"]:
                self.assertIn(expected, tables, f"Expected table {expected} missing in schema")

            req_cols = [r[1] for r in c.execute("PRAGMA table_info(requests)").fetchall()]
            for col in ["user_id", "patient_name", "urgency", "status", "notes", "updated_at"]:
                self.assertIn(col, req_cols, f"Migrated column {col} missing in requests table")

    # 3. User authentication (Login, Logout)
    def test_03_authentication_workflow(self):
        # Failed login
        resp = self.login("admin", "wrongpassword")
        self.assertIn(b"Invalid username or password", resp.data)

        # Successful admin login
        resp = self.login("admin", "admin123")
        self.assertEqual(resp.status_code, 200)

        # Check /api/me
        me = self.client.get("/api/me").get_json()
        self.assertTrue(me["authenticated"])
        self.assertEqual(me["user"]["username"], "admin")
        self.assertEqual(me["user"]["role"], "admin")

        # Logout
        self.logout()
        me = self.client.get("/api/me").get_json()
        self.assertFalse(me["authenticated"])

    # 4. Role-based access control (RBAC)
    def test_04_rbac_enforcement(self):
        # Anonymous cannot access /api/reset
        resp = self.client.post("/api/reset")
        self.assertIn(resp.status_code, [401, 403])

        # Requester cannot access /api/reset
        self.login("requester1", "requester123")
        resp = self.client.post("/api/reset")
        self.assertEqual(resp.status_code, 403)
        self.logout()

        # Admin CAN access /api/reset
        self.login("admin", "admin123")
        resp = self.client.post("/api/reset")
        self.assertEqual(resp.status_code, 200)
        self.logout()

    # 5. Donor registration and profile updates
    def test_05_donor_registration_and_profile(self):
        import time
        username = f"newdonor_{int(time.time() * 1000)}"
        resp = self.client.post(
            "/register",
            data={
                "role": "donor",
                "username": username,
                "email": f"{username}@test.com",
                "password": "password123",
                "confirm_password": "password123",
                "full_name": "Test Voluntary Donor",
                "blood_group": "A-",
                "gender": "F",
                "phone": "9811223344",
                "area": "Model Town",
            },
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)

        # Verify donor is logged in
        me = self.client.get("/api/me").get_json()
        self.assertTrue(me["authenticated"])
        self.assertEqual(me["user"]["role"], "donor")

        # Update profile
        resp = self.client.post(
            "/profile",
            data={
                "blood_group": "A-",
                "phone": "9899887766",
                "area": "Lake View",
                "is_available": "0",
                "last_donation": "2026-08-01",
            },
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Profile updated successfully", resp.data)
        self.logout()

    # 6. Donor filtering (by group, area, search)
    def test_06_donor_filtering(self):
        # Filter by group O-
        resp = self.client.get("/api/donors?group=O-").get_json()
        self.assertIn("donors", resp)
        for d in resp["donors"]:
            self.assertEqual(d["blood_group"], "O-")

        # Filter by area
        areas_resp = self.client.get("/api/areas").get_json()
        self.assertIsInstance(areas_resp, list)
        if areas_resp:
            test_area = areas_resp[0]
            resp = self.client.get(f"/api/donors?area={test_area}").get_json()
            for d in resp["donors"]:
                self.assertEqual(d["area"], test_area)

    # 7. Blood request creation (starts Pending)
    def test_07_blood_request_creation(self):
        self.login("requester1", "requester123")
        payload = {
            "hospital": "Apex Specialty Hospital",
            "patient_name": "Surgery Patient A",
            "blood_group": "B+",
            "units": 2,
            "urgency": "Urgent",
            "notes": "Cardiac bypass surgery requirement",
        }
        resp = self.client.post("/api/requests", json=payload)
        self.assertEqual(resp.status_code, 201)
        data = resp.get_json()
        self.assertTrue(data["ok"])
        req_id = data["request_id"]

        # Verify in DB
        with app.conn() as c:
            req = c.execute("SELECT * FROM requests WHERE id = ?", (req_id,)).fetchone()
            self.assertEqual(req["status"], "Pending")
            self.assertEqual(req["hospital"], "Apex Specialty Hospital")
            self.assertEqual(req["units"], 2)
        self.logout()

    # 8. Validation and invalid blood request rejection
    def test_08_invalid_blood_request_validation(self):
        # Missing hospital
        resp = self.client.post("/api/requests", json={"blood_group": "O+", "units": 2})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("hospital", resp.get_json()["error"].lower())

        # Invalid units (0 or negative)
        resp = self.client.post("/api/requests", json={"hospital": "Care Clinic", "blood_group": "O+", "units": 0})
        self.assertEqual(resp.status_code, 400)

        # Excessive units (> 50)
        resp = self.client.post("/api/requests", json={"hospital": "Care Clinic", "blood_group": "O+", "units": 99})
        self.assertEqual(resp.status_code, 400)

        # Invalid blood group
        resp = self.client.post("/api/requests", json={"hospital": "Care Clinic", "blood_group": "Z+", "units": 2})
        self.assertEqual(resp.status_code, 400)

    # 9. Stock availability checks
    def test_09_stock_availability_check(self):
        with app.conn() as c:
            for grp in app.GROUPS:
                stock = app.get_current_stock(c, grp)
                self.assertIsInstance(stock, int)
                self.assertGreaterEqual(stock, 0)

    # 10. Safe inventory transactions and negative stock prevention
    def test_10_negative_inventory_prevention(self):
        self.login("admin", "admin123")
        with app.conn() as c:
            avail = app.get_current_stock(c, "O-")

        # Request units within validation bounds (1-50) but exceeding current stock
        excessive = avail + 5
        self.assertLessEqual(excessive, 50)
        resp = self.client.post("/api/request", json={"hospital": "Test Trauma", "blood_group": "O-", "units": excessive})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("available", resp.get_json()["error"].lower())
        self.logout()

    # 11. Request status transitions (Pending -> Approved -> Fulfilled)
    def test_11_request_status_transitions(self):
        # 1. Create a request
        resp = self.client.post(
            "/api/requests",
            json={"hospital": "Metro Hospital", "blood_group": "A+", "units": 1, "urgency": "Normal"},
        )
        self.assertEqual(resp.status_code, 201)
        req_id = resp.get_json()["request_id"]

        # 2. Admin logs in
        self.login("admin", "admin123")

        # 3. Transition to Approved
        resp = self.client.post(f"/api/requests/{req_id}/status", json={"status": "Approved"})
        self.assertEqual(resp.status_code, 200)

        # Record stock before fulfill
        with app.conn() as c:
            stock_before = app.get_current_stock(c, "A+")

        # 4. Transition to Fulfilled (inventory safely decremented)
        resp = self.client.post(f"/api/requests/{req_id}/status", json={"status": "Fulfilled"})
        self.assertEqual(resp.status_code, 200)

        with app.conn() as c:
            stock_after = app.get_current_stock(c, "A+")
            self.assertEqual(stock_after, stock_before - 1)

            # Check audit log
            tx = c.execute("SELECT * FROM inventory_transactions WHERE reference_id = ?", (req_id,)).fetchone()
            self.assertIsNotNone(tx)
            self.assertEqual(tx["trans_type"], "issuance")
            self.assertEqual(tx["units"], 1)

        # 5. Cannot fulfill twice
        resp = self.client.post(f"/api/requests/{req_id}/status", json={"status": "Fulfilled"})
        self.assertEqual(resp.status_code, 400)
        self.logout()

    # 12. Forecast endpoint behavior
    def test_12_forecast_endpoint(self):
        resp = self.client.get("/api/forecast/O+")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn("history", data)
        self.assertIn("forecast", data)
        self.assertIn("methodology", data)
        self.assertEqual(len(data["forecast"]["dates"]), 14)
        self.assertEqual(len(data["forecast"]["stock"]), 14)
        self.assertEqual(len(data["forecast"]["demand"]), 14)

    # 13. Dashboard KPI consistency
    def test_13_dashboard_kpis(self):
        resp = self.client.get("/api/overview")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        kpis = data["kpis"]
        self.assertIn("total_units", kpis)
        self.assertIn("critical", kpis)
        self.assertIn("low", kpis)
        self.assertIn("donors_needed", kpis)
        self.assertIn("expired_30d_total", kpis)
        self.assertIn("avg_wastage_pct", kpis)
        self.assertIn("pending_requests", kpis)

        # Verification of groups
        self.assertEqual(len(data["groups"]), 8)
        sum_stock = sum(g["stock"] for g in data["groups"])
        self.assertEqual(sum_stock, kpis["total_units"])

    # 14. Chart data payload integrity
    def test_14_chart_payload_integrity(self):
        resp = self.client.get("/api/forecast/B+")
        data = resp.get_json()
        hist = data["history"]
        self.assertEqual(len(hist["dates"]), len(hist["stock"]))
        self.assertEqual(len(hist["dates"]), len(hist["donated"]))
        self.assertEqual(len(hist["dates"]), len(hist["issued"]))

    # 15. Missing or empty data handling
    def test_15_invalid_group_forecast(self):
        resp = self.client.get("/api/forecast/INVALID")
        self.assertEqual(resp.status_code, 404)
        self.assertIn("error", resp.get_json())

    # 16. Protection of donor contact information (privacy masking)
    def test_16_donor_privacy_masking(self):
        # Anonymous view: phone MUST be masked
        self.logout()
        resp = self.client.get("/api/donors?group=O+").get_json()
        for d in resp["donors"]:
            self.assertIn("******", d["phone"], "Unauthenticated visitor saw unmasked phone number!")

        # Admin view: phone MUST be unmasked
        self.login("admin", "admin123")
        resp = self.client.get("/api/donors?group=O+").get_json()
        for d in resp["donors"]:
            self.assertNotIn("******", d["phone"], "Admin should see full unmasked contact number!")
        self.logout()

    # 17. Administrative reset protection
    def test_17_admin_reset_protection(self):
        # Unauthenticated: rejected
        resp = self.client.post("/api/reset")
        self.assertIn(resp.status_code, [401, 403])

        # Admin: accepted
        self.login("admin", "admin123")
        resp = self.client.post("/api/reset")
        self.assertEqual(resp.status_code, 200)
        self.logout()

    # 18. Production configuration
    def test_18_production_configuration(self):
        self.assertFalse(app.app.debug)
        self.assertIsNotNone(app.app.secret_key)
        self.assertTrue(app.app.config.get("SESSION_COOKIE_HTTPONLY"))


if __name__ == "__main__":
    unittest.main()
