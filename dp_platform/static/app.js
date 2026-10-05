// Vanilla JS for all pages. Every value that came from a PDF or a filename is written with
// textContent / .value, never innerHTML, so a hostile drawing cannot inject markup.
"use strict";

const FIELDS = [
  ["change_no", "Change No."], ["zone", "Zone"], ["ltr", "Ltr"],
  ["description", "Description"], ["date", "Date"], ["approved", "Approved"],
  ["name", "Name"], ["size", "Size"], ["cage_code", "Cage Code"],
  ["drawing_number", "Drawing Number"],
];

// Compact columns shown in the results table (the full set above is in the
// correction form). These five identify the part and its current revision.
const TABLE_FIELDS = ["drawing_number", "name", "ltr", "size", "cage_code"];

function el(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") e.className = v;
    else if (k === "text") e.textContent = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v);
  }
  for (const kid of kids) if (kid != null) e.append(kid);
  return e;
}

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (r.status === 401 && !location.pathname.startsWith("/login")) { location.href = "/login"; throw new Error("auth"); }
  if (!r.ok) {
    let detail = r.statusText;
    try { detail = (await r.json()).detail || detail; } catch (_) {}
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return r.json();
}

function applyBrand(b) {
  document.documentElement.style.setProperty("--brand", b.brand_color);
  document.title = b.app_name;
}

function buildHeader(me) {
  const h = document.getElementById("top");
  h.append(
    el("a", { href: "/", class: "name", text: me.app_name }),
    el("span", { class: "tag", text: me.tagline }),
    el("span", { class: "spacer" }),
    el("span", { class: "tag", text: me.username }),
    el("button", { text: "Sign out", onclick: async () => { await api("/api/logout", { method: "POST" }); location.href = "/login"; } }),
  );
}

function chip(cls, text) { return el("span", { class: "chip " + cls, text: text || cls }); }
const pretty = s => s.replace("_", " ");

// ---------------------------------------------------------------- login
async function loginPage() {
  applyBrand(await fetch("/api/branding").then(r => r.json()));
  const b = await fetch("/api/branding").then(r => r.json());
  document.getElementById("app-name").textContent = b.app_name;
  document.getElementById("app-tag").textContent = b.tagline;
  document.getElementById("login-form").addEventListener("submit", async e => {
    e.preventDefault();
    const msg = document.getElementById("msg"); msg.textContent = "";
    try {
      await api("/api/login", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: e.target.username.value, password: e.target.password.value }) });
      location.href = "/";
    } catch (err) { msg.textContent = err.message; msg.className = "msg err"; }
  });
}

