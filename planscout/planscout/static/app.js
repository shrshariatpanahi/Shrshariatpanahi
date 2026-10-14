"use strict";

const appEl = document.getElementById("app");
const navEl = document.getElementById("nav");
const state = { me: undefined, catalog: null, draft: null, horizon: 24, selected: null, renderSeq: 0, province: loadProvince() };

// Views load data asynchronously; a view whose render has been superseded must not touch the page.
const stale = (seq) => seq !== state.renderSeq;

const METHOD_LABELS = {
  sandbox_oauth: "Signed in with carrier (sandbox)",
  oauth: "Signed in with carrier",
  bill_upload: "Uploaded bill",
  manual: "Entered manually",
};

const CONNECT_ERRORS = {
  denied: "You declined access at your carrier, so nothing was shared.",
  expired: "That sign-in took too long or was already used. Please try again.",
  carrier: "We couldn't get your account from the carrier. Try again, or upload a bill instead.",
};

// ---------------------------------------------------------------- helpers

const esc = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

function money(value) {
  const n = Number(value) || 0;
  const cents = Math.round(Math.abs(n) * 100) % 100 !== 0;
  const text = Math.abs(n).toLocaleString(undefined, {
    minimumFractionDigits: cents ? 2 : 0,
    maximumFractionDigits: 2,
  });
  return `${n < 0 ? "-" : ""}${state.catalog?.currency_symbol ?? "$"}${text}`;
}

const carrier = (id) => state.catalog.carriers.find((c) => c.id === id);
const hasProvinces = () => Object.keys(state.catalog?.provinces || {}).length > 0;
const provinceName = (code) => state.catalog.provinces?.[code] || code;
const serves = (item, province) => !province || !item.provinces?.length || item.provinces.includes(province);
const withTax = (amount, rate) => Math.round(amount * (100 + rate)) / 100;

function loadProvince() {
  try {
    return localStorage.getItem("planscout.province") || "";
  } catch {
    return "";
  }
}

function saveProvince(code) {
  state.province = code;
  try {
    localStorage.setItem("planscout.province", code);
  } catch {
    // storage unavailable (private mode); the choice just isn't remembered
  }
}

function provinceOptions(selected) {
  return `<option value="">Choose your province…</option>${Object.entries(state.catalog.provinces)
    .map(([code, name]) => `<option value="${esc(code)}" ${code === selected ? "selected" : ""}>${esc(name)}</option>`)
    .join("")}`;
}
const dot = (c) => `<span class="carrier-dot" style="background:${esc(c?.brand_color)}"></span>`;
const list = (items, cls = "") => (items.length ? `<ul class="${cls}">${items.map((i) => `<li>${esc(i)}</li>`).join("")}</ul>` : "");

async function api(path, { method = "GET", body, form } = {}) {
  const options = { method, headers: {}, credentials: "same-origin" };
  if (form) options.body = form;
  else if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  if (response.status === 204) return null;
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = data?.detail;
    const message =
      typeof detail === "string"
        ? detail
        : Array.isArray(detail)
          ? detail.map((d) => `${d.loc?.slice(1).join(".")}: ${d.msg}`).join("; ")
          : `Something went wrong (${response.status})`;
    const error = new Error(message);
    error.status = response.status;
    throw error;
  }
  return data;
}

function go(hash) {
  if (location.hash === hash) render();
  else location.hash = hash;
}

