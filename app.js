/* ============================================================
   app.js — Longbridge Scanner (النسخة النهائية)
   ============================================================ */
const API_BASE = window.location.origin;

let currentScan = null;
let scanCards = [];
let scanTimer = null;
let monitorTimers = new Map();
let pricePollTimer = null;
let lastAlertedState = {};
let deletedSymbols = new Set();

const VALID_COLORS = ["green", "red"];

/* ===== Storage ===== */
function saveDeleted() {
  try { localStorage.setItem("scanner_deleted", JSON.stringify([...deletedSymbols])); } catch (e) {}
}
function loadDeleted() {
  try {
    const r = localStorage.getItem("scanner_deleted");
    if (r) deletedSymbols = new Set(JSON.parse(r));
  } catch (e) {}
}
function saveCards() {
  try {
    // ✅ نحفظ فقط الأخضر والأحمر
    const filtered = scanCards.filter(c => VALID_COLORS.includes(c.card?.color));
    localStorage.setItem("scanner_cards", JSON.stringify(filtered));
  } catch (e) {}
}
function loadCards() {
  try {
    const r = localStorage.getItem("scanner_cards");
    if (!r) return [];
    const arr = JSON.parse(r);
    // ✅ نحذف أي بطاقة رمادية محفوظة سابقاً
    return arr.filter(c => VALID_COLORS.includes(c.card?.color));
  } catch (e) { return []; }
}

/* ===== DOM ===== */
const cardsArea = document.getElementById("cardsArea");
const emptyState = document.getElementById("emptyState");
const themeBtn = document.getElementById("themeBtn");
const themeIcon = document.getElementById("themeIcon");
const marketStatus = document.getElementById("marketStatus");
const connectionStatus = document.getElementById("connectionStatus");
const scanBtn = document.getElementById("scanBtn");
const scanCancelBtn = document.getElementById("scanCancelBtn");
const progressWrap = document.getElementById("progressWrap");
const progressFill = document.getElementById("progressFill");
const progressText = document.getElementById("progressText");
const progressFound = document.getElementById("progressFound");
const totalSymbols = document.getElementById("totalSymbols");

/* ===== Theme ===== */
function applyTheme(light) {
  document.body.classList.toggle("light", light);
  themeIcon.textContent = light ? "☀️" : "🌙";
  localStorage.setItem("theme", light ? "light" : "dark");
}
themeBtn.addEventListener("click", () => applyTheme(!document.body.classList.contains("light")));
applyTheme(localStorage.getItem("theme") === "light");

/* ===== Market ===== */
function updateMarketStatus() {
  const now = new Date();
  const nyH = (now.getUTCHours() - 4 + 24) % 24;
  const total = nyH * 60 + now.getUTCMinutes();
  let label = "", cls = "status";
  if (total >= 570 && total < 960)       { label = "السوق مفتوح";    cls += " online"; }
  else if (total >= 240 && total < 570)  { label = "قبل الافتتاح";   cls += " pre"; }
  else if (total >= 960 && total < 1200) { label = "بعد الإغلاق";    cls += " after"; }
  else                                    { label = "التداول الليلي"; cls += " overnight"; }
  marketStatus.className = cls;
  marketStatus.querySelector(".label").textContent = label;
}
updateMarketStatus();
setInterval(updateMarketStatus, 60000);

/* ===== Connection ===== */
function setConnection(online) {
  connectionStatus.className = "status " + (online ? "online" : "offline");
  connectionStatus.querySelector(".label").textContent = online ? "متصل" : "غير متصل";
}
async function checkBackendStatus() {
  try {
    const r = await fetch(`${API_BASE}/api/status`);
    const d = await r.json();
    setConnection(!!d.connected);
  } catch (e) { setConnection(false); }
}

