const state = { step: 1, maxStep: 1, dtype: "ledger", job: null, categories: [], page: 1, size: 50 };
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const money = (s) => (s === "" || s == null ? "" : Number(s).toLocaleString("en-IN", { minimumFractionDigits: 2 }));
const base = () => `/api/${state.dtype}/jobs/${state.job.job_id}`;

async function api(url, opts = {}) {
  const r = await fetch(url, opts);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch {}
    throw new Error(msg);
  }
  return r.json();
}

/* ---------------- navigation ---------------- */
function go(n) {
  if (n > state.maxStep + 1 || (n >= 4 && !state.job)) return;
  state.step = n;
  state.maxStep = Math.max(state.maxStep, n);
  document.querySelectorAll("section").forEach((s) => s.classList.toggle("active", s.dataset.step === String(n)));
  document.querySelectorAll("nav button").forEach((b) => {
    const k = +b.dataset.step;
    b.classList.toggle("active", k === n);
    b.classList.toggle("done", k < n);
    b.disabled = k > state.maxStep || (k >= 4 && !state.job);
  });
  $("#steps").hidden = false;
  if (n === 3) document.querySelectorAll(".uploads").forEach((u) => (u.hidden = u.dataset.for !== state.dtype));
  if (n === 4) renderMapping();
  if (n === 5) { setupPreview(); renderStats($("#stats")); loadRows(1); }
  if (n === 6) renderDownload();
}
document.querySelectorAll("nav button").forEach((b) => (b.onclick = () => go(+b.dataset.step)));

async function chooseType() {
  const t = document.querySelector("input[name=dtype]:checked").value;
  if (t !== state.dtype) { state.job = null; state.maxStep = 1; }
  state.dtype = t;
  const sw = await api(`/api/meta/softwares?data_type=${t}`);
  $("#software").innerHTML = sw.map((s) => `<option value="${esc(s.id)}">${esc(s.name)}</option>`).join("");
  go(2);
}

async function init() {
  state.categories = await api("/api/meta/categories");
  go(1);
}

/* ---------------- upload ---------------- */
async function convert() {
  const box = $("#uploadError");
  box.hidden = true;
  const fd = new FormData();
  fd.append("software", $("#software").value);
  if (state.dtype === "ledger") {
    const lf = $("#ledgerFile").files[0];
    if (!lf) { box.textContent = "Please choose the ledger list file."; box.hidden = false; return; }
    fd.append("ledger_file", lf);
    if ($("#tbFile").files[0]) fd.append("tb_file", $("#tbFile").files[0]);
  } else {
    const sf = $("#stockFile").files[0];
    if (!sf) { box.textContent = "Please choose the stock item file."; box.hidden = false; return; }
    fd.append("stock_file", sf);
  }
  const btn = $("#convertBtn");
  btn.disabled = true; btn.textContent = "Converting…";
  try {
    state.job = await api(`/api/${state.dtype}/jobs`, { method: "POST", body: fd });
    go(4);
  } catch (e) {
    box.textContent = e.message; box.hidden = false;
  } finally {
    btn.disabled = false; btn.textContent = "Convert";
  }
}

