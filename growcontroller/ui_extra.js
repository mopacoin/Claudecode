// Zusätzliche Ansichten des Dashboards (wird nach index.html geladen und nutzt dessen Helfer: S, CFG, api, $, esc, ico,
// cssv, rgba, toast). renderAll() ruft renderExtra() nach jedem Neuzeichnen auf.
'use strict';

// ---- Systemzustand (Leiste oben in der Übersicht) ----
const HEALTH_COL = {ok: '--ok', warn: '--warn', bad: '--bad', off: '--mute'};
let healthOpen = false;
function renderHealth() {
  const box = $('#health'); if (!box || !S.health) return;
  const worst = ['bad', 'warn', 'ok', 'off'].find(s => S.health.some(h => h.state === s)) || 'ok';
  box.innerHTML = S.health.map(h => `<button class="chip hchip" title="${esc(h.detail)}" style="--hc:var(${HEALTH_COL[h.state]})"><span class="d" style="background:var(${HEALTH_COL[h.state]})"></span>${esc(h.name)}</button>`).join('')
    + `<button class="chip" id="healthMore">${healthOpen ? 'weniger' : 'Details'}</button>`
    + (healthOpen ? `<div class="hdet">${S.health.map(h => `<div><span class="d" style="background:var(${HEALTH_COL[h.state]})"></span><b>${esc(h.name)}</b> <span class="sub">${esc(h.detail)}</span></div>`).join('')}</div>` : '');
  box.dataset.worst = worst;
}
document.addEventListener('click', e => {
  if (e.target.closest('#healthMore') || e.target.closest('.hchip')) { healthOpen = !healthOpen; renderHealth(); }
});

// ---- Tagesbericht (letzter gesendeter + aktuelle Auswertung) ----
let reportData = null, reportShowNow = false, reportT = 0;
async function loadReport(force) {
  if (!force && Date.now() - reportT < 300000 && reportData) return renderReportCard();
  try { reportData = await api('/api/report'); reportT = Date.now(); } catch (e) { return; }
  renderReportCard();
}
function renderReportCard() {
  const card = $('#reportCard'); if (!card) return;
  if (!reportData) { card.innerHTML = `<h3>${ico('bell')} Tagesbericht</h3><div class="sub">wird geladen …</div>`; return; }
  const last = reportData.last, r = reportShowNow || !last ? reportData.preview : last;
  const head = reportShowNow || !last ? 'Aktuelle Auswertung (letzte 24 Std)' : `Gesendet ${last.t.slice(8, 10)}.${last.t.slice(5, 7)}. ${last.t.slice(11, 16)}`;
  card.innerHTML = `<div class="row"><h3 class="sp" style="margin:0">${ico('bell')} Tagesbericht</h3>
      ${last ? `<button class="chip ${reportShowNow ? '' : 'on'}" data-rep="last">Gesendet</button>` : ''}
      <button class="chip ${reportShowNow || !last ? 'on' : ''}" data-rep="now">Jetzt</button></div>
    <div class="sub" style="margin:8px 0 6px">${esc(head)}</div>
    <div style="font-weight:600;margin-bottom:6px">${esc(r.title)}</div>
    <div class="reptext">${esc(r.text)}</div>`;
}
document.addEventListener('click', e => {
  const b = e.target.closest('[data-rep]'); if (!b) return;
  reportShowNow = b.dataset.rep === 'now'; if (reportShowNow) loadReport(true); else renderReportCard();
});

// ================= Auswertung: VPD-Matrix, Statistik, Energie =================
let anaHist = [], statDays = 30, statData = null, enDays = 7, enData = null;
async function loadAnalysis() {
  try { anaHist = await api('/api/history?range=6h'); } catch (e) { anaHist = []; }
  drawVpdMatrix(); loadStats(); loadEnergy();
}