/* ===== Sound ===== */
function playAlertSound(type) {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const now = ctx.currentTime;
    const notes = type === "green" ? [523.25, 783.99] : [783.99, 523.25];
    notes.forEach((freq, i) => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = "sine"; osc.frequency.value = freq;
      osc.connect(gain); gain.connect(ctx.destination);
      const start = now + i * 0.2;
      gain.gain.setValueAtTime(0.3, start);
      gain.gain.exponentialRampToValueAtTime(0.01, start + 0.25);
      osc.start(start); osc.stop(start + 0.3);
    });
  } catch (e) {}
}
function checkSound(symbol, color) {
  const prev = lastAlertedState[symbol];
  if ((color === "green" || color === "red") && prev !== color) playAlertSound(color);
  lastAlertedState[symbol] = color;
}

/* ===== Labels ===== */
function stageLabel(stage) {
  if (stage === "daily_break_weekly") return "اليومي اخترق الأسبوع";
  if (stage === "4h_break_daily")     return "4H اخترق اليوم";
  return "";
}
function monitorStatusLabel(s) {
  if (s === "target_hit") return { label: "✅ هدف", cls: "ms-target" };
  if (s === "stop_hit")   return { label: "❌ وقف",  cls: "ms-stop" };
  if (s === "expired")    return { label: "⏰ منتهي", cls: "ms-expired" };
  if (s === "active")     return { label: "🟢 مراقبة", cls: "ms-active" };
  return { label: "", cls: "" };
}

/* ===== OI Block ===== */
function buildOIBlockStatic(title, kind, color) {
  let rows = "";
  for (let i = 0; i < 5; i++) {
    rows += `<div class="oi-row">
      <div class="oi-bar-wrap"><div class="oi-bar" data-oi-bar="${kind}-${i}" style="width:0%;background:${color};"></div></div>
      <div class="oi-strike" data-oi-strike="${kind}-${i}">—</div>
    </div>`;
  }
  return `<div class="oi-box"><div class="oi-title">${title}</div><div class="oi-rows">${rows}</div></div>`;
}
function updateOIBlockData(root, kind, data) {
  if (!root) return;
  data = data || [];
  const maxOI = Math.max(...data.map(x => x.oi || 0), 1);
  for (let i = 0; i < 5; i++) {
    const bar = root.querySelector(`[data-oi-bar="${kind}-${i}"]`);
    const strike = root.querySelector(`[data-oi-strike="${kind}-${i}"]`);
    if (!bar || !strike) continue;
    if (i < data.length) {
      bar.style.width = `${((data[i].oi || 0) / maxOI) * 100}%`;
      strike.textContent = data[i].strike;
    } else {
      bar.style.width = "0%";
      strike.textContent = "—";
    }
  }
}

/* ===== Whales ===== */
function buildWhalesStatic(sym) {
  let rows = "";
  for (let i = 0; i < 5; i++) {
    rows += `<div class="whale-pill" data-whale-row="${sym}-${i}" style="display:none;">
      <span class="whale-type" data-whale-type="${sym}-${i}">—</span>
      <span class="whale-sep">·</span>
      <span class="whale-strike" data-whale-strike="${sym}-${i}">—</span>
      <span class="whale-sep">·</span>
      <span class="whale-vol">حجم <b data-whale-vol="${sym}-${i}">—</b></span>
      <span class="whale-sep">·</span>
      <span class="whale-oi">مفتوحة <b data-whale-oi="${sym}-${i}">—</b></span>
      <span class="whale-sep">·</span>
      <span class="whale-dir" data-whale-dir="${sym}-${i}">—</span>
    </div>`;
  }
  return `<div class="whales-section" id="whales-${sym}" style="display:none;">
    <div class="whales-title">🐋 الحيتان المكتشفة</div>
    <div class="whales-list">${rows}</div>
  </div>`;
}
function updateWhalesData(root, sym, whales) {
  if (!root) return;
  const wrap = root.querySelector(`#whales-${sym}`);
  if (!wrap) return;
  whales = whales || [];
  if (whales.length === 0) { wrap.style.display = "none"; return; }
  wrap.style.display = "block";
  for (let i = 0; i < 5; i++) {
    const row = root.querySelector(`[data-whale-row="${sym}-${i}"]`);
    if (!row) continue;
    if (i < whales.length) {
      const w = whales[i];
      row.style.display = "inline-flex";
      row.className = "whale-pill " + (w.type === "CALL" ? "whale-call" : "whale-put");
      const type = row.querySelector(`[data-whale-type="${sym}-${i}"]`);
      const strike = row.querySelector(`[data-whale-strike="${sym}-${i}"]`);
      const vol = row.querySelector(`[data-whale-vol="${sym}-${i}"]`);
      const oi = row.querySelector(`[data-whale-oi="${sym}-${i}"]`);
      const dir = row.querySelector(`[data-whale-dir="${sym}-${i}"]`);
      if (type) type.textContent = w.type;
      if (strike) strike.textContent = w.strike;
      const volFmt = w.volume >= 1000 ? (w.volume / 1000).toFixed(1) + "K" : w.volume;
      const oiFmt  = w.oi >= 1000 ? (w.oi / 1000).toFixed(1) + "K" : w.oi;
      if (vol) vol.textContent = volFmt;
      if (oi)  oi.textContent  = oiFmt;
      if (dir) dir.textContent = w.direction === "buy" ? "🟢 يشتري"
                              : w.direction === "sell" ? "🔴 يبيع"
                              : "⚪ محايد";
    } else {
      row.style.display = "none";
    }
  }
}

