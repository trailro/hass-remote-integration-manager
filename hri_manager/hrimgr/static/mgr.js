'use strict';
// HRI Manager's page.  Every URL is relative: Home Assistant's ingress serves the page under a prefix it never sees.
// The one exception is panelHref(): an instance's own sidebar panel, a page of Home Assistant itself (top window).
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
let releases = [], latest = null, instances = [], watching = null;

async function answer(r) {
  const text = await r.text();
  try { return JSON.parse(text); } catch (e) { return {ok: false, error: `HTTP ${r.status}: ${text.trim().slice(0, 200) || r.statusText}`}; }
}
async function get(path) {
  try { return await answer(await fetch(path, {cache: 'no-store'})); } catch (e) { return {ok: false, error: String(e)}; }
}
async function send(method, path, body) {
  try {
    return await answer(await fetch(path, {method, headers: {'content-type': 'application/json', 'X-Requested-With': 'fetch'}, body: JSON.stringify(body || {})}));
  } catch (e) { return {ok: false, error: String(e)}; }
}
function flash(msg, kind) {
  const el = $('#flash'); el.textContent = msg || ''; el.className = 'flash' + (kind ? ' ' + kind : '');
  clearTimeout(el._t); if (msg) el._t = setTimeout(() => { el.textContent = ''; }, 12000);
}
const vparts = v => { const m = String(v || '').match(/^(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?$/); return m ? [+m[1], +m[2], +m[3], m[4] ? 0 : 1, m[4] ? ({a: 1, b: 2, rc: 3}[m[4]] * 10000 + +m[5]) : 0] : null; };
const vcmp = (a, b) => { const x = vparts(a) || [], y = vparts(b) || []; for (let i = 0; i < 5; i++) { const d = (x[i] || 0) - (y[i] || 0); if (d) return d; } return 0; };

function panelHref(i) {
  // Home Assistant registers an app's sidebar panel at /<slug>
  if (i.ingress_panel && /^(local_hri_[a-z][a-z0-9_]{0,19}|[a-z0-9]{1,16}_hass_remote_integration)$/.test(i.slug || '')) return {href: '/' + i.slug, target: '_top'};
  if (/^\/api\/hassio_ingress\/[A-Za-z0-9_-]+\/$/.test(i.ingress_url || '')) return {href: i.ingress_url, target: '_blank'};
  return null;
}

function chip(label, value, cls) { return `<span class="chip ${cls || ''}"><span class="dot"></span>${esc(label)} <b>${esc(value)}</b></span>`; }

async function loadStatus() {
  const s = await get('api/status');
  if (!s.ok) { flash(s.error, 'err'); return; }
  $('#ver').textContent = 'v' + s.version + (s.dev ? ' · dev' : '');
  $('#tb-chips').innerHTML = [
    chip('Supervisor', s.supervisor || '?', s.supervisor ? 'ok' : 'warn'),
    chip('Core', s.homeassistant || '?', s.homeassistant ? 'ok' : 'warn'),
    chip('local apps', s.map_ok ? 'ok' : 'missing', s.map_ok ? 'ok' : 'bad'),
    chip('role', s.role || '?', s.role_ok ? 'ok' : 'bad'),
  ].join('');
  const box = $('#problems');
  box.hidden = !(s.problems && s.problems.length);
  box.innerHTML = box.hidden ? '' : '<h2>Problems</h2>' + s.problems.map(p => `<div class="bad">${esc(p)}</div>`).join('');
}

async function loadReleases(refresh) {
  const r = await get('api/releases' + (refresh ? '?refresh=1' : ''));
  const sel = $('#c-version');
  if (!r.ok) { sel.innerHTML = '<option value="">GitHub unreachable</option>'; flash('Releases: ' + r.error, 'err'); return; }
  releases = r.releases || []; latest = r.latest;
  sel.innerHTML = releases.length ? releases.map(x => `<option value="${esc(x.version)}"${x.version === latest ? ' selected' : ''}>${esc(x.version)}${x.prerelease ? ' (pre-release)' : ''}${x.version === latest ? ' (latest)' : ''}</option>`).join('')
    : '<option value="">no release 0.25.0 or newer</option>';
}

function stateCell(i) {
  if (!i.installed) return '<span class="state unknown"><span class="dot"></span>not installed</span>';
  const st = i.state || 'unknown';
  return `<span class="state ${esc(st)}"><span class="dot"></span>${esc(st)}</span>`;
}
function versionCell(i) {
  const shown = i.installed_version || i.version || '?';
  let out = esc(shown);
  if (i.newer_release) out += ` <span class="tag acc" title="a newer HRI release">${esc(i.newer_release)} available</span>`;
  else if (i.update_available) out += ' <span class="tag warn" title="the Supervisor offers the definition in the local apps folder, another version than the installed one (newer or older): use the manager\'s Update, never the Update of the app\'s own page">definition differs</span>';
  return out;
}
function channelCell(i) {
  const bt = i.bluetooth ? ' <span class="tag acc" title="the instance has the host\'s D-Bus (host_dbus), for Bluetooth">Bluetooth</span>' : '';
  if (i.channel === 'git') return `<span class="tag warn" title="testing build from source">git · testing</span>${bt}<span class="sub">${esc(i.ref_kind)} ${esc(i.ref)} @ ${esc((i.sha || '').slice(0, 12))}</span>`;
  if (i.channel === 'release') return '<span class="tag">release</span>' + bt;
  return '<span class="tag bad">unknown</span>' + bt;
}
function autoNote(i) {
  // the instance's last automatic repair (a restore left it detached): running, done, or failed and retried
  // needs attention: never retried automatically; its problem line gives the reason and the actions
  const a = i.auto_repair;
  if (!a || i.needs_attention) return '';
  if (a.state === 'running') return '<span class="problem">installed but detached (a restore without the local apps folder?): its definition is being written again automatically</span>';
  if (a.state === 'succeeded') return `<span class="sub">definition written again automatically at ${esc(a.at)}</span>`;
  const next = a.next_try_in ? `, next try in about ${Math.max(1, Math.round(a.next_try_in / 60))} min` : '';
  const count = a.failures > 1 ? ` (${a.failures} times)` : '';
  return `<span class="problem">automatic repair failed${esc(count)}: ${esc(a.error)}${esc(next)}. Repair tries now.</span>`;
}
const LABELS = {start: 'Start', stop: 'Stop', restart: 'Restart', update: 'Update', delete: 'Delete', repair: 'Repair', install: 'Install', finish: 'Finish setup'};
function actionsCell(i) {
  const busy = i.job && i.job.state === 'running';
  const link = panelHref(i);
  let out = link ? `<a class="btn" href="${esc(link.href)}" target="${link.target}" rel="noopener">Open</a>` : '';
  if (busy) return out + ` <button data-job="${esc(i.job.id)}">${esc(i.job.action)}…</button>`;
  for (const a of i.actions || []) {
    const label = (i.labels || {})[a] || (a === 'update' && i.channel === 'git' ? 'Rebuild' : LABELS[a] || a);
    out += `<button data-act="${esc(a)}" data-name="${esc(i.name)}"${a === 'delete' ? ' class="danger"' : ''}>${esc(label)}</button>`;
  }
  return out;
}

async function loadInstances() {
  const r = await get('api/instances');
  if (!r.ok) { flash('Instances: ' + r.error, 'err'); return; }
  instances = r.instances || [];
  const body = $('#inst tbody');
  body.innerHTML = instances.map(i => `<tr><td><b>${esc(i.name)}</b><span class="sub">${esc(i.slug)}</span>${i.problem ? `<span class="problem">${esc(i.problem)}</span>` : ''}${autoNote(i)}</td>`
    + `<td>${stateCell(i)}</td><td>${versionCell(i)}</td><td>${channelCell(i)}</td><td class="act">${actionsCell(i)}</td></tr>`).join('');
  $('#empty').hidden = instances.length > 0;
  const others = r.others || [];
  $('#otherscard').hidden = !others.length;
  $('#others tbody').innerHTML = others.map(o => {
    const note = o.kind === 'published' ? 'the published single app' : o.kind === 'local_build' ? 'a local build of HRI' : o.problem || '';
    const link = o.kind === 'published' || o.kind === 'local_build' ? panelHref({slug: o.slug, ingress_panel: true}) : null;
    return `<tr><td><b>${esc(o.name || o.slug)}</b><span class="sub">${esc(o.slug)}</span></td><td>${o.installed ? stateCell(o) : '—'}</td>`
      + `<td>${esc(o.version || '')}${o.update_available ? ' <span class="tag acc">update</span>' : ''}</td><td class="mut">${esc(note)}</td><td class="act">${link ? `<a class="btn" href="${esc(link.href)}" target="${link.target}" rel="noopener">Open</a>` : ''}`
      + `${(o.actions || []).includes('forget') ? `<button data-forget="${esc(o.instance)}" class="danger">Forget</button>` : ''}</td></tr>`;
  }).join('');
  $('#others tbody').querySelectorAll('button[data-forget]').forEach(b => { b.onclick = () => forget(b.dataset.forget); });
  body.querySelectorAll('button[data-act]').forEach(b => { b.onclick = () => act(b.dataset.act, b.dataset.name); });
  body.querySelectorAll('button[data-job]').forEach(b => { b.onclick = () => watch(b.dataset.job); });
}

function dialog({title, text, ok, danger, versions, selected, ref, refKind, data, name, bluetooth}) {
  const d = $('#confirm');
  $('#cf-title').textContent = title; $('#cf-text').textContent = text;
  const okb = $('#cf-ok'); okb.textContent = ok; okb.className = danger ? 'danger' : 'primary';
  $('#cf-version-row').hidden = !versions;
  if (versions) $('#cf-version').innerHTML = versions.map(v => `<option value="${esc(v.version)}"${v.version === selected ? ' selected' : ''}>${esc(v.version)}${v.prerelease ? ' (pre-release)' : ''}${v.version === latest ? ' (latest)' : ''}</option>`).join('');
  $('#cf-ref-row').hidden = ref === undefined; $('#cf-ref').value = ref || '';
  $('#cf-kind-row').hidden = ref === undefined; $('#cf-kind').value = refKind || 'branch';
  $('#cf-data-row').hidden = !data; $('#cf-data').checked = false;
  $('#cf-bt-row').hidden = bluetooth === undefined; $('#cf-bt').checked = !!bluetooth;
  $('#cf-name-row').hidden = !name; $('#cf-name').value = ''; $('#cf-name-hint').textContent = name || '';
  const sync = () => { okb.disabled = !!name && $('#cf-name').value !== name; };
  $('#cf-data').onchange = sync; $('#cf-name').oninput = sync; sync();
  d.returnValue = '';
  return new Promise(resolve => {
    d.onclose = () => resolve(d.returnValue === 'ok' ? {version: $('#cf-version').value, ref: $('#cf-ref').value.trim(), refKind: $('#cf-kind').value, removeData: $('#cf-data').checked, confirm: $('#cf-name').value, bluetooth: $('#cf-bt').checked} : null);
    d.showModal();
  });
}

async function act(action, name) {
  const i = instances.find(x => x.name === name) || {name};
  let r;
  if (action === 'update') {
    if (i.channel === 'git') {
      const c = await dialog({title: `Rebuild ${name}`, text: 'Downloads the branch or tag of hass-remote-integration again and, when its commit changed, builds and runs that code on this machine. For testing only. The app restarts; its data stays. Bluetooth changes only with a new commit.', ok: 'Rebuild', ref: i.ref || '', refKind: i.ref_kind, bluetooth: !!i.bluetooth});
      if (!c) return;
      r = await send('POST', `api/instances/${encodeURIComponent(name)}/update`, {...(c.ref ? {ref_kind: c.refKind, ref: c.ref} : {}), bluetooth: c.bluetooth});
    } else {
      // the newer of the definition and the installed app: the manager does not downgrade either
      const floor = vcmp(i.installed_version, i.version) > 0 ? i.installed_version : i.version;
      // an instance without its definition (needs attention): only newer releases; the installed one is Repair's
      const choices = releases.filter(x => i.managed ? vcmp(x.version, floor) >= 0 : vcmp(x.version, floor) > 0);
      if (!choices.length) { flash('No release at or above ' + floor + ' is known yet.', 'err'); return; }
      const c = await dialog({title: `Update ${name}`, text: `From ${i.installed_version || i.version}. The Supervisor pulls the new image and restarts the app; its options and data stay. Bluetooth changes only with a newer version.`, ok: 'Update', versions: choices, selected: i.newer_release || latest, bluetooth: !!i.bluetooth});
      if (!c) return;
      r = await send('POST', `api/instances/${encodeURIComponent(name)}/update`, {version: c.version, bluetooth: c.bluetooth});
    }
  } else if (action === 'delete') {
    const c = await dialog({title: `Delete ${name}`, text: `Stops and uninstalls ${i.slug} and removes its definition. The Supervisor always removes the instance's options (its password, ingress_users). Without the box below only its /config folder (its Home Assistant, integration and configuration) is kept, and a new instance named ${name} would reuse that folder, without those options.`, ok: 'Delete', danger: true, data: true, name});
    if (!c) return;
    r = await send('DELETE', `api/instances/${encodeURIComponent(name)}`, {remove_data: c.removeData, confirm: c.confirm});
  } else if (action === 'repair') {
    const c = await dialog({title: `Repair ${name}`, text: 'Writes the definition folder again for the installed version, so the app can be updated and managed again.', ok: 'Repair'});
    if (!c) return;
    r = await send('POST', `api/instances/${encodeURIComponent(name)}/repair`, {});
  } else {
    r = await send('POST', `api/instances/${encodeURIComponent(name)}/${action}`, {});
  }
  if (!r.ok) { flash(r.error, 'err'); return; }
  watch(r.job.id);
  loadInstances();
}

async function forget(name) {
  const c = await dialog({title: `Forget ${name}`, text: `${name} is neither installed nor defined. Forget drops the manager's registry entry and its copy of the definition; nothing else is touched.`, ok: 'Forget', danger: true, name});
  if (!c) return;
  const r = await send('POST', `api/instances/${encodeURIComponent(name)}/forget`, {confirm: c.confirm});
  if (!r.ok) { flash(r.error, 'err'); return; }
  watch(r.job.id);
  loadInstances();
}

function renderJob(job) {
  $('#jobcard').hidden = false;
  const cls = job.state === 'succeeded' ? 'ok' : job.state === 'failed' ? 'bad' : 'acc';
  $('#jobtitle').className = 'tag ' + cls;
  $('#jobtitle').textContent = `${job.action} ${job.instance} · ${job.state}`;
  const pre = $('#joblog');
  pre.textContent = job.lines.map(l => `${String(l.t.toFixed(1)).padStart(6)}s  ${l.msg}`).join('\n');
  pre.scrollTop = pre.scrollHeight;
}

async function watch(id) {
  watching = id;
  for (;;) {
    const r = await get(`api/jobs/${encodeURIComponent(id)}`);
    if (watching !== id) return;
    if (!r.ok) { flash(r.error, 'err'); return; }
    renderJob(r.job);
    if (r.job.state !== 'running') {
      const warning = r.job.state === 'succeeded' && r.job.result && r.job.result.warning;
      flash(warning ? `${r.job.action} ${r.job.instance}: ${warning}` : r.job.state === 'succeeded' ? `${r.job.action} ${r.job.instance}: done` : `${r.job.action} ${r.job.instance} failed: ${r.job.error}`, warning ? 'err' : r.job.state === 'succeeded' ? 'okmsg' : 'err');
      loadInstances();
      return;
    }
    await new Promise(res => setTimeout(res, 1000));
  }
}

function syncForm() {
  const git = $('#c-channel').value === 'git';
  $('#c-version-row').hidden = git; $('#c-ref-row').hidden = !git; $('#c-kind-row').hidden = !git; $('#c-git-note').hidden = !git;
  $('#c-slug').textContent = 'local_hri_' + ($('#c-name').value || '…');
}

$('#create').addEventListener('submit', async ev => {
  ev.preventDefault();
  const name = $('#c-name').value.trim(), channel = $('#c-channel').value;
  const bluetooth = $('#c-bt').checked;
  const body = channel === 'git' ? {name, channel, ref_kind: $('#c-kind').value, ref: $('#c-ref').value.trim(), bluetooth} : {name, channel, version: $('#c-version').value, bluetooth};
  const b = $('#c-go'); b.disabled = true;
  const r = await send('POST', 'api/instances', body);
  b.disabled = false;
  if (!r.ok) { flash(r.error, 'err'); return; }
  $('#c-name').value = ''; $('#c-bt').checked = false; syncForm();
  watch(r.job.id);
  loadInstances();
});
$('#c-channel').onchange = syncForm;
$('#c-name').oninput = syncForm;

(async () => {
  syncForm();
  await Promise.all([loadStatus(), loadInstances(), loadReleases(false)]);
  const j = await get('api/jobs');
  const running = (j.jobs || []).find(x => x.state === 'running');
  if (running) watch(running.id);
  setInterval(() => { if (!$('#confirm').open) loadInstances(); }, 15000);
})();