// ---- VPD-Matrix: Temperatur × Luftfeuchte, farbig nach VPD, Ziel hervorgehoben, aktueller Punkt mit Spur ----
const VPD_ZONES = [[0.4, '#4db8ff', 'unter 0,4 – zu feucht'], [0.8, '#2dd4bf', '0,4–0,8 Keimling/Steckling'], [1.2, '#3ddc97', '0,8–1,2 Wachstum'],
  [1.6, '#ffb547', '1,2–1,6 Blüte'], [99, '#ff5d6c', 'über 1,6 – zu trocken']];
function drawVpdMatrix() {
  const box = $('#vpdMatrix'); if (!box || !S || !CFG) return;
  if (!box.querySelector('canvas')) box.innerHTML = `<h3>${ico('drops')} VPD-Matrix</h3><p class="hint">Welche Kombination aus Temperatur und Luftfeuchte welches VPD ergibt. Der helle Bereich ist dein Ziel, der Punkt der aktuelle Wert, die Linie die letzten 6 Stunden.</p>
    <div style="position:relative"><canvas id="vpdCanvas" style="width:100%;display:block"></canvas></div><div class="chips" id="vpdLegend" style="margin-top:10px"></div>`;
  const cv = $('#vpdCanvas'), cl = CFG.values.climate, leaf = cl.leaf_offset ?? -2, day = S.is_day;
  const target = cl[day ? 'vpd_day' : 'vpd_night'], hyst = cl.vpd_hyst;
  const T0 = 15, T1 = 32, H0 = 30, H1 = 90, pad = {l: 40, r: 12, t: 10, b: 30};
  const dpr = devicePixelRatio || 1, w = cv.clientWidth; if (!w) return;
  const h = Math.min(420, Math.max(260, w * .55));
  cv.width = w * dpr; cv.height = h * dpr; cv.style.height = h + 'px';
  const g = cv.getContext('2d'); g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, w, h);
  const X = t => pad.l + (t - T0) / (T1 - T0) * (w - pad.l - pad.r), Y = rh => pad.t + (1 - (rh - H0) / (H1 - H0)) * (h - pad.t - pad.b);
  const vpd = (t, rh) => svpK(t + leaf) - rh / 100 * svpK(t);
  const dt = .25, dh = 1;
  for (let t = T0; t < T1; t += dt) for (let rh = H0; rh < H1; rh += dh) {
    const v = vpd(t + dt / 2, rh + dh / 2), z = VPD_ZONES.find(z => v < z[0]), inT = Math.abs(v - target) <= hyst;
    g.fillStyle = rgba(z[1], inT ? .85 : .28); g.fillRect(X(t), Y(rh + dh), X(t + dt) - X(t) + .6, Y(rh) - Y(rh + dh) + .6);
  }
  g.font = '11px system-ui'; g.fillStyle = cssv('--mute'); g.textAlign = 'center';
  for (let t = T0; t <= T1; t += 2) g.fillText(t + '°', X(t), h - 10);
  g.textAlign = 'right'; for (let rh = H0; rh <= H1; rh += 10) g.fillText(rh + ' %', pad.l - 6, Y(rh) + 4);
  // Soll-Temperatur als Linie
  const tt = cl[day ? 'temp_day' : 'temp_night'];
  g.setLineDash([4, 4]); g.strokeStyle = rgba(cssv('--text'), .5); g.beginPath(); g.moveTo(X(tt), pad.t); g.lineTo(X(tt), h - pad.b); g.stroke(); g.setLineDash([]);
  g.fillStyle = cssv('--text'); g.textAlign = 'left'; g.fillText(`Soll ${tt} °C`, X(tt) + 4, pad.t + 12);
  // Spur der letzten 6 Stunden
  const tr = anaHist.filter(r => r.temp != null && r.hum != null);
  if (tr.length > 1) {
    g.lineWidth = 2; g.lineJoin = g.lineCap = 'round';
    for (let i = 1; i < tr.length; i++) { g.strokeStyle = rgba(cssv('--text'), .12 + .6 * i / tr.length); g.beginPath(); g.moveTo(X(tr[i - 1].temp), Y(tr[i - 1].hum)); g.lineTo(X(tr[i].temp), Y(tr[i].hum)); g.stroke(); }
  }
  const r = S.readings;
  if (r.temp != null && r.hum != null) {
    const x = X(Math.min(T1, Math.max(T0, r.temp))), y = Y(Math.min(H1, Math.max(H0, r.hum)));
    g.beginPath(); g.arc(x, y, 9, 0, 7); g.fillStyle = rgba(cssv('--bg'), .7); g.fill();
    g.beginPath(); g.arc(x, y, 6, 0, 7); g.fillStyle = '#fff'; g.fill(); g.lineWidth = 2.5; g.strokeStyle = cssv('--vpd'); g.stroke();
    g.fillStyle = cssv('--text'); g.font = '600 12px system-ui'; const right = x > w - 200; g.textAlign = right ? 'right' : 'left';
    g.fillText(`${r.temp.toFixed(1)} °C · ${Math.round(r.hum)} % → ${vpd(r.temp, r.hum).toFixed(2)} kPa`, x + (right ? -14 : 14), y - 10);
  }
  $('#vpdLegend').innerHTML = VPD_ZONES.map(z => `<span class="chip"><span style="background:${z[1]};width:9px;height:9px;border-radius:3px;display:inline-block;margin-right:6px"></span>${z[2]}</span>`).join('')
    + `<span class="chip"><span style="background:#fff;width:9px;height:9px;border-radius:3px;display:inline-block;margin-right:6px"></span>Ziel ${target} ± ${hyst} kPa (${day ? 'Tag' : 'Nacht'})</span>`;
}
addEventListener('resize', () => $('#tab-analysis.on') && drawVpdMatrix());

