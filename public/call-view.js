// Draws one call — who, summary, recording, transcript, cost. Shared by the pages that show a call.
//
// Everything that came out of a call (names, summaries, transcripts) is untrusted
// text: it only ever goes in through textContent, never innerHTML.

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

function formatTime(iso) {
  return new Date(iso).toLocaleString('vi-VN', { dateStyle: 'short', timeStyle: 'short' });
}

function formatDuration(seconds) {
  const total = seconds || 0;
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`;
}

function formatUsd(amount) {
  return `$${(amount || 0).toFixed(4)}`;
}

function urgencyBadge(call) {
  if (call.category === 'spam') return element('span', 'badge', 'Spam');
  if (call.urgency === 'cao') return element('span', 'badge urgent', 'Khẩn cấp');
  return call.urgency ? element('span', 'badge', `Độ khẩn: ${call.urgency}`) : null;
}

function renderCall(container, call, audioUrl) {
  const view = element('article', 'call-view');

  view.append(element('h2', '', call.caller || 'Số không rõ'));
  const meta = element('p', 'meta', `${formatTime(call.started_at)} · ${formatDuration(call.duration_seconds)} `);
  const badge = urgencyBadge(call);
  if (badge) meta.append(badge);
  view.append(meta);

  const facts = element('dl');
  facts.append(element('dt', '', 'Người gọi'), element('dd', '', call.caller_name || 'Không rõ'));
  facts.append(element('dt', '', 'Nội dung'), element('dd', '', call.summary || 'Đang tóm tắt…'));
  if (call.cost) {
    const vnd = call.cost.total_vnd ? ` (~${call.cost.total_vnd.toLocaleString('vi-VN')}đ)` : '';
    facts.append(element('dt', '', 'Chi phí'), element('dd', '', formatUsd(call.cost.total_usd) + vnd));
  }
  view.append(facts);

  if ((call.recordings || []).includes('caller')) {
    const audio = element('audio');
    audio.controls = true;
    audio.preload = 'none';
    audio.src = audioUrl;
    audio.setAttribute('aria-label', 'Ghi âm giọng người gọi');
    view.append(audio);
  }

  const transcript = element('ol', 'transcript');
  for (const turn of call.transcript || []) {
    const bubble = element('li', `bubble ${turn.role === 'user' ? 'user' : 'assistant'}`);
    bubble.append(element('span', 'speaker', turn.role === 'user' ? 'Người gọi' : 'Trợ lý'));
    bubble.append(document.createTextNode(turn.text));
    transcript.append(bubble);
  }
  view.append(transcript);

  container.replaceChildren(view);
  return view;
}