/* ---------------- mapping ---------------- */
async function renderMapping() {
  const m = await api(`${base()}/mapping`);
  $("#mappingTable tbody").innerHTML = m.targets.map((t) =>
    `<tr><td><code>${esc(t.target)}</code></td><td class="${t.kind === "blank" ? "blank" : ""}">${
      t.kind === "blank" ? "left blank" : esc(t.source)}</td></tr>`).join("");
  $("#droppedCols").textContent = m.dropped.length ? `Source columns not used: ${m.dropped.join(", ")}` : "";
  const j = state.job;
  if (state.dtype === "ledger") {
    $("#mappingSide").innerHTML = `<h3>Ledger classification</h3>
      <table class="tbl"><thead><tr><th>Category</th><th>annexuredr / annexurecr</th><th>Ledgers</th></tr></thead><tbody>${
      state.categories.map((c) => `<tr><td>${esc(c.label)}</td><td><small>${esc(c.annexuredr)}<br>${esc(c.annexurecr)}</small></td><td class="num">${j.by_category[c.id] || 0}</td></tr>`).join("")
      }</tbody></table>
      <h3>Accobiz prebuilt ledgers</h3>
      <p class="muted">${j.merged_into_prebuilt} source ledgers already exist in Accobiz and are not created again.
      ${j.prebuilt_rows_written} of them carry an opening balance, written as balance-only rows using the Accobiz name.</p>`;
  } else {
    const st = j.settings;
    $("#mappingSide").innerHTML = `<h3>Stock rules applied</h3>
      <table class="tbl"><tbody>
        <tr><td>Goods / Services</td><td class="num">${j.goods} / ${j.services}</td></tr>
        <tr><td>NIL rated (GST 0%)</td><td class="num">${j.nil_rated}</td></tr>
        <tr><td>GST TDS ${esc(st.gst_tds.rate)}% (HSN ${esc(st.gst_tds.hsn_prefixes.join(", "))})</td><td class="num">${j.gst_tds_yes}</td></tr>
        <tr><td>TCS ${esc(st.tcs.rate)}% (words: ${esc(st.tcs.name_keywords.join(", "))})</td><td class="num">${j.tcs_yes}</td></tr>
        <tr><td>Unit</td><td>${esc(st.unit)}</td></tr>
        <tr><td>Purchase / Sales account</td><td>${esc(st.purchaseaccount)} / ${esc(st.salesaccount)}</td></tr>
      </tbody></table>
      <p class="muted">Rates and lists can be changed under Settings.</p>`;
  }
}

/* ---------------- preview ---------------- */
const FILTERS = {
  ledger: [["all", "All ledgers"], ["errors", "Errors only"], ["issues", "Errors & warnings"],
           ["prebuilt", "Matched to prebuilt"], ["ai", "AI suggestions to review"], ["excluded", "Excluded"]],
  stock: [["all", "All items"], ["errors", "Errors only"], ["issues", "Errors & warnings"],
          ["service", "Services"], ["tds", "GST TDS / TCS applicable"], ["excluded", "Excluded"]],
};
const HEADS = {
  ledger: ["Row", "Include", "Ledger name", "Category / Prebuilt", "annexuredr", "annexurecr", "Cash/Bank",
           "Opening bal.", "Type", "GSTIN", "State", "Issues"],
  stock: ["Row", "Include", "Description", "Type", "HSN", "GST %", "GST type", "Unit", "GST TDS", "TCS",
          "Sale rate", "Pur. rate", "Issues"],
};
function setupPreview() {
  const f = $("#filter");
  if (f.dataset.for !== state.dtype) {
    f.innerHTML = FILTERS[state.dtype].map(([v, l]) => `<option value="${v}">${esc(l)}</option>`).join("");
    f.dataset.for = state.dtype;
  }
  $("#rows thead").innerHTML = "<tr>" + HEADS[state.dtype].map((h) =>
    `<th class="${/bal|rate/i.test(h) ? "num" : ""}">${h}</th>`).join("") + "</tr>";
}

function renderStats(el) {
  const j = state.job;
  let items;
  if (state.dtype === "ledger") {
    const c = j.controls, tbOk = !c.tb_loaded || c.tb_net === c.out_net;
    items = [
      ["Source ledgers", j.source_ledgers], ["New ledgers", j.new_ledgers],
      ["Prebuilt balance rows", j.prebuilt_rows_written], ["Output rows", j.output_rows],
      ["Errors", j.errors, j.errors ? "bad" : "good"], ["Warnings", j.warnings],
      c.tb_loaded ? ["Dr / Cr · trial balance", `${money(c.tb_debit)} / ${money(c.tb_credit)}`, "wide"] : ["Trial balance", "Not uploaded"],
      ["Dr / Cr · output" + (c.tb_loaded ? (tbOk ? " · matches ✓" : " · mismatch") : ""), `${money(c.out_debit)} / ${money(c.out_credit)}`, "wide " + (tbOk ? "good" : "bad")],
    ];
  } else {
    items = [
      ["Source items", j.source_items], ["Output rows", j.output_rows],
      ["Goods / Services", `${j.goods} / ${j.services}`], ["NIL rated", j.nil_rated],
      ["GST TDS / TCS", `${j.gst_tds_yes} / ${j.tcs_yes}`],
      ["Errors", j.errors, j.errors ? "bad" : "good"], ["Warnings", j.warnings],
    ];
  }
  el.innerHTML = items.map(([k, v, cls]) => `<div class="stat ${cls || ""}"><b>${esc(v)}</b><span>${esc(k)}</span></div>`).join("");
  renderAiPanel();
  $("#datasetIssues").innerHTML = j.dataset_issues.map((i) =>
    `<div class="${i.level === "error" ? "error-box" : "warn-box"}">${esc(i.message)}</div>`).join("");
}

