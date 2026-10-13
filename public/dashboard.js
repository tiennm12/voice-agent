// The owner's dashboard: inbox of missed calls, statistics, settings.
// Rendering of a single call lives in call-view.js.

const PAGE_SIZE = 30;
const TEXT_SETTINGS = ['owner_name', 'greeting', 'system_doc'];

let calls = [];
let filter = '';
let openCallId = null;

async function api(path, { method = 'GET', body } = {}) {
  const response = await fetch(path, {
    method,
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (response.status === 401) {
    location.href = '/login'; // the session expired
    throw new Error('Phiên đăng nhập đã hết.');
  }
  if (!response.ok) throw new Error((await response.text()) || response.statusText);
  return response.json();
}

function setStatus(id, text, isError = false) {
  const status = document.getElementById(id);
  status.textContent = text;
  status.classList.toggle('error', isError);
}

// --- sections ---------------------------------------------------------------

const loaders = { inbox: () => loadCalls(), stats: loadStats, settings: loadSettings };

function showSection() {
  const current = location.hash.slice(1) in loaders ? location.hash.slice(1) : 'inbox';
  for (const name of Object.keys(loaders)) {
    document.getElementById(name).hidden = name !== current;
    const link = document.querySelector(`.topbar nav a[href="#${name}"]`);
    if (name === current) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  }
  loaders[current]();
}

// --- inbox ------------------------------------------------------------------

async function loadCalls({ append = false } = {}) {
  const params = new URLSearchParams({ limit: PAGE_SIZE });
  if (filter) params.set('filter', filter);
  if (append && calls.length) params.set('before', calls[calls.length - 1].started_at);
  try {
    const page = (await api(`/api/calls?${params}`)).calls;
    calls = append ? calls.concat(page) : page;
    renderCallList();
    document.getElementById('moreCalls').hidden = page.length < PAGE_SIZE;
    setStatus('inboxStatus', calls.length ? '' : 'Chưa có cuộc gọi nào.');
  } catch (err) {
    setStatus('inboxStatus', `Không tải được hộp thư: ${err.message}`, true);
  }
}

function renderCallList() {
  document.getElementById('calls').replaceChildren(...calls.map(callRow));
}

function callRow(call) {
  const row = element('button', 'call-row');
  row.classList.toggle('unread', !call.read);
  row.classList.toggle('spam', call.category === 'spam');
  if (call.id === openCallId) row.setAttribute('aria-current', 'true');

  const who = element('span', 'who', call.caller_name && call.caller_name !== 'Không rõ'
    ? `${call.caller_name} · ${call.caller || 'số không rõ'}`
    : call.caller || 'Số không rõ');
  if (!call.read) who.setAttribute('aria-label', `Chưa đọc: ${who.textContent}`);
  row.append(who, element('span', 'when', formatTime(call.started_at)));

  const snippet = element('span', 'snippet', call.summary || 'Đang tóm tắt…');
  if (call.urgency === 'cao' && call.category !== 'spam') snippet.prepend(element('span', 'badge urgent', 'Khẩn cấp'), ' ');
  row.append(snippet);

  row.addEventListener('click', () => openCall(call.id));
  const item = element('li');
  item.append(row);
  return item;
}

async function openCall(id) {
  openCallId = id;
  renderCallList();
  const detail = document.getElementById('callDetail');
  try {
    const call = await api(`/api/calls/${id}`);
    const view = renderCall(detail, call, `/api/calls/${id}/audio`);
    const actions = element('div', 'actions');
    const unread = element('button', '', 'Đánh dấu chưa đọc');
    unread.addEventListener('click', () => setRead(id, false));
    actions.append(unread);
    if (call.caller) actions.append(blockButton(call.caller));
    view.querySelector('audio').after(actions);
    if (!call.read) await setRead(id, true);
  } catch (err) {
    detail.replaceChildren(element('p', 'status error', `Không mở được cuộc gọi: ${err.message}`));
  }
}

function blockButton(caller) {
  const button = element('button', '', 'Chặn số này');
  button.addEventListener('click', async () => {
    button.disabled = true;
    try {
      const settings = await api('/api/settings');
      await api('/api/settings', { method: 'PUT', body: { blocked_numbers: [...settings.blocked_numbers, caller] } });
      button.textContent = 'Đã chặn';
    } catch (err) {
      button.textContent = `Không chặn được: ${err.message}`;
      button.disabled = false;
    }
  });
  return button;
}

async function setRead(id, read) {
  await api(`/api/calls/${id}/read`, { method: 'POST', body: { read } });
  const call = calls.find((candidate) => candidate.id === id);
  if (call) call.read = read;
  renderCallList();
}

document.querySelectorAll('.filters button').forEach((button) => {
  button.addEventListener('click', () => {
    filter = button.dataset.filter;
    document.querySelectorAll('.filters button').forEach((other) => {
      other.setAttribute('aria-pressed', String(other === button));
    });
    loadCalls();
  });
});
document.getElementById('moreCalls').addEventListener('click', () => loadCalls({ append: true }));

// --- stats ------------------------------------------------------------------

async function loadStats() {
  const by = document.getElementById('statsBy').value;
  try {
    const { rows } = await api(`/api/stats?by=${by}`);
    const busiest = Math.max(1, ...rows.map((row) => row.total));
    document.getElementById('statsRows').replaceChildren(...rows.map((row) => {
      const line = element('tr');
      const bar = element('div', 'bar');
      bar.style.width = `${Math.round((row.total / busiest) * 100)}%`;
      const barCell = element('td');
      barCell.append(bar);
      line.append(
        element('td', '', row.period),
        element('td', 'number', row.total),
        barCell,
        element('td', 'number', row.urgent),
        element('td', 'number', row.spam),
        element('td', 'number', formatUsd(row.total_usd)),
      );
      return line;
    }));
    setStatus('statsStatus', rows.length ? '' : 'Chưa có cuộc gọi nào.');
  } catch (err) {
    setStatus('statsStatus', `Không tải được thống kê: ${err.message}`, true);
  }
}

document.getElementById('statsBy').addEventListener('change', loadStats);

// --- settings ---------------------------------------------------------------

function fillSettings(settings) {
  const form = document.getElementById('settingsForm');
  for (const name of TEXT_SETTINGS) form.elements[name].value = settings[name];
  form.elements.blocked_numbers.value = settings.blocked_numbers.join('\n');
}

async function loadSettings() {
  try {
    fillSettings(await api('/api/settings'));
    setStatus('settingsStatus', '');
  } catch (err) {
    setStatus('settingsStatus', `Không tải được cài đặt: ${err.message}`, true);
  }
}

async function saveSettings(values, done) {
  try {
    fillSettings(await api('/api/settings', { method: 'PUT', body: values }));
    setStatus('settingsStatus', done);
  } catch (err) {
    setStatus('settingsStatus', `Không lưu được: ${err.message}`, true);
  }
}

document.getElementById('settingsForm').addEventListener('submit', (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const values = Object.fromEntries(TEXT_SETTINGS.map((name) => [name, form.elements[name].value]));
  values.blocked_numbers = form.elements.blocked_numbers.value.split('\n');
  saveSettings(values, 'Đã lưu. Áp dụng từ cuộc gọi kế tiếp.');
});
document.getElementById('resetSettings').addEventListener('click', () => {
  // The blocked list is the owner's data, not a default to restore: it is left alone.
  const defaults = Object.fromEntries(TEXT_SETTINGS.map((name) => [name, null]));
  saveSettings(defaults, 'Đã khôi phục mặc định.');
});

window.addEventListener('hashchange', showSection);
showSection();
