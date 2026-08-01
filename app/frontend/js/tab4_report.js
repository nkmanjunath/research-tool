$("genAssetsBtn").addEventListener("click", async () => {
  const [t1, t2, fig, mtxt, checklist] = await Promise.all([
    fetch(`${API}/report/tables/table1`).then((r) => r.json()),
    fetch(`${API}/report/tables/table2`).then((r) => r.json()),
    fetch(`${API}/report/figures/forest-plot`).then((r) => r.json()),
    fetch(`${API}/report/manuscript/methods`).then((r) => r.json()),
    fetch(`${API}/report/checklist`).then((r) => r.json()),
  ]);

  $("table1Out").innerHTML = t1.rows.map((r) => `${r.variable}: ${JSON.stringify(r.by_group)} · missing ${r.missing_pct}%`).join("<br/>");
  show("table1Card");

  $("table2Out").innerHTML = t2.rows.map((r) => `${r.variable}: OR ${r.adjusted_or} [${r.adjusted_ci_95[0]}, ${r.adjusted_ci_95[1]}] p=${r.adjusted_p} · E-value ${r.e_value}`).join("<br/>")
    + `<br/><br/>k=${t2.footer.k} · N_eff=${t2.footer.n_effective} · E_eff=${t2.footer.e_effective}`;
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
    `<div class="seal">&#128274; Hbundle ${data.bundle_fingerprint_hbundle.slice(0, 20)}…</div>` +
    `<br/><a href="${data.download_url}" target="_blank">Download study_audit_binder.zip</a>`;
});
