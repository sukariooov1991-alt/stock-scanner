/* ============================================================
   app.js — Longbridge Scanner
   زر مسح + مراقبة + شارات الحالة + زر حذف
   البطاقات تتراكم — لا تختفي عند مسح جديد
   ============================================================ */
const API_BASE = window.location.origin;

let currentScan = null;
let scanCards = [];
let scanTimer = null;
let monitorTimers = new Map();
let activeFilter = "all";
let lastAlertedState = {};

// ✅ الرموز المحذوفة يدوياً — لا تعود أبداً
let deletedSymbols = new Set();

// ✅ حفظ المحذوفات في localStorage
function saveDeleted() {
  try {
    localStorage.setItem("scanner_deleted", JSON.stringify([...deletedSymbols]));
  } catch (e) {}
}
function loadDeleted() {
  try {
    const r = localStorage.getItem("scanner_deleted");
    if (r) deletedSymbols = new Set(JSON.parse(r));
  } catch (e) {}
}

// ✅ حفظ البطاقات في localStorage
function saveCards() {
  try {
    localStorage.setItem("scanner_cards", JSON.stringify(scanCards));
  } catch (e) {}
}
function loadCards() {
  try {
    const r = localStorage.getItem("scanner_cards");
    return r ? JSON.parse(r) : [];
  } catch (e) { return []; }
}

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
const filterSection = document.getElementById("filterSection");
const totalSymbols = document.getElementById("totalSymbols");

/* ===== الثيم ===== */
function applyTheme(light) {
  document.body.classList.toggle("light", light);
  themeIcon.textContent = light ? "☀️" : "🌙";
  localStorage.setItem("theme", light ? "light" : "dark");
}
themeBtn.addEventListener("click", () => applyTheme(!document.body.classList.contains("light")));
applyTheme(localStorage.getItem("theme") === "light");

/* ===== حالة السوق ===== */
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

/* ===== حالة الاتصال ===== */
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

/* ===== الصوت ===== */
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