let t;
function debounceLoad() { clearTimeout(t); t = setTimeout(() => loadRows(1), 250); }

async function loadRows(page) {
  state.page = page;
  const f = $("#filter").value, q = encodeURIComponent($("#search").value);
  const seq = (loadRows.seq = (loadRows.seq || 0) + 1);
  const d = await api(`${base()}/records?filter=${f}&q=${q}&page=${page}&size=${state.size}`);
  if (seq !== loadRows.seq) return;          // a newer request has started; drop this stale result
  const fn = state.dtype === "ledger" ? ledgerRow : stockRow;
  $("#rows tbody").innerHTML = d.rows.map(fn).join("") ||
    `<tr><td colspan="13" class="blank">Nothing matches.</td></tr>`;
  const pages = Math.max(1, Math.ceil(d.total / d.size));
  $("#pager").innerHTML = `${d.total} rows · page ${page}/${pages}
    <button ${page <= 1 ? "disabled" : ""} onclick="loadRows(${page - 1})">‹</button>
    <button ${page >= pages ? "disabled" : ""} onclick="loadRows(${page + 1})">›</button>`;
}

const rowClass = (r) => !r.include ? "off" : r.issues.some((i) => i.level === "error") ? "err" : r.issues.length ? "warn" : "";
const issuesHtml = (r) => r.issues.map((i) => `<span class="issue ${i.level}">${esc(i.message)}</span>`).join("");
const includeBox = (r) => `<input type="checkbox" ${r.include ? "checked" : ""} onchange="patch(${r.idx}, {include: this.checked})">`;

function ledgerRow(r) {
  const v = r.values;
  let catCell;
  const catSelect = `<select title="${esc(r.reason)}" onchange="patch(${r.idx}, {category: this.value})">${
    state.categories.map((c) => `<option value="${c.id}" ${c.id === r.category ? "selected" : ""}>${esc(c.label)}</option>`).join("")}</select>`;
  if (r.prebuilt) {
    catCell = `<span class="chip brand">Prebuilt: ${esc(r.prebuilt.name)}</span>
      <select onchange="patch(${r.idx}, {prebuilt_decision: this.value})">
        <option value="merge" ${r.prebuilt_decision === "merge" ? "selected" : ""}>Use Accobiz ledger</option>
        <option value="new" ${r.prebuilt_decision === "new" ? "selected" : ""}>Create as new ledger</option>
      </select>${r.merged ? "" : "<br>" + catSelect}`;
  } else catCell = catSelect;
  catCell += aiCell(r);
  const m = r.merged;
  return `<tr class="${rowClass(r)}">
    <td class="num">${r.src_row}</td><td>${includeBox(r)}</td><td>${esc(v.name)}</td><td>${catCell}</td>
    <td>${m ? "" : esc(v.annexuredr)}</td><td>${m ? "" : esc(v.annexurecr)}</td><td>${m ? "" : esc(v.cashbank)}</td>
    <td class="num">${money(v.openingbalance)}</td><td>${esc(v.balancetype)}</td>
    <td>${esc(v.gstin)}</td><td>${esc(v.state)}</td><td>${issuesHtml(r)}</td></tr>`;
}

const catLabel = (id) => (state.categories.find((c) => c.id === id) || { label: id }).label;

function aiCell(r) {
  const a = r.ai;
  if (!a) return "";
  if (a.status === "pending")
    return `<div class="ai-sug" title="${esc(a.reason)}">
      <span class="ai-badge">AI</span> ${esc(catLabel(a.category))}
      <span class="conf ${a.confidence}">${a.confidence}</span>
      <button class="mini ok" title="Accept" onclick="aiAccept([${r.idx}])">✓</button>
      <button class="mini" title="Dismiss" onclick="aiDismiss(${r.idx})">✕</button>
      <div class="ai-reason">${esc(a.reason)}</div></div>`;
  if (a.status === "accepted") return `<div class="ai-note"><span class="ai-badge">AI</span> accepted</div>`;
  if (a.status === "confirmed") return `<div class="ai-note"><span class="ai-badge">AI</span> agrees: Other</div>`;
  return "";
}