function parseHash() {
  const [path, query = ""] = location.hash.replace(/^#\/?/, "").split("?");
  return { parts: path.split("/").filter(Boolean), params: new URLSearchParams(query) };
}

function setBusy(button, busy, label) {
  if (!button) return;
  button.disabled = busy;
  if (label) button.textContent = label;
}

function renderNav(active) {
  if (!state.me) {
    navEl.innerHTML = "";
    return;
  }
  const link = (hash, label) => `<a href="#/${hash}" class="${active === hash ? "active" : ""}">${label}</a>`;
  navEl.innerHTML = `${link("dashboard", "Dashboard")}${link("connect", "Connect")}${link("privacy", "Privacy")}
    <button type="button" data-action="logout">Sign out</button>`;
}

// ---------------------------------------------------------------- router

async function render() {
  const seq = ++state.renderSeq;
  const { parts, params } = parseHash();
  const route = parts[0] || "dashboard";
  try {
    if (!state.catalog) state.catalog = await api("/api/catalog");
    if (state.me === undefined) {
      state.me = await api("/api/me").catch((e) => (e.status === 401 ? null : Promise.reject(e)));
    }
    if (stale(seq)) return;
    if (!state.me && !["login", "signup"].includes(route)) return go("#/login");
    if (state.me && ["login", "signup"].includes(route)) return go("#/dashboard");
    renderNav(route);

    if (route === "login" || route === "signup") return viewAuth(route);
    if (route === "connect" && parts[1] && parts[2] === "details") return viewAccountForm({ carrierId: parts[1] });
    if (route === "connect" && parts[1]) return viewConnectCarrier(parts[1]);
    if (route === "connect") return viewConnect(params);
    if (route === "edit" && parts[1]) return viewEdit(Number(parts[1]), seq);
    if (route === "privacy") return viewPrivacy(seq);
    return viewDashboard(params, seq);
  } catch (error) {
    if (stale(seq)) return;
    if (error.status === 401) {
      state.me = null;
      return go("#/login");
    }
    appEl.innerHTML = `<div class="error">${esc(error.message)}</div>`;
  }
}

// ---------------------------------------------------------------- auth

function viewAuth(mode) {
  const signup = mode === "signup";
  appEl.innerHTML = `
    <div class="auth card">
      <h1>${signup ? "Create your account" : "Welcome back"}</h1>
      <p class="muted">Find a better phone plan and deals using your real usage, without sharing your carrier password.</p>
      <div id="auth-error"></div>
      <form id="auth-form" class="stack">
        <div><label for="email">Email</label><input id="email" name="email" type="email" autocomplete="email" required></div>
        <div><label for="password">Password ${signup ? '<span class="hint">(at least 10 characters)</span>' : ""}</label>
          <input id="password" name="password" type="password" autocomplete="${signup ? "new-password" : "current-password"}" required minlength="${signup ? 10 : 1}"></div>
        <button class="btn" type="submit">${signup ? "Create account" : "Sign in"}</button>
      </form>
      <p class="small muted" style="margin-top:16px">${
        signup ? 'Already have an account? <a href="#/login">Sign in</a>' : 'New here? <a href="#/signup">Create an account</a>'
      }</p>
    </div>`;
  const form = document.getElementById("auth-form");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = form.querySelector("button");
    setBusy(button, true);
    try {
      state.me = await api(`/api/auth/${signup ? "signup" : "login"}`, {
        method: "POST",
        body: { email: form.email.value, password: form.password.value },
      });
      go("#/dashboard");
    } catch (error) {
      document.getElementById("auth-error").innerHTML = `<div class="error">${esc(error.message)}</div>`;
      setBusy(button, false);
    }
  });
}

// ---------------------------------------------------------------- dashboard

function sampleBanner() {
  const catalog = state.catalog;
  if (catalog.is_sample) {
    return `<div class="banner"><strong>Sample data.</strong> The carriers, plans and prices in this demo are fictional.
       Connect a real offer feed before using PlanScout with customers.</div>`;
  }
  const ageDays = Math.floor((Date.now() - new Date(`${catalog.as_of}T00:00:00`).getTime()) / 86400000);
  const staleNote = ageDays > 45
    ? ` <strong>They were last checked ${ageDays} days ago and may be out of date.</strong>`
    : "";
  return `<div class="banner">Plans and prices were checked on ${esc(catalog.as_of)}.${staleNote}
     Offers change often, so confirm on the carrier's website before you switch.</div>`;
}

