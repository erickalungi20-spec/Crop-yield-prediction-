// CropSense AI — web form logic. Calls the same engine as the USSD
// line via /api/advisory and /api/rank (see app.py).

const form         = document.getElementById('advisory-form');
const resultEl      = document.getElementById('result');
const errorEl       = document.getElementById('error');
const submitBtn     = document.getElementById('submit-btn');
const districtSel   = document.getElementById('district');
const cropSel       = document.getElementById('crop_key');
const purposeInput  = document.getElementById('purpose');
const plantedToday  = document.getElementById('planted-today');
const dateInput     = document.getElementById('planting_date');

let mode = 'advisory'; // 'advisory' | 'rank'

// ── Populate district + crop dropdowns from the backend's own data ──────────
async function loadMeta() {
  try {
    const res = await fetch('/api/meta');
    const data = await res.json();

    data.districts.forEach(d => {
      const opt = document.createElement('option');
      opt.value = d;
      opt.textContent = d.replace(/\b\w/g, c => c.toUpperCase());
      districtSel.appendChild(opt);
    });

    data.crops.forEach(c => {
      const opt = document.createElement('option');
      opt.value = c.key;
      opt.textContent = c.display;
      cropSel.appendChild(opt);
    });
  } catch (e) {
    showError('Could not load district/crop list. Check your connection and reload.');
  }
}
loadMeta();

// ── Mode switch ──────────────────────────────────────────────────────────────
document.querySelectorAll('.mode-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.mode-btn').forEach(b => {
      b.classList.remove('active');
      b.setAttribute('aria-selected', 'false');
    });
    btn.classList.add('active');
    btn.setAttribute('aria-selected', 'true');
    mode = btn.dataset.mode;

    const cropOnlyFields = ['crop-field', 'area-field', 'purpose-field', 'date-field', 'phone-field'];
    cropOnlyFields.forEach(id => {
      document.getElementById(id).hidden = (mode === 'rank');
    });
    document.getElementById('crop_key').required = (mode === 'advisory');

    submitBtn.textContent = mode === 'rank' ? 'Rank crops for my district' : 'Get advisory';
    resultEl.hidden = true;
    errorEl.hidden = true;
  });
});

// ── Purpose pill group ───────────────────────────────────────────────────────
document.querySelectorAll('#purpose-field .pill').forEach(p => {
  p.addEventListener('click', () => {
    document.querySelectorAll('#purpose-field .pill').forEach(x => x.classList.remove('active'));
    p.classList.add('active');
    purposeInput.value = p.dataset.value;
  });
});

// ── Planted-today toggle disables the date picker ────────────────────────────
plantedToday.addEventListener('change', () => {
  dateInput.disabled = plantedToday.checked;
  if (plantedToday.checked) dateInput.value = '';
});
dateInput.disabled = plantedToday.checked;

// ── Submit ────────────────────────────────────────────────────────────────────
form.addEventListener('submit', async (e) => {
  e.preventDefault();
  resultEl.hidden = true;
  errorEl.hidden = true;
  submitBtn.disabled = true;
  submitBtn.textContent = 'Fetching live data…';

  try {
    if (mode === 'rank') {
      await runRank();
    } else {
      await runAdvisory();
    }
  } catch (err) {
    showError('Something went wrong reaching the server. Please try again.');
  } finally {
    submitBtn.disabled = false;
    submitBtn.textContent = mode === 'rank' ? 'Rank crops for my district' : 'Get advisory';
  }
});

async function runAdvisory() {
  const district = districtSel.value;
  const crop_key = cropSel.value;
  const area_acres = document.getElementById('area_acres').value;
  const purpose = purposeInput.value;
  const phone = document.getElementById('phone').value.trim();
  const planting_date = plantedToday.checked ? '' : dateInput.value;

  const res = await fetch('/api/advisory', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ district, crop_key, area_acres, purpose, planting_date, phone }),
  });
  const data = await res.json();

  if (!res.ok) {
    showError(data.error || 'Could not generate advisory for that input.');
    return;
  }
  renderAdvisory(data);
}

async function runRank() {
  const district = districtSel.value;
  if (!district) {
    showError('Select a district first.');
    return;
  }
  const res = await fetch('/api/rank', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ district }),
  });
  const data = await res.json();

  if (!res.ok) {
    showError(data.error || 'Could not rank crops for that district.');
    return;
  }
  renderRank(data);
}