function renderAiPanel() {
  const el = $("#aiPanel");
  if (state.dtype !== "ledger") { el.innerHTML = ""; return; }
  const a = state.job.ai;
  let body, actions = "";
  if (!a.available) {
    body = `<b>AI review is not set up.</b> <span class="muted">Add an Anthropic API key to the <code>.env</code> file and restart to get AI suggestions for ledgers left in "Everything else".</span>`;
  } else if (!a.reviewed && a.candidates) {
    body = `<b>${a.candidates} ledgers</b> are in "Everything else" because no name rule matched.
      <span class="muted">AI can suggest a better category for each. Only ledger names are sent; nothing changes until you accept.</span>`;
    actions = `<button class="primary" id="aiRunBtn" onclick="aiRun()">Review with AI</button>`;
  } else if (a.reviewed) {
    body = `<b>${a.pending} suggestion(s) to review</b>
      <span class="muted">· ${a.pending_high} high confidence · ${a.accepted} accepted · ${a.confirmed_other} confirmed as Other</span>`;
    if (a.pending) actions = `
      <button onclick="showAi()">Show suggestions</button>
      ${a.pending_high ? `<button class="primary" onclick="aiAccept(null, 'high')">Accept ${a.pending_high} high-confidence</button>` : ""}
      <button onclick="aiAccept(null, null)">Accept all</button>`;
  } else {
    el.innerHTML = ""; return;
  }
  el.innerHTML = `<div class="ai-panel"><div class="ai-icon">✦</div><div class="ai-text">${body}</div><div class="ai-actions">${actions}</div></div>`;
}

async function aiRun() {
  const btn = $("#aiRunBtn");
  btn.disabled = true; btn.textContent = "Reviewing… (may take a minute)";
  try {
    const d = await api(`${base()}/ai-review`, { method: "POST" });
    state.job = d.summary;
    renderStats($("#stats"));
    toast(`AI reviewed ${d.reviewed} ledgers`);
    if (state.job.ai.pending) showAi(); else loadRows(state.page);
  } catch (e) {
    toast(e.message);
    renderAiPanel();
  }
}

function showAi() { $("#filter").value = "ai"; loadRows(1); }

async function aiAccept(indexes, minConfidence) {
  try {
    const d = await api(`${base()}/ai-accept`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ indexes, min_confidence: minConfidence }) });
    state.job = d.summary;
    renderStats($("#stats"));
    if (!indexes) toast(`${d.accepted} suggestion(s) accepted`);
    loadRows(state.page);
  } catch (e) { toast(e.message); }
}

async function aiDismiss(idx) {
  try {
    const d = await api(`${base()}/ai-dismiss/${idx}`, { method: "POST" });
    state.job = d.summary;
    renderStats($("#stats"));
    loadRows(state.page);
  } catch (e) { toast(e.message); }
}

function stockRow(r) {
  const v = r.values;
  const typeSel = `<select title="${esc(r.type_reason)}" onchange="patch(${r.idx}, {item_type: this.value})">${
    ["Goods", "Service"].map((x) => `<option ${x === v.type ? "selected" : ""}>${x}</option>`).join("")}</select>`;
  const yn = (a, rate) => a === "Yes" ? `<span class="chip yes">Yes · ${esc(rate)}%</span>` : `<span class="muted">No</span>`;
  return `<tr class="${rowClass(r)}">
    <td class="num">${r.src_row}</td><td>${includeBox(r)}</td><td>${esc(v.description)}</td><td>${typeSel}</td>
    <td>${esc(v.hsn)}</td><td>${esc(v.gstrate)}</td><td>${esc(v.gsttype)}</td><td>${esc(v.unit)}</td>
    <td>${yn(v.gsttdsapplicable, v.gsttdsrate)}</td><td>${yn(v.tcsapplicable, v.tcsrate)}</td>
    <td class="num">${money(v.salerate)}</td><td class="num">${money(v.purchaserate)}</td><td>${issuesHtml(r)}</td></tr>`;
}