/* ===== Put/Call Boxes ===== */
function updatePutCallBoxes(root, sym, card) {
  const callOI = card.call_oi || [];
  const putOI  = card.put_oi  || [];
  const tCOI = callOI.reduce((s, x) => s + (x.oi || 0), 0);
  const tPOI = putOI.reduce((s, x) => s + (x.oi || 0), 0);
  const tCV  = callOI.reduce((s, x) => s + (x.volume || 0), 0);
  const tPV  = putOI.reduce((s, x) => s + (x.volume || 0), 0);
  const totOI = tCOI + tPOI;
  const totV  = tCV + tPV;
  const callOITxt  = totOI > 0 ? Math.round(tCOI / totOI * 100) + "%" : "—";
  const putOITxt   = totOI > 0 ? Math.round(tPOI / totOI * 100) + "%" : "—";
  const callVolTxt = totV  > 0 ? Math.round(tCV  / totV  * 100) + "%" : "—";
  const putVolTxt  = totV  > 0 ? Math.round(tPV  / totV  * 100) + "%" : "—";
  const set = (id, val) => { const el = root.querySelector(`#${id}-${sym}`); if (el) el.textContent = val; };
  set("pcbox-call-oi",  callOITxt);
  set("pcbox-put-oi",   putOITxt);
  set("pcbox-call-vol", callVolTxt);
  set("pcbox-put-vol",  putVolTxt);
}

/* ===== TF Box ===== */
function buildTFBox(tf) {
  const label = tf.label || "—";
  const support = tf.support;
  const resistance = tf.resistance;
  const brokeRes = !!tf.broke_resistance;
  const brokeSup = !!tf.broke_support;

  let statusHtml = "";
  let boxClass = "tf-cell neutral";

  if (brokeRes) {
    boxClass = "tf-cell up";
    statusHtml = `<div class="tf-status tf-broke-up">مخترق ⬆</div>`;
  } else if (brokeSup) {
    boxClass = "tf-cell down";
    statusHtml = `<div class="tf-status tf-broke-down">مكسور ⬇</div>`;
  } else {
    statusHtml = `<div class="tf-status tf-no">لم يُخترق</div>`;
  }

  const supTxt = support != null ? `$${support.toFixed(2)}` : "—";
  const resTxt = resistance != null ? `$${resistance.toFixed(2)}` : "—";

  return `
    <div class="${boxClass}">
      <div class="tf-label"><span>${label}</span></div>
      <div class="tf-line"><span class="tf-k">مقاومة</span><span class="tf-v">${resTxt}</span></div>
      <div class="tf-line"><span class="tf-k">دعم</span><span class="tf-v">${supTxt}</span></div>
      ${statusHtml}
    </div>
  `;
}

