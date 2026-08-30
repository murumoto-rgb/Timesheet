// Small, browser-only workspace features. No background task posts time.
// Accounting mutations go through one durable, company-scoped retry ticket.
const ProjectWorkspace = (() => {
  let ready = false, draftTimer, timerTick, timer = null, restoreCandidate = null;
  let categoryOverrides = {}, reconciliationSequence = 0, returnView = "log", returnProject = null;
  const inFlight = new Map(), modalStack = [];
  const uuid = () => {
    if (crypto.randomUUID) return crypto.randomUUID();
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
    const hex = [...bytes].map((value) => value.toString(16).padStart(2, "0")).join("");
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  };
  const el = (id) => document.getElementById(id);
  const escape = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const storageKey = (kind) => {
    if (!appCompanyKey) throw new Error("Company identity is unavailable. Reload before saving work.");
    return `timesheet:${appCompanyKey}:${kind}`;
  };
  function read(kind, fallback) {
    try { const raw = localStorage.getItem(storageKey(kind)); return raw === null ? fallback : JSON.parse(raw); }
    catch { return fallback; }
  }
  function store(kind, value) {
    try { localStorage.setItem(storageKey(kind), JSON.stringify(value)); }
    catch { throw new Error("This browser cannot preserve your work safely. Enable site storage before saving."); }
  }
  const isObject = (value) => !!value && typeof value === "object" && !Array.isArray(value);
  const validDate = (value) => typeof value === "string" && /^\d{4}-\d{2}-\d{2}$/.test(value)
    && Number.isFinite(Date.parse(value + "T00:00:00Z")) && new Date(value + "T00:00:00Z").toISOString().slice(0, 10) === value;
  function pendingTickets() {
    try {
      const raw = localStorage.getItem(storageKey("pending-writes"));
      const tickets = raw === null ? {} : JSON.parse(raw);
      if (!isObject(tickets) || Object.entries(tickets).some(([signature, ticket]) =>
        !isObject(ticket) || !isObject(ticket.body) || !/^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(ticket.id)
        || !["POST", "PUT", "DELETE"].includes(ticket.method) || !/^\/api\/timeactivity(?:\/[A-Za-z0-9_-]+)?$/.test(ticket.endpoint)
        || signature !== JSON.stringify([ticket.method, ticket.endpoint, stable(ticket.body)]))) throw new Error();
      return tickets;
    } catch {
      throw new Error("Saved retry information on this device is unreadable. Saving is paused to avoid duplicate records. Keep this browser data and review QuickBooks before recovering it.");
    }
  }
  function warning(message) {
    el("writeWarning").hidden = !message;
    el("writeWarning").textContent = message || "";
  }
  function stable(value) {
    if (Array.isArray(value)) return value.map(stable);
    if (value && typeof value === "object") return Object.fromEntries(Object.keys(value).sort().filter((k) => value[k] !== undefined && k !== "operation_id").map((k) => [k, stable(value[k])]));
    return value;
  }
  function locked(kind, callback, { ifAvailable = false } = {}) {
    if (!window.isSecureContext || !navigator.locks?.request) {
      throw new Error("Safe saving requires a browser with Web Locks on HTTPS or localhost. Open the secure app address in a current browser before saving; no request was sent.");
    }
    return navigator.locks.request(storageKey(kind) + ":lock", { mode: "exclusive", ifAvailable }, (lock) => {
      if (!lock) throw new Error("Another tab is processing this batch. Wait for it to finish or reopen its results; no new entries were started here.");
      return callback();
    });
  }
  async function write(method, endpoint, body) {
    if (method !== "POST" && body.sync_token == null) throw new Error("Reload this entry before changing it. Its QuickBooks version is missing.");
    const original = stable(body), explicitId = body.operation_id, companyKey = appCompanyKey;
    const signature = JSON.stringify([method, endpoint, original]);
    if (inFlight.has(signature)) return (await inFlight.get(signature)).clone();
    const task = (async () => {
      const { ticket, firstAttempt } = await locked("pending-writes", () => {
        const tickets = pendingTickets(), existing = tickets[signature];
        if (explicitId && existing && existing.id !== explicitId) throw new Error("Another saved request has these same fields. Review the original save IDs in Reconcile time before retrying.");
        const ticket = existing || { id: explicitId || uuid(), method, endpoint, body: original, createdAt: new Date().toISOString() };
        tickets[signature] = ticket;
        // All tabs read and persist atomically, before any outbound request.
        store("pending-writes", tickets);
        return { ticket, firstAttempt: !existing && !explicitId };
      });
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 45000);
      try {
        const response = await fetch(endpoint, { method, headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ...ticket.body, company_key: companyKey, operation_id: ticket.id }), signal: controller.signal });
        let data;
        try { data = await response.clone().json(); } catch { data = null; }
        const code = data?.detail?.code || "";
        const rejectedReceipt = response.status >= 400 && response.status < 500
          && data?.detail?.operationStatus === "failed" && data.detail.operationId === ticket.id;
        const firstValidationRejection = firstAttempt && [400, 422].includes(response.status)
          && !/UNCERTAIN|PENDING|IN_PROGRESS|BUSY|UNKNOWN|COMPANY|OPERATION_MISMATCH/.test(code);
        const confirmed = response.ok && data && (data.Id || data.deleted) && data.operationId === ticket.id;
        if (confirmed || rejectedReceipt || firstValidationRejection) {
          await locked("pending-writes", () => {
            const current = pendingTickets();
            // A delayed callback must never erase another tab's newer ticket.
            if (current[signature]?.id === ticket.id) { delete current[signature]; store("pending-writes", current); }
          });
        }
        if (response.ok && !confirmed) throw new Error("QuickBooks returned an unclear save result. Use Reconcile time before retrying; the original save ID has been kept.");
        if (data?.appWarning) warning(data.appWarning);
        else if (confirmed) pendingWarning();
        return response;
      } catch (error) {
        warning("A save result is uncertain. Your original save ID is kept on this device. Check Reconcile time, or retry the unchanged entry; do not create a replacement.");
        throw error;
      } finally { clearTimeout(timeout); }
    })();
    inFlight.set(signature, task);
    try { return (await task).clone(); } finally { inFlight.delete(signature); }
  }
  function pendingWarning() {
    try {
      const count = Object.keys(pendingTickets()).length;
      warning(count ? `${count} save ${count === 1 ? "result needs" : "results need"} review. Open Tools & settings → Reconcile time. Nothing retries automatically.` : "");
    } catch (error) { warning(error.message); }
  }

  function focusables(node) {
    return [...node.querySelectorAll('button:not([disabled]),a[href],input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex="0"]')]
      .filter((item) => !item.hidden && item.getClientRects().length);
  }
  function openModal(node, first) {
    if (modalStack.some((item) => item.node === node)) return;
    const previous = document.activeElement;
    const inert = [...document.body.children].filter((item) => item !== node && !item.contains(node) && !["SCRIPT", "STYLE", "LINK"].includes(item.tagName)).map((item) => [item, item.inert]);
    inert.forEach(([item]) => { item.inert = true; });
    modalStack.push({ node, previous, inert });
    (first || focusables(node)[0] || node).focus();
  }
  function closeModal(node) {
    const index = modalStack.findIndex((item) => item.node === node);
    if (index < 0) return;
    const [state] = modalStack.splice(index, 1);
    state.inert.forEach(([item, old]) => { item.inert = old; });
    if (state.previous?.isConnected && !state.previous.closest("[hidden]")) state.previous.focus();
  }
  document.addEventListener("keydown", (event) => {
    const active = modalStack.at(-1); if (!active) return;
    if (event.key === "Escape") {
      event.preventDefault();
      if (active.node.id === "confirmDialog") finishConfirm(false);
      if (active.node.id === "picker") closePicker();
      if (active.node.id === "cellChooser") closeCellChooser();
    }
    if (event.key !== "Tab") return;
    const nodes = focusables(active.node), first = nodes[0], last = nodes.at(-1);
    if (!first) { event.preventDefault(); return; }
    if (event.shiftKey && (document.activeElement === first || !active.node.contains(document.activeElement))) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && (document.activeElement === last || !active.node.contains(document.activeElement))) { event.preventDefault(); first.focus(); }
  }, true);

  function snapshotDraft() {
    const who = selectedWho();
    return { schema: 1, companyKey: appCompanyKey, savedAt: new Date().toISOString(),
      project: selProject, who: who ? { id: who.value, type: who.dataset.type } : null,
      item: el("item").value, date: el("date").value, hours: el("durh").value, notes: el("desc").value,
      billable: el("billable").checked, multi: el("multiDay").checked,
      through: el("dateEnd").value, weekends: el("includeWeekends").checked,
      editing: editingId, before: editingId ? editingBefore : null, timer };
  }
  function draftSignature() {
    const { savedAt, ...draft } = snapshotDraft();
    return JSON.stringify(stable(draft));
  }
  const draftMatches = (signature) => signature === draftSignature();
  function saveDraft() {
    if (!ready || restoreCandidate || el("logForm").classList.contains("billed")) return;
    try {
      const draft = snapshotDraft();
      store("draft", draft.hours || draft.notes || draft.project || timer ? draft : null);
      el("draftStatus").textContent = "Draft saved on this device · review before posting to QuickBooks.";
    } catch (error) { el("draftStatus").textContent = error.message; }
  }
  function scheduleDraft() { if (!ready) return; clearTimeout(draftTimer); draftTimer = setTimeout(saveDraft, 180); }
  function clearDraft() {
    if (!ready) return;
    clearTimeout(draftTimer); restoreCandidate = null; timer = null; updateTimer();
    try { store("draft", null); } catch (error) { warning(error.message); }
    el("draftNotice").hidden = true;
    el("draftStatus").textContent = "No saved draft · nothing posts automatically.";
  }
  function restoreDraft(draft) {
    if (!draft || draft.companyKey !== appCompanyKey) return;
    restoreCandidate = null;
    if (draft.editing && draft.before) startEdit(draft.before); else { cancelEdit(); showView("log"); }
    if (draft.project) setProject(draft.project, false, false);
    const option = [...el("employee").options].find((o) => o.value === draft.who?.id && o.dataset.type === draft.who?.type);
    if (!option || ![...el("item").options].some((o) => o.value === draft.item)) {
      warning("This draft uses a person or service no longer in the current lists. Review and reselect them before saving.");
    }
    if (option) option.selected = true; else el("employee").selectedIndex = -1;
    el("item").value = draft.item;
    el("date").value = draft.date; el("durh").value = draft.hours; el("desc").value = draft.notes;
    el("billable").checked = draft.billable;
    el("multiDay").checked = !draft.editing && draft.multi;
    el("multiDay").dispatchEvent(new Event("change"));
    el("dateEnd").value = draft.through || ""; el("includeWeekends").checked = !!draft.weekends;
    timer = draft.timer || null;
    el("draftNotice").hidden = true; updateTimer(); saveDraft();
  }
  function updateTimer() {
    el("timerStart").hidden = !!timer;
    el("timerStop").hidden = !timer;
    const elapsed = timer ? Math.max(0, Math.floor((Date.now() - timer.startedAt) / 1000)) : 0;
    el("timerReadout").textContent = timer ? `${Math.floor(elapsed / 3600)}:${String(Math.floor(elapsed / 60) % 60).padStart(2, "0")}:${String(elapsed % 60).padStart(2, "0")} · not posted` : "";
    el("timerStart").disabled = !!editingId || el("logForm").classList.contains("billed");
  }
  async function startTimer() {
    if (editingId || el("logForm").classList.contains("billed")) return;
    timer = { startedAt: Date.now(), date: el("date").value };
    saveDraft(); updateTimer();
  }
  async function stopTimer() {
    if (!timer) return;
    const minutes = Math.max(1, Math.round((Date.now() - timer.startedAt) / 60000));
    if (minutes > 1440) { warning("The timer exceeds 24 hours. It has been stopped; enter the correct duration manually."); timer = null; updateTimer(); saveDraft(); return; }
    if (el("durh").value && !await askConfirm(`Replace the entered duration with ${fmtMins(minutes)} from the timer?`, "Use timer", "Review timer")) return;
    el("durh").value = minsToHours(minutes); timer = null; updateTimer(); saveDraft();
    el("msg").className = "msg good"; el("msg").textContent = "Timer stopped. Review the project, date, duration and notes, then Log time.";
  }

  function isExpense(entry, fallback) {
    return Object.prototype.hasOwnProperty.call(categoryOverrides, entry.itemId) ? categoryOverrides[entry.itemId] : fallback.test(entry.service || "");
  }
  function editCategories() {
    let box = el("serviceCategories"); if (box) { box.remove(); return; }
    box = document.createElement("div"); box.id = "serviceCategories";
    box.innerHTML = '<h2>Expense quantities</h2><p class="sub">Checked services are treated as mileage/expense quantities. Overrides stay on this device for this company; QuickBooks items are unchanged.</p>'
      + [...el("item").options].map((o) => `<label class="optck"><input type="checkbox" data-service="${escape(o.value)}" ${isExpense({ itemId: o.value, service: o.textContent }, EXPENSE_RE) ? "checked" : ""}>${escape(o.textContent)}</label>`).join("")
      + '<button type="button" class="ie-btn" id="saveServiceCategories">Save categories</button>';
    el("workspaceMenu").appendChild(box);
    el("saveServiceCategories").onclick = () => {
      try { categoryOverrides = Object.fromEntries([...box.querySelectorAll("input")].map((input) => [input.dataset.service, input.checked])); store("service-categories", categoryOverrides); box.remove(); rerenderActive(); showToast("Service categories saved on this device.", "", null); }
      catch (error) { warning(error.message); }
    };
  }

  function context() {
    const box = el("projectContext"); box.hidden = !rep.projectId;
    el("reportScope").classList.toggle("project-scope", !!rep.projectId);
    if (el("projectDatePresets")) el("projectDatePresets").hidden = !rep.projectId;
    if (!rep.projectId) { box.innerHTML = ""; return; }
    const project = projectCatalog().find((p) => p.id === rep.projectId);
    const parent = project?.parentId ? projectNameFor(project.parentId) : "";
    box.innerHTML = projectBackHtml() + `<h2 id="projectTitle">${escape(rep.projectName)}</h2>`
      + `<p class="sub">${parent ? escape(parent) + " · " : ""}Project / client #${escape(rep.projectId)} · Child projects are separate</p>`
      + '<nav class="project-jumps" aria-label="Project sections"><button type="button" data-jump="projectSummary">Overview</button><button type="button" data-jump="projectNotes">Work &amp; notes</button><button type="button" data-jump="projectPlanning">Plan &amp; billing</button></nav>';
    el("repDrillBack").onclick = closeProjectDrill;
    box.querySelectorAll("[data-jump]").forEach((button) => { button.onclick = () => { const target = el(button.dataset.jump); if (target) { target.scrollIntoView({ behavior: "smooth", block: "start" }); target.focus({ preventScroll: true }); } }; });
  }
  function projectPanels() {
    return '<div class="card" id="projectPlanning" tabindex="-1"><h2>Plan &amp; fees</h2><p class="project-note">A planning budget for the selected dates. Saved only in this browser, separate from your accounting records.</p>'
      + '<div id="budgetSummary"></div><div class="row"><div><label for="budgetHours">Planned labor hours</label><input type="number" id="budgetHours" min="0" step="0.25" placeholder="e.g. 40"></div><div><label for="budgetFee">Agreed fee (USD)</label><input type="number" id="budgetFee" min="0" step="0.01" placeholder="Optional"></div></div>'
      + '<button type="button" class="secondary" id="saveBudget">Save budget for these dates</button><p class="sub" id="budgetStatus" role="status"></p></div>'
      + '<div class="card" id="projectBilling"><h2>Invoices &amp; payments</h2><p class="project-note">Only explicit project and invoice relationships are included. Payments are allocated by invoice line, never guessed from company totals.</p><button type="button" class="secondary" id="loadProjectBilling">Load accounting history</button><div id="projectBillingResults" aria-live="polite"></div></div>'
      + '<div class="card export-tools"><h2>Take this report with you</h2><p class="project-note">Exports include every project entry in the selected dates and expense scope, regardless of the notes search. Files may contain private client details.</p><button type="button" class="ie-btn" id="projectCSV">Download CSV</button><button type="button" class="ie-btn" id="projectPrint">Print / save PDF</button></div>';
  }
  function bindPanels(entries, win) {
    const id = rep.projectId, range = { start: ymd(win.start), end: ymd(win.end) };
    const budgets = read("budgets", {}), budget = budgets[id];
    const labor = entries.filter((entry) => !isExpense(entry, EXPENSE_RE)).reduce((sum, entry) => sum + mins(entry), 0);
    const sameRange = budget?.start === range.start && budget?.end === range.end;
    const showBudget = () => {
      const box = el("budgetSummary");
      if (!budget) { box.innerHTML = `<p class="sub">Current labor: ${fmtMins(labor)} · ${escape(range.start)} – ${escape(range.end)}. Expense quantities excluded.</p>`; return; }
      const remaining = budget.hours == null ? null : Math.round(budget.hours * 60) - labor;
      box.innerHTML = `<p><strong>${budget.hours == null ? "No hours budget" : `${escape(budget.hours)} planned hours`}</strong>${budget.fee != null ? ` · ${projectMoney(budget.fee)} agreed fee` : ""}</p><p class="sub">${escape(budget.start)} – ${escape(budget.end)} · Fee is a planning reference, not revenue or profit.</p>`
        + (sameRange ? `<p>${fmtMins(labor)} labor used${remaining == null ? "" : ` · <strong>${fmtMins(Math.abs(remaining))} ${remaining < 0 ? "over budget" : "remaining"}</strong>`}</p>`
          : '<p class="project-alert">This budget has different dates. No comparison is made against a partial period.</p><button type="button" class="ie-btn" id="openBudgetRange">Show budget period</button>');
      if (el("openBudgetRange")) el("openBudgetRange").onclick = () => { setProjectDates(new Date(budget.start + "T00:00"), new Date(budget.end + "T00:00")); renderReport(); };
    };
    el("budgetHours").value = sameRange && budget.hours != null ? budget.hours : "";
    el("budgetFee").value = sameRange && budget.fee != null ? budget.fee : "";
    showBudget();
    el("saveBudget").onclick = async () => {
      const hoursText = el("budgetHours").value, feeText = el("budgetFee").value;
      const hours = hoursText === "" ? null : Number(hoursText), fee = feeText === "" ? null : Number(feeText);
      if ((hours === null && fee === null) || (hours !== null && (!Number.isFinite(hours) || hours < 0 || hours > 1000000)) || (fee !== null && (!Number.isFinite(fee) || fee < 0 || fee > 1000000000))) { el("budgetStatus").textContent = "Enter planned hours, an agreed fee, or both, using non-negative amounts."; return; }
      if (budget && !sameRange && !await askConfirm(`Replace the budget for ${budget.start} – ${budget.end} with a budget for ${range.start} – ${range.end}?`, "Replace budget", "Review budget dates")) return;
      try { const current = read("budgets", {}); current[id] = { ...range, hours, fee, currency: "USD" }; store("budgets", current); bindPanels(entries, win); el("budgetStatus").textContent = "Budget saved on this device. QuickBooks unchanged."; }
      catch (error) { el("budgetStatus").textContent = error.message; }
    };
    el("loadProjectBilling").onclick = () => loadBilling(id, range);
    el("projectCSV").onclick = () => exportCSV(entries, win);
    el("projectPrint").onclick = () => printProject(entries, win);
  }
  async function loadBilling(id, range) {
    const button = el("loadProjectBilling"), box = el("projectBillingResults");
    button.disabled = true; box.textContent = "Loading accounting history…";
    try {
      const data = await getJSON(`/api/project-financials?project_id=${encodeURIComponent(id)}&start=${range.start}&end=${range.end}`);
      if (!box.isConnected || rep.projectId !== id || ymd(drillWindow().start) !== range.start || ymd(drillWindow().end) !== range.end) return;
      if (!Array.isArray(data.invoices) || !Array.isArray(data.payments)) throw new Error("Accounting history was not returned. Retry when the server is available.");
      const money = (amount, currency) => amount == null || !Number.isFinite(Number(amount)) ? "Unknown amount" : `${escape(currency === "home" ? "Home currency" : currency || "Unspecified currency")} ${Number(amount).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
      box.innerHTML = `<p class="sub">${escape(data.start)} – ${escape(data.end)} · ${escape(data.scope || "Exact project links only")}</p>`
        + (data.mixedCurrency ? '<p class="project-alert">Multiple currencies: shown separately, never added together.</p>' : "")
        + '<h3>Invoices</h3>' + (data.invoices.length ? data.invoices.map((invoice) => `<div class="billing-row"><strong>Invoice ${escape(invoice.docNumber || invoice.number || invoice.id)}</strong><span>${escape(invoice.date)} · ${money(invoice.amount, invoice.currency)}</span><span>Open balance: ${money(invoice.balance, invoice.currency)} · ID ${escape(invoice.id)}</span></div>`).join("") : '<p class="sub">No explicitly linked invoices in this period.</p>')
        + '<h3>Allocated payments</h3>' + (data.payments.length ? data.payments.map((payment) => `<div class="billing-row"><strong>${escape(payment.kind || "Payment")} ${escape(payment.id)}</strong><span>${escape(payment.date)} · ${money(payment.amount, payment.currency)}</span><span>Invoice ${escape((payment.invoiceIds || []).join(", ") || "not supplied")}</span></div>`).join("") : '<p class="sub">No unambiguous invoice-linked payment allocations in this period.</p>')
        + (data.skippedAmbiguousPaymentLines || data.skippedInvalidPaymentLines ? `<p class="project-alert">Excluded payment lines: ${Number(data.skippedAmbiguousPaymentLines || 0)} with ambiguous invoice links; ${Number(data.skippedInvalidPaymentLines || 0)} with invalid amounts. These amounts are not allocated to this project.</p>` : "")
        + (data.caveats || []).map((text) => `<p class="project-note">${escape(text)}</p>`).join("");
    } catch (error) { if (box.isConnected) box.textContent = `Could not load accounting history: ${error.message}`; }
    finally { if (button.isConnected) button.disabled = false; }
  }
  function download(name, content, type) {
    const url = URL.createObjectURL(new Blob([content], { type })), link = document.createElement("a");
    link.href = url; link.download = name; document.body.appendChild(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  const csvCell = (value) => {
    let text = String(value ?? "");
    if (/^[\s]*[=+\-@\t\r]/.test(text)) text = "'" + text; // prevent spreadsheet formula execution
    return '"' + text.replace(/"/g, '""') + '"';
  };
  function exportCSV(entries, win) {
    const rows = [["Project", "Project ID", "Period start", "Period end", "Expense scope", "Entry ID", "Date", "Person", "Person type", "Person ID", "Service", "Hours", "Minutes", "Status", "Recorded rate", "Rate known", "Recorded time value (not invoice total)", "Notes"]];
    entries.forEach((entry) => rows.push([rep.projectName, rep.projectId, ymd(win.start), ymd(win.end), opts.hideExpenses ? "Expenses hidden" : "All services", entry.id, entry.date, entry.employee, entry.vendorId ? "Vendor" : "Employee", entry.vendorId || entry.employeeId || "", entry.service, entry.hours, entry.minutes, entry.billableStatus, hasRecordedRate(entry) ? entry.hourlyRate : "", hasRecordedRate(entry) ? "yes" : "no", entry.billable && hasRecordedRate(entry) ? dollarsOf(entry).toFixed(2) : entry.billable ? "unknown" : "0.00", entry.description]));
    download(`project-${rep.projectId}-${ymd(win.start)}-${ymd(win.end)}.csv`, "\uFEFF" + rows.map((row) => row.map(csvCell).join(",")).join("\r\n"), "text/csv;charset=utf-8");
  }
  function printProject(entries, win) {
    el("projectPrintReport")?.remove();
    const box = document.createElement("section"); box.id = "projectPrintReport";
    const metrics = projectMetrics(entries);
    box.innerHTML = `<h1>${escape(rep.projectName)}</h1><p>${escape(ymd(win.start))} – ${escape(ymd(win.end))} · Project ID ${escape(rep.projectId)}</p><p>Total ${fmtMins(metrics.total)} · Billable ${fmtMins(metrics.billable)} · To invoice ${fmtMins(metrics.unbilled)} · Billed ${fmtMins(metrics.billed)}</p><p>${opts.hideExpenses ? "Mileage/expense services excluded." : "All services included; expense quantities can affect time totals."} Recorded-rate values are not invoice totals, cash or profit. ${metrics.missingRates.length} billable entries have unknown rates.</p><table><thead><tr><th>Date / ID</th><th>Person / service</th><th>Time / status</th><th>Notes</th></tr></thead><tbody>`
      + entries.map((entry) => `<tr><td>${escape(entry.date)}<br>${escape(entry.id)}</td><td>${escape(entry.employee)}<br>${escape(entry.service)}</td><td>${fmtMins(mins(entry))}<br>${escape(entry.billableStatus)}</td><td>${escape(entry.description)}</td></tr>`).join("") + '</tbody></table>';
    document.body.appendChild(box); window.print();
  }

  function openReconciliation() {
    returnView = document.body.dataset.view || "log";
    if (returnView === "reconcile") returnView = "log";
    returnProject = rep.projectId ? { id: rep.projectId, name: rep.projectName, source: rep.drillSource, start: ymd(drillWindow().start), end: ymd(drillWindow().end) } : null;
    const win = rep.projectId ? drillWindow() : periodRange();
    showView("reconcile"); el("workspaceMenu").hidden = true; el("workspaceMenuButton").setAttribute("aria-expanded", "false");
    el("reconcileStart").value = ymd(win.start); el("reconcileEnd").value = ymd(win.end);
    el("reconcileResults").innerHTML = '<p class="sub">Choose dates and run a check. No records will be changed.</p>';
  }
  async function reconcile() {
    const start = el("reconcileStart").value, end = el("reconcileEnd").value, box = el("reconcileResults");
    if (!validDate(start) || !validDate(end) || end < start) { box.textContent = "Choose valid dates with the end on or after the start."; return; }
    const sequence = ++reconciliationSequence;
    // First read is the app snapshot; the server then performs a fresh independent read.
    const displayed = repFetched.filter((entry) => entry.date >= start && entry.date <= end);
    const sameRange = !repLoading && !repLoadError && repLoadedRange?.start === start && repLoadedRange?.end === end;
    ["reconcileRun", "reconcileStart", "reconcileEnd"].forEach((id) => { el(id).disabled = true; }); box.textContent = "Checking time and save receipts…";
    try {
      const snapshot = sameRange ? displayed : await getJSON(`/api/timeactivities?start=${start}&end=${end}`);
      const data = await getJSON(`/api/reconciliation?start=${start}&end=${end}`);
      if (sequence !== reconciliationSequence) return;
      if (!Array.isArray(snapshot) || !data.freshQbo || !Array.isArray(data.flags)) throw new Error("The reconciliation response is unavailable.");
      const minutes = snapshot.reduce((sum, entry) => sum + mins(entry), 0), matches = minutes === data.freshQbo.minutes && snapshot.length === data.freshQbo.entries;
      box.innerHTML = `<p class="${matches ? "good-text" : "project-alert"}"><strong>${matches ? "Time totals match the fresh read" : "The displayed snapshot differs from the fresh read"}</strong></p><p>App snapshot: ${snapshot.length} entries · ${fmtMins(minutes)}<br>Fresh QuickBooks read: ${data.freshQbo.entries} entries · ${fmtMins(data.freshQbo.minutes)}</p><p class="sub">All people and services, including expense quantities. This is a second entity read, not an independent accounting report or proof that every entry is correct.</p>`
        + `<h3>Review flags (${data.flags.length})</h3>` + (data.flags.length ? data.flags.map((flag) => `<p class="project-alert">${escape(flag.message || flag.detail || flag.code)}${flag.entryId ? ` · entry ${escape(flag.entryId)}` : ""}</p>`).join("") : '<p>No duplicate or consistency flags in the checked scope.</p>')
        + `<h3>Save receipts</h3><p>${Number(data.journal?.completed || 0)} completed operations in the period · ${Number(data.auditEvents || 0)} retained activity events.</p>`
        + (data.journal?.unresolved || []).map((operation) => `<p class="project-alert">Unresolved save ${escape(operation.operationId || operation.id || "")} · ${escape(operation.state || "Review required")}</p>`).join("")
        + (data.caveats || []).map((text) => `<p class="project-note">${escape(text)}</p>`).join("")
        + '<button type="button" class="ie-btn" id="downloadReconciliation">Download check</button>';
      el("downloadReconciliation").onclick = () => download(`reconciliation-${start}-${end}.json`, JSON.stringify({ checkedAt: new Date().toISOString(), appSnapshot: { entries: snapshot.length, minutes }, ...data }, null, 2), "application/json");
      const tickets = Object.values(pendingTickets());
      if (tickets.length) {
        const pending = document.createElement("div"); pending.innerHTML = `<h3>Uncertain saves on this device (${tickets.length})</h3><p class="sub">Review each original request before retrying. The same save ID is reused; successful records are not recreated.</p>`;
        tickets.forEach((ticket) => {
          const row = document.createElement("div"); row.className = "billing-row";
          const text = document.createElement("span"); text.textContent = `${ticket.method} · ${ticket.body.txn_date || ticket.endpoint.split("/").pop()} · ${ticket.body.description || ""} · ${ticket.id}`;
          const retry = document.createElement("button"); retry.type = "button"; retry.className = "ie-btn"; retry.textContent = "Review original save";
          retry.onclick = async () => { if (!await askConfirm(`Retry the original ${ticket.method} request with save ID ${ticket.id}? Its original fields and QuickBooks version are preserved.`, "Retry original", "Review uncertain save")) return;
            retry.disabled = true; try { const response = await write(ticket.method, ticket.endpoint, { ...ticket.body, operation_id: ticket.id }); if (!response.ok) throw await apiError(response); text.textContent = "Original save confirmed. Refresh your time views."; retry.remove(); refreshData(); } catch (error) { text.textContent = error.message; retry.disabled = false; } };
          row.append(text, retry); pending.appendChild(row);
        });
        box.appendChild(pending);
      }
    } catch (error) { if (sequence === reconciliationSequence) box.textContent = `Could not reconcile: ${error.message}`; }
    finally { if (sequence === reconciliationSequence) ["reconcileRun", "reconcileStart", "reconcileEnd"].forEach((id) => { el(id).disabled = false; }); }
  }
  function exportLocal() {
    try { download("timesheet-plans-and-drafts.json", JSON.stringify({ schema: 1, companyKey: appCompanyKey, savedAt: new Date().toISOString(), budgets: read("budgets", {}), draft: read("draft", null), serviceCategories: categoryOverrides }, null, 2), "application/json"); }
    catch (error) { warning(error.message); }
  }
  async function importLocal(file) {
    try {
      if (!file || file.size > 2000000) throw new Error("Choose a plans-and-drafts JSON backup under 2 MB.");
      const data = JSON.parse(await file.text());
      if (data.schema !== 1 || data.companyKey !== appCompanyKey || !data.budgets || typeof data.budgets !== "object" || Array.isArray(data.budgets)) throw new Error("This backup is invalid or belongs to a different company.");
      for (const budget of Object.values(data.budgets)) if (!budget || (budget.hours == null && budget.fee == null) || (budget.hours != null && (!Number.isFinite(budget.hours) || budget.hours < 0 || budget.hours > 1000000)) || !validDate(budget.start) || !validDate(budget.end) || budget.end < budget.start || (budget.fee != null && (!Number.isFinite(budget.fee) || budget.fee < 0 || budget.fee > 1000000000)) || budget.currency !== "USD") throw new Error("A budget in this backup is invalid.");
      if (data.draft && (!isObject(data.draft) || data.draft.companyKey !== appCompanyKey || typeof data.draft.notes !== "string" || data.draft.notes.length > 100000 || !validDate(data.draft.date) || !["string", "number"].includes(typeof data.draft.hours) || (data.draft.timer && (!Number.isFinite(data.draft.timer.startedAt) || data.draft.timer.startedAt <= 0 || data.draft.timer.startedAt > Date.now())) || (data.draft.project && (typeof data.draft.project.id !== "string" || typeof data.draft.project.name !== "string")) || (data.draft.editing && (!isObject(data.draft.before) || data.draft.before.id !== data.draft.editing)))) throw new Error("The draft in this backup is invalid.");
      if (!isObject(data.serviceCategories) || Object.values(data.serviceCategories).some((value) => typeof value !== "boolean")) throw new Error("The service categories in this backup are invalid.");
      if (!await askConfirm("Restore these browser-only plans, categories and draft? Existing local values will be replaced. QuickBooks records and save IDs will not change.", "Restore local work", "Review backup")) return;
      const restored = { budgets: data.budgets, draft: data.draft || null, "service-categories": data.serviceCategories };
      const previous = Object.fromEntries(Object.keys(restored).map((kind) => [kind, localStorage.getItem(storageKey(kind))]));
      try { Object.entries(restored).forEach(([kind, value]) => store(kind, value)); }
      catch (error) {
        Object.entries(previous).forEach(([kind, value]) => { if (value === null) localStorage.removeItem(storageKey(kind)); else localStorage.setItem(storageKey(kind), value); });
        throw error;
      }
      location.reload();
    } catch (error) { warning(error.message); }
  }
  let batchBusy = false;
  const batchIdentity = (record) => record.batchId || JSON.stringify([record.companyKey, record.createdAt, record.jobs.map((job) => job.body.operation_id)]);
  function storedBatch() {
    try {
      const raw = localStorage.getItem(storageKey("batch"));
      const record = raw === null ? null : JSON.parse(raw);
      if (record === null) return null;
      if (!isObject(record) || record.companyKey !== appCompanyKey || !["multi", "bulk"].includes(record.kind)
          || !Array.isArray(record.jobs) || !record.jobs.length || record.jobs.some((job) =>
            !isObject(job) || !isObject(job.body) || !["POST", "PUT", "DELETE"].includes(job.method)
            || !/^\/api\/timeactivity(?:\/[A-Za-z0-9_-]+)?$/.test(job.endpoint)
            || !/^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(job.body.operation_id)
            || !["waiting", "sending", "unconfirmed", "done"].includes(job.state))) throw new Error();
      return record;
    } catch { throw new Error("Saved batch recovery information is unreadable. Keep this browser data and reconcile before starting another batch."); }
  }
  function showBatch(record) {
    const box = el(record.kind === "multi" ? "batchResults" : "bulkResults");
    const other = el(record.kind === "multi" ? "bulkResults" : "batchResults");
    other.hidden = true; other.innerHTML = "";
    box.hidden = false;
    const completed = record.jobs.filter((job) => job.state === "done").length;
    box.innerHTML = `<h2>${record.kind === "multi" ? "Multi-day entries" : "Selected-entry changes"}</h2><p>${completed} of ${record.jobs.length} confirmed. Successful entries will not be sent again.</p>`
      + record.jobs.map((job) => `<div class="billing-row"><strong>${escape(job.label)}</strong><span>${job.state === "done" ? "Confirmed" : job.state === "sending" ? "Saving…" : job.error ? escape(job.error) : "Waiting"}${job.resultId ? " · entry #" + escape(job.resultId) : ""}</span></div>`).join("")
      + (completed < record.jobs.length ? '<button type="button" class="secondary" id="retryFailedBatch">Retry unconfirmed entries only</button>' : "")
      + '<button type="button" class="secondary" id="dismissBatch">Close results</button>';
    el("submit").disabled = batchBusy || (record.kind === "multi" && completed < record.jobs.length);
    const retry = el("retryFailedBatch");
    if (retry) { retry.disabled = batchBusy; retry.onclick = async () => { if (await askConfirm(`Retry only ${record.jobs.length - completed} unconfirmed entries with their original save IDs and values? Review the outcomes above first.`, "Retry unconfirmed", "Review batch recovery")) await executeBatch(record); }; }
    el("dismissBatch").disabled = batchBusy;
    el("dismissBatch").onclick = async () => {
      if (completed < record.jobs.length && !await askConfirm("Close this batch? Confirmed records stay in QuickBooks. Uncertain save IDs remain available in Reconcile time. The form will be cleared to prevent accidental resubmission.", "Close and clear", "Keep confirmed entries")) return;
      try {
        await locked("batch", () => {
          const current = storedBatch();
          if (!current || batchIdentity(current) !== batchIdentity(record)) throw new Error("This batch changed in another tab. Reopen the current results before closing them.");
          store("batch", null); box.hidden = true;
          if (record.kind === "multi" && draftMatches(record.formSignature)) clearFormFields();
          el("submit").disabled = false;
        }, { ifAvailable: true });
      }
      catch (error) { warning(error.message); }
    };
  }
  async function executeBatch(record, starting = false) {
    if (batchBusy) return;
    batchBusy = true;
    try {
      await locked("batch", async () => {
        const current = storedBatch();
        if (starting) {
          if (current?.jobs.some((job) => job.state !== "done")) throw new Error("Review or close the previous batch before starting another one. Its original save IDs have been preserved.");
        } else {
          if (!current || batchIdentity(current) !== batchIdentity(record)) throw new Error("This batch was closed or replaced in another tab. Reopen the current results; no old entries were resent.");
          record = current; // never replay a stale tab's unconfirmed snapshot
        }
        let persisted = false;
        try {
          store("batch", record); showBatch(record);
          persisted = true;
          for (const job of record.jobs) {
            if (job.state === "done") continue;
            job.state = "sending"; store("batch", record); showBatch(record);
            try {
              const response = await write(job.method, job.endpoint, job.body);
              if (!response.ok) throw await apiError(response, "The entry was not confirmed.");
              const data = await response.json(); job.state = "done"; job.resultId = data.Id || data.deleted;
              job.error = data.appWarning || "";
            } catch (error) { job.state = "unconfirmed"; job.error = error.message || "Uncertain outcome; review before retrying."; }
            store("batch", record); showBatch(record);
          }
        } catch (error) { warning(error.message); }
        finally {
          batchBusy = false;
          if (persisted) {
            const completed = record.jobs.filter((job) => job.state === "done").length;
            if (record.kind === "multi") {
              const unchanged = draftMatches(record.formSignature);
              if (completed === record.jobs.length && unchanged) clearFormFields();
              else saveDraft();
              el("msg").className = completed === record.jobs.length ? "msg good" : "msg bad";
              el("msg").textContent = `Logged ${completed} ${completed === 1 ? "entry" : "entries"} across ${record.jobs.length} days` + (completed < record.jobs.length ? ". Review and retry only the unconfirmed entries below." : unchanged ? "" : ". Your newer draft has been kept.");
            } else { bulkSel.clear(); rep.selecting = false; }
            showBatch(record); refreshData();
          }
        }
      }, { ifAvailable: true });
    } catch (error) {
      batchBusy = false;
      warning(error.message);
      try { const current = storedBatch(); if (current) showBatch(current); else el("submit").disabled = false; }
      catch { el("submit").disabled = true; }
    } finally {
      batchBusy = false;
    }
  }
  async function runBatch(kind, jobs) {
    const record = { batchId: uuid(), kind, companyKey: appCompanyKey, createdAt: new Date().toISOString(), formSignature: draftSignature(), jobs: jobs.map((job) => ({ ...job, body: { ...job.body, operation_id: uuid() }, state: "waiting" })) };
    if (kind === "multi") saveDraft();
    await executeBatch(record, true);
  }
  function init() {
    ready = true; categoryOverrides = read("service-categories", {});
    if (!isObject(categoryOverrides) || Object.values(categoryOverrides).some((value) => typeof value !== "boolean")) categoryOverrides = {};
    el("workspaceMenuButton").hidden = false;
    el("workspaceMenuButton").onclick = () => { el("workspaceMenu").hidden = !el("workspaceMenu").hidden; el("workspaceMenuButton").setAttribute("aria-expanded", String(!el("workspaceMenu").hidden)); };
    el("openReconciliation").onclick = openReconciliation;
    el("reconcileBack").onclick = () => {
      if (returnProject) openProjectDeepDive(returnProject.id, returnProject.name, { source: returnProject.source, start: new Date(returnProject.start + "T00:00"), end: new Date(returnProject.end + "T00:00") });
      else showView(returnView);
    };
    el("reconcileRun").onclick = reconcile;
    el("refreshRecent").onclick = loadRecent;
    el("workspaceReminders").onclick = enableReminders;
    el("manageExpenseServices").onclick = editCategories;
    el("exportWorkspace").onclick = exportLocal;
    el("importWorkspace").onclick = () => el("workspaceImportFile").click();
    el("workspaceImportFile").onchange = () => importLocal(el("workspaceImportFile").files[0]);
    el("timerStart").onclick = startTimer; el("timerStop").onclick = stopTimer;
    clearInterval(timerTick); timerTick = setInterval(updateTimer, 1000);
    const draft = read("draft", null);
    if (draft?.companyKey === appCompanyKey && (draft.hours || draft.notes || draft.project || draft.timer)) {
      restoreCandidate = draft; el("draftNotice").hidden = false;
      el("draftNotice").innerHTML = `<h2>Unfinished work</h2><p>${escape(draft.project?.name || "No project selected")} · ${escape(draft.date || "")} · ${draft.editing ? "Existing entry edit" : "New entry"}${draft.timer ? " · timer was running" : ""}</p><p class="sub">Saved on this device; never submitted automatically. If a save was interrupted, check its result before creating a replacement.</p><button type="button" class="ie-btn" id="restoreDraft">Restore draft</button><button type="button" class="ie-btn" id="discardDraft">Discard draft</button>`;
      el("restoreDraft").onclick = () => restoreDraft(draft);
      el("discardDraft").onclick = () => { timer = null; clearDraft(); updateTimer(); };
    }
    el("logForm").addEventListener("input", scheduleDraft); el("logForm").addEventListener("change", scheduleDraft);
    window.addEventListener("beforeunload", saveDraft);
    updateTimer(); pendingWarning();
    try { const batch = storedBatch(); if (batch) showBatch(batch); }
    catch (error) { warning(error.message); el("submit").disabled = true; }
  }
  return { init, write, warning, openModal, closeModal, scheduleDraft, saveDraft, clearDraft, updateTimer, draftSignature, draftMatches,
    isExpense, context, projectPanels, bindPanels, openReconciliation, read, store, download, runBatch,
    timerRunning: () => !!timer };
})();