// ---- Statistik: Anteil der Zeit im Zielbereich je Tag ----
async function loadStats() {
  try { statData = await api('/api/stats?days=' + statDays); } catch (e) { return; }
  renderStats();
}
function renderStats() {
  const box = $('#statsCard'); if (!box || !statData) return;
  const keys = statData.vpd_mode ? [['vpd', 'VPD'], ['temp', 'Temp.']] : [['vpd', 'VPD'], ['temp', 'Temp.'], ['hum', 'Feuchte']];
  // Grün (im Ziel) beginnt immer links -> Tage lassen sich wie ein Balkendiagramm vergleichen; „zu niedrig“ nur dezent gestreift
  const col = v => v == null ? '--mute' : v >= 90 ? '--ok' : v >= 70 ? '--warn' : '--bad';
  const bar = st => !st || st.in == null ? '<div class="stcell"><div class="stbar"></div><span class="sub">–</span></div>' :
    `<div class="stcell" title="${st.in} % im Ziel · ${st.lo} % zu niedrig · ${st.hi} % zu hoch"><div class="stbar"><i class="in" style="width:${st.in}%"></i><i class="hi" style="width:${st.hi}%"></i><i class="lo" style="width:${st.lo}%"></i></div>
      <span class="stv"><b style="color:var(${col(st.in)})">${st.in}</b><small>${st.lo >= st.hi ? `↓${st.lo}` : `↑${st.hi}`}</small></span></div>`;
  const all = statData.days.slice().reverse(), last = all.findLastIndex(d => d.all != null);
  const days = all.slice(0, last + 1);   // Tage vor Beginn der Aufzeichnung weglassen
  const withData = days.filter(d => d.all != null), avg = withData.length ? Math.round(withData.reduce((a, d) => a + d.all, 0) / withData.length) : null;
  const avgOf = k => { const v = withData.filter(d => d[k]?.in != null); return v.length ? Math.round(v.reduce((a, d) => a + d[k].in, 0) / v.length) : null; };
  box.innerHTML = `<div class="row"><h3 class="sp" style="margin:0">${ico('chart')} Zeit im Zielbereich</h3>${[14, 30, 90].map(n => `<button class="chip ${statDays === n ? 'on' : ''}" data-sd="${n}">${n} Tage</button>`).join('')}</div>
    <p class="hint">Je Tag, wie viel Prozent der Zeit die Werte in Soll ± Hysterese lagen (Tag/Nacht getrennt, gemessen an den aktuellen Sollwerten). <span class="stleg"><i class="in"></i>im Ziel <i class="hi"></i>zu hoch <i class="lo"></i>zu niedrig</span> Die Zahl ist „% im Ziel“, klein daneben der größere Rest (↓ zu niedrig / ↑ zu hoch).${avg != null ? ` Durchschnitt „alle gleichzeitig im Ziel“: <b style="color:var(${col(avg)})">${avg} %</b>.` : ''}</p>
    <div class="sttab" style="--cols:52px ${'1fr '.repeat(keys.length)}52px"><div class="sthead"><span>Tag</span>${keys.map(k => { const a = avgOf(k[0]); return `<span>${k[1]}${a != null ? ` <b style="color:var(${col(a)})">Ø ${a} %</b>` : ''}</span>`; }).join('')}<span>alle</span></div>
    ${days.map(d => `<div class="strow"><span class="sub">${d.day.slice(8, 10)}.${d.day.slice(5, 7)}.</span>${keys.map(k => bar(d[k[0]])).join('')}<b style="color:var(${col(d.all)})">${d.all == null ? '–' : d.all + ' %'}</b></div>`).join('')}</div>`;
}
document.addEventListener('click', e => { const b = e.target.closest('[data-sd]'); if (b) { statDays = +b.dataset.sd; loadStats(); } });

