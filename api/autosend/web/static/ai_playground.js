// AI Playground page (see admin_pages.AIPlaygroundView / web/ai_playground_router.py).
// modelSelect/effortSelect are only rendered for superadmins (see
// ai_playground.html) - everyone else always runs on the platform-wide AI
// Credentials default, so every use of them below is null-guarded.
(function () {
  const unitSelect = document.getElementById('pg-unit-select');
  const numberSelect = document.getElementById('pg-number-select');
  const modelSelect = document.getElementById('pg-model-select');
  const effortSelect = document.getElementById('pg-effort-select');
  const clearBtn = document.getElementById('pg-clear-btn');
  const chat = document.getElementById('pg-chat');
  const form = document.getElementById('pg-form');
  const messageInput = document.getElementById('pg-message');
  const sendBtn = document.getElementById('pg-send-btn');
  const inspector = document.getElementById('pg-inspector');

  let currentUnitId = null;
  let currentNumberId = null;
  // Client-side-only turn history - never sent anywhere except back to
  // /api/ai/playground on the next call, never persisted server-side.
  let history = [];

  function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str == null ? '' : String(str);
    return div.innerHTML;
  }

  async function loadUnits() {
    const res = await fetch('/api/ai/units');
    const units = await res.json();
    unitSelect.innerHTML = units.map((u) => `<option value="${u.id}">${escapeHtml(u.name)}</option>`).join('');
    if (units.length) {
      currentUnitId = units[0].id;
      unitSelect.value = currentUnitId;
    }
    await loadNumbers();
  }

  // The knowledge base stays scoped to the whole unit (plus that unit's
  // org-wide entries), but bot description/custom instructions/handoff
  // message are per-number (see services/ai_reply.py::
  // generate_ai_response's whatsapp_number_id param) - "No specific
  // number" previews the platform-wide settings only, same as a unit
  // with none of its numbers configured yet.
  async function loadNumbers() {
    if (!currentUnitId) {
      numberSelect.innerHTML = '';
      currentNumberId = null;
      return;
    }
    const res = await fetch(`/api/numbers?unit_id=${currentUnitId}`);
    const numbers = await res.json();
    const options = ['<option value="">No specific number</option>']
      .concat(numbers.map((n) => `<option value="${n.id}">${escapeHtml(n.label)}</option>`));
    numberSelect.innerHTML = options.join('');
    currentNumberId = numbers.length === 1 ? numbers[0].id : null;
    numberSelect.value = currentNumberId || '';
  }

  unitSelect.addEventListener('change', async () => {
    currentUnitId = parseInt(unitSelect.value, 10);
    await loadNumbers();
    resetChat();
  });

  numberSelect.addEventListener('change', () => {
    currentNumberId = numberSelect.value ? parseInt(numberSelect.value, 10) : null;
    resetChat();
  });

  // Claude Haiku 4.5 rejects the effort parameter outright (400), unlike
  // Opus 5/Sonnet 5 - keep the picker from offering a combination that
  // will just error.
  function syncEffortAvailability() {
    if (!modelSelect || !effortSelect) return;
    const disabled = modelSelect.value === 'claude-haiku-4-5';
    effortSelect.disabled = disabled;
    effortSelect.title = disabled ? 'Haiku 4.5 does not support an effort level' : '';
  }
  if (modelSelect) modelSelect.addEventListener('change', syncEffortAvailability);
  syncEffortAvailability();

  function resetChat() {
    history = [];
    chat.innerHTML = '<p class="text-sm text-slate-400 text-center">Send a message to try the AI auto-reply.</p>';
    inspector.innerHTML = '<p class="text-slate-400">No response yet.</p>';
  }

  clearBtn.addEventListener('click', resetChat);

  function appendBubble(role, text) {
    if (chat.querySelector('p.text-center')) chat.innerHTML = '';
    const isUser = role === 'user';
    const bubble = document.createElement('div');
    bubble.className = `flex ${isUser ? 'justify-end' : 'justify-start'}`;
    bubble.innerHTML = `
      <div class="max-w-[80%] rounded-lg px-4 py-2 text-sm ${isUser
        ? 'bg-brand-primary text-white'
        : 'bg-slate-100 dark:bg-slate-700 text-slate-800 dark:text-slate-100'}">
        ${escapeHtml(text)}
      </div>`;
    chat.appendChild(bubble);
    chat.scrollTop = chat.scrollHeight;
  }

  function renderInspector(result) {
    const confidencePct = Math.round((result.confidence || 0) * 100);
    const escalateBadge = result.escalate
      ? '<span class="px-2 py-1 rounded-full text-xs font-semibold bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-400">Escalated</span>'
      : '<span class="px-2 py-1 rounded-full text-xs font-semibold bg-emerald-100 text-emerald-800 dark:bg-emerald-900/30 dark:text-emerald-400">Answered</span>';
    const optOutBadge = result.opt_out
      ? '<span class="px-2 py-1 rounded-full text-xs font-semibold bg-rose-100 text-rose-800 dark:bg-rose-900/30 dark:text-rose-400">Opt-out</span>'
      : '';

    const entriesHtml = result.retrieved_entries.length
      ? result.retrieved_entries.map((e) => `
          <div class="border border-slate-200 dark:border-slate-700 rounded-lg p-3">
            <div class="font-semibold text-xs uppercase text-slate-400 mb-1">${escapeHtml(e.source_type)}</div>
            <div class="font-medium">${escapeHtml(e.title)}</div>
            <div class="text-slate-500 dark:text-slate-400 text-xs mt-1">${escapeHtml(e.content)}</div>
          </div>`).join('')
      : '<p class="text-slate-400">No knowledge base entries were retrieved for this message.</p>';

    inspector.innerHTML = `
      <div class="flex items-center gap-2">${escalateBadge}${optOutBadge}<span class="text-slate-500 dark:text-slate-400">Confidence: ${confidencePct}%</span></div>
      <div class="text-slate-500 dark:text-slate-400">
        ${escapeHtml(result.model)} &middot; ${result.prompt_tokens}+${result.completion_tokens} tokens
      </div>
      <div>
        <div class="font-semibold text-xs uppercase text-slate-400 mb-1">Retrieved Entries</div>
        <div class="space-y-2">${entriesHtml}</div>
      </div>`;
  }

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const message = messageInput.value.trim();
    if (!message || !currentUnitId) return;

    appendBubble('user', message);
    messageInput.value = '';
    sendBtn.disabled = true;

    try {
      const res = await fetch('/api/ai/playground', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          unit_id: currentUnitId, whatsapp_number_id: currentNumberId, message, history,
          model: modelSelect ? modelSelect.value : null,
          effort: (!effortSelect || effortSelect.disabled) ? null : effortSelect.value,
        }),
      });
      const result = await res.json().catch(() => ({}));
      if (!res.ok) {
        appendBubble('assistant', `Error: ${result.detail || 'Failed to generate a reply.'}`);
        return;
      }
      appendBubble('assistant', result.reply);
      history.push({ role: 'user', content: message });
      history.push({ role: 'assistant', content: result.reply });
      renderInspector(result);
    } finally {
      sendBtn.disabled = false;
    }
  });

  loadUnits();
})();
