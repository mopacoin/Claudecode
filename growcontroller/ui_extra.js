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

function renderExtra() {
  renderHealth();
  loadReport(false);
  for (const f of (window.extraRenderers || [])) { try { f(); } catch (e) { console.error(e); } }
}