function showError(msg) {
  errorEl.textContent = msg;
  errorEl.hidden = false;
}

function renderAdvisory(data) {
  const limiting = Object.entries(data.explanation)
    .filter(([, v]) => v.impact === 'limiting')
    .sort((a, b) => b[1].share_of_penalty_pct - a[1].share_of_penalty_pct);
  const optimal = Object.entries(data.explanation)
    .filter(([, v]) => v.impact === 'optimal');

  const factorRows = [...limiting, ...optimal].map(([name, v]) => `
    <div class="factor-row ${v.impact}">
      <span class="factor-name">${name.replace('_', ' ')}</span>
      <span class="factor-share">${v.impact === 'limiting' ? v.share_of_penalty_pct + '% of shortfall' : 'optimal'}</span>
    </div>`).join('');

  const riskHtml = data.risks.length
    ? data.risks.map(r => `
        <div class="risk-chip">
          <span class="risk-name">${r.name}</span>
          <span class="risk-pct"> · ~${r.risk_pct}% elevated risk</span>
          <div class="risk-advice">${r.advice}</div>
        </div>`).join('')
    : `<div class="no-risk">No elevated disease/pest risk detected right now.</div>`;

  let sourceNote = '';
  if (data.used_sensor_reading) {
    sourceNote = `<div class="source-note sensor">✓ Boosted by your ESP32 soil sensor reading</div>`;
  } else if (!data.data_sources_live.weather || !data.data_sources_live.soil) {
    sourceNote = `<div class="source-note">Some data estimated — low live-signal area</div>`;
  } else {
    sourceNote = `<div class="source-note">Live weather + soil data</div>`;
  }

  resultEl.innerHTML = `
    <div class="result-header">
      <span class="result-crop">${data.crop}</span>
      <span class="result-district">${data.district}</span>
    </div>

    <div class="gauge-wrap">
      <div class="gauge" style="--pct:${data.confidence_pct}">
        <span class="gauge-value">${data.confidence_pct}%</span>
      </div>
      <div class="gauge-label">
        Prediction confidence — reflects data quality and how close conditions are to <strong>${data.crop}</strong>'s optimal range.
      </div>
    </div>

    <div class="yield-block">
      <span class="yield-number">${data.yield_per_acre.toLocaleString()}</span>
      <span class="yield-unit">kg/acre &nbsp;·&nbsp; ${data.total_yield.toLocaleString()}kg total</span>
      <div class="yield-range">Expected range: ${data.yield_range[0].toLocaleString()}–${data.yield_range[1].toLocaleString()}kg/acre</div>
    </div>

    <div class="factor-list">${factorRows}</div>

    <div class="stage-block">
      <div class="stage-title">${data.growth_stage} &nbsp;·&nbsp; harvest in ~${data.days_to_harvest} days</div>
      <div class="stage-advice">${data.stage_advice}</div>
      <div class="kernel-row">${kernelBar(data.days_to_harvest)}</div>
    </div>

    <div class="risk-list">${riskHtml}</div>

    <div class="tip-block">
      <span class="tip-label">Harvest tip:</span> ${data.harvest_tip}
    </div>

    ${sourceNote}
  `;
  resultEl.hidden = false;
  resultEl.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function kernelBar(daysRemaining, totalSegments = 10) {
  // Approximate progress bar: fewer days remaining -> more filled segments.
  // Purely visual pacing indicator, not a precise stage map.
  const filledEstimate = Math.max(0, totalSegments - Math.round((daysRemaining / 10)));
  const filled = Math.min(totalSegments, Math.max(1, filledEstimate));
  return Array.from({ length: totalSegments }, (_, i) =>
    `<div class="kernel ${i < filled ? 'filled' : ''}"></div>`
  ).join('');
}

function renderRank(data) {
  const rows = data.ranking.map((c, i) => `
    <div class="rank-row">
      <span class="rank-pos">${String(i + 1).padStart(2, '0')}</span>
      <span class="rank-name">${c.display}</span>
      <span class="rank-score">${c.suitability}%</span>
    </div>`).join('');

  resultEl.innerHTML = `
    <div class="result-header">
      <span class="result-crop">Best crops for you</span>
      <span class="result-district">${data.district}</span>
    </div>
    <div class="rank-list">${rows}</div>
  `;
  resultEl.hidden = false;
  resultEl.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}