/* ===== Summary Bar ===== */
function buildSummaryBar(cardData) {
  const c = cardData.card || {};
  const color = c.color || "gray";
  const label = c.label || "—";
  const stage = c.stage ? stageLabel(c.stage) : "";
  const rr = cardData.levels?.rr ?? "—";
  const rrOk = typeof rr === "number" && rr >= 2.0;
  const pattern = cardData.levels?.pattern || "";

  return `
    <div class="summary-bar">
      <span class="summary-badge badge-${color}">${label}</span>
      ${stage ? `<span class="summary-item">${stage}</span><span class="summary-sep">·</span>` : ""}
      ${pattern && pattern !== "none" ? `<span class="summary-item">${pattern}</span><span class="summary-sep">·</span>` : ""}
      <span class="summary-item">R:R <b>${rr}</b> ${rrOk ? "✅" : "⚠️"}</span>
    </div>
  `;
}

/* ===== Build Card ===== */
function buildCard(cardData) {
  const c = cardData.card || {};
  // ✅ تجاهل الرمادي كلياً
  if (!VALID_COLORS.includes(c.color)) return null;

  const lv = cardData.levels || {};
  const cls = c.color;
  const price = cardData.price ?? 0;
  const sym = cardData.symbol;
  const monitorStatus = cardData.monitor_status || "none";
  const ms = monitorStatusLabel(monitorStatus);

  checkSound(sym, cls);

  const div = document.createElement("div");
  div.className = "card " + cls;
  div.dataset.symbol = sym;

  const monitorBadge = ms.label
    ? `<span class="monitor-badge ${ms.cls}">${ms.label}</span>`
    : "";

  const row1 = `<div class="card-row row-1">
    <div class="cell symbol-cell">${sym}</div>
    <div class="cell price-cell">
      <div class="val" data-price>$${price.toFixed(2)}</div>
      <div class="sub">السعر الحالي</div>
    </div>
    <div class="cell score-cell">
      <div class="val" data-score>${lv.rr ?? "—"}</div>
      <div class="sub">R:R</div>
    </div>
    <div class="cell badge-cell">
      <div class="badge" data-badge>🔥 ${c.label || "—"}</div>
      ${monitorBadge}
      <button class="card-trash" data-trash>
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <polyline points="3 6 5 6 21 6"></polyline>
          <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"></path>
        </svg>
      </button>
    </div>
  </div>`;

  const row2 = `<div class="card-row row-2">
    <div class="cell"><div class="label">الأيام</div><div class="val">${lv.dte || "—"}</div></div>
    <div class="cell"><div class="label">سعر العقد</div><div class="val">${lv.premium && lv.premium !== "—" ? "$" + lv.premium : "—"}</div></div>
    <div class="cell"><div class="label">الانتهاء</div><div class="val">${lv.expiry || "—"}</div></div>
    <div class="cell"><div class="label">التنفيذ</div><div class="val">${lv.strike || "—"}</div></div>
  </div>`;

  const row3 = `<div class="card-row row-3">
    <div class="cell"><div class="label">الوقف</div><div class="val">${lv.stop ? "$" + lv.stop : "—"}</div></div>
    <div class="cell"><div class="label">R:R</div><div class="val">${lv.rr || "—"}</div></div>
    <div class="cell"><div class="label">الهدف</div><div class="val">${lv.target1 ? "$" + lv.target1 : "—"}</div></div>
    <div class="cell"><div class="label">الدخول</div><div class="val">${lv.entry ? "$" + lv.entry : "—"}</div></div>
  </div>`;

  const tfs = cardData.timeframes || [];
  const tfsHtml = tfs.map(t => buildTFBox(t)).join("");

  const oiHtml = `<div class="oi-grid">
    ${buildOIBlockStatic("مفتوحة - PUT", "put-oi", "#8b5cf6")}
    ${buildOIBlockStatic("سيولة - PUT", "put-liq", "#ef4444")}
    ${buildOIBlockStatic("مفتوحة - CALL", "call-oi", "#3b82f6")}
    ${buildOIBlockStatic("سيولة - CALL", "call-liq", "#22c55e")}
  </div>`;

  const boxesHtml = `
    <div class="putcall-boxes">
      <div class="pcbox pcbox-call">
        <div class="pcbox-label">مفتوحة - CALL</div>
        <div class="pcbox-pct" id="pcbox-call-oi-${sym}">—</div>
      </div>
      <div class="pcbox pcbox-put">
        <div class="pcbox-label">مفتوحة - PUT</div>
        <div class="pcbox-pct" id="pcbox-put-oi-${sym}">—</div>
      </div>
      <div class="pcbox pcbox-call">
        <div class="pcbox-label">حجم - CALL</div>
        <div class="pcbox-pct" id="pcbox-call-vol-${sym}">—</div>
      </div>
      <div class="pcbox pcbox-put">
        <div class="pcbox-label">حجم - PUT</div>
        <div class="pcbox-pct" id="pcbox-put-vol-${sym}">—</div>
      </div>
    </div>`;

  const whalesHtml = buildWhalesStatic(sym);

  const expanded = `<div class="card-expanded">
    <div class="tf-grid">${tfsHtml}</div>
    ${oiHtml}
    ${boxesHtml}
    ${whalesHtml}
    <div data-summary-wrap>${buildSummaryBar(cardData)}</div>
  </div>`;

  div.innerHTML = row1 + row2 + row3 + expanded;
  updateAllDynamic(div, sym, cardData);

  div.addEventListener("click", (e) => {
    if (e.target.closest("[data-trash]")) return;
    div.classList.toggle("open");
  });

  div.querySelector("[data-trash]").addEventListener("click", async (e) => {
    e.stopPropagation();
    await deleteCard(sym);
  });

  return div;
}