async function viewDashboard(params, seq) {
  const connections = await api("/api/connections");
  if (stale(seq)) return;
  if (!connections.length) {
    appEl.innerHTML = `${sampleBanner()}
      <div class="card">
        <h1>Are you overpaying for your phone plan?</h1>
        <p class="muted">Connect your carrier account and PlanScout compares your actual usage against every plan
          and promotion we track, including deals on new phones at your carrier and others.</p>
        <div class="actions"><a class="btn" href="#/connect">Connect your carrier</a></div>
      </div>`;
    return;
  }
  const requested = Number(params.get("connection"));
  const ids = connections.map((c) => c.id);
  state.selected = ids.includes(requested) ? requested : ids.includes(state.selected) ? state.selected : ids[0];
  const id = state.selected;
  appEl.innerHTML = `<p class="muted">Crunching the numbers…</p>`;
  const [detail, report] = await Promise.all([
    api(`/api/connections/${id}`),
    api(`/api/connections/${id}/recommendations?horizon=${state.horizon}`),
  ]);
  if (stale(seq)) return;

  const tabs =
    connections.length > 1
      ? `<div class="tabs">${connections
          .map(
            (c) =>
              `<a href="#/dashboard?connection=${c.id}" class="${c.id === id ? "active" : ""}">${esc(c.carrier_name)} · ${esc(
                c.plan_name || "Account",
              )}</a>`,
          )
          .join("")}</div>`
      : "";

  const best = report.best_option;
  const cur = report.current;
  appEl.innerHTML = `
    ${sampleBanner()}
    ${tabs}
    <h1>${dot(carrier(report.carrier_id))}${esc(report.carrier_name)} · ${esc(detail.account.plan_name || "Your plan")}</h1>
    <p class="muted small">${esc(METHOD_LABELS[detail.method] || detail.method)} · updated ${esc(new Date(detail.updated_at).toLocaleString())}</p>

    <div class="stats">
      <div class="card stat"><div class="label">You pay now</div><div class="value">${money(cur.monthly_total)}<span class="small muted">/mo</span></div>
        <div class="sub">${money(cur.monthly_plan)} plan + ${money(cur.monthly_devices)} phones${
          report.sales_tax_rate != null ? ` · ≈ ${money(withTax(cur.monthly_total, report.sales_tax_rate))} with tax` : ""
        }</div></div>
      <div class="card stat"><div class="label">Best option</div><div class="value">${best ? money(best.monthly_plan_cost) : "–"}<span class="small muted">${best ? "/mo" : ""}</span></div>
        <div class="sub">${best ? `${esc(best.carrier_name)} ${esc(best.plan_name)}${
          report.sales_tax_rate != null ? ` · ≈ ${money(withTax(best.monthly_plan_cost, report.sales_tax_rate))} with tax` : ""
        }` : "You're already on a good deal"}</div></div>
      <div class="card stat"><div class="label">You could save</div><div class="value">${best ? money(best.savings) : money(0)}</div>
        <div class="sub">over ${report.horizon_months} months, after switching costs</div></div>
    </div>

    <div class="card verdict ${best ? "good" : ""}">
      <h3>${best ? "Our recommendation" : "Good news"}</h3>
      <p style="margin:0">${esc(report.verdict)}</p>
      ${report.quick_wins.length ? `<p class="small" style="margin:8px 0 0"><strong>Quick win:</strong> ${esc(report.quick_wins.join(" · "))}</p>` : ""}
    </div>

    <div class="section-head">
      <h2>Plans that fit your usage</h2>
      <div class="segmented" role="group" aria-label="Comparison period">
        ${[12, 24, 36]
          .map((h) => `<button type="button" data-action="horizon" data-horizon="${h}" class="${h === report.horizon_months ? "active" : ""}">${h} mo</button>`)
          .join("")}
      </div>
    </div>
    <div class="stack">${topOptions(report.plan_options).map((o) => optionCard(o, report.horizon_months)).join("") || '<p class="muted">No other plans cover your usage.</p>'}</div>
    ${report.plan_options.length > topOptions(report.plan_options).length
      ? `<p class="small muted" style="margin-top:8px">Showing the best ${topOptions(report.plan_options).length} of ${report.plan_options.length} plans that fit, at most 2 per carrier.</p>`
      : ""}

    <h2>Phone deals for you</h2>
    ${
      report.device_deals.length
        ? `<div class="grid">${report.device_deals.map(dealCard).join("")}</div>`
        : '<p class="muted">No phone promotions you qualify for right now.</p>'
    }

    ${report.upgrade_status.length ? `<h2>Your phones</h2><div class="card"><ul class="plain">${report.upgrade_status.map(upgradeItem).join("")}</ul></div>` : ""}

    <h2>Your account</h2>
    ${accountCard(detail)}

    <details class="small muted"><summary>How we calculate this</summary>${list(report.assumptions)}
      <p class="small" style="margin-top:8px">Catalog updated ${esc(report.catalog_as_of)}.</p></details>`;
}

// Cheapest options first, but at most two per carrier so one brand can't crowd out the rest.
function topOptions(options, limit = 6, perCarrier = 2) {
  const counts = {};
  return options
    .filter((o) => (counts[o.carrier_id] = (counts[o.carrier_id] || 0) + 1) <= perCarrier)
    .slice(0, limit);
}