// ---- Energie: Verbrauch je Gerät aus Laufzeit × Leistung (Abluft gemessen) ----
async function loadEnergy() {
  try { enData = await api('/api/energy?days=' + enDays); } catch (e) { return; }
  renderEnergy();
}
function renderEnergy() {
  const box = $('#energyCard'); if (!box || !enData) return;
  const fmt = (v, d = 2) => v == null ? '–' : v.toFixed(d).replace('.', ',');
  const sum = a => a.reduce((x, y) => x + (y || 0), 0), n = enData.days.length, price = enData.price_ct / 100;
  const devs = enData.devices.filter(d => sum(d.kwh) > 0 || (d.on_min && sum(d.on_min) > 0)).sort((a, b) => sum(b.kwh) - sum(a.kwh));
  const total = sum(enData.total_kwh), perDay = total / n, unknown = devs.filter(d => d.w == null && d.source !== 'gemessen');
  const max = Math.max(...devs.map(d => sum(d.kwh)), .001);
  box.innerHTML = `<div class="row"><h3 class="sp" style="margin:0">${ico('flame')} Energie</h3>${[7, 30].map(k => `<button class="chip ${enDays === k ? 'on' : ''}" data-ed="${k}">${k} Tage</button>`).join('')}</div>
    <div class="row" style="gap:22px;margin:10px 0 4px;flex-wrap:wrap">
      <div><div class="sub">Ø pro Tag</div><b style="font-size:22px">${fmt(perDay)} kWh</b> <span class="sub">≈ ${fmt(perDay * price)} €</span></div>
      <div><div class="sub">hochgerechnet pro Monat</div><b style="font-size:22px">${fmt(perDay * 30 * price)} €</b></div>
      <div><div class="sub">Strompreis</div><div class="ig" style="width:140px"><input id="enPrice" type="number" step="0.1" min="0" max="200" value="${enData.price_ct}"><span>ct/kWh</span></div></div></div>
    <p class="hint">Laufzeit × Leistung je Gerät, die Abluft gemessen. „Annahme“ ist ein Richtwert – trag die echte Leistung ein, wo du sie kennst${unknown.length ? `; <b style="color:var(--warn)">ohne Leistung: ${unknown.map(d => esc(LABEL[d.name] || d.name)).join(', ')}</b>` : ''}. Lampen zählen mit voller Leistung (Dimmung noch nicht berücksichtigt).</p>
    <div class="etable">${devs.map(d => { const k = sum(d.kwh); return `<div class="erow"><span>${esc(LABEL[d.name] || d.name)}</span>
      <div class="ebar"><i style="width:${k / max * 100}%"></i></div><b>${fmt(k)} kWh</b><span class="sub">${fmt(k * price)} €</span>
      ${d.source === 'gemessen' ? '<span class="sub">gemessen</span>' : `<div class="ig"><input data-pw="${esc(d.name)}" type="number" min="0" max="5000" step="1" value="${d.w ?? ''}" placeholder="?"><span>W</span></div>`}
      <span class="sub">${d.source === 'Annahme' ? 'Annahme' : d.source === 'eingetragen' ? 'eingetragen' : d.source === 'config' ? 'config' : ''}</span></div>`; }).join('')}</div>
    <div class="actions" style="margin-top:12px"><button class="pri" id="enSave">Speichern</button></div>`;
}
document.addEventListener('click', async e => {
  const b = e.target.closest('[data-ed]'); if (b) { enDays = +b.dataset.ed; loadEnergy(); return; }
  if (e.target.id === 'enSave') {
    const power = {}; document.querySelectorAll('[data-pw]').forEach(i => power[i.dataset.pw] = i.value === '' ? null : +i.value);
    try { enData = await api('/api/energy', {price_ct: +$('#enPrice').value, power, days: enDays}); renderEnergy(); toast('Gespeichert ✓'); }
    catch (er) { toast(er.data?.error || er.message, 1); }
  }
});

