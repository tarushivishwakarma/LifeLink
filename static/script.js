const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);

function cssVar(n) {
  return getComputedStyle(document.documentElement).getPropertyValue(n).trim();
}

const COLORS = {
  critical: cssVar("--critical") || "#B3122F",
  low: cssVar("--low") || "#C56A15",
  safe: cssVar("--safe") || "#1E7E62",
  info: cssVar("--info") || "#2B6CB0",
};

const BODY_PATH = "M10 24Q10 14 20 14H60Q70 14 70 24V92Q70 104 58 104H22Q10 104 10 92Z";
const GROUPS = ["O+", "A+", "B+", "AB+", "O-", "A-", "B-", "AB-"];
const qg = new URLSearchParams(location.search).get("g");
let selected = GROUPS.includes(qg) ? qg : "O-";
let stockChart, flowChart;

// Toast notifications
function showToast(text, type = "info") {
  let toast = $("#toast");
  if (!toast) {
    toast = document.createElement("div");
    toast.id = "toast";
    toast.className = "toast";
    document.body.appendChild(toast);
  }
  toast.textContent = text;
  toast.className = `toast ${type} show`;
  setTimeout(() => {
    toast.className = "toast";
  }, 4000);
}

async function api(url, opts = {}) {
  try {
    const r = await fetch(url, opts);
    const d = await r.json().catch(() => ({}));
    if (!r.ok) {
      throw new Error(d.error || `Request failed with status ${r.status}`);
    }
    return d;
  } catch (err) {
    console.error(`API Error on ${url}:`, err);
    throw err;
  }
}

const idFor = (g) => "clip" + g.replace("+", "p").replace("-", "n");

function bagSVG(g) {
  const frac = Math.max(0.05, Math.min(1, g.cover_days / 21));
  const top = 104 - frac * 90;
  const statusColor = COLORS[g.status] || COLORS.safe;
  return `<svg viewBox="0 0 80 110" class="bag" aria-hidden="true">
    <defs><clipPath id="${idFor(g.group)}"><path d="${BODY_PATH}"/></clipPath></defs>
    <g clip-path="url(#${idFor(g.group)})">
      <rect class="liquid" x="0" y="${top}" width="80" height="110" fill="${statusColor}"/>
    </g>
    <path class="outline" d="${BODY_PATH}" fill="none"/>
    <path class="outline" d="M34 14V4h12v10" fill="none"/>
  </svg>`;
}

function fillGroupSelects() {
  $$(".grp").forEach((sel) => {
    sel.innerHTML = GROUPS.map((g) => `<option value="${g}">${g}</option>`).join("");
  });
  const gs = $("#groupSel");
  if (gs && !gs.children.length) {
    gs.innerHTML = GROUPS.map((g) => `<option value="${g}">${g}</option>`).join("");
    gs.value = selected;
  }
}