async function patch(idx, body) {
  try {
    const d = await api(`${base()}/records/${idx}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    state.job = d.summary;
    renderStats($("#stats"));
    loadRows(state.page);
  } catch (e) { toast(e.message); }
}

/* ---------------- download ---------------- */
function renderDownload() {
  renderStats($("#finalStats"));
  const label = state.dtype === "ledger" ? "Ledger" : "Stock";
  const j = state.job;
  const report = `<a href="${base()}/report"><button>Conversion report (.xlsx)</button></a>`;
  $("#downloadBlock").innerHTML = j.can_download
    ? `<div class="download-card"><div class="big">✓</div><div><b>Ready to import</b>
         <span class="muted">${j.output_rows} rows in the Accobiz ${label} import template, with dropdowns on every row.</span></div></div>
       <div class="actions inline">
         <a href="${base()}/download"><button class="primary">Download Accobiz ${label.toLowerCase()} file</button></a>${report}</div>`
    : `<div class="download-card blocked"><div class="big">!</div><div><b>${j.errors} error(s) to resolve</b>
         <span class="muted">Fix or exclude them in step 5 (Preview &amp; validate), then return here.</span></div></div>
       <div class="actions inline"><button onclick="go(5)">Go to errors</button>${report}</div>`;
}

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg; t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (t.hidden = true), 3500);
}

document.querySelectorAll(".drop input[type=file]").forEach((inp) => {
  const drop = inp.closest(".drop"), fname = drop.querySelector(".fname"), initial = fname.textContent;
  inp.addEventListener("change", () => {
    const f = inp.files[0];
    drop.classList.toggle("has-file", !!f);
    fname.textContent = f ? `${f.name} · ${(f.size / 1024).toFixed(0)} KB` : initial;
  });
  ["dragenter", "dragover"].forEach((e) => drop.addEventListener(e, () => drop.classList.add("dragover")));
  ["dragleave", "drop"].forEach((e) => drop.addEventListener(e, () => drop.classList.remove("dragover")));
});

/* ---------------- settings ---------------- */
async function openSettings() {
  const d = await api("/api/settings/stock");
  const s = d.settings, c = d.choices;
  const opts = (list, sel) => list.map((x) => `<option ${x === sel ? "selected" : ""}>${esc(x)}</option>`).join("");
  $("#setUnit").innerHTML = opts(c.unit, s.unit);
  $("#setPurchase").innerHTML = opts(c.purchaseaccount, s.purchaseaccount);
  $("#setSales").innerHTML = opts(c.salesaccount, s.salesaccount);
  $("#setTdsHsn").value = s.gst_tds.hsn_prefixes.join(", ");
  $("#setTdsRate").value = s.gst_tds.rate;
  $("#setTcsWords").value = s.tcs.name_keywords.join(", ");
  $("#setTcsRate").value = s.tcs.rate;
  $("#settingsMsg").innerHTML = "";
  document.querySelectorAll("section").forEach((x) => x.classList.toggle("active", x.dataset.step === "settings"));
  $("#steps").hidden = true;
}
function closeSettings() { go(state.step); }

async function saveSettings() {
  const split = (s) => s.split(",").map((x) => x.trim()).filter(Boolean);
  const body = {
    unit: $("#setUnit").value, purchaseaccount: $("#setPurchase").value, salesaccount: $("#setSales").value,
    gst_tds: { hsn_prefixes: split($("#setTdsHsn").value), rate: $("#setTdsRate").value },
    tcs: { name_keywords: split($("#setTcsWords").value), rate: $("#setTcsRate").value },
  };
  try {
    await api("/api/settings/stock", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    let msg = "Settings saved. They apply to all new stock conversions.";
    if (state.dtype === "stock" && state.job) {
      state.job = await api(`${base()}/reapply`, { method: "POST" });
      msg += " The open conversion has been updated too.";
    }
    $("#settingsMsg").innerHTML = `<div class="ok-box">${msg}</div>`;
    toast("Settings saved");
  } catch (e) {
    $("#settingsMsg").innerHTML = `<div class="error-box">${esc(e.message)}</div>`;
  }
}

init();