// ---- Sicherung / Wiederherstellung ----
function renderBackupCard() {
  const box = $('#backupCard'); if (!box || box.dataset.done) return;
  box.dataset.done = 1;
  box.innerHTML = `<h3>Sicherung</h3><p class="hint">Einstellungen, Zyklen, Geräte (config.json) und Zustand als eine Datei. Passwörter aus <code>.env</code> sind nicht enthalten.</p>
    <div class="row" style="gap:8px"><button class="sec" id="bkDown">Herunterladen</button><button class="sec" id="bkUp">Wiederherstellen …</button>
    <input type="file" id="bkFile" accept=".json,application/json" hidden></div>`;
}
document.addEventListener('click', async e => {
  if (e.target.id === 'bkDown') {
    try {
      const r = await fetch('/api/backup', {headers: {'X-Token': token}}); if (!r.ok) throw new Error((await r.json()).error || 'Fehler');
      const a = document.createElement('a'); a.href = URL.createObjectURL(await r.blob());
      a.download = `growcontroller-backup-${new Date().toISOString().slice(0, 10)}.json`; a.click(); URL.revokeObjectURL(a.href);
    } catch (er) { toast(er.message, 1); }
  }
  if (e.target.id === 'bkUp') $('#bkFile').click();
});
document.addEventListener('change', async e => {
  if (e.target.id !== 'bkFile' || !e.target.files[0]) return;
  const f = e.target.files[0]; e.target.value = '';
  let b; try { b = JSON.parse(await f.text()); } catch (er) { return toast('Datei ist kein gültiges JSON', 1); }
  if (!confirm(`Sicherung vom ${String(b.created || '?').replace('T', ' ')} wiederherstellen?\nAktuelle Einstellungen und Geräte werden ersetzt (die alten Dateien bleiben als .bak erhalten). Der Controller startet danach neu.`)) return;
  try { await api('/api/restore', b); toast('Wiederhergestellt – Neustart läuft …'); waitRestart(); }
  catch (er) { toast(er.data?.error || er.message, 1); }
});
function waitRestart() {   // nach ca. 5 s neu laden, sobald der Dienst wieder antwortet
  let n = 0; const t = setInterval(async () => {
    if (++n > 40) return clearInterval(t);
    try { const r = await fetch('/api/status'); if (r.ok && n > 2) { clearInterval(t); location.reload(); } } catch (e) {}
  }, 1500);
}