function optionCard(option, horizon) {
  const positive = option.savings > 0;
  return `
    <div class="card">
      <div class="option-head">
        <h3>${dot(carrier(option.carrier_id))}${esc(option.carrier_name)} · ${esc(option.plan_name)}
          ${option.same_carrier ? '<span class="badge">Your carrier</span>' : ""}</h3>
        <span class="savings ${positive ? "good" : "bad"}">${positive ? "Save" : "Costs"} ${money(Math.abs(option.savings))}
          <span class="small muted">over ${horizon} mo</span></span>
      </div>
      <div class="figures">
        <span>Monthly <strong>${money(option.monthly_plan_cost)}</strong></span>
        <span>Upfront <strong>${money(option.one_time_costs)}</strong></span>
        <span>Promotions <strong>−${money(option.promo_savings)}</strong></span>
        <span>${horizon}-month total <strong>${money(option.horizon_total)}</strong></span>
      </div>
      ${option.promotions.length ? `<p class="small" style="margin:4px 0 0">${option.promotions.map((p) => `<span class="badge good">${esc(p)}</span>`).join(" ")}</p>` : ""}
      <details><summary>Why this plan, and what to check</summary>
        ${list(option.reasons)}${list(option.caveats, "caveats")}
      </details>
    </div>`;
}

function dealCard(deal) {
  return `
    <div class="card">
      <span class="badge ${deal.same_carrier ? "" : "accent"}">${deal.same_carrier ? "Your carrier" : "If you switch"}</span>
      <h3 style="margin-top:8px">${esc(deal.device_model)}</h3>
      <p class="small muted">${dot(carrier(deal.carrier_id))}${esc(deal.carrier_name)} · ${esc(deal.title)}</p>
      <p style="margin:4px 0"><span class="price-old">${money(deal.retail_price)}</span><span class="price-new">${money(deal.effective_device_price)}</span></p>
      <p class="small">Real cost incl. plan requirements: <strong>${money(deal.net_cost)}</strong></p>
      <details><summary>Details</summary>${list(deal.notes, "caveats")}</details>
    </div>`;
}

function upgradeItem(item) {
  const badge = { paid_off: "good", almost_paid_off: "accent", financed: "" }[item.status];
  const label = { paid_off: "Paid off", almost_paid_off: "Almost paid off", financed: "Financing" }[item.status];
  return `<li><strong>${esc(item.label)}</strong> · ${esc(item.device_model)} <span class="badge ${badge}">${label}</span>
    <div class="small muted">${esc(item.message)}</div></li>`;
}

function accountCard(detail) {
  const account = detail.account;
  const synced = detail.method === "oauth" || detail.method === "sandbox_oauth";
  const rows = account.lines
    .map((line) => {
      const d = line.device;
      const payments = d && d.financed ? `${esc(d.installments_remaining)} × ${money(d.monthly_installment)}` : d?.model ? "Paid off" : "–";
      return `<tr><td>${esc(line.label)}</td><td>${esc(line.avg_data_gb)} GB</td><td>${esc(line.hotspot_gb)} GB</td>
        <td>${esc(d?.model || "–")}</td><td>${payments}</td></tr>`;
    })
    .join("");
  return `
    <div class="card">
      <div class="figures" style="margin-top:0">
        <span>Plan <strong>${esc(account.plan_name || "Unknown")}</strong></span>
        <span>Plan cost <strong>${money(account.monthly_plan_cost)}/mo</strong></span>
        <span>AutoPay <strong>${account.autopay ? "On" : "Off"}</strong></span>
        ${account.province ? `<span>Province <strong>${esc(provinceName(account.province))}</strong></span>` : ""}
      </div>
      <div class="table-wrap"><table>
        <thead><tr><th>Line</th><th>Avg data</th><th>Hotspot</th><th>Phone</th><th>Payments left</th></tr></thead>
        <tbody>${rows}</tbody></table></div>
      <div class="actions">
        ${
          synced
            ? `<button type="button" class="btn secondary" data-action="refresh" data-id="${detail.id}">Refresh from carrier</button>`
            : `<a class="btn secondary" href="#/edit/${detail.id}">Edit details</a>`
        }
        <button type="button" class="btn link" data-action="disconnect" data-id="${detail.id}">Disconnect and delete data</button>
      </div>
    </div>`;
}

// ---------------------------------------------------------------- connect

