let isAmendmentActive = false;

async function enterTab2() {
  const amRes = await fetch(`${API}/execute/amendment/state`);
  const amState = amRes.ok ? await amRes.json() : { amendment_mode: false };

  if (amState.amendment_mode) {
    isAmendmentActive = true;
    setupAmendmentMode(amState);
    return;
  }

  isAmendmentActive = false;
  if ($("amendmentBannerCard")) $("amendmentBannerCard").hidden = true;

  // Restore Step A & Step B controls
  $("exposureCol").disabled = false;
  $("referenceLevel").disabled = false;
  $("saveExposure").disabled = false;
  $("saveExposure").innerText = "Set exposure";
  $("saveTimeCol").disabled = false;
  $("saveTimeCol").innerText = "Confirm";

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

function setupAmendmentMode(state) {
  const parentShort = state.parent_plan_hash ? state.parent_plan_hash.slice(0, 16) + "…" : "H1";
  $("amendmentBanner").innerHTML = `
    <strong>⚠️ Amendment in progress</strong> — revising plan <code>${parentShort}</code> following a <strong>${state.failed_gate}</strong> failure.<br/>
    Steps A/B are locked; only confounders, interactions, and missing-data strategy may change.
  `;
  show("amendmentBannerCard");

  // Step A · Exposure — locked / read-only
  const exp = state.locked_readonly.exposure;
  if (exp && exp.column_name) {
    $("exposureCol").innerHTML = `<option value="${exp.column_name}">${exp.column_name}</option>`;
    $("exposureCol").disabled = true;
    $("referenceLevel").value = exp.reference_level || "";
    $("referenceLevel").disabled = true;
    $("saveExposure").disabled = true;
    $("saveExposure").innerText = "🔒 Exposure Locked";
  }

  // Step B · Outcome confirmation — locked / read-only
  const outc = state.locked_readonly.outcome_confirmation;
  if (outc && outc.column_name) {
    $("outcomeConfirmOut").textContent =
      `outcome: ${outc.column_name}  ·  event=${outc.event_value}  ·  censored=${outc.censored_value} [LOCKED]`;
    if (outc.time_column) {
      $("timeCol").value = outc.time_column;
      $("timeCol").disabled = true;
    }
    $("saveTimeCol").disabled = true;
    $("saveTimeCol").innerText = "🔒 Outcome Locked";
    show("stepB");
  }

  // Step C · Confounder checklist with flagged variables
  renderConfounderChecksAmendment(state.editable.confounders, state.flagged_variables);
  show("stepC");

  if (state.prefilled_rationale) {
    $("interactionRationale").value = state.prefilled_rationale;
  }

  // Step D & Step Lock
  show("stepD");
  show("stepLock");
  refreshEpv();
}

function renderConfounderChecksAmendment(selectedConfounders, flaggedVars) {
  const exposureName = $("exposureCol").value;
  const vaultedOutcome = $("outcomeCol").value;
  const timeColVal = $("timeCol").value ? $("timeCol").value.trim() : "";
  const idCols = new Set((typeof savedMappings !== "undefined" ? savedMappings : [])
    .filter((m) => m.type === "identifier")
    .map((m) => m.name));

  const selectedSet = new Set(selectedConfounders || []);
  const flaggedSet = new Set(flaggedVars || []);

  const box = $("confounderChecks");
  box.innerHTML = columns
    .filter((c) => c.name !== exposureName && c.name !== vaultedOutcome && c.name !== timeColVal && !idCols.has(c.name))
    .map((c) => {
      const isChecked = selectedSet.has(c.name);
      const isFlagged = flaggedSet.has(c.name);
      const style = isFlagged
        ? `border: 1px solid #ef4444; background: rgba(239, 68, 68, 0.12); padding: 8px 12px; border-radius: 6px; margin-bottom: 6px; display: block;`
        : `display: block; margin-bottom: 6px;`;
      const flagBadge = isFlagged ? ` <span class="badge" style="border-color:#ef4444; color:#ef4444; font-size:10px; padding:2px 6px; vertical-align:middle; margin-left:6px;">⚠️ FLAGGED FOR ${isFlagged}</span>` : "";
      return `<label style="${style}"><input type="checkbox" value="${c.name}" ${isChecked ? "checked" : ""} class="conf-check" /> ${c.name}${flagBadge}</label>`;
    })
    .join("");

  document.querySelectorAll(".conf-check").forEach((cb) => {
    cb.addEventListener("change", syncConfoundersAndRefreshEpv);
  });
}

async function syncConfoundersAndRefreshEpv() {
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
  await refreshEpv();
}

$("saveExposure").addEventListener("click", async () => {
  if (isAmendmentActive) return;
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
  if (isAmendmentActive) {
    show("stepC");
    return;
  }
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
      (c) => `<label style="display:block; margin-bottom:6px;"><input type="checkbox" value="${c.name}" class="conf-check" /> ${c.name}</label>`
    )
    .join("");

  document.querySelectorAll(".conf-check").forEach((cb) => {
    cb.addEventListener("change", syncConfoundersAndRefreshEpv);
  });
}

$("saveConfounders").addEventListener("click", async () => {
  await syncConfoundersAndRefreshEpv();
  show("stepD");
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

  const isAmended = payload.provenance.parent_plan_hash || payload.provenance.amendment_rationale;
  if (isAmended) {
    $("lockPlanResult").innerHTML = `
      <div class="seal" style="border-color: #0ea5e9; color: #0ea5e9; background: rgba(14, 165, 233, 0.1);">
        🔒 Amended Plan H<sub>n</sub> (${payload.provenance.plan_fingerprint_h1.slice(0, 20)}…)
        <span class="badge badge-pass" style="margin-left: 8px;">was_amended: true</span>
      </div>
      <p class="small hint" style="margin-top: 6px;">
        Chained off parent failed plan: <code>${payload.provenance.parent_plan_hash.slice(0, 16)}…</code>
      </p>
    `;
  } else {
    $("lockPlanResult").innerHTML = `<div class="seal">&#128274; H1 ${payload.provenance.plan_fingerprint_h1.slice(0, 20)}…</div>`;
  }
});
