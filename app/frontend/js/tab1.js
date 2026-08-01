let columns = [];

const $ = (id) => document.getElementById(id);
const show = (id) => { $(id).hidden = false; };

$("fileInput").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const fd = new FormData();
  fd.append("file", file);
  const res = await fetch(`${API}/ingest/upload`, { method: "POST", body: fd });
  const data = await res.json();
  columns = data.columns;

  $("uploadResult").textContent =
    `fingerprint ${data.dataset_fingerprint.slice(0, 16)}…  ·  ${data.n_rows} rows  ·  ${columns.length} cols`;

  renderSchemaRows();
  show("schemaCard");
});

function renderSchemaRows() {
  const box = $("schemaRows");
  box.innerHTML = "";
  columns.forEach((c) => {
    const row = document.createElement("div");
    row.className = "schema-row";
    row.innerHTML = `
      <span>${c.name}</span>
      <select data-col="${c.name}">
        <option value="identifier" ${c.guessed_type === "identifier" ? "selected" : ""}>identifier</option>
        <option value="numeric_covariate" ${c.guessed_type === "numeric_covariate" ? "selected" : ""}>numeric covariate</option>
        <option value="categorical_covariate" ${c.guessed_type === "categorical_covariate" ? "selected" : ""}>categorical covariate</option>
        <option value="primary_outcome">primary outcome</option>
        <option value="time_to_event">time to event</option>
      </select>`;
    box.appendChild(row);
  });
}

let savedMappings = [];

$("saveSchema").addEventListener("click", async () => {
  const mappings = [...document.querySelectorAll("#schemaRows select")].map((sel) => ({
    name: sel.dataset.col,
    type: sel.value,
  }));
  const res = await fetch(`${API}/ingest/schema`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ column_mappings: mappings }),
  });
  const data = await res.json();
  savedMappings = data.column_mappings || mappings;

  const nonIdCols = savedMappings.filter((m) => m.type !== "identifier").map((m) => m.name);
  const outSel = $("outcomeCol");
  outSel.innerHTML = nonIdCols.map((c) => `<option value="${c}">${c}</option>`).join("");
  show("outcomeCard");
});

$("saveOutcome").addEventListener("click", async () => {
  await fetch(`${API}/ingest/outcome`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      column_name: $("outcomeCol").value,
      event_value: $("eventVal").value,
      censored_value: $("censoredVal").value,
    }),
  });
  $("vaultBadge").hidden = false;
  show("sentinelCard");
});

$("saveSentinels").addEventListener("click", async () => {
  const globalNa = $("globalNa").value.split(",").map((s) => s.trim()).filter(Boolean);
  await fetch(`${API}/ingest/sentinels`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ global_na_strings: globalNa, column_overrides: {} }),
  });
  await runInspection();
  show("inspectionCard");
  show("lockCard");
});

async function runInspection() {
  const [banner, miss, redund, sparse] = await Promise.all([
    fetch(`${API}/ingest/cohort-banner`).then((r) => r.json()),
    fetch(`${API}/ingest/missingness`).then((r) => r.json()),
    fetch(`${API}/ingest/redundancy`).then((r) => r.json()),
    fetch(`${API}/ingest/sparse-crosstabs`).then((r) => r.json()),
  ]);

  $("cohortBanner").innerHTML =
    `<span>N ${banner.n_total}</span><span>E ${banner.e_total}</span><span>rate ${(banner.event_rate * 100).toFixed(1)}%</span>`;

  $("missingnessOut").innerHTML = Object.entries(miss)
    .map(([k, v]) => `${k}: ${(v * 100).toFixed(1)}%`).join("<br/>") || "none";

  $("redundancyOut").innerHTML = redund.high_correlation_pairs
    .map((p) => `${p.var1} × ${p.var2}: r=${p.r}`).join("<br/>") || "none flagged";

  $("sparseOut").innerHTML = sparse.sparse_cross_tabs
    .map((p) => `${p.var1} × ${p.var2}: min cell ${p.min_cell_count}`).join("<br/>") || "none flagged";
}

$("lockBtn").addEventListener("click", async () => {
  const res = await fetch(`${API}/ingest/lock`, { method: "POST" });
  const payload = await res.json();
  $("lockResult").innerHTML = `<div class="seal">&#128274; H0 ${payload.provenance.payload_fingerprint_h0.slice(0, 20)}…</div>`;
});