// ------------------------------------------------------------- Pages
const pages = {
  // 1. Dashboard
  async dashboard() {
    try {
      const o = await api("/api/overview");
      const k = o.kpis;

      const a = $("#asOf");
      if (a) a.textContent = k.as_of;

      $("#kpis").innerHTML = `
        <div class="kpi">
          <b>${k.total_units}</b>
          <span>Total Units in Stock</span>
        </div>
        <div class="kpi ${k.critical ? "bad" : ""}">
          <b>${k.critical}</b>
          <span>Critical Groups (&lt; 3d)</span>
        </div>
        <div class="kpi ${k.low ? "warning" : ""}">
          <b>${k.low}</b>
          <span>Low Groups (&lt; 7d)</span>
        </div>
        <div class="kpi">
          <b>${k.donors_needed}</b>
          <span>Donors to Mobilize</span>
        </div>
        <div class="kpi">
          <b>${k.expired_30d_total || 0} <small style="font-size:1.1rem;font-weight:500;">(${k.avg_wastage_pct || 0}%)</small></b>
          <span>30-Day Expiry Losses</span>
        </div>
        <div class="kpi">
          <b>${k.pending_requests || 0}</b>
          <span>Active Pending Requests</span>
        </div>`;

      // Render bags
      $("#bags").innerHTML = o.groups
        .map(
          (g) => `
        <a class="bagbtn" href="/forecast?g=${encodeURIComponent(g.group)}">
          ${bagSVG(g)}
          <strong>${g.group}</strong>
          <span class="u">${g.stock} unit${g.stock === 1 ? "" : "s"}</span>
          <span class="c">${g.cover_days} days cover</span>
          <span class="tag ${g.status}">${g.status}</span>
          ${g.rare ? '<span class="rare">rare Rh-</span>' : ""}
        </a>`
        )
        .join("");

      // Render Expiry & Wastage table
      const wb = $("#wastageBody");
      if (wb) {
        wb.innerHTML = o.groups
          .map(
            (g) => `
          <tr>
            <td><strong>${g.group}</strong> ${g.rare ? '<span class="rare">rare</span>' : ""}</td>
            <td>${g.stock} units</td>
            <td>${g.expired_30d} units</td>
            <td><strong>${g.expired_pct}%</strong></td>
            <td>
              <span class="tag ${g.expired_pct >= 5 ? "low" : "safe"}">
                ${g.expired_pct >= 5 ? "Elevated Loss" : "Nominal"}
              </span>
            </td>
          </tr>`
          )
          .join("");
      }

      // Compact alerts
      const da = $("#dashboardAlerts");
      if (da) {
        da.innerHTML = o.alerts.length
          ? o.alerts
              .slice(0, 4)
              .map(
                (al) => `
            <li class="${al.level}">
              <b>${al.title}</b>
              <span class="text-muted" style="font-size:0.88rem;">${al.text}</span>
            </li>`
              )
              .join("")
          : `<li><b>No active alerts</b><span class="text-muted">All inventories are within normal thresholds.</span></li>`;
      }
    } catch (err) {
      showToast("Failed to load dashboard overview.", "error");
    }
  },

  // 2. Forecast
  async forecast() {
    fillGroupSelects();
    const gs = $("#groupSel");
    if (gs) {
      gs.value = selected;
      gs.addEventListener("change", (e) => {
        selected = e.target.value;
        history.replaceState(null, "", "?g=" + encodeURIComponent(selected));
        loadForecastData();
      });
    }

    async function loadForecastData() {
      try {
        const [o, f] = await Promise.all([
          api("/api/overview"),
          api("/api/forecast/" + encodeURIComponent(selected)),
        ]);
        const g = o.groups.find((x) => x.group === selected) || {};

        $("#chartTitle").textContent = `${selected} Reserve Trajectory & Flow`;
        $("#fstats").innerHTML = `
          <div class="stat"><b>${g.stock || 0}</b><span>Units in Stock</span></div>
          <div class="stat"><b><span class="tag ${g.status || "safe"}">${g.cover_days || 0} days</span></b><span>Coverage Horizon</span></div>
          <div class="stat"><b>${g.demand_daily || 0}</b><span>Forecasted Daily Demand</span></div>
          <div class="stat"><b>${g.donation_daily || 0}</b><span>Daily Inflow (Donations)</span></div>
          <div class="stat"><b>${g.run_out || "None (&gt;14d)"}</b><span>Est. Run-Out Date</span></div>`;

        const h = f.history;
        const p = f.forecast;
        const labels = [...h.dates, ...p.dates];
        const histData = [...h.stock, ...p.dates.map(() => null)];
        const projData = [...h.dates.map(() => null), ...p.stock];
        projData[h.dates.length - 1] = h.stock[h.stock.length - 1];

        const ink = cssVar("--ink") || "#1B2128";
        const grid = cssVar("--line") || "#E1E6EB";

        const baseOpts = {
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: { labels: { color: ink, font: { family: "DM Sans" } } },
          },
          scales: {
            x: {
              ticks: { color: ink, maxTicksLimit: 10, font: { family: "DM Sans" } },
              grid: { color: grid },
            },
            y: {
              beginAtZero: true,
              ticks: { color: ink, font: { family: "DM Sans" } },
              grid: { color: grid },
            },
          },
        };

        if (stockChart) stockChart.destroy();
        stockChart = new Chart($("#stockChart"), {
          type: "line",
          data: {
            labels,
            datasets: [
              {
                label: "Actual Stock (Last 30 Days)",
                data: histData,
                borderColor: ink,
                backgroundColor: ink,
                borderWidth: 2.2,
                pointRadius: 0,
                tension: 0.25,
              },
              {
                label: "Projected Stock (Next 14 Days)",
                data: projData,
                borderColor: COLORS.critical,
                backgroundColor: COLORS.critical,
                borderWidth: 2.2,
                borderDash: [6, 4],
                pointRadius: 0,
                tension: 0.25,
              },
            ],
          },
          options: baseOpts,
        });

        if (flowChart) flowChart.destroy();
        flowChart = new Chart($("#flowChart"), {
          type: "bar",
          data: {
            labels: h.dates,
            datasets: [
              {
                label: "Units Donated",
                data: h.donated,
                backgroundColor: COLORS.safe,
              },
              {
                label: "Units Issued to Hospitals",
                data: h.issued,
                backgroundColor: COLORS.critical,
              },
            ],
          },
          options: baseOpts,
        });
      } catch (err) {
        showToast("Error loading forecast data.", "error");
      }
    }

    await loadForecastData();
  },

  // 3. Requests Management
  async requests() {
    fillGroupSelects();
    const tableBody = $("#requestsBody");
    const statusFilter = $("#statusFilter");

    async function loadRequests() {
      try {
        const filterVal = statusFilter ? statusFilter.value : "all";
        const res = await api(`/api/requests?status=${encodeURIComponent(filterVal)}`);
        const reqs = res.requests;
        const isAdmin = res.is_admin;

        if (!reqs || !reqs.length) {
          tableBody.innerHTML = `<tr><td colspan="9" class="text-center text-muted">No blood requests found for this filter.</td></tr>`;
          return;
        }

        tableBody.innerHTML = reqs
          .map((r) => {
            const statusClass = `badge-status-${r.status.toLowerCase()}`;
            const urgencyClass = `badge-urgency-${r.urgency.toLowerCase()}`;

            let actionsHtml = `<span class="text-muted">—</span>`;
            if (isAdmin && r.status === "Pending") {
              actionsHtml = `
              <div style="display:flex;gap:4px;">
                <button type="button" class="btn-ghost-sm" onclick="updateReqStatus(${r.id}, 'Approved')">Approve</button>
                <button type="button" class="btn-primary-sm" onclick="updateReqStatus(${r.id}, 'Fulfilled')">Fulfill</button>
                <button type="button" class="btn-outline-danger" style="padding:4px 8px;font-size:0.78rem;" onclick="updateReqStatus(${r.id}, 'Rejected')">Reject</button>
              </div>`;
            } else if (isAdmin && r.status === "Approved") {
              actionsHtml = `
              <div style="display:flex;gap:4px;">
                <button type="button" class="btn-primary-sm" onclick="updateReqStatus(${r.id}, 'Fulfilled')">Fulfill & Dispatch</button>
                <button type="button" class="btn-outline-danger" style="padding:4px 8px;font-size:0.78rem;" onclick="updateReqStatus(${r.id}, 'Rejected')">Reject</button>
              </div>`;
            }

            return `
            <tr>
              <td><strong>#${r.id}</strong></td>
              <td>${r.req_date}</td>
              <td><strong>${r.hospital}</strong></td>
              <td>${r.patient_name || "Emergency"}</td>
              <td><span class="tag ${r.blood_group.includes("-") ? "critical" : "safe"}">${r.blood_group}</span></td>
              <td><strong>${r.units}</strong></td>
              <td><span class="${urgencyClass}">${r.urgency}</span></td>
              <td><span class="${statusClass}">${r.status}</span></td>
              <td>${actionsHtml}</td>
            </tr>`;
          })
          .join("");
      } catch (err) {
        tableBody.innerHTML = `<tr><td colspan="9" class="text-center text-critical">Failed to load requests.</td></tr>`;
      }
    }

    if (statusFilter) {
      statusFilter.addEventListener("change", loadRequests);
    }

    window.updateReqStatus = async function (reqId, status) {
      try {
        const res = await api(`/api/requests/${reqId}/status`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ status }),
        });
        showToast(res.message, "success");
        await loadRequests();
      } catch (err) {
        showToast(err.message, "error");
      }
    };

    const newReqForm = $("#newRequestForm");
    if (newReqForm) {
      newReqForm.addEventListener("submit", async (e) => {
        e.preventDefault();
        const submitBtn = $("#submitReqBtn");
        submitBtn.disabled = true;
        submitBtn.textContent = "Submitting…";
        const body = Object.fromEntries(new FormData(e.target));
        try {
          const res = await api("/api/requests", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
          });
          showToast(res.message, "success");
          newReqForm.reset();
          await loadRequests();
        } catch (err) {
          showToast(err.message, "error");
        } finally {
          submitBtn.disabled = false;
          submitBtn.textContent = "Submit Request";
        }
      });
    }

    await loadRequests();
  },

  // 4. Alerts
  async alerts() {
    try {
      const o = await api("/api/overview");
      const el = $("#alerts");
      el.innerHTML = o.alerts.length
        ? o.alerts
            .map(
              (a) => `
          <li class="${a.level}">
            <b>${a.title}</b>
            <span class="text-muted">${a.text}</span>
          </li>`
            )
            .join("")
        : `<li><b>All Clear</b><span>All blood group inventories are safely above critical thresholds.</span></li>`;
    } catch (err) {
      showToast("Error retrieving alerts.", "error");
    }
  },

  // 5. Camp Planner
  async camp() {
    try {
      const o = await api("/api/overview");
      const tbody = $("#campTable tbody");
      tbody.innerHTML = o.camp.length
        ? o.camp
            .map(
              (r) => `
          <tr>
            <td><strong>${r.group}</strong> ${r.rare ? '<span class="rare">rare Rh-</span>' : ""}</td>
            <td><span class="tag ${r.status}">${r.cover_days} days</span></td>
            <td><strong>${r.gap} units</strong></td>
            <td><strong>${r.invite} donors</strong></td>
            <td><span class="tag ${r.status}">${r.status}</span></td>
            <td>
              <a href="/donors?group=${encodeURIComponent(r.group)}" class="btn-ghost-sm">
                Mobilize Donors &rarr;
              </a>
            </td>
          </tr>`
            )
            .join("")
        : `<tr><td colspan="6" class="text-center text-muted">All blood reserves exceed the 10-day buffer. No emergency camps required at present.</td></tr>`;
    } catch (err) {
      showToast("Error computing camp requirements.", "error");
    }
  },

  // 6. Donors Directory
  async donors() {
    const gs = $("#groupSel");
    const as = $("#areaSel");
    const searchInput = $("#donorSearch");
    const donorsContainer = $("#donors");
    const donorCountSpan = $("#donorCount");

    // Populate Group Dropdown
    if (gs && !gs.children.length) {
      gs.innerHTML = `<option value="all">All Groups</option>` + GROUPS.map((g) => `<option value="${g}">${g}</option>`).join("");
      if (GROUPS.includes(selected)) gs.value = selected;
    }

    async function loadDonorsData() {
      try {
        const groupVal = gs ? gs.value : "all";
        const areaVal = as ? as.value : "all";
        const searchVal = searchInput ? searchInput.value.trim() : "";

        let url = `/api/donors?group=${encodeURIComponent(groupVal)}&area=${encodeURIComponent(areaVal)}&search=${encodeURIComponent(searchVal)}`;
        const d = await api(url);

        // Populate Area Dropdown if needed
        if (as && as.children.length <= 1 && d.areas) {
          d.areas.forEach((area) => {
            const opt = document.createElement("option");
            opt.value = area;
            opt.textContent = area;
            as.appendChild(opt);
          });
        }

        donorCountSpan.textContent = `Found ${d.total_count} registered donors (${d.eligible_total} currently eligible & rested). Showing top priority matches:`;

        if (!d.donors || !d.donors.length) {
          donorsContainer.innerHTML = `<div class="loading-item text-muted">No donor profiles matched the specified filter criteria.</div>`;
          return;
        }

        donorsContainer.innerHTML = d.donors
          .map(
            (x) => `
          <div class="donor-card">
            <div class="donor-card-top">
              <div>
                <strong>${x.name}</strong>
                <div class="text-muted" style="font-size:0.84rem;">${x.area} · ${x.gender === "M" ? "Male" : "Female"}</div>
              </div>
              <span class="donor-group-badge">${x.blood_group}</span>
            </div>

            <div style="font-size:0.86rem;margin-top:6px;">
              <span>Last donated: <strong>${x.days_since !== null ? x.days_since + " days ago" : "First time"}</strong></span>
              <div style="margin-top:3px;">
                <span class="tag ${x.eligible ? "safe" : "low"}">${x.eligible ? "Eligible to Donate" : "Resting / Deferral"}</span>
                ${!x.is_available ? '<span class="tag low" style="margin-left:4px;">Paused</span>' : ""}
              </div>
            </div>

            <div class="donor-contact">
              <span>Contact: <strong>${x.phone}</strong></span>
              ${d.is_admin ? `<a href="tel:${x.phone}" class="btn-ghost-sm" style="padding:2px 8px;">Call</a>` : `<span class="badge-neutral" title="Log in as admin to unmask">Masked</span>`}
            </div>
          </div>`
          )
          .join("");
      } catch (err) {
        donorsContainer.innerHTML = `<div class="loading-item text-critical">Failed to retrieve donor records.</div>`;
      }
    }

    $("#applyFiltersBtn")?.addEventListener("click", loadDonorsData);
    $("#resetFiltersBtn")?.addEventListener("click", () => {
      if (gs) gs.value = "all";
      if (as) as.value = "all";
      if (searchInput) searchInput.value = "";
      loadDonorsData();
    });

    gs?.addEventListener("change", loadDonorsData);
    as?.addEventListener("change", loadDonorsData);
    searchInput?.addEventListener("keydown", (e) => {
      if (e.key === "Enter") loadDonorsData();
    });

    await loadDonorsData();
  },

  // 7. Inventory Log
  async log() {
    fillGroupSelects();

    async function loadAuditLedger() {
      const tbody = $("#auditBody");
      if (!tbody) return;
      try {
        const txs = await api("/api/transactions");
        if (!txs.length) {
          tbody.innerHTML = `<tr><td colspan="7" class="text-center text-muted">No inventory transactions logged yet.</td></tr>`;
          return;
        }
        tbody.innerHTML = txs
          .map(
            (t) => `
          <tr>
            <td>#${t.id}</td>
            <td>${t.trans_date}</td>
            <td><span class="tag safe">${t.blood_group}</span></td>
            <td><strong>${t.trans_type}</strong></td>
            <td>${t.units} unit${t.units === 1 ? "" : "s"}</td>
            <td>${t.remarks || "—"}</td>
            <td><code>${t.performed_by}</code></td>
          </tr>`
          )
          .join("");
      } catch (err) {
        tbody.innerHTML = `<tr><td colspan="7" class="text-center text-critical">Failed to load audit ledger.</td></tr>`;
      }
    }

    async function submitTx(e, url) {
      e.preventDefault();
      const body = Object.fromEntries(new FormData(e.target));
      try {
        const res = await api(url, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        showToast(res.message, "success");
        e.target.reset();
        await loadAuditLedger();
      } catch (err) {
        showToast(err.message, "error");
      }
    }

    $("#donationForm")?.addEventListener("submit", (e) => submitTx(e, "/api/donation"));
    $("#requestForm")?.addEventListener("submit", (e) => submitTx(e, "/api/request"));
    $("#expiryForm")?.addEventListener("submit", (e) => submitTx(e, "/api/expiry"));
    $("#refreshAuditBtn")?.addEventListener("click", loadAuditLedger);

    $("#resetBtn")?.addEventListener("click", async () => {
      if (!confirm("Are you sure you want to reset demo data? This will restore clean synthetic datasets ending yesterday.")) return;
      try {
        const res = await api("/api/reset", { method: "POST" });
        showToast(res.message, "success");
        await loadAuditLedger();
      } catch (err) {
        showToast(err.message, "error");
      }
    });

    await loadAuditLedger();
  },
};

// Mobile Nav Toggle
const navToggle = $("#navToggle");
const navMenu = $("#navMenu");
if (navToggle && navMenu) {
  navToggle.addEventListener("click", () => {
    const isOpen = navMenu.classList.toggle("open");
    navToggle.setAttribute("aria-expanded", isOpen);
  });
}

// Route Execution
const activePage = document.body.dataset.page;
if (pages[activePage]) {
  pages[activePage]().catch((err) => console.error(err));
}