// ---------------------------------------------------------------- index
async function indexPage() {
  const me = await api("/api/me"); applyBrand(me); buildHeader(me);
  const drop = document.getElementById("drop"), input = document.getElementById("file");
  const msg = document.getElementById("msg");

  async function upload(files) {
    files = [...files];
    if (!files.length) return;
    msg.className = "msg"; msg.textContent = "Uploading " + files.length + " file(s)...";
    const fd = new FormData();
    files.forEach(f => fd.append("files", f));
    try {
      const r = await api("/api/batches", { method: "POST", body: fd });
      if (r.rejected.length) sessionStorage.setItem("rejected", JSON.stringify(r.rejected));
      location.href = "/batch/" + r.batch_id;
    } catch (err) { msg.className = "msg err"; msg.textContent = err.message; }
  }
  drop.addEventListener("click", () => input.click());
  input.addEventListener("change", () => upload(input.files));
  ["dragenter", "dragover"].forEach(t => drop.addEventListener(t, e => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach(t => drop.addEventListener(t, e => { e.preventDefault(); drop.classList.remove("over"); }));
  drop.addEventListener("drop", e => upload(e.dataTransfer.files));

  const batches = await api("/api/batches");
  const body = document.getElementById("batches");
  if (!batches.length) { body.append(el("tr", {}, el("td", { colspan: 7, class: "empty", text: "No batches yet. Upload some drawings above." }))); return; }
  for (const b of batches) {
    const tr = el("tr", { class: "clickable", onclick: () => location.href = "/batch/" + b.id },
      el("td", { text: b.name }),
      el("td", { text: b.done + " / " + b.total + (b.done < b.total ? " processing" : "") }),
      el("td", {}, chip("DEFENSE", b.defense)), el("td", {}, chip("NON_DEFENSE", b.non_defense)),
      el("td", {}, chip("NEEDS_REVIEW", b.needs_review)), el("td", {}, chip("FAILED", b.failed)),
      el("td", { class: "muted", text: b.created_at }));
    body.append(tr);
  }
}

// ---------------------------------------------------------------- batch
async function batchPage() {
  const me = await api("/api/me"); applyBrand(me); buildHeader(me);
  const batchId = location.pathname.split("/").pop();
  let selected = null, shownState = null, data = null, timer = null;

  const rej = sessionStorage.getItem("rejected");
  if (rej) {
    sessionStorage.removeItem("rejected");
    const note = document.getElementById("rejected");
    note.className = "msg err";
    note.textContent = "Skipped: " + JSON.parse(rej).map(r => r.filename + " (" + r.reason + ")").join(", ");
  }

  function renderCounts() {
    const box = document.getElementById("counts"); box.replaceChildren();
    for (const k of ["DEFENSE", "NON_DEFENSE", "NEEDS_REVIEW", "FAILED"]) {
      box.append(el("div", { class: "panel count" }, el("div", { class: "n", text: String(data.counts[k]) }), el("div", { class: "l", text: pretty(k) })));
    }
    document.getElementById("bar").style.width = (data.total ? 100 * data.done / data.total : 0) + "%";
    document.getElementById("progress-text").textContent = data.finished
      ? "All " + data.total + " drawings processed" : data.done + " of " + data.total + " processed. Scanned drawings take about 1.5 minutes each.";
    const dl = document.getElementById("download");
    dl.href = "/api/batches/" + batchId + "/export.xlsx";
    dl.style.display = data.done ? "" : "none";
  }

  function renderTable() {
    const body = document.getElementById("rows"); body.replaceChildren();
    for (const d of data.drawings) {
      const ed = new Set(d.edited_fields);
      const cell = (k, mono, nw) => el("td", { class: (mono ? "mono " : "") + (nw ? "nowrap " : "") + (ed.has(k) ? "edited" : ""), text: d.state === "done" ? d[k] : "" });
      body.append(el("tr", { class: "clickable" + (selected === d.id ? " selected" : ""), onclick: () => select(d.id) },
        el("td", { text: d.filename }),
        el("td", {}, d.state === "done" ? chip(d.status, pretty(d.status)) : chip(d.state)),
        ...TABLE_FIELDS.map(k => cell(k, k !== "name"))));
    }
  }

  function renderDetail() {
    const box = document.getElementById("detail"); box.replaceChildren();
    const d = data.drawings.find(x => x.id === selected);
    if (!d) { box.append(el("div", { class: "empty", text: "Select a drawing to preview it and correct its fields." })); shownState = null; return; }
    shownState = d.state;
    box.append(el("h1", { text: d.filename }));
    if (d.state !== "done") { box.append(el("div", { class: "reason", text: "Waiting to be processed..." })); return; }
    box.append(el("div", {}, chip(d.status, pretty(d.status))), el("div", { class: "reason", text: d.reason }));
    if (d.warnings) box.append(el("div", { class: "reason", text: "Notes: " + d.warnings }));
    const img = el("img", { src: "/api/drawings/" + d.id + "/preview.png", alt: "Drawing preview", class: "preview" });
    img.addEventListener("error", () => img.replaceWith(el("div", { class: "reason", text: "No preview available for this file." })));
    box.append(img, el("a", { href: "/api/drawings/" + d.id + "/pdf", target: "_blank", rel: "noopener", class: "muted", text: "Open original PDF" }));
    if (d.status === "FAILED") { box.append(el("div", { class: "reason", text: "No text could be read, so there are no fields to correct." })); return; }
    const form = el("form", {});
    for (const [k, label] of FIELDS) {
      const inp = el("input", { name: k, maxlength: 120 }); inp.value = d[k];
      if (d.edited_fields.includes(k)) inp.classList.add("was-edited");
      form.append(el("label", { text: label }), inp);
    }
    const status = el("span", { class: "muted" });
    form.append(el("div", { class: "actions" }, el("button", { class: "btn", type: "submit", text: "Save corrections" }), status));
    form.addEventListener("submit", async e => {
      e.preventDefault(); status.textContent = "Saving...";
      const body = {}; for (const [k] of FIELDS) body[k] = form.elements[k].value;
      try { await api("/api/drawings/" + d.id, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }); await refresh(true); }
      catch (err) { status.textContent = err.message; }
    });
    box.append(form);
  }

  function select(id) { selected = id; renderTable(); renderDetail(); }

  async function refresh(forceDetail) {
    data = await api("/api/batches/" + batchId);
    renderCounts(); renderTable();
    const d = data.drawings.find(x => x.id === selected);
    // Redraw the form only when it would not clobber typing: on save, or when the selected
    // drawing has just finished processing.
    if (forceDetail || (d && d.state !== shownState)) renderDetail();
    if (data.finished && timer) { clearInterval(timer); timer = null; }
  }
  await refresh(true);
  if (!data.finished) timer = setInterval(() => refresh(false).catch(() => {}), 2000);
}

const page = document.body.dataset.page;
({ login: loginPage, index: indexPage, batch: batchPage })[page]().catch(e => { if (e.message !== "auth") console.error(e); });