// ---- Geräte-Editor ----
let devData = null, devDirty = false;
const DEV_SEC = {outputs: ['Ausgänge', 'type'], sensors: ['Sensoren', 'driver']};
async function loadDevices() {
  try { devData = await api('/api/devices'); devDirty = false; } catch (e) { devData = {error: e.message}; }
  renderDevices();
}
function devInput(sec, n, k, v) {
  const at = `data-ds="${sec}" data-dn="${esc(n)}" data-dk="${esc(k)}"`;
  if (typeof v === 'boolean') return `<div class="ig"><select ${at} data-dt="bool"><option value="true" ${v ? 'selected' : ''}>ja</option><option value="false" ${v ? '' : 'selected'}>nein</option></select></div>`;
  if (typeof v === 'number') return `<div class="ig"><input ${at} data-dt="num" type="number" step="any" value="${v}"></div>`;
  if (v !== null && typeof v === 'object') return `<div class="ig"><input ${at} data-dt="json" value="${esc(JSON.stringify(v))}"></div>`;
  const secret = v === devData.mask;
  return `<div class="ig"><input ${at} data-dt="str" value="${esc(v ?? '')}" ${secret ? 'placeholder="unverändert" title="Gespeicherter geheimer Wert – leer lassen bzw. ••••, um ihn zu behalten"' : ''}></div>`;
}
function renderDevices() {
  const box = $('#devEditor'); if (!box) return;
  if (!devData) { box.innerHTML = '<h3>Geräte</h3><div class="sub">wird geladen …</div>'; return; }
  if (devData.error) { box.innerHTML = `<h3>Geräte</h3><div class="sub">${esc(devData.error)}</div>`; return; }
  box.innerHTML = `<div class="row"><h3 class="sp" style="margin:0">Geräte</h3>${devDirty ? '<span class="sub" style="color:var(--warn)">nicht gespeichert</span>' : ''}</div>
    <p class="hint" style="margin-top:6px">Ausgänge und Sensoren aus der config.json. Änderungen werden erst nach einem Neustart aktiv; vor jedem Speichern wird eine Sicherungskopie angelegt.
      Passwörter zeigt die Oberfläche nur als ${devData.mask}; besser <code>\${NAME}</code> aus der .env verwenden.</p>
    ${Object.entries(DEV_SEC).map(([sec, [title, tk]]) => `<h4 style="margin:16px 0 8px">${title}</h4><div class="devlist">
      ${Object.entries(devData[sec]).map(([n, c]) => `<div class="devc"><div class="row"><b class="sp">${esc(LABEL[n] || n)}</b><span class="sub">${esc(n)} · ${esc(c[tk] || (sec === 'outputs' ? 'gpio' : '?'))}</span>
          <button class="chip" data-ddel="${sec}|${esc(n)}" title="Gerät entfernen">✕</button></div>
        <div class="devf">${Object.entries(c).filter(([k]) => k !== tk).map(([k, v]) => `<label class="sub" title="${esc(k)}">${esc(k)}</label>${devInput(sec, n, k, v)}<button class="chip" data-dfdel="${sec}|${esc(n)}|${esc(k)}" title="Feld entfernen">−</button>`).join('')}</div>
        <div class="row" style="gap:6px;margin-top:8px"><div class="ig" style="flex:1"><input data-dnewk="${sec}|${esc(n)}" placeholder="neues Feld, z. B. power_w"></div><button class="chip" data-dfadd="${sec}|${esc(n)}">+ Feld</button></div></div>`).join('')}
      </div>
      <div class="row" style="gap:6px;margin-top:8px"><div class="ig"><select id="dnt-${sec}">${Object.keys(devData.types[sec]).map(t => `<option>${t}</option>`).join('')}</select></div>
        <div class="ig" style="flex:1"><input id="dnn-${sec}" placeholder="Name (a–z, 0–9, _)"></div><button class="chip" data-dadd="${sec}">+ ${sec === 'outputs' ? 'Ausgang' : 'Sensor'}</button></div>`).join('')}
    <div class="actions" style="margin-top:14px"><button class="sec" id="devReload">Verwerfen</button><button class="sec" id="devRestart">Neu starten</button><button class="pri" id="devSave">Speichern</button></div>`;
}
document.addEventListener('input', e => {
  const i = e.target.closest('[data-ds]'); if (!i || !devData) return;
  const c = devData[i.dataset.ds][i.dataset.dn], t = i.dataset.dt, v = i.value;
  if (t === 'bool') c[i.dataset.dk] = v === 'true';
  else if (t === 'num') c[i.dataset.dk] = v === '' ? null : +v;
  else if (t === 'json') { try { c[i.dataset.dk] = JSON.parse(v); i.closest('.ig').style.borderColor = ''; } catch (er) { i.closest('.ig').style.borderColor = 'var(--bad)'; } }
  else c[i.dataset.dk] = v === '' && i.placeholder === 'unverändert' ? devData.mask : v;
  devDirty = true;
});
document.addEventListener('change', e => { if (e.target.closest('[data-ds]')) e.target.dispatchEvent(new Event('input', {bubbles: true})); });
document.addEventListener('click', async e => {
  if (!devData || !e.target.closest('#devEditor')) return;
  const t = e.target.closest('button'); if (!t) return;
  const d = t.dataset;
  if (d.ddel) { const [s, n] = d.ddel.split('|'); if (!confirm(`„${n}“ entfernen?`)) return; delete devData[s][n]; devDirty = true; renderDevices(); }
  else if (d.dfdel) { const [s, n, k] = d.dfdel.split('|'); delete devData[s][n][k]; devDirty = true; renderDevices(); }
  else if (d.dfadd) {
    const [s, n] = d.dfadd.split('|'), k = document.querySelector(`[data-dnewk="${d.dfadd}"]`).value.trim();
    if (!/^[a-z0-9_]+$/.test(k)) return toast('Feldname nur a–z, 0–9, _', 1);
    if (!(k in devData[s][n])) devData[s][n][k] = ''; devDirty = true; renderDevices();
  } else if (d.dadd) {
    const s = d.dadd, n = $('#dnn-' + s).value.trim(), typ = $('#dnt-' + s).value;
    if (!/^[a-z0-9_]{1,40}$/.test(n)) return toast('Name nur a–z, 0–9, _', 1);
    if (devData.outputs[n] || devData.sensors[n]) return toast('Name ist schon vergeben', 1);
    devData[s][n] = {[DEV_SEC[s][1]]: typ, ...JSON.parse(JSON.stringify(devData.types[s][typ]))}; devDirty = true; renderDevices();
  } else if (t.id === 'devReload') loadDevices();
  else if (t.id === 'devSave') {
    try { await api('/api/devices', {outputs: devData.outputs, sensors: devData.sensors}); devDirty = false; renderDevices(); toast('Gespeichert – aktiv nach „Neu starten“'); }
    catch (er) { toast(er.data?.error || er.message, 1); }
  } else if (t.id === 'devRestart') {
    if (devDirty && !confirm('Es gibt ungespeicherte Änderungen. Trotzdem neu starten?')) return;
    if (!confirm('Controller neu starten? Alle Ausgänge gehen kurz in den sicheren Zustand.')) return;
    try { await api('/api/restart', {}); toast('Neustart läuft …'); waitRestart(); } catch (er) { toast(er.message, 1); }
  }
});
function loadSystemExtra() { renderBackupCard(); if (!devDirty) loadDevices(); }

function renderExtra() {
  renderHealth();
  loadReport(false);
  if ($('#tab-analysis.on')) drawVpdMatrix();   // aktueller Punkt wandert mit
  for (const f of (window.extraRenderers || [])) { try { f(); } catch (e) { console.error(e); } }
}
// beim direkten Aufruf mit #system/#analysis lief showTab schon, bevor diese Datei geladen war
if (location.hash === '#system') loadSystemExtra();
if (location.hash === '#analysis') loadAnalysis();