/* ===== Dynamic Update ===== */
function updateAllDynamic(root, sym, card) {
  if (!root || !card) return;
  const callOI = card.call_oi || [];
  const putOI  = card.put_oi  || [];
  updateOIBlockData(root, "put-oi",   putOI);
  updateOIBlockData(root, "put-liq",  putOI.map(x => ({strike: x.strike, oi: x.volume})));
  updateOIBlockData(root, "call-oi",  callOI);
  updateOIBlockData(root, "call-liq", callOI.map(x => ({strike: x.strike, oi: x.volume})));
  updatePutCallBoxes(root, sym, card);
  updateWhalesData(root, sym, card.whales || []);
}

/* ===== Update In Place ===== */
function updateCardInPlace(cardEl, data) {
  const c = data.card || {};
  if (!VALID_COLORS.includes(c.color)) {
    cardEl.remove();
    return;
  }
  const lv = data.levels || {};
  const price = data.price ?? 0;

  const wasOpen = cardEl.classList.contains("open");
  const newCls = "card " + c.color + (wasOpen ? " open" : "");
  if (cardEl.className !== newCls) cardEl.className = newCls;

  const priceEl = cardEl.querySelector("[data-price]");
  if (priceEl) priceEl.textContent = "$" + price.toFixed(2);

  const scoreEl = cardEl.querySelector("[data-score]");
  if (scoreEl) scoreEl.textContent = lv.rr ?? "—";

  const badgeEl = cardEl.querySelector("[data-badge]");
  if (badgeEl) badgeEl.textContent = "🔥 " + (c.label || "—");

  const row2 = cardEl.querySelector(".row-2");
  if (row2) {
    const cells = row2.querySelectorAll(".cell .val");
    if (cells[0]) cells[0].textContent = lv.dte || "—";
    if (cells[1]) cells[1].textContent = (lv.premium && lv.premium !== "—") ? "$" + lv.premium : "—";
    if (cells[2]) cells[2].textContent = lv.expiry || "—";
    if (cells[3]) cells[3].textContent = lv.strike || "—";
  }

  const row3 = cardEl.querySelector(".row-3");
  if (row3) {
    const cells = row3.querySelectorAll(".cell .val");
    if (cells[0]) cells[0].textContent = lv.stop ? "$" + lv.stop : "—";
    if (cells[1]) cells[1].textContent = lv.rr || "—";
    if (cells[2]) cells[2].textContent = lv.target1 ? "$" + lv.target1 : "—";
    if (cells[3]) cells[3].textContent = lv.entry ? "$" + lv.entry : "—";
  }

  const tfGrid = cardEl.querySelector(".tf-grid");
  if (tfGrid) {
    const tfs = data.timeframes || [];
    tfGrid.innerHTML = tfs.map(t => buildTFBox(t)).join("");
  }

  updateAllDynamic(cardEl, data.symbol, data);

  const sumWrap = cardEl.querySelector("[data-summary-wrap]");
  if (sumWrap) sumWrap.innerHTML = buildSummaryBar(data);
}

