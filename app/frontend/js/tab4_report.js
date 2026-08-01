$("genAssetsBtn").addEventListener("click", async () => {
  const [t1, t2, fig, mtxt, checklist] = await Promise.all([
    fetch(`${API}/report/tables/table1`).then((r) => r.json()),
    fetch(`${API}/report/tables/table2`).then((r) => r.json()),
    fetch(`${API}/report/figures/forest-plot`).then((r) => r.json()),
    fetch(`${API}/report/manuscript/methods`).then((r) => r.json()),
    fetch(`${API}/report/checklist`).then((r) => r.json()),
  ]);

  // Render publication-grade Table 1 HTML
  const g1 = t1.groups && t1.groups[0] ? t1.groups[0] : "Arm A";
  const g2 = t1.groups && t1.groups[1] ? t1.groups[1] : "Arm B";

  const t1RowsHtml = t1.rows.map((r) => {
    const val1 = r.by_group[g1] || "N/A";
    const val2 = r.by_group[g2] || "N/A";
    const imbTag = r.imbalanced
      ? `<span style="color:#ef4444; font-weight:600;">IMBALANCED (|SMD|>0.1)</span>`
      : `<span style="color:#10b981;">BALANCED</span>`;
    return `<tr>
      <td><strong>${r.variable}</strong></td>
      <td>${val1}</td>
      <td>${val2}</td>
      <td>${r.smd.toFixed(3)}</td>
      <td>${imbTag}</td>
      <td>${r.missing_pct}%</td>
    </tr>`;
  }).join("");

  $("table1Out").innerHTML = `
    <table class="journal-table">
      <thead>
        <tr><th>Variable</th><th>${g1}</th><th>${g2}</th><th>SMD</th><th>Balance Status</th><th>Missing %</th></tr>
      </thead>
      <tbody>${t1RowsHtml}</tbody>
    </table>
  `;
  show("table1Card");

  // Render publication-grade Table 2 HTML
  const effectLabel = t2.footer.model_type === "cox_ph" ? "Adjusted HR" : "Adjusted OR";
  const t2RowsHtml = t2.rows.map((r) => `
    <tr>
      <td><strong>${r.variable}</strong></td>
      <td>${r.adjusted_or}</td>
      <td>[${r.adjusted_ci_95[0]}–${r.adjusted_ci_95[1]}]</td>
      <td>${r.adjusted_p}</td>
      <td><span class="badge ${r.classification === 'significant' ? 'badge-pass' : r.classification === 'borderline/trend' ? 'badge-warn' : ''}">${r.classification}</span></td>
      <td>${r.e_value_formatted}</td>
    </tr>
  `).join("");

  $("table2Out").innerHTML = `
    <table class="journal-table">
      <thead>
        <tr><th>Variable</th><th>${effectLabel}</th><th>95% CI</th><th>p</th><th>Classification</th><th>E-value (CI bound)</th></tr>
      </thead>
      <tbody>${t2RowsHtml}</tbody>
    </table>
    <p class="small hint" style="margin-top: 10px;">
      Model Engine: <strong>${t2.footer.model_type}</strong> · k=${t2.footer.k} · N<sub>eff</sub>=${t2.footer.n_effective} · E<sub>eff</sub>=${t2.footer.e_effective}
      ${t2.footer.survival_note ? `<br/><em>${t2.footer.survival_note}</em>` : ""}
    </p>
  `;
  show("table2Card");

  $("forestOut").innerHTML = fig.svg;
  show("forestCard");

  $("manuscriptOut").textContent = mtxt.text;
  $("checklistOut").innerHTML = checklist.items.map((i) => `[${i.item}] ${i.description} → ${i.satisfied_by}`).join("<br/>");
  show("manuscriptCard");

  show("binderCard");
});

$("buildBinderBtn").addEventListener("click", async () => {
  const res = await fetch(`${API}/report/audit-binder`, { method: "POST" });
  const data = await res.json();
  if (!res.ok) { alert(data.detail); return; }
  $("binderOut").innerHTML =
    `<div class="seal">🔒 Hbundle ${data.bundle_fingerprint_hbundle.slice(0, 20)}…</div>` +
    `<br/><a href="${data.download_url}" target="_blank" class="download-link">Download study_audit_binder.zip</a>`;
});