function viewConnect(params) {
  const error = CONNECT_ERRORS[params.get("error")];
  const province = hasProvinces() ? state.province : "";
  const shown = state.catalog.carriers.filter((c) => serves(c, province));
  const hidden = state.catalog.carriers.length - shown.length;
  appEl.innerHTML = `
    ${error ? `<div class="error">${esc(error)}</div>` : ""}
    <h1>Which carrier are you with?</h1>
    <p class="muted">Pick your current carrier. You'll choose how to share your account on the next step.</p>
    ${
      hasProvinces()
        ? `<div class="field-row" style="max-width:360px"><div><label for="province-filter">Where do you live?</label>
            <select id="province-filter">${provinceOptions(province)}</select></div></div>`
        : ""
    }
    <div class="grid">
      ${shown
        .map(
          (c) => `
        <a class="card carrier-card" href="#/connect/${esc(c.id)}" style="border-top-color:${esc(c.brand_color)}">
          <h3>${esc(c.name)}</h3>
          <p class="small muted" style="margin:0">${c.type === "prepaid" ? "Prepaid" : "Postpaid"}${
            c.parent && c.parent !== c.name ? ` · owned by ${esc(c.parent)}` : ""
          }</p>
        </a>`,
        )
        .join("")}
    </div>
    ${hidden ? `<p class="small muted" style="margin-top:12px">${hidden} other carrier${hidden > 1 ? "s don't" : " doesn't"} sell service in ${esc(provinceName(province))}.</p>` : ""}`;
  document.getElementById("province-filter")?.addEventListener("change", (event) => {
    saveProvince(event.target.value);
    viewConnect(new URLSearchParams());
  });
}

function viewConnectCarrier(carrierId) {
  const c = carrier(carrierId);
  if (!c) return go("#/connect");
  const scopes = Object.values(state.catalog.scopes);
  appEl.innerHTML = `
    <p><a href="#/connect">← All carriers</a></p>
    <h1>${dot(c)}Connect ${esc(c.name)}</h1>
    <div id="connect-error"></div>
    <div class="stack">
      ${
        c.oauth
          ? `<div class="card">
          ${
            c.oauth_is_sandbox
              ? `<h3>Try the demo sign-in <span class="badge">Demo</span></h3>
          <p class="muted small">Signing in with ${esc(c.name)} for real needs a data-sharing agreement with ${esc(c.name)}.
            This demo shows how it would work using sample customers. No real ${esc(c.name)} account is accessed.
            With a real integration we'd ask for <strong>read-only</strong> access to:</p>`
              : `<h3>Sign in with ${esc(c.name)} <span class="badge good">Recommended</span></h3>
          <p class="muted small">You'll sign in on ${esc(c.name)}'s own page. PlanScout never sees your password, and you can revoke access any time.
            We ask for <strong>read-only</strong> access to:</p>`
          }
          ${list(scopes, "small")}
          <div class="actions"><button type="button" class="btn" data-action="oauth" data-carrier="${esc(c.id)}">${
            c.oauth_is_sandbox ? "Start the demo" : `Continue to ${esc(c.name)}`
          }</button></div>
        </div>`
          : ""
      }
      <div class="card">
        <h3>Upload a bill</h3>
        <p class="muted small">Upload your latest ${esc(c.name)} bill (PDF). We read it once, show you what we found so you can check it,
          and don't keep the file.</p>
        <form id="bill-form" class="actions">
          <input id="bill-file" name="file" type="file" accept=".pdf,.txt,application/pdf,text/plain" required aria-label="Bill file">
          <button class="btn secondary" type="submit">Read my bill</button>
        </form>
      </div>
      <div class="card">
        <h3>Enter details yourself</h3>
        <p class="muted small">Takes about two minutes. Your bill or carrier app has everything you need.</p>
        <div class="actions"><a class="btn secondary" href="#/connect/${esc(c.id)}/details" data-action="manual">Enter details</a></div>
      </div>
    </div>`;

  document.getElementById("bill-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = event.target.querySelector("button");
    const file = document.getElementById("bill-file").files[0];
    if (!file) return;
    const form = new FormData();
    form.append("file", file);
    setBusy(button, true, "Reading…");
    try {
      state.draft = await api(`/api/connect/${encodeURIComponent(c.id)}/bill`, { method: "POST", form });
      go(`#/connect/${c.id}/details`);
    } catch (error) {
      document.getElementById("connect-error").innerHTML = `<div class="error">${esc(error.message)}</div>`;
      setBusy(button, false, "Read my bill");
    }
  });
}

// ---------------------------------------------------------------- account form (manual entry, bill review, edit)