/* ===== Delete Card ===== */
async function deleteCard(symbol) {
  scanCards = scanCards.filter(c => c.symbol !== symbol);
  deletedSymbols.add(symbol);
  saveDeleted();
  saveCards();

  try {
    await fetch(`${API_BASE}/api/monitor/${symbol}/remove`, { method: "POST" });
  } catch (e) {}

  if (monitorTimers.has(symbol)) {
    clearInterval(monitorTimers.get(symbol));
    monitorTimers.delete(symbol);
  }

  const el = cardsArea.querySelector(`[data-symbol="${symbol}"]`);
  if (el) el.remove();
  if (scanCards.length === 0) emptyState.style.display = "block";
}

/* ===== Render ===== */
function renderCards() {
  const filtered = scanCards
    .filter(c => !deletedSymbols.has(c.symbol))
    .filter(c => VALID_COLORS.includes(c.card?.color))
    .sort((a, b) => (b.levels?.rr ?? 0) - (a.levels?.rr ?? 0));

  Array.from(cardsArea.children).forEach(el => {
    if (el.classList && el.classList.contains("card")) {
      const sym = el.dataset.symbol;
      if (!filtered.find(c => c.symbol === sym)) el.remove();
    }
  });

  filtered.forEach((cardData, idx) => {
    const sym = cardData.symbol;
    let el = cardsArea.querySelector(`[data-symbol="${sym}"]`);

    if (el) {
      updateCardInPlace(el, cardData);
    } else {
      el = buildCard(cardData);
      if (!el) return;
    }

    const current = Array.from(cardsArea.children).filter(c => c.classList && c.classList.contains("card"));
    if (current[idx] !== el) {
      if (idx >= current.length) cardsArea.appendChild(el);
      else cardsArea.insertBefore(el, current[idx]);
    }
  });

  emptyState.style.display = filtered.length ? "none" : "block";
  if (filtered.length === 0 && scanCards.length > 0) {
    emptyState.innerHTML = "<p>لا توجد فرص حالياً</p>";
  } else if (scanCards.length === 0) {
    emptyState.innerHTML = "<p>اضغط \"ابدأ المسح\" لفحص السوق</p>";
  }
}

/* ===== Scan ===== */
async function startScan() {
  scanBtn.disabled = true;
  scanBtn.querySelector(".scan-btn-text").textContent = "جاري المسح...";

  progressWrap.style.display = "block";
  progressFill.style.width = "0%";
  progressText.textContent = "0 / 500";
  progressFound.textContent = "0 فرص";

  try {
    const r = await fetch(`${API_BASE}/api/scan/start`, { method: "POST" });
    const d = await r.json();

    if (!d.scan_id) {
      alert("فشل بدء المسح");
      resetScanButton();
      return;
    }

    currentScan = { scan_id: d.scan_id, total: d.total };
    totalSymbols.textContent = d.total;

    scanTimer = setInterval(pollScanProgress, 2000);
    pollScanProgress();

  } catch (e) {
    alert("خطأ: " + e.message);
    resetScanButton();
  }
}

function resetScanButton() {
  scanBtn.disabled = false;
  scanBtn.querySelector(".scan-btn-text").textContent = "ابدأ المسح";
}

function mergeScanResults(newResults) {
  const existingMap = new Map(scanCards.map(c => [c.symbol, c]));

  newResults.forEach(newCard => {
    const sym = newCard.symbol;
    if (deletedSymbols.has(sym)) return;
    // ✅ تجاهل الرمادي كلياً
    if (!VALID_COLORS.includes(newCard.card?.color)) return;

    const existing = existingMap.get(sym);
    if (existing) {
      Object.assign(existing, newCard);
    } else {
      scanCards.push(newCard);
    }
  });

  saveCards();
  renderCards();
}

