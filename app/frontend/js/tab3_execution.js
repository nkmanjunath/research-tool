$("runBtn").addEventListener("click", async () => {
  const res = await fetch(`${API}/execute/run`, { method: "POST" });
  const data = await res.json();
  if (!res.ok) { alert(data.detail); return; }

  show("gatesCard");
  renderGates(data.diagnostics_summary.tests);

  if (data.route === "AUTOPSY_CANVAS") {
    $("autopsyCard").hidden = false;
    $("passCard").hidden = true;
    renderAutopsy(data);
  } else {
    $("passCard").hidden = false;
    $("autopsyCard").hidden = true;
    renderPublicationRoute(data);
  }
});

function renderGates(tests) {
  $("gatesOut").innerHTML = tests.map((t) => {
    const cls = t.status === "PASS" ? "gate-pass" : t.status === "WARNING" ? "gate-warn" : t.status === "FAIL" ? "gate-fail" : "gate-na";
    return `<div class="gate-row ${cls}">${t.test_name}: <strong>${t.status}</strong>${t.metric_value !== undefined ? ` (${t.metric_value})` : ""}</div>`;
  }).join("");
}

function renderPublicationRoute(data) {
  const modelLabel = data.model_results.model_type === "cox_ph" ? "Cox Proportional Hazards" : "Logistic Regression";
  const metricLabel = data.model_results.model_type === "cox_ph" ? "aHR" : "aOR";

  $("routeOut").textContent =
    data.route === "PUBLICATION_PACKAGE"
      ? `PASS — standard publication package (${modelLabel})`
      : `WARNING — publication package (${modelLabel}) + auto-injected Sensitivity & Limitations section`;

  $("coeffOut").innerHTML = data.model_results.coefficients
    .map((c) => `<strong>${c.variable}</strong>: ${metricLabel} ${c.adjusted_or} [95% CI ${c.adjusted_ci_95[0]}–${c.adjusted_ci_95[1]}], p=${c.adjusted_p}`)
    .join("<br/>");

  $("hexecSeal").innerHTML = `🔒 Hexec ${data.provenance.execution_fingerprint.slice(0, 20)}…`;
}

function renderAutopsy(data) {
  $("autopsyOut").innerHTML = `
    <div style="background: rgba(239, 68, 68, 0.1); border: 1px solid rgba(239, 68, 68, 0.3); padding: 12px; border-radius: 6px; margin-bottom: 12px;">
      <strong style="color: #ef4444;">⚠️ Diagnostic Gate Failure: ${data.failed_gate}</strong>
      <p style="margin: 4px 0 0 0; font-size: 13px; color: #9ca3af;">Implicated Variables: ${data.implicated_variables.join(", ") || "Model Parameters / EPV Floor"}</p>
    </div>
  `;

  $("remediationChoices").innerHTML = data.remediation_options
    .map((opt, i) => `
      <label style="display: flex; align-items: center; gap: 8px; margin-bottom: 8px; cursor: pointer; background: #111827; padding: 8px 12px; border-radius: 4px; border: 1px solid #1e293b;">
        <input type="radio" name="remediation" value="${opt}" ${i === 0 ? "checked" : ""}/>
        <span>${opt}</span>
      </label>
    `)
    .join("");
}

$("prepareAmendmentBtn").addEventListener("click", async () => {
  const chosen = document.querySelector('input[name="remediation"]:checked')?.value || "";
  const rationale = $("amendmentRationale").value;
  if (!rationale || rationale.trim().length < 15) {
    alert("Please provide a valid protocol amendment rationale (at least 15 characters).");
    return;
  }

  const res = await fetch(`${API}/execute/amendment/prepare`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ chosen_remediation: chosen, rationale }),
  });
  const data = await res.json();
  if (!res.ok) { alert(data.detail); return; }
  $("amendmentOut").innerHTML = `<span style="color:#10b981;">✓ Amendment prepared (H<sub>n</sub>) — Navigating to Tab 2 to edit Steps C/D...</span>`;

  setTimeout(() => {
    const tab2Btn = document.querySelector('.tab[data-tab="2"]');
    if (tab2Btn) tab2Btn.click();
  }, 350);
});