function trendLabel(t) {
  if (t === "up") return "صاعد ↑";
  if (t === "down") return "هابط ↓";
  return "محايد —";
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

/* ===== الحيتان ===== */
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

/* ===== مربعات CALL/PUT ===== */
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

function buildSummaryBar(cardData) {
  const c = cardData.card || {};
  const color = c.color || "gray";
  const label = c.label || "—";
  const tw = c.trend_w  || "neutral";
  const td = c.trend_d  || "neutral";
  const t4 = c.trend_4h || "neutral";
  const twIcon = tw === "up" ? "🟢" : tw === "down" ? "🔴" : "⚪";
  const tdIcon = td === "up" ? "🟢" : td === "down" ? "🔴" : "⚪";
  const t4Icon = t4 === "up" ? "🟢" : t4 === "down" ? "🔴" : "⚪";
  const rvol = c.rvol ?? "—";
  const rr   = c.rr ?? "—";
  const rrOk = typeof rr === "number" && rr >= 2.0;
  return `
    <div class="summary-bar">
      <span class="summary-badge badge-${color}">${label}</span>
      <span class="summary-item">1W ${twIcon}</span>
      <span class="summary-sep">·</span>
      <span class="summary-item">1D ${tdIcon}</span>
      <span class="summary-sep">·</span>
      <span class="summary-item">4H ${t4Icon}</span>
      <span class="summary-sep">·</span>
      <span class="summary-item">RVOL <b>${rvol}</b></span>
      <span class="summary-sep">·</span>
      <span class="summary-item">R:R <b>${rr}</b> ${rrOk ? "✅" : "⚠️"}</span>
    </div>
  `;
}

function buildCard(cardData) {
  const c = cardData.card || {};
  const lv = cardData.levels || {};
  const cls = c.color || "gray";
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
      <div class="val" data-score>${c.score ?? 0}%</div>
      <div class="sub">قوة الإشارة</div>
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
  const tfsHtml = tfs.map((t, idx) => `
    <div class="tf-cell ${t.trend}" data-tf-cell="${idx}">
      <div class="tf-label"><span>${t.label}</span><span data-tf-trend>${trendLabel(t.trend)}</span></div>
      <div class="tf-row"><span>EMA</span><span class="v">${t.ema20}/${t.ema50}</span></div>
      <div class="tf-row"><span>RSI</span><span class="v">${t.rsi}</span></div>
      <div class="tf-row"><span>ADX</span><span class="v">${t.adx}</span></div>
      <div class="tf-row"><span>RVOL</span><span class="v">${t.rvol}x</span></div>
    </div>`).join("");

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
    <div class="bottom-grid">
      <div class="cell"><div class="label">VWAP</div><div class="val">$${cardData.vwap ?? "—"}</div></div>
      <div class="cell"><div class="label">مقاومات</div><div class="val">${(cardData.resistances||[]).join(" / ") || "—"}</div></div>
      <div class="cell"><div class="label">دعوم</div><div class="val">${(cardData.supports||[]).join(" / ") || "—"}</div></div>
      <div class="cell"><div class="label">السعر</div><div class="val" data-btm-price>$${price.toFixed(2)}</div></div>
    </div>
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

/* ===== حذف البطاقة ===== */
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

  renderCards();
}

/* ===== عرض البطاقات ===== */
function renderCards() {
  cardsArea.innerHTML = "";

  const filtered = scanCards.filter(c => {
    if (deletedSymbols.has(c.symbol)) return false;
    if (activeFilter === "all") return true;
    return c.card?.color === activeFilter;
  });

  filtered.sort((a, b) => (b.card?.score ?? 0) - (a.card?.score ?? 0));

  filtered.forEach(c => cardsArea.appendChild(buildCard(c)));

  emptyState.style.display = filtered.length ? "none" : "block";

  if (filtered.length === 0 && scanCards.length > 0) {
    emptyState.innerHTML = "<p>لا توجد فرص مطابقة للفلتر المحدد</p>";
  } else if (scanCards.length === 0) {
    emptyState.innerHTML = "<p>اضغط \"ابدأ المسح\" لفحص السوق</p>";
  }
}

/* ===== الفلاتر ===== */
document.querySelectorAll(".chip").forEach(chip => {
  chip.addEventListener("click", () => {
    document.querySelectorAll(".chip").forEach(c => c.classList.remove("active"));
    chip.classList.add("active");
    activeFilter = chip.dataset.filter;
    renderCards();
  });
});

/* ===== زر المسح ===== */
async function startScan() {
  scanBtn.disabled = true;
  scanBtn.querySelector(".scan-btn-text").textContent = "جاري المسح...";

  progressWrap.style.display = "block";
  filterSection.style.display = "block";

  // ✅ لا نحذف scanCards القديمة
  // ✅ لا نحذف deletedSymbols

  progressFill.style.width = "0%";
  progressText.textContent = "0 / 100";
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

/* ✅ دمج نتائج المسح الجديد مع القديمة */
function mergeScanResults(newResults) {
  // خريطة الرموز الحالية
  const existingMap = new Map(scanCards.map(c => [c.symbol, c]));

  newResults.forEach(newCard => {
    const sym = newCard.symbol;

    // تجاهل المحذوفات
    if (deletedSymbols.has(sym)) return;

    const existing = existingMap.get(sym);
    if (existing) {
      // ✅ حدّث البطاقة الموجودة (احتفظ بحالة المراقبة)
      existing.card = newCard.card;
      existing.levels = newCard.levels;
      existing.timeframes = newCard.timeframes;
      existing.price = newCard.price;
      existing.vwap = newCard.vwap;
      existing.supports = newCard.supports;
      existing.resistances = newCard.resistances;
      existing.call_oi = newCard.call_oi;
      existing.put_oi = newCard.put_oi;
      existing.total_call_oi = newCard.total_call_oi;
      existing.total_put_oi = newCard.total_put_oi;
      existing.total_call_vol = newCard.total_call_vol;
      existing.total_put_vol = newCard.total_put_vol;
      existing.whales = newCard.whales;
      // monitor_status يبقى كما هو من السيرفر
      existing.monitor_status = newCard.monitor_status || existing.monitor_status;
    } else {
      // ✅ بطاقة جديدة
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

    // ✅ دمج بدل استبدال
    mergeScanResults(d.results || []);

    if (d.status === "done" || d.status === "cancelled") {
      clearInterval(scanTimer);
      scanTimer = null;
      currentScan = null;
      resetScanButton();
      progressWrap.style.display = d.status === "done" ? "none" : "block";
      filterSection.style.display = "block";

      startMonitoring();
    }
  } catch (e) {}
}

scanBtn.addEventListener("click", startScan);

/* ===== إلغاء المسح ===== */
scanCancelBtn.addEventListener("click", async () => {
  if (!currentScan) return;
  try {
    await fetch(`${API_BASE}/api/scan/${currentScan.scan_id}/cancel`, { method: "POST" });
  } catch (e) {}
});

/* ===== المراقبة اللحظية ===== */
function startMonitoring() {
  // أوقف أي مراقبة سابقة
  monitorTimers.forEach(t => clearInterval(t));
  monitorTimers.clear();

  scanCards.forEach(card => {
    const sym = card.symbol;
    if (deletedSymbols.has(sym)) return;

    fetchMonitorStatus(sym);
    const timer = setInterval(() => fetchMonitorStatus(sym), 30000);
    monitorTimers.set(sym, timer);
  });
}

async function fetchMonitorStatus(symbol) {
  if (deletedSymbols.has(symbol)) return;

  try {
    const priceRes = await fetch(`${API_BASE}/api/price/${symbol}`);
    if (priceRes.ok) {
      const pData = await priceRes.json();
      if (pData.price) updateCardPrice(symbol, pData.price);
    }

    const mRes = await fetch(`${API_BASE}/api/monitor/${symbol}`);
    if (mRes.ok) {
      const m = await mRes.json();
      if (m.monitored) updateCardMonitorStatus(symbol, m);
    }
  } catch (e) {}
}

function updateCardPrice(symbol, price) {
  const cardEl = cardsArea.querySelector(`[data-symbol="${symbol}"]`);
  if (!cardEl) return;
  const priceEl = cardEl.querySelector("[data-price]");
  if (priceEl) priceEl.textContent = "$" + parseFloat(price).toFixed(2);
  const btmPrice = cardEl.querySelector("[data-btm-price]");
  if (btmPrice) btmPrice.textContent = "$" + parseFloat(price).toFixed(2);

  const card = scanCards.find(c => c.symbol === symbol);
  if (card) card.price = parseFloat(price);
}

function updateCardMonitorStatus(symbol, m) {
  const card = scanCards.find(c => c.symbol === symbol);
  if (card) {
    card.monitor_status = m.status;
    card.monitor_price = m.last_price;
    card.monitor_expires_at = m.expires_at;
  }

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

/* ===== التهيئة ===== */
(function init() {
  // استرجاع البطاقات المحفوظة
  scanCards = loadCards();
  loadDeleted();

  // استبعد المحذوفات
  scanCards = scanCards.filter(c => !deletedSymbols.has(c.symbol));

  renderCards();

  // شغّل المراقبة للبطاقات الموجودة
  if (scanCards.length > 0) {
    startMonitoring();
    filterSection.style.display = "block";
  }

  fetch(`${API_BASE}/api/symbols`)
    .then(r => r.json())
    .then(d => { totalSymbols.textContent = d.total; })
    .catch(() => {});

  checkBackendStatus();
  setInterval(checkBackendStatus, 60000);
})();
