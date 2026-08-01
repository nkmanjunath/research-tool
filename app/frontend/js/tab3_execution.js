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
  $("routeOut").textContent =
    data.route === "PUBLICATION_PACKAGE"
      ? "PASS — standard publication package"
      : "WARNING — publication package + auto-injected Sensitivity & Limitations section";

  $("coeffOut").innerHTML = data.model_results.coefficients
    .map((c) => `${c.variable}: OR ${c.adjusted_or} [${c.adjusted_ci_95[0]}, ${c.adjusted_ci_95[1]}] p=${c.adjusted_p}`)
    .join("<br/>");

  $("hexecSeal").innerHTML = `&#128274; Hexec ${data.provenance.execution_fingerprint.slice(0, 20)}…`;
}

function renderAutopsy(data) {
  $("autopsyOut").textContent =
    `Failed gate: ${data.failed_gate}  ·  implicated: ${data.implicated_variables.join(", ") || "n/a"}`;

  $("remediationChoices").innerHTML = data.remediation_options
    .map((opt, i) => `<label style="display:block"><input type="checkbox" name="remediation" value="${opt}" ${i === 0 ? "checked" : ""}/> ${opt}</label>`)
    .join("");
}

$("prepareAmendmentBtn").addEventListener("click", async () => {
  const chosen = document.querySelector('input[name="remediation"]:checked')?.value || "";
  const rationale = $("amendmentRationale").value;
  const res = await fetch(`${API}/execute/amendment/prepare`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ chosen_remediation: chosen, rationale }),
  });
  const data = await res.json();
  if (!res.ok) { alert(data.detail); return; }
  $("amendmentOut").textContent = "Amendment prepared — go to Tab 2, edit Steps C/D only, then re-lock.";
});
