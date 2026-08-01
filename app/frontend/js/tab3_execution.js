const GATE_DISPLAY_MAP = {
  "complete_separation": "Separation & Firth Penalization",
  "multicollinearity_vif": "Multicollinearity (VIF)",
  "proportional_hazards": "Proportional Hazards (Schoenfeld Residuals)",
  "linearity_continuous_terms": "Linearity of Continuous Terms",
  "events_per_variable_epv": "Events Per Variable (EPV)",
};

function formatClinicalLabel(varName) {
  if (!varName) return "";
  const baseMap = {
    "treatment_arm": "Treatment Group",
    "high_risk_fish": "High-Risk Cytogenetics",
    "prior_lines": "Prior Lines of Therapy",
    "age": "Age, years",
    "iss_stage": "ISS Stage",
    "sex": "Sex",
  };
  if (varName === "age") return "Age, years (per year increase)";
  if (varName === "prior_lines") return "Prior Lines of Therapy (per line)";

  for (const base of ["treatment_arm", "high_risk_fish", "iss_stage", "prior_lines", "sex"]) {
    if (varName.startsWith(base + "_")) {
      const level = varName.slice(base.length + 1);
      const baseLabel = baseMap[base] || base;
      if (base === "treatment_arm") return `${baseLabel}: Arm ${level} (vs Arm A)`;
      if (base === "high_risk_fish") return `${baseLabel} (${level.toUpperCase() === 'YES' ? 'Yes vs No' : 'No vs Yes'})`;
      if (base === "iss_stage") return `${baseLabel} Stage ${level} (vs Stage I)`;
      if (base === "sex") return `Sex: ${level === 'M' ? 'Male (vs Female)' : 'Female (vs Male)'}`;
      return `${baseLabel}: ${level}`;
    }
  }
  return baseMap[varName] || varName.replace(/_/g, " ").replace(/\b\w/g, (l) => l.toUpperCase());
}

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
    const statusCls = t.status === "PASS" ? "pass" : t.status === "WARNING" ? "warn" : t.status === "FAIL" ? "fail" : "na";
    const gateCls = t.status === "PASS" ? "gate-pass" : t.status === "WARNING" ? "gate-warn" : t.status === "FAIL" ? "gate-fail" : "gate-na";
    const gateTitle = GATE_DISPLAY_MAP[t.test_name] || t.test_name.replace(/_/g, " ").replace(/\b\w/g, (l) => l.toUpperCase());
    const metricStr = t.metric_value !== undefined ? ` <span class="mono" style="font-size:11px; opacity:0.85;">(${t.metric_value})</span>` : "";

    return `<div class="gate-row ${gateCls}">
      <span style="font-weight: 600;">${gateTitle}</span>
      <span class="status-pill ${statusCls}">${t.status}${metricStr}</span>
    </div>`;
  }).join("");
}

function renderPublicationRoute(data) {
  const modelLabel = data.model_results.model_type === "cox_ph" ? "Cox Proportional Hazards" : "Logistic Regression";
  const metricLabel = data.model_results.model_type === "cox_ph" ? "aHR" : "aOR";

  const routeTag = data.route === "PUBLICATION_PACKAGE"
    ? `<span class="status-pill pass">PASS</span> Standard publication package (${modelLabel})`
    : `<span class="status-pill warn">WARNING</span> Publication package (${modelLabel}) + auto-injected Sensitivity & Limitations section`;

  $("routeOut").innerHTML = routeTag;

  const tableRows = data.model_results.coefficients.map((c) => {
    const label = formatClinicalLabel(c.variable);
    const pFormatted = c.adjusted_p < 0.001 ? "< 0.001" : c.adjusted_p.toFixed(4);
    return `<tr>
      <td><strong>${label}</strong></td>
      <td><code>${metricLabel} ${c.adjusted_or}</code></td>
      <td><code>[${c.adjusted_ci_95[0]} – ${c.adjusted_ci_95[1]}]</code></td>
      <td><code>p = ${pFormatted}</code></td>
    </tr>`;
  }).join("");

  $("coeffOut").innerHTML = `
    <table class="journal-table" style="margin-top: 12px;">
      <thead>
        <tr><th>Variable / Covariate</th><th>Adjusted Estimate</th><th>95% Confidence Interval</th><th>p-value</th></tr>
      </thead>
      <tbody>${tableRows}</tbody>
    </table>
  `;

  $("hexecSeal").innerHTML = `🔒 Hexec ${data.provenance.execution_fingerprint.slice(0, 20)}…`;
}

function renderAutopsy(data) {
  const failedGateLabel = GATE_DISPLAY_MAP[data.failed_gate] || data.failed_gate;
  $("autopsyOut").innerHTML = `
    <div style="background: rgba(239, 68, 68, 0.12); border: 1px solid rgba(239, 68, 68, 0.4); padding: 14px 18px; border-radius: 8px; margin-bottom: 14px; backdrop-filter: blur(8px);">
      <strong style="color: #f87171; font-size: 14px;">⚠️ Diagnostic Gate Failure: ${failedGateLabel}</strong>
      <p style="margin: 6px 0 0 0; font-size: 13px; color: #9ca3af;">Implicated Variables: ${data.implicated_variables.map(formatClinicalLabel).join(", ") || "Model Parameters / EPV Floor"}</p>
    </div>
  `;

  $("remediationChoices").innerHTML = data.remediation_options
    .map((opt, i) => `
      <label style="display: flex; align-items: center; gap: 8px; margin-bottom: 8px; cursor: pointer; background: #111827; padding: 10px 14px; border-radius: 6px; border: 1px solid #1e293b;">
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