const EMPTY_LINE = () => ({ label: "", avg_data_gb: 0, hotspot_gb: 0, device: null });

async function viewEdit(connectionId, seq) {
  const detail = await api(`/api/connections/${connectionId}`);
  if (stale(seq)) return;
  viewAccountForm({ carrierId: detail.carrier_id, account: detail.account, connectionId });
}

function viewAccountForm({ carrierId, account, connectionId }) {
  const c = carrier(carrierId);
  if (!c) return go("#/connect");
  const fromBill = !account && state.draft?.draft?.carrier_id === carrierId;
  const data = account || (fromBill && state.draft.draft) || { plan_name: "", plan_id: null, monthly_plan_cost: "", autopay: true, lines: [EMPTY_LINE()] };
  const source = account ? account.source : fromBill ? "bill_upload" : "manual";
  const carrierPlans = state.catalog.plans.filter((p) => p.carrier_id === carrierId);
  const initialProvince = data.province || (hasProvinces() && serves(c, state.province) ? state.province : "");
  const plansFor = (province) => carrierPlans.filter((p) => serves(p, province) || p.id === data.plan_id);
  const plans = plansFor(initialProvince);
  const knownPlan = plans.some((p) => p.id === data.plan_id);
  const planOptions = (list) =>
    `${list.map((p) => `<option value="${esc(p.id)}" ${p.id === data.plan_id ? "selected" : ""}>${esc(p.name)}</option>`).join("")}
     <option value="" ${list.some((p) => p.id === data.plan_id) ? "" : "selected"}>Other / not listed</option>`;
  let lines = data.lines.map((l) => ({ ...l, device: l.device ? { ...l.device } : null }));

  appEl.innerHTML = `
    <p><a href="#/connect/${esc(c.id)}">← Back</a></p>
    <h1>${dot(c)}${connectionId ? "Edit your" : fromBill ? "Check your" : "Your"} ${esc(c.name)} details</h1>
    ${
      fromBill
        ? `<div class="banner">${state.draft.found.length ? `Found: ${esc(state.draft.found.join(", "))}. ` : ""}Please check everything below.
           ${state.draft.warnings.length ? list(state.draft.warnings) : ""}</div>`
        : ""
    }
    <div id="form-error"></div>
    <form id="account-form" class="card" novalidate>
      <div class="field-row">
        ${
          hasProvinces()
            ? `<div><label for="province">Province</label>
                <select id="province" name="province" required>${provinceOptions(initialProvince)}</select></div>`
            : ""
        }
        <div>
          <label for="plan_choice">Plan</label>
          <select id="plan_choice" name="plan_choice">${planOptions(plans)}</select>
        </div>
        <div id="plan-name-wrap" ${knownPlan ? "hidden" : ""}>
          <label for="plan_name">Plan name</label>
          <input id="plan_name" name="plan_name" type="text" maxlength="80" value="${esc(knownPlan ? "" : data.plan_name)}">
        </div>
        <div>
          <label for="monthly_plan_cost">Monthly plan cost <span class="hint">after discounts, without phone payments or taxes</span></label>
          <input id="monthly_plan_cost" name="monthly_plan_cost" type="number" min="0" step="0.01" required value="${esc(data.monthly_plan_cost)}">
        </div>
      </div>
      <label class="check"><input type="checkbox" name="autopay" ${data.autopay ? "checked" : ""}> AutoPay is on</label>
      <h2>Lines</h2>
      <div id="lines"></div>
      <div class="actions">
        <button type="button" class="btn secondary" id="add-line">+ Add line</button>
        <span style="flex:1"></span>
        <button type="submit" class="btn">${connectionId ? "Save changes" : "See my recommendations"}</button>
      </div>
    </form>`;

  const form = document.getElementById("account-form");
  const linesEl = document.getElementById("lines");

  const lineHtml = (line, i) => {
    const d = line.device || {};
    const financed = !!d.financed;
    return `
      <fieldset class="line ${financed ? "financed" : ""}" data-index="${i}">
        <legend>Line ${i + 1}</legend>
        <div class="field-row">
          <div><label>Name <span class="hint">e.g. "Mom" or last 4 digits</span></label>
            <input type="text" name="label" maxlength="40" value="${esc(line.label)}"></div>
          <div><label>Average data per month (GB)</label>
            <input type="number" name="avg_data_gb" min="0" step="0.1" value="${esc(line.avg_data_gb)}"></div>
          <div><label>Hotspot per month (GB) <span class="hint">part of the data above</span></label>
            <input type="number" name="hotspot_gb" min="0" step="0.1" value="${esc(line.hotspot_gb)}"></div>
          <div><label>Phone model</label>
            <input type="text" name="model" maxlength="80" value="${esc(d.model)}"></div>
        </div>
        <label class="check"><input type="checkbox" name="financed" ${financed ? "checked" : ""}> I'm still paying this phone off</label>
        <div class="field-row finance-only" style="margin-top:12px">
          <div><label>Monthly phone payment</label><input type="number" name="monthly_installment" min="0" step="0.01" value="${esc(d.monthly_installment ?? "")}"></div>
          <div><label>Payments left</label><input type="number" name="installments_remaining" min="0" step="1" value="${esc(d.installments_remaining ?? "")}"></div>
          <div><label>Monthly promo credit <span class="hint">if any</span></label><input type="number" name="monthly_promo_credit" min="0" step="0.01" value="${esc(d.monthly_promo_credit ?? "")}"></div>
          <div><label class="check" style="margin-top:28px"><input type="checkbox" name="unlocked" ${d.unlocked ? "checked" : ""}> Phone is unlocked</label></div>
        </div>
        ${lines.length > 1 ? `<button type="button" class="btn link small" data-remove="${i}">Remove line</button>` : ""}
        <div style="height:8px"></div>
      </fieldset>`;
  };

  const readLines = () =>
    [...linesEl.querySelectorAll("fieldset.line")].map((fs, i) => {
      const get = (name) => fs.querySelector(`[name="${name}"]`);
      const num = (name) => Number(get(name).value) || 0;
      const model = get("model").value.trim();
      const financed = get("financed").checked;
      return {
        label: get("label").value.trim() || `Line ${i + 1}`,
        avg_data_gb: num("avg_data_gb"),
        hotspot_gb: num("hotspot_gb"),
        device:
          model || financed
            ? {
                model,
                financed,
                monthly_installment: financed ? num("monthly_installment") : 0,
                installments_remaining: financed ? Math.round(num("installments_remaining")) : 0,
                monthly_promo_credit: financed ? num("monthly_promo_credit") : 0,
                unlocked: financed ? get("unlocked").checked : true,
              }
            : null,
      };
    });

  const drawLines = () => {
    linesEl.innerHTML = lines.map(lineHtml).join("");
  };
  drawLines();

  document.getElementById("add-line").addEventListener("click", () => {
    lines = [...readLines(), EMPTY_LINE()];
    drawLines();
  });
  linesEl.addEventListener("click", (event) => {
    const index = event.target.dataset.remove;
    if (index === undefined) return;
    lines = readLines().filter((_, i) => i !== Number(index));
    drawLines();
  });
  linesEl.addEventListener("change", (event) => {
    if (event.target.name === "financed") event.target.closest("fieldset").classList.toggle("financed", event.target.checked);
  });
  form.plan_choice.addEventListener("change", () => {
    document.getElementById("plan-name-wrap").hidden = !!form.plan_choice.value;
  });
  form.province?.addEventListener("change", () => {
    form.plan_choice.innerHTML = planOptions(plansFor(form.province.value));
    document.getElementById("plan-name-wrap").hidden = !!form.plan_choice.value;
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const planId = form.plan_choice.value || null;
    const planName = planId ? carrierPlans.find((p) => p.id === planId).name : form.plan_name.value.trim();
    const province = form.province?.value || null;
    const body = {
      plan_id: planId,
      plan_name: planName,
      monthly_plan_cost: Number(form.monthly_plan_cost.value),
      autopay: form.autopay.checked,
      province,
      lines: readLines(),
      source,
    };
    const errorEl = document.getElementById("form-error");
    if (hasProvinces() && !province) {
      errorEl.innerHTML = '<div class="error">Choose your province so we only suggest carriers that serve it.</div>';
      form.province.focus();
      return;
    }
    if (province) saveProvince(province);
    if (!form.monthly_plan_cost.value || body.monthly_plan_cost <= 0) {
      errorEl.innerHTML = '<div class="error">Enter what you pay for your plan each month.</div>';
      form.monthly_plan_cost.focus();
      return;
    }
    const button = form.querySelector('button[type="submit"]');
    setBusy(button, true);
    try {
      let id = connectionId;
      if (connectionId) await api(`/api/connections/${connectionId}/account`, { method: "PUT", body });
      else id = (await api(`/api/connect/${encodeURIComponent(carrierId)}/account`, { method: "POST", body })).id;
      state.draft = null;
      go(`#/dashboard?connection=${id}`);
    } catch (error) {
      errorEl.innerHTML = `<div class="error">${esc(error.message)}</div>`;
      setBusy(button, false);
    }
  });
}