async function pollScanProgress() {
  if (!currentScan) return;

  try {
    const r = await fetch(`${API_BASE}/api/scan/${currentScan.scan_id}`);
    if (!r.ok) return;
    const d = await r.json();

    const pct = (d.completed / d.total) * 100;
    progressFill.style.width = pct + "%";
    progressText.textContent = `${d.completed} / ${d.total}`;
    progressFound.textContent = `${d.found} فرص`;

    mergeScanResults(d.results || []);

    if (d.status === "done" || d.status === "cancelled") {
      clearInterval(scanTimer);
      scanTimer = null;
      currentScan = null;
      resetScanButton();
      progressWrap.style.display = d.status === "done" ? "none" : "block";

      startMonitoring();
    }
  } catch (e) {}
}

scanBtn.addEventListener("click", startScan);

scanCancelBtn.addEventListener("click", async () => {
  if (!currentScan) return;
  try {
    await fetch(`${API_BASE}/api/scan/${currentScan.scan_id}/cancel`, { method: "POST" });
  } catch (e) {}
});

/* ===== Monitoring ===== */
function startMonitoring() {
  monitorTimers.forEach(t => clearInterval(t));
  monitorTimers.clear();

  scanCards.forEach(card => {
    const sym = card.symbol;
    if (deletedSymbols.has(sym)) return;
    if (!VALID_COLORS.includes(card.card?.color)) return;
    fetchMonitorStatus(sym);
    const timer = setInterval(() => fetchMonitorStatus(sym), 30000);
    monitorTimers.set(sym, timer);
  });

  startPricePolling();
}

async function fetchMonitorStatus(symbol) {
  if (deletedSymbols.has(symbol)) return;
  try {
    const mRes = await fetch(`${API_BASE}/api/monitor/${symbol}`);
    if (mRes.ok) {
      const m = await mRes.json();
      if (m.monitored) updateCardMonitorStatus(symbol, m);
    }
  } catch (e) {}
}

function updateCardMonitorStatus(symbol, m) {
  const card = scanCards.find(c => c.symbol === symbol);
  if (card) card.monitor_status = m.status;

  const cardEl = cardsArea.querySelector(`[data-symbol="${symbol}"]`);
  if (!cardEl) return;
  const badgeCell = cardEl.querySelector(".badge-cell");
  if (!badgeCell) return;

  const old = badgeCell.querySelector(".monitor-badge");
  if (old) old.remove();

  const ms = monitorStatusLabel(m.status);
  if (ms.label) {
    const span = document.createElement("span");
    span.className = "monitor-badge " + ms.cls;
    span.textContent = ms.label;
    badgeCell.insertBefore(span, badgeCell.querySelector("[data-trash]"));
  }
}

/* ===== Price Polling ===== */
async function pollPrices() {
  for (const card of scanCards) {
    if (deletedSymbols.has(card.symbol)) continue;
    if (!VALID_COLORS.includes(card.card?.color)) continue;
    try {
      const r = await fetch(`${API_BASE}/api/price/${card.symbol}`);
      if (!r.ok) continue;
      const d = await r.json();
      if (d.price) {
        card.price = d.price;
        const el = cardsArea.querySelector(`[data-symbol="${card.symbol}"]`);
        if (el) {
          const priceEl = el.querySelector("[data-price]");
          if (priceEl) priceEl.textContent = "$" + parseFloat(d.price).toFixed(2);
        }
      }
    } catch (e) {}
  }
}

function startPricePolling() {
  if (pricePollTimer) return;
  setTimeout(pollPrices, 1000);
  pricePollTimer = setInterval(pollPrices, 5000);
}

/* ===== Init ===== */
(function init() {
  scanCards = loadCards();
  loadDeleted();
  scanCards = scanCards
    .filter(c => !deletedSymbols.has(c.symbol))
    .filter(c => VALID_COLORS.includes(c.card?.color));
  saveCards();
  renderCards();

  if (scanCards.length > 0) {
    startMonitoring();
  }

  fetch(`${API_BASE}/api/symbols`)
    .then(r => r.json())
    .then(d => { totalSymbols.textContent = d.total; })
    .catch(() => {});

  checkBackendStatus();
  setInterval(checkBackendStatus, 60000);
})();
