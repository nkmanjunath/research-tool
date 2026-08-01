function enterTab2() {
  const sel = $("exposureCol");
  if (sel.options.length === 0) {
    const vaultedOutcome = $("outcomeCol").value;
    const idCols = new Set((typeof savedMappings !== "undefined" ? savedMappings : [])
      .filter((m) => m.type === "identifier")
      .map((m) => m.name));
    sel.innerHTML = columns
      .filter((c) => c.name !== vaultedOutcome && !idCols.has(c.name))
      .map((c) => `<option value="${c.name}">${c.name}</option>`)
      .join("");
  }
}

$("saveExposure").addEventListener("click", async () => {
  await fetch(`${API}/plan/exposure`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      column_name: $("exposureCol").value,
      reference_level: $("referenceLevel").value,
    }),
  });

  const confirm = await fetch(`${API}/plan/outcome-confirm`).then((r) => r.json());
  $("outcomeConfirmOut").textContent =
    `outcome: ${confirm.column_name}  ·  event=${confirm.event_value}  ·  censored=${confirm.censored_value}`;
  show("stepB");
});

$("saveTimeCol").addEventListener("click", async () => {
  if ($("timeCol").value.trim()) {
    await fetch(`${API}/plan/time-column`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ time_column: $("timeCol").value.trim() }),
    });
  }
  renderConfounderChecks();
  show("stepC");
});

function renderConfounderChecks() {
  const exposureName = $("exposureCol").value;
  const vaultedOutcome = $("outcomeCol").value;
  const timeColVal = $("timeCol").value ? $("timeCol").value.trim() : "";
  const idCols = new Set((typeof savedMappings !== "undefined" ? savedMappings : [])
    .filter((m) => m.type === "identifier")
    .map((m) => m.name));

  const box = $("confounderChecks");
  box.innerHTML = columns
    .filter((c) => c.name !== exposureName && c.name !== vaultedOutcome && c.name !== timeColVal && !idCols.has(c.name))
    .map(
      (c) => `<label style="display:block"><input type="checkbox" value="${c.name}" /> ${c.name}</label>`
    )
    .join("");
}

$("saveConfounders").addEventListener("click", async () => {
  const picked = [...document.querySelectorAll("#confounderChecks input:checked")].map((i) => i.value);
  await fetch(`${API}/plan/confounders`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ confounders: picked }),
  });
  const warn = await fetch(`${API}/plan/redundancy-warnings`).then((r) => r.json());
  $("redundancyWarn").textContent = warn.warnings.length
    ? warn.warnings.map((w) => `⚠ ${w.var1} × ${w.var2} r=${w.r}`).join(" · ")
    : "no redundancy warnings among selected confounders";
  show("stepD");
  await refreshEpv();
});

$("addInteraction").addEventListener("click", async () => {
  const res = await fetch(`${API}/plan/interactions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      term: $("interactionTerm").value,
      rationale: $("interactionRationale").value,
    }),
  });
  if (!res.ok) {
    const err = await res.json();
    alert(err.detail);
    return;
  }
  const mirror = await fetch(`${API}/plan/manuscript-mirror`).then((r) => r.json());
  $("manuscriptMirror").textContent = mirror.methods_section_preview;
  await refreshEpv();
});

$("saveStrategy").addEventListener("click", async () => {
  await fetch(`${API}/plan/missing-strategy`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ global_default: $("globalStrategy").value, column_overrides: {} }),
  });
  await refreshEpv();
  show("stepLock");
});

async function refreshEpv() {
  const res = await fetch(`${API}/plan/epv-live`);
  if (!res.ok) return;
  const epv = await res.json();
  const lowEpv = epv.epv !== null && epv.epv < 10;
  $("epvGauge").className = `banner ${lowEpv ? "gate-warn" : ""}`;
  $("epvGauge").innerHTML =
    `<span>N_eff ${epv.n_effective}</span><span>E_eff ${epv.e_effective}</span><span>k ${epv.parameters_k}</span><span class="${lowEpv ? "gate-fail" : ""}">EPV ${epv.epv} ${lowEpv ? "⚠️ (EPV < 10)" : ""}</span>`;
}

$("lockPlanBtn").addEventListener("click", async () => {
  const res = await fetch(`${API}/plan/lock`, { method: "POST" });
  const payload = await res.json();
  if (!res.ok) { alert(payload.detail); return; }
  $("lockPlanResult").innerHTML = `<div class="seal">&#128274; H1 ${payload.provenance.plan_fingerprint_h1.slice(0, 20)}…</div>`;
});