// ---------------------------------------------------------------- privacy

async function viewPrivacy(seq) {
  const [connections, consents] = await Promise.all([api("/api/connections"), api("/api/consents")]);
  if (stale(seq)) return;
  const name = (id) => carrier(id)?.name || id;
  appEl.innerHTML = `
    <h1>Privacy and access</h1>
    <div class="card">
      <h3>What PlanScout can see</h3>
      ${list([
        "Your plan, what you pay for it, and how much data each line uses",
        "Which phones are on your account and how many payments are left",
        "Only the last 4 digits of each phone number",
      ])}
      <h3 style="margin-top:12px">What PlanScout never gets</h3>
      ${list([
        "Your carrier password or PIN",
        "Who you call or text, your browsing, or your location",
        "Payment details or the ability to change your account",
      ])}
    </div>

    <h2>Connected accounts</h2>
    ${
      connections.length
        ? `<div class="card"><ul class="plain">${connections
            .map(
              (c) => `<li>
            <div class="option-head"><strong>${dot(carrier(c.carrier_id))}${esc(c.carrier_name)} · ${esc(c.plan_name || "")}</strong>
              <button type="button" class="btn link" data-action="disconnect" data-id="${c.id}">Disconnect and delete data</button></div>
            <div class="small muted">${esc(METHOD_LABELS[c.method] || c.method)} since ${esc(new Date(c.created_at).toLocaleDateString())}
              ${c.scopes.length ? ` · read-only access: ${esc(c.scopes.join(", "))}` : ""}</div></li>`,
            )
            .join("")}</ul></div>`
        : '<p class="muted">No connected accounts.</p>'
    }

    <h2>Consent history</h2>
    ${
      consents.length
        ? `<div class="card table-wrap"><table><thead><tr><th>When</th><th>Carrier</th><th>How</th><th>Event</th></tr></thead><tbody>
          ${consents
            .map(
              (e) => `<tr><td>${esc(new Date(e.at).toLocaleString())}</td><td>${esc(name(e.carrier_id))}</td>
              <td>${esc(METHOD_LABELS[e.method] || e.method)}</td><td>${e.event === "granted" ? "Access granted" : "Access revoked, data deleted"}</td></tr>`,
            )
            .join("")}</tbody></table></div>`
        : '<p class="muted">Nothing yet.</p>'
    }

    <h2>Delete your account</h2>
    <div class="card">
      <p class="muted">Revokes every carrier connection and permanently deletes your PlanScout account and data.</p>
      <button type="button" class="btn danger" data-action="delete-account">Delete my account</button>
    </div>`;
}

// ---------------------------------------------------------------- global actions

document.addEventListener("click", async (event) => {
  const target = event.target.closest("[data-action]");
  if (!target) return;
  const action = target.dataset.action;
  try {
    if (action === "logout") {
      await api("/api/auth/logout", { method: "POST" });
      state.me = null;
      go("#/login");
    } else if (action === "horizon") {
      state.horizon = Number(target.dataset.horizon);
      render();
    } else if (action === "manual") {
      state.draft = null;
    } else if (action === "oauth") {
      setBusy(target, true, "Opening sign-in…");
      const { authorize_url } = await api(`/api/connect/${encodeURIComponent(target.dataset.carrier)}/oauth`, { method: "POST" });
      window.location.assign(authorize_url);
    } else if (action === "refresh") {
      setBusy(target, true, "Refreshing…");
      await api(`/api/connections/${target.dataset.id}/refresh`, { method: "POST" });
      render();
    } else if (action === "disconnect") {
      if (!confirm("Disconnect this account? We'll revoke our access and delete its data.")) return;
      await api(`/api/connections/${target.dataset.id}`, { method: "DELETE" });
      state.selected = null;
      render();
    } else if (action === "delete-account") {
      if (!confirm("Permanently delete your PlanScout account and all data?")) return;
      await api("/api/me", { method: "DELETE" });
      state.me = null;
      go("#/signup");
    }
  } catch (error) {
    if (error.status === 401) {
      state.me = null;
      return go("#/login");
    }
    alert(error.message);
    render();
  }
});

window.addEventListener("hashchange", render);
render();
