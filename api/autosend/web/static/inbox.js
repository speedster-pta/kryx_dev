(function () {
    const shell = document.getElementById('inbox-shell');
    const listPane = document.getElementById('conversation-list');
    const searchInput = document.getElementById('conversation-search');
    const numberFilter = document.getElementById('number-filter');
    const threadEmpty = document.getElementById('thread-empty');
    const threadActive = document.getElementById('thread-active');
    const threadContactName = document.getElementById('thread-contact-name');
    const threadNumberLabel = document.getElementById('thread-number-label');
    const messageThread = document.getElementById('message-thread');
    const sessionClosedBanner = document.getElementById('session-closed-banner');
    const composerTextGroup = document.getElementById('composer-text-group');
    const textBody = document.getElementById('text-composer-body');
    const composerSend = document.getElementById('composer-send');
    const composerError = document.getElementById('composer-error');
    const templateComposer = document.getElementById('template-composer');
    const templateSelect = document.getElementById('template-composer-select');
    const templateVars = document.getElementById('template-composer-vars');
    const templateSend = document.getElementById('template-composer-send');
    const threadBack = document.getElementById('thread-back');
    const aiStatusControls = document.getElementById('ai-status-controls');
    const aiStatusBadge = document.getElementById('ai-status-badge');
    const aiStatusToggleBtn = document.getElementById('ai-status-toggle-btn');
    const newMessageBtn = document.getElementById('new-message-btn');
    const newMessageOverlay = document.getElementById('new-message-overlay');
    const newMessageCloseBtn = document.getElementById('new-message-close-btn');
    const newMessageCancelBtn = document.getElementById('new-message-cancel-btn');
    const newMessageForm = document.getElementById('new-message-form');
    const newMessageNumberSelect = document.getElementById('new-message-number');
    const newMessageWaId = document.getElementById('new-message-wa-id');
    const newMessageContactName = document.getElementById('new-message-contact-name');
    const newMessageTemplateSelect = document.getElementById('new-message-template');
    const newMessageVars = document.getElementById('new-message-vars');
    const newMessageError = document.getElementById('new-message-error');

    const AI_STATUS_LABELS = { active: 'AI Active', escalated: 'AI Escalated', paused: 'AI Paused' };
    const AI_STATUS_CLASSES = {
        active: 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300',
        escalated: 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300',
        paused: 'bg-slate-200 text-slate-600 dark:bg-slate-700 dark:text-slate-300',
    };

    let conversations = [];
    let activeConversationId = null;
    let lastThreadSignature = null;
    let threadPollTimer = null;

    // Once a conversation's session window closes, freeform text is no
    // longer allowed (Meta rejects it) - the composer switches to a
    // template picker instead of just disabling input. Templates are a
    // property of the WhatsApp number (WABA), not the conversation, so
    // they're only refetched when the active conversation's number
    // actually changes; templatesResetForConversationId tracks when the
    // in-progress selection should be cleared because the staff member
    // switched to a different contact (not just a 3s poll tick on the
    // same one).
    let templatesForNumber = [];
    let templatesLoadedForNumberId = null;
    let templatesResetForConversationId = null;

    // Templates loaded for the "New message" modal's own number/template
    // selects - kept separate from templatesForNumber above since the
    // modal can be open on a different number than the active thread.
    let newMessageTemplatesForNumber = [];

    function timeAgo(iso) {
        if (!iso) return '';
        const diffMs = Date.now() - new Date(iso).getTime();
        const mins = Math.floor(diffMs / 60000);
        if (mins < 1) return 'now';
        if (mins < 60) return `${mins}m`;
        const hours = Math.floor(mins / 60);
        if (hours < 24) return `${hours}h`;
        return `${Math.floor(hours / 24)}d`;
    }

    function renderConversationList() {
        const query = searchInput.value.trim().toLowerCase();
        const numberId = numberFilter.value;
        const filtered = conversations.filter(c => {
            if (numberId && String(c.whatsapp_number_id) !== numberId) return false;
            if (!query) return true;
            const haystack = `${c.contact_name || ''} ${c.contact_wa_id}`.toLowerCase();
            return haystack.includes(query);
        });

        if (!filtered.length) {
            listPane.innerHTML = '<div class="p-4 text-sm text-slate-400">No conversations yet.</div>';
            return;
        }

        listPane.innerHTML = filtered.map(c => `
            <div class="conversation-item cursor-pointer px-3 py-3 border-b border-slate-100 dark:border-slate-800 hover:bg-slate-50 dark:hover:bg-slate-800/60 ${c.id === activeConversationId ? 'active' : ''}"
                 data-id="${c.id}">
                <div class="flex items-center justify-between gap-2">
                    <span class="font-medium text-sm truncate">${WAPreview.escapeHtml(c.contact_name || c.contact_wa_id)}</span>
                    <span class="text-[11px] text-slate-400 flex-shrink-0">${timeAgo(c.last_message_at)}</span>
                </div>
                <div class="flex items-center justify-between gap-2 mt-0.5">
                    <span class="text-xs text-slate-500 truncate">${WAPreview.escapeHtml(c.last_message_preview || '')}</span>
                    ${c.unread_count ? `<span class="flex-shrink-0 bg-brand-primary text-white text-[10px] font-semibold rounded-full min-w-[18px] h-[18px] flex items-center justify-center px-1">${c.unread_count}</span>` : ''}
                </div>
            </div>
        `).join('');

        listPane.querySelectorAll('.conversation-item').forEach(el => {
            el.addEventListener('click', () => openConversation(parseInt(el.dataset.id, 10)));
        });
    }

    async function loadConversations() {
        try {
            const res = await fetch('/api/conversations');
            if (!res.ok) return;
            conversations = await res.json();
            renderConversationList();
        } catch (e) {
            // Silent - the next 5s poll retries.
        }
    }

    async function loadNumberFilter() {
        try {
            const res = await fetch('/api/numbers');
            if (!res.ok) return;
            const numbers = await res.json();
            numberFilter.innerHTML = '<option value="">All WhatsApp numbers</option>' +
                numbers.map(n => `<option value="${n.id}">${WAPreview.escapeHtml(n.label)}</option>`).join('');
        } catch (e) {
            // Filter just stays at "All numbers" if this fails.
        }
    }

    function isThreadScrolledNearBottom() {
        return messageThread.scrollHeight - messageThread.scrollTop - messageThread.clientHeight < 80;
    }

    function renderMessageBubble(m) {
        const isOut = m.direction === 'out';
        const align = isOut ? 'ml-auto items-end' : 'mr-auto items-start';
        const bubbleColor = isOut ? 'bg-brand-primary text-white' : 'bg-white dark:bg-slate-800 text-slate-900 dark:text-slate-100';
        let bodyHtml;

        if (m.message_type === 'text' || m.message_type === 'template' || !m.message_type) {
            bodyHtml = WAPreview.whatsappMarkupToHtml(m.body || '');
        } else if (m.media_download_status === 'downloaded') {
            const mediaUrl = `/api/conversations/media/${m.id}`;
            if (m.message_type === 'image') {
                bodyHtml = `<img src="${mediaUrl}" class="rounded-md max-w-[240px] max-h-[240px] object-cover">`;
            } else if (m.message_type === 'audio') {
                bodyHtml = `<audio controls preload="none" src="${mediaUrl}" class="max-w-[240px]"></audio>`;
            } else if (m.message_type === 'video') {
                bodyHtml = `<video controls preload="none" src="${mediaUrl}" class="rounded-md max-w-[240px]"></video>`;
            } else {
                bodyHtml = `<a href="${mediaUrl}" target="_blank" rel="noopener noreferrer" class="underline text-sm"><i class="fa-solid fa-paperclip mr-1"></i>Document</a>`;
            }
            if (m.body) bodyHtml += `<div class="mt-1">${WAPreview.whatsappMarkupToHtml(m.body)}</div>`;
        } else if (m.media_download_status === 'failed') {
            bodyHtml = `<span class="italic text-xs opacity-70">Couldn't download this ${m.message_type}.</span>`;
        } else {
            bodyHtml = `<span class="italic text-xs opacity-70">${WAPreview.escapeHtml(m.message_type)} - downloading…</span>`;
        }

        let statusIcon = '';
        if (isOut) {
            if (m.delivery_status === 'read') statusIcon = '<i class="fa-solid fa-check-double text-[10px]"></i>';
            else if (m.delivery_status === 'delivered') statusIcon = '<i class="fa-solid fa-check-double text-[10px] opacity-60"></i>';
            else if (m.status === 'failed' || m.status === 'deferred') statusIcon = '<i class="fa-solid fa-triangle-exclamation text-[10px] text-amber-300"></i>';
            else if (m.status !== 'draft') statusIcon = '<i class="fa-solid fa-check text-[10px] opacity-60"></i>';
        }

        // 'automation' = mirrored PCO/serving-reminder/campaign send (see
        // storage.mirror_outbound_to_inbox) - not sent from this thread,
        // shown with its own tag so staff can tell it apart from a manual
        // reply.
        const aiTag = m.sender_type === 'ai' ? '<span class="font-medium">AI</span> &middot;'
            : m.sender_type === 'automation' ? '<span class="font-medium">Automation</span> &middot;' : '';

        if (m.status === 'draft') {
            // A drafted AI/keyword reply awaiting staff approval - see
            // services/ai_reply.py's draft_review_enabled branch. Held out
            // of the normal sent/received bubble styling (dashed border,
            // no delivery tick) with its own send/discard controls, since
            // the contact has never seen this message yet.
            return `
                <div class="flex flex-col ${align} max-w-[75%]">
                    <div class="border-2 border-dashed border-amber-300 dark:border-amber-700 bg-amber-50 dark:bg-amber-900/20 rounded-lg px-3 py-2 text-sm break-words text-slate-900 dark:text-slate-100">${bodyHtml}</div>
                    <div class="text-[10px] text-amber-600 dark:text-amber-400 mt-0.5">${aiTag} Draft, pending review &middot; ${timeAgo(m.created_at)}</div>
                    <div class="flex gap-2 mt-1">
                        <button data-action="send-draft" data-message-id="${m.id}" class="text-xs px-2.5 py-1 rounded-md bg-brand-primary text-white hover:opacity-90 transition">Send</button>
                        <button data-action="discard-draft" data-message-id="${m.id}" class="text-xs px-2.5 py-1 rounded-md border border-slate-300 dark:border-slate-600 text-slate-600 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-slate-800 transition">Discard</button>
                    </div>
                </div>
            `;
        }

        return `
            <div class="flex flex-col ${align} max-w-[75%]">
                <div class="${bubbleColor} rounded-lg px-3 py-2 text-sm break-words">${bodyHtml}</div>
                <div class="text-[10px] text-slate-400 mt-0.5 flex items-center gap-1">${aiTag} ${timeAgo(m.created_at)} ${statusIcon}</div>
            </div>
        `;
    }

    function threadSignature(messages) {
        return JSON.stringify(messages.map(m => [m.id, m.status, m.delivery_status, m.media_download_status]));
    }

    function renderAiStatusControls(conversation) {
        if (!conversation || !conversation.ai_auto_reply_enabled) {
            aiStatusControls.classList.add('hidden');
            return;
        }
        aiStatusControls.classList.remove('hidden');
        const status = conversation.ai_status || 'active';
        aiStatusBadge.textContent = AI_STATUS_LABELS[status] || status;
        aiStatusBadge.className = `text-xs font-medium px-2 py-1 rounded-full ${AI_STATUS_CLASSES[status] || AI_STATUS_CLASSES.active}`;
        aiStatusToggleBtn.textContent = status === 'paused' ? 'Resume AI' : 'Pause AI';
        aiStatusToggleBtn.dataset.nextStatus = status === 'paused' ? 'active' : 'paused';
    }

    aiStatusToggleBtn.addEventListener('click', async () => {
        if (!activeConversationId) return;
        const nextStatus = aiStatusToggleBtn.dataset.nextStatus || 'paused';
        const res = await fetch(`/api/conversations/${activeConversationId}/ai-status`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ai_status: nextStatus }),
        });
        if (res.ok) {
            const conversation = conversations.find(c => c.id === activeConversationId);
            if (conversation) conversation.ai_status = nextStatus;
            renderAiStatusControls(conversation || { ai_status: nextStatus, ai_auto_reply_enabled: true });
        }
    });

    // Reuses WATemplates (app.js) for the same "load this number's approved
    // templates, count {{n}} body variables" logic the campaign/automation
    // builders already use, rather than reimplementing it here. Takes the
    // target <select> explicitly so both the reply composer and the "New
    // message" modal (see below) can share this instead of duplicating it.
    async function populateTemplateSelect(numberId, selectEl) {
        if (!numberId) {
            selectEl.innerHTML = '<option value="">Select a number first</option>';
            return [];
        }
        try {
            const res = await fetch(`/api/templates?number_id=${numberId}`);
            if (!res.ok) {
                selectEl.innerHTML = '<option value="">Unable to load templates</option>';
                return [];
            }
            const templates = (await res.json()).filter(t => t.status === 'APPROVED');
            selectEl.innerHTML = WATemplates.buildTemplateOptions(templates, { includePlaceholder: true });
            return templates;
        } catch (e) {
            selectEl.innerHTML = '<option value="">Unable to load templates</option>';
            return [];
        }
    }

    function renderTemplateVariableInputs(templates, selectEl, varsEl) {
        varsEl.innerHTML = '';
        const idx = selectEl.value;
        if (idx === '') return;
        const template = templates[parseInt(idx, 10)];
        const body = WATemplates.getComponent(template, 'BODY');
        const count = WATemplates.countBodyVariables(body ? body.text : '');
        for (let i = 1; i <= count; i++) {
            varsEl.insertAdjacentHTML('beforeend', `
                <input type="text" data-var-index="${i}" placeholder="Variable {{${i}}}"
                       class="text-sm rounded-lg border border-slate-300 dark:border-slate-600 bg-slate-50 dark:bg-slate-800 p-2 focus-brand">
            `);
        }
    }

    function collectTemplateVariables(varsEl) {
        return Array.from(varsEl.querySelectorAll('[data-var-index]'))
            .sort((a, b) => parseInt(a.dataset.varIndex, 10) - parseInt(b.dataset.varIndex, 10))
            .map(input => input.value);
    }

    // Fills a template's BODY text with the entered {{n}} values, purely
    // so the thread shows what was actually said instead of a generic
    // placeholder - the real send below always goes by template_name +
    // the positional variables array, never this rendered string.
    function renderTemplateBody(template, variables) {
        const body = WATemplates.getComponent(template, 'BODY');
        if (!body || !body.text) return null;
        return body.text.replace(/\{\{\s*(\d+)\s*\}\}/g, (match, n) => {
            const value = variables[parseInt(n, 10) - 1];
            return value ? value : match;
        });
    }

    async function loadTemplatesForComposer(numberId) {
        templatesForNumber = await populateTemplateSelect(numberId, templateSelect);
        renderTemplateVariableInputs(templatesForNumber, templateSelect, templateVars);
    }

    templateSelect.addEventListener('change', () => renderTemplateVariableInputs(templatesForNumber, templateSelect, templateVars));

    function renderComposerMode(conversation, sessionWindowOpen) {
        if (sessionWindowOpen) {
            composerTextGroup.classList.remove('hidden');
            templateComposer.classList.add('hidden');
            return;
        }
        composerTextGroup.classList.add('hidden');
        templateComposer.classList.remove('hidden');

        const numberId = conversation ? conversation.whatsapp_number_id : null;
        if (templatesLoadedForNumberId !== numberId) {
            loadTemplatesForComposer(numberId);
            templatesLoadedForNumberId = numberId;
            templatesResetForConversationId = activeConversationId;
        } else if (templatesResetForConversationId !== activeConversationId) {
            // Same number's template list, but switched to a different
            // conversation - clear the selection so staff can't
            // accidentally send whatever was picked/typed for someone else.
            templateSelect.value = '';
            renderTemplateVariableInputs(templatesForNumber, templateSelect, templateVars);
            templatesResetForConversationId = activeConversationId;
        }
        // else: same number, same conversation (a poll tick) - leave the
        // in-progress selection/typing untouched.
    }

    templateSend.addEventListener('click', sendTemplateReply);

    async function sendTemplateReply() {
        const idx = templateSelect.value;
        if (idx === '' || !activeConversationId) return;
        const template = templatesForNumber[parseInt(idx, 10)];
        const variables = collectTemplateVariables(templateVars);
        const renderedBody = renderTemplateBody(template, variables);

        composerError.classList.add('hidden');
        templateSend.disabled = true;
        try {
            const res = await fetch(`/api/conversations/${activeConversationId}/reply`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    type: 'template', template_name: template.name, language: template.language,
                    variables, text: renderedBody,
                }),
            });
            if (!res.ok) {
                const data = await res.json().catch(() => ({}));
                composerError.textContent = data.detail || 'Failed to send template message.';
                composerError.classList.remove('hidden');
                return;
            }
            templateSelect.value = '';
            renderTemplateVariableInputs(templatesForNumber, templateSelect, templateVars);
            await loadThread(true);
            await loadConversations();
        } catch (e) {
            composerError.textContent = 'Failed to send template message.';
            composerError.classList.remove('hidden');
        } finally {
            templateSend.disabled = false;
        }
    }

    async function loadThread(forceScrollToBottom) {
        if (!activeConversationId) return;
        try {
            const res = await fetch(`/api/conversations/${activeConversationId}/messages`);
            if (!res.ok) return;
            const data = await res.json();

            renderAiStatusControls(data.conversation);
            sessionClosedBanner.classList.toggle('hidden', data.session_window_open);
            renderComposerMode(data.conversation, data.session_window_open);

            const signature = threadSignature(data.messages);
            if (signature === lastThreadSignature && !forceScrollToBottom) return;

            // Only re-render (and only auto-scroll) when the fetched
            // messages actually changed, or the thread was just opened -
            // an unconditional replace on every poll tick would tear down
            // and restart any currently-playing audio/video, and an
            // unconditional scroll would yank a scrolled-up reader back
            // down to the bottom.
            const wasNearBottom = isThreadScrolledNearBottom();
            lastThreadSignature = signature;
            messageThread.innerHTML = data.messages.map(renderMessageBubble).join('');
            messageThread.querySelectorAll('[data-action="send-draft"]').forEach(btn => {
                btn.addEventListener('click', () => sendDraft(parseInt(btn.dataset.messageId, 10)));
            });
            messageThread.querySelectorAll('[data-action="discard-draft"]').forEach(btn => {
                btn.addEventListener('click', () => discardDraft(parseInt(btn.dataset.messageId, 10)));
            });
            if (forceScrollToBottom || wasNearBottom) {
                messageThread.scrollTop = messageThread.scrollHeight;
            }
        } catch (e) {
            // Silent - the next 3s poll retries.
        }
    }

    async function sendDraft(messageId) {
        if (!activeConversationId) return;
        composerError.classList.add('hidden');
        try {
            const res = await fetch(`/api/conversations/${activeConversationId}/messages/${messageId}/send`, {
                method: 'POST',
            });
            if (!res.ok) {
                const data = await res.json().catch(() => ({}));
                composerError.textContent = data.detail || 'Failed to send this draft.';
                composerError.classList.remove('hidden');
                return;
            }
            await loadThread(true);
            await loadConversations();
        } catch (e) {
            composerError.textContent = 'Failed to send this draft.';
            composerError.classList.remove('hidden');
        }
    }

    async function discardDraft(messageId) {
        if (!activeConversationId) return;
        composerError.classList.add('hidden');
        try {
            const res = await fetch(`/api/conversations/${activeConversationId}/messages/${messageId}`, {
                method: 'DELETE',
            });
            if (!res.ok) {
                const data = await res.json().catch(() => ({}));
                composerError.textContent = data.detail || 'Failed to discard this draft.';
                composerError.classList.remove('hidden');
                return;
            }
            await loadThread(true);
        } catch (e) {
            composerError.textContent = 'Failed to discard this draft.';
            composerError.classList.remove('hidden');
        }
    }

    async function openConversation(id) {
        activeConversationId = id;
        lastThreadSignature = null;

        const conversation = conversations.find(c => c.id === id);
        threadEmpty.classList.add('hidden');
        threadActive.classList.remove('hidden');
        shell.classList.add('mobile-thread-open');
        if (conversation) {
            threadContactName.textContent = conversation.contact_name || conversation.contact_wa_id;
            threadNumberLabel.textContent = conversation.number_label || '';
        }
        renderAiStatusControls(conversation);
        renderConversationList();

        await loadThread(true);
        clearInterval(threadPollTimer);
        threadPollTimer = setInterval(() => loadThread(false), 3000);

        try {
            await fetch(`/api/conversations/${id}/read`, { method: 'POST' });
            if (conversation) conversation.unread_count = 0;
            renderConversationList();
        } catch (e) {
            // Non-critical - unread count just won't clear until the next poll.
        }
    }

    threadBack.addEventListener('click', () => {
        shell.classList.remove('mobile-thread-open');
    });

    searchInput.addEventListener('input', renderConversationList);
    numberFilter.addEventListener('change', renderConversationList);

    // ---------------------------------------------------------------------
    // Composer editing model
    //
    // #text-composer-body is a contenteditable div, not a <textarea> - it
    // renders WhatsApp's formatting live, inline, while staff type (matching
    // WhatsApp Web's own composer) instead of showing a separate preview
    // pane. That means the DOM (rich HTML, built by WAPreview.liveMarkupToHtml)
    // and the "real" value - the plain markup text that actually gets sent,
    // e.g. "*bold*" - are two different representations of the same content
    // that must stay in sync on every keystroke, including cursor position.
    //
    // The approach: the DOM is always just a *rendering* of a plain-text
    // string we already know from the previous render. Structural edits
    // (Enter, Backspace/Delete, paste, drag-drop) are intercepted via
    // `beforeinput`/`paste`/`drop` and applied directly to that string,
    // rather than trusted to whatever DOM shape the browser's own native
    // contenteditable editing would produce - that shape (especially around
    // line breaks) varies enough between Chrome/Firefox/Safari to not be
    // reliably parseable back into text. Only plain character insertion
    // (typing, IME composition, autocorrect/spellcheck replacement) is left
    // to native editing, since it never changes the DOM's block structure -
    // it just edits an existing text node in place - and native handling is
    // what keeps IME composition and mobile keyboards working correctly.
    //
    // composerPlainText() below is the inverse of WAPreview.liveMarkupToHtml:
    // given the rendered DOM, reconstruct the exact string that produced it.
    // This never has to reconstruct a *stripped* character (unlike a typical
    // markdown live-preview editor that hides "**"/"_" near the cursor) since
    // liveMarkupToHtml always keeps every marker character visibly (dimmed)
    // in the output - it only has to know which elements represent a line
    // break that isn't already a literal "\n" in a text node: <br>, and the
    // boundary after a <li>/<ul>/<ol>/<div> (the quote block) that isn't the
    // last thing on the line. <pre> (monospace blocks) needs no special
    // handling - per liveMarkupToHtml, it sits inline between the <br>s of
    // the line it started on, and its own text node already contains the
    // block's real embedded newlines verbatim.

    function isLastMeaningfulSibling(node) {
        let sib = node.nextSibling;
        while (sib) {
            if (sib.nodeType === Node.TEXT_NODE && sib.data === '') { sib = sib.nextSibling; continue; }
            return false;
        }
        return true;
    }

    // A zero-width space used purely as an internal caret anchor (see
    // composerPositionAt) - never part of a real message. Every text-reading
    // helper below strips it out, so it can never leak into the plain-text
    // value that actually gets sent, no matter where in the DOM it ends up.
    const ZWSP = '​';
    function plainTextOf(data) { return data.split(ZWSP).join(''); }
    // Native (raw) offset within `data` whose preceding non-ZWSP character
    // count equals `plainCount` - the inverse of plainTextOf for a single
    // text node, used to place a caret at a specific *plain-text* position
    // inside a node that may contain a ZWSP anchor character.
    function nativeOffsetForPlainCount(data, plainCount) {
        let seen = 0;
        for (let i = 0; i < data.length; i++) {
            if (data[i] === ZWSP) continue;
            if (seen === plainCount) return i;
            seen++;
        }
        return data.length;
    }

    function composerPlainText(root) {
        let text = '';
        function walk(node) {
            if (node.nodeType === Node.TEXT_NODE) { text += plainTextOf(node.data); return; }
            if (node.nodeType !== Node.ELEMENT_NODE) return;
            const tag = node.tagName;
            if (tag === 'BR') { text += '\n'; return; }
            node.childNodes.forEach(walk);
            if ((tag === 'LI' || tag === 'UL' || tag === 'OL' || tag === 'DIV') && !isLastMeaningfulSibling(node)) {
                text += '\n';
            }
        }
        root.childNodes.forEach(walk);
        return text;
    }

    // Length (in composerPlainText's terms) that a single node/subtree
    // contributes - shared by composerOffsetOf and composerPositionAt so the
    // "how long is this thing" rule only needs to be stated once.
    function composerNodeLength(node) {
        if (node.nodeType === Node.TEXT_NODE) return plainTextOf(node.data).length;
        if (node.nodeType !== Node.ELEMENT_NODE) return 0;
        const tag = node.tagName;
        if (tag === 'BR') return 1;
        let len = 0;
        node.childNodes.forEach((c) => { len += composerNodeLength(c); });
        if ((tag === 'LI' || tag === 'UL' || tag === 'OL' || tag === 'DIV') && !isLastMeaningfulSibling(node)) len += 1;
        return len;
    }

    // DOM (Range) position -> plain-text character offset.
    function composerOffsetOf(root, targetNode, targetOffset) {
        if (targetNode === root) {
            let extra = 0;
            Array.from(root.childNodes).slice(0, targetOffset).forEach((c) => { extra += composerNodeLength(c); });
            return extra;
        }
        let total = 0;
        let result = null;
        function walk(node) {
            if (result !== null) return;
            if (node === targetNode) {
                if (node.nodeType === Node.TEXT_NODE) {
                    result = total + plainTextOf(node.data.slice(0, targetOffset)).length;
                } else {
                    let extra = 0;
                    Array.from(node.childNodes).slice(0, targetOffset).forEach((c) => { extra += composerNodeLength(c); });
                    result = total + extra;
                }
                return;
            }
            if (node.nodeType === Node.TEXT_NODE) { total += plainTextOf(node.data).length; return; }
            if (node.nodeType !== Node.ELEMENT_NODE) return;
            const tag = node.tagName;
            if (tag === 'BR') { total += 1; return; }
            node.childNodes.forEach(walk);
            if (result !== null) return;
            if ((tag === 'LI' || tag === 'UL' || tag === 'OL' || tag === 'DIV') && !isLastMeaningfulSibling(node)) {
                total += 1;
            }
        }
        root.childNodes.forEach(walk);
        return result !== null ? result : total;
    }

    // Plain-text character offset -> DOM (node, offset) position, for
    // restoring the caret/selection after a re-render.
    function composerPositionAt(root, targetOffset) {
        let remaining = targetOffset;
        let fallback = { node: root, offset: root.childNodes.length };
        let found = null;
        function indexOfChild(node) {
            return Array.prototype.indexOf.call(node.parentNode.childNodes, node);
        }
        function walk(node) {
            if (found) return;
            if (node.nodeType === Node.TEXT_NODE) {
                const len = plainTextOf(node.data).length;
                if (remaining <= len) { found = { node, offset: nativeOffsetForPlainCount(node.data, remaining) }; return; }
                remaining -= len;
                fallback = { node, offset: node.data.length };
                return;
            }
            if (node.nodeType !== Node.ELEMENT_NODE) return;
            const tag = node.tagName;
            if (tag === 'BR') {
                if (remaining <= 1) { found = { node: node.parentNode, offset: indexOfChild(node) + 1 }; return; }
                remaining -= 1;
                fallback = { node: node.parentNode, offset: indexOfChild(node) + 1 };
                return;
            }
            for (const child of Array.from(node.childNodes)) {
                walk(child);
                if (found) return;
            }
            if ((tag === 'LI' || tag === 'UL' || tag === 'OL' || tag === 'DIV') && !isLastMeaningfulSibling(node)) {
                if (remaining <= 1) { found = { node: node.parentNode, offset: indexOfChild(node) + 1 }; return; }
                remaining -= 1;
                fallback = { node: node.parentNode, offset: indexOfChild(node) + 1 };
            }
        }
        for (const child of Array.from(root.childNodes)) {
            walk(child);
            if (found) break;
        }
        const pos = found || fallback;
        // Never return a position expressed as "child index N of this element"
        // (e.g. "right after this <br>") - Chrome in particular does not
        // reliably treat that as a stable place to type: even when a Selection
        // is explicitly set there, it can silently insert the next typed
        // character into the nearest preceding text node instead (observed
        // concretely as typed lines merging into the previous one right after
        // Enter). A genuinely *empty* anchor text node turned out not to be
        // enough either - Chrome tends to clean up/bypass empty text nodes
        // during native text insertion. A single zero-width space gives it real
        // (if invisible) content to anchor to, which is what battle-tested
        // contenteditable editors rely on for exactly this "place a caret on an
        // otherwise-empty line" case.
        if (pos.node.nodeType !== Node.TEXT_NODE) {
            const anchor = document.createTextNode(ZWSP);
            pos.node.insertBefore(anchor, pos.node.childNodes[pos.offset] || null);
            return { node: anchor, offset: anchor.data.length };
        }
        return pos;
    }

    function getComposerSelectionOffsets() {
        const sel = window.getSelection();
        if (!sel || sel.rangeCount === 0 || !textBody.contains(sel.anchorNode)) {
            const end = composerPlainText(textBody).length;
            return { start: end, end };
        }
        const range = sel.getRangeAt(0);
        const start = composerOffsetOf(textBody, range.startContainer, range.startOffset);
        const end = composerOffsetOf(textBody, range.endContainer, range.endOffset);
        return start <= end ? { start, end } : { start: end, end: start };
    }

    function setComposerSelectionOffsets(start, end) {
        const startPos = composerPositionAt(textBody, start);
        const endPos = end === start ? startPos : composerPositionAt(textBody, end);
        const range = document.createRange();
        range.setStart(startPos.node, startPos.offset);
        range.setEnd(endPos.node, endPos.offset);
        const sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(range);
    }

    // Pure DOM update - re-renders from `text` and restores the caret, with
    // no undo-history bookkeeping (used directly by undo/redo themselves, so
    // replaying history doesn't recursively push more history).
    function applyComposerText(text, selStart, selEnd) {
        textBody.innerHTML = WAPreview.liveMarkupToHtml(text);
        textBody.focus();
        setComposerSelectionOffsets(selStart, selEnd === undefined ? selStart : selEnd);
    }

    function getComposerText() {
        return composerPlainText(textBody);
    }

    // A small custom undo/redo stack. A plain <textarea> gets robust native
    // undo/redo for free from every browser; a contenteditable we fully
    // re-render on every keystroke does not - the browser's own contenteditable
    // undo history tracks DOM mutations that no longer correspond to our
    // string-based model once we've replaced the DOM out from under it. Rapid
    // consecutive edits within the same half-second (ordinary typing, holding
    // Backspace) coalesce into one undo step, matching the "grouped" undo feel
    // most text editors have, rather than undoing one character at a time.
    const composerHistory = { stack: [{ text: '', selStart: 0, selEnd: 0 }], index: 0, lastPushAt: 0 };
    function pushComposerHistory(text, selStart, selEnd, coalesce) {
        const now = Date.now();
        if (coalesce && composerHistory.index >= 0 && now - composerHistory.lastPushAt < 500) {
            composerHistory.stack[composerHistory.index] = { text, selStart, selEnd };
        } else {
            composerHistory.stack = composerHistory.stack.slice(0, composerHistory.index + 1);
            composerHistory.stack.push({ text, selStart, selEnd });
            composerHistory.index = composerHistory.stack.length - 1;
        }
        composerHistory.lastPushAt = now;
    }
    function resetComposerHistory(text) {
        composerHistory.stack = [{ text, selStart: text.length, selEnd: text.length }];
        composerHistory.index = 0;
        composerHistory.lastPushAt = 0;
    }
    function composerUndo() {
        if (composerHistory.index <= 0) return;
        composerHistory.index -= 1;
        const s = composerHistory.stack[composerHistory.index];
        applyComposerText(s.text, s.selStart, s.selEnd);
    }
    function composerRedo() {
        if (composerHistory.index >= composerHistory.stack.length - 1) return;
        composerHistory.index += 1;
        const s = composerHistory.stack[composerHistory.index];
        applyComposerText(s.text, s.selStart, s.selEnd);
    }

    // The one entry point everything else uses to change the composer's
    // content - renders, restores the caret, and records an undo step.
    function setComposerText(text, selStart, selEnd, coalesce) {
        applyComposerText(text, selStart, selEnd);
        pushComposerHistory(text, selStart, selEnd === undefined ? selStart : selEnd, coalesce);
    }

    function replaceComposerRange(start, end, insertText, coalesce) {
        const value = getComposerText();
        const newValue = value.slice(0, start) + insertText + value.slice(end);
        setComposerText(newValue, start + insertText.length, undefined, coalesce);
    }

    // Wraps the current selection (or, with nothing selected, just inserts a
    // marker pair with the cursor left between them) in WhatsApp's markup
    // character for that style, mirroring WhatsApp Web's own formatting
    // toolbar. Also used for the ```monospace block``` button, whose marker
    // is a 3-character string rather than a single character.
    function wrapComposerSelection(markerChar) {
        const { start, end } = getComposerSelectionOffsets();
        const value = getComposerText();
        const selected = value.slice(start, end);
        const newValue = value.slice(0, start) + markerChar + selected + markerChar + value.slice(end);
        setComposerText(newValue, start + markerChar.length, start + markerChar.length + selected.length);
    }

    // Prefixes every line touched by the current selection (expanded to full
    // lines, so partial-line selections still format the whole line) with
    // whatever `prefixer(line, indexWithinBlock)` returns - used for the
    // list/quote buttons, whose markup lives at the start of a line rather
    // than wrapped around a run of text.
    function prefixComposerLines(prefixer) {
        const { start, end } = getComposerSelectionOffsets();
        const value = getComposerText();
        const lineStart = value.lastIndexOf('\n', start - 1) + 1;
        let lineEnd = value.indexOf('\n', end);
        if (lineEnd === -1) lineEnd = value.length;
        const block = value.slice(lineStart, lineEnd);
        const prefixed = block.split('\n').map(prefixer).join('\n');
        const newValue = value.slice(0, lineStart) + prefixed + value.slice(lineEnd);
        setComposerText(newValue, lineStart, lineStart + prefixed.length);
    }

    // Structural edits are handled ourselves (see the block comment above) -
    // Enter/Shift+Enter always insert a plain "\n" (a dedicated keydown
    // handler further down sends on a plain Enter instead, before this ever
    // fires), and every flavour of delete (character/word/line, either
    // direction) is resolved via the browser's own getTargetRanges() so
    // Ctrl+Backspace/Cmd+Backspace etc. delete the same span native editing
    // would have, just applied to our text model instead of left to native
    // DOM mutation.
    textBody.addEventListener('beforeinput', (e) => {
        const type = e.inputType || '';
        if (type === 'insertParagraph' || type === 'insertLineBreak') {
            e.preventDefault();
            const { start, end } = getComposerSelectionOffsets();
            replaceComposerRange(start, end, '\n');
            return;
        }
        if (type.startsWith('delete')) {
            e.preventDefault();
            let { start, end } = getComposerSelectionOffsets();
            if (start === end) {
                const targetRanges = typeof e.getTargetRanges === 'function' ? e.getTargetRanges() : [];
                if (targetRanges.length) {
                    const r = targetRanges[0];
                    start = composerOffsetOf(textBody, r.startContainer, r.startOffset);
                    end = composerOffsetOf(textBody, r.endContainer, r.endOffset);
                } else if (type.endsWith('Forward')) {
                    end = Math.min(end + 1, getComposerText().length);
                } else {
                    start = Math.max(start - 1, 0);
                }
            }
            replaceComposerRange(start, end, '', true);
        }
    });

    // Paste/drag-drop always insert as plain text, never whatever rich HTML
    // the source provided - both to keep the message's real markup accurate
    // (rich HTML has no WhatsApp-markup equivalent to fall back to) and so a
    // pasted/dropped <img onerror=...> or similar can never execute: it's
    // read as inert text via getData(), the source HTML is never touched.
    textBody.addEventListener('paste', (e) => {
        e.preventDefault();
        const pasted = (e.clipboardData || window.clipboardData).getData('text/plain');
        const { start, end } = getComposerSelectionOffsets();
        replaceComposerRange(start, end, pasted);
    });
    textBody.addEventListener('drop', (e) => {
        e.preventDefault();
        const dropped = e.dataTransfer.getData('text/plain');
        if (!dropped) return;
        const { start, end } = getComposerSelectionOffsets();
        replaceComposerRange(start, end, dropped);
    });

    // Plain character insertion (typing, autocorrect/spellcheck replacement)
    // is left to native editing so IME composition and mobile keyboards keep
    // working - composition-in-progress must not be re-rendered out from
    // under the input method, so re-sync is deferred to compositionend.
    let composerComposing = false;
    textBody.addEventListener('compositionstart', () => { composerComposing = true; });
    textBody.addEventListener('compositionend', () => {
        composerComposing = false;
        const { start } = getComposerSelectionOffsets();
        setComposerText(getComposerText(), start, undefined, true);
    });
    textBody.addEventListener('input', () => {
        if (composerComposing) return;
        const { start } = getComposerSelectionOffsets();
        setComposerText(getComposerText(), start, undefined, true);
    });

    // A plain Enter sends (matching the old <textarea>'s behaviour); Shift+Enter
    // falls through to the beforeinput handler above, which inserts a "\n".
    textBody.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey && !e.metaKey && !e.ctrlKey) {
            e.preventDefault();
            sendReply();
            return;
        }
        const mod = e.metaKey || e.ctrlKey;
        if (!mod) return;
        if (e.key.toLowerCase() === 'z' && !e.shiftKey) { e.preventDefault(); composerUndo(); }
        else if (e.key.toLowerCase() === 'y' || (e.key.toLowerCase() === 'z' && e.shiftKey)) { e.preventDefault(); composerRedo(); }
    });

    document.getElementById('format-bold-btn').addEventListener('click', () => wrapComposerSelection('*'));
    document.getElementById('format-italic-btn').addEventListener('click', () => wrapComposerSelection('_'));
    document.getElementById('format-strike-btn').addEventListener('click', () => wrapComposerSelection('~'));
    document.getElementById('format-mono-btn').addEventListener('click', () => wrapComposerSelection('`'));
    document.getElementById('format-monoblock-btn').addEventListener('click', () => wrapComposerSelection('```'));
    document.getElementById('format-list-btn').addEventListener('click', () => prefixComposerLines((line) => `- ${line}`));
    document.getElementById('format-numbered-btn').addEventListener('click', () => prefixComposerLines((line, i) => `${i + 1}. ${line}`));
    document.getElementById('format-quote-btn').addEventListener('click', () => prefixComposerLines((line) => `> ${line}`));

    composerSend.addEventListener('click', sendReply);

    async function sendReply() {
        const text = getComposerText().trim();
        if (!text || !activeConversationId) return;

        composerError.classList.add('hidden');
        composerSend.disabled = true;
        try {
            const res = await fetch(`/api/conversations/${activeConversationId}/reply`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ text }),
            });
            if (!res.ok) {
                const data = await res.json().catch(() => ({}));
                composerError.textContent = data.detail || 'Failed to send message.';
                composerError.classList.remove('hidden');
                return;
            }
            applyComposerText('', 0, 0);
            resetComposerHistory('');
            await loadThread(true);
            await loadConversations();
        } catch (e) {
            composerError.textContent = 'Failed to send message.';
            composerError.classList.remove('hidden');
        } finally {
            composerSend.disabled = false;
        }
    }

    function closeNewMessageModal() {
        newMessageOverlay.classList.add('hidden');
    }

    newMessageBtn.addEventListener('click', async () => {
        newMessageForm.reset();
        newMessageVars.innerHTML = '';
        newMessageError.classList.add('hidden');
        const numbers = await WATemplates.fetchAndPopulateNumbers(newMessageNumberSelect);
        const currentFilterId = numberFilter.value ? parseInt(numberFilter.value, 10) : null;
        const preselect = currentFilterId || (numbers[0] && numbers[0].id);
        if (preselect) newMessageNumberSelect.value = String(preselect);
        newMessageTemplatesForNumber = await populateTemplateSelect(preselect, newMessageTemplateSelect);
        renderTemplateVariableInputs(newMessageTemplatesForNumber, newMessageTemplateSelect, newMessageVars);
        newMessageOverlay.classList.remove('hidden');
    });

    newMessageNumberSelect.addEventListener('change', async () => {
        const numberId = newMessageNumberSelect.value ? parseInt(newMessageNumberSelect.value, 10) : null;
        newMessageTemplatesForNumber = await populateTemplateSelect(numberId, newMessageTemplateSelect);
        renderTemplateVariableInputs(newMessageTemplatesForNumber, newMessageTemplateSelect, newMessageVars);
    });

    newMessageTemplateSelect.addEventListener('change', () => {
        renderTemplateVariableInputs(newMessageTemplatesForNumber, newMessageTemplateSelect, newMessageVars);
    });

    newMessageCloseBtn.addEventListener('click', closeNewMessageModal);
    newMessageCancelBtn.addEventListener('click', closeNewMessageModal);
    newMessageOverlay.addEventListener('click', (e) => {
        if (e.target === newMessageOverlay) closeNewMessageModal();
    });

    newMessageForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        const numberId = newMessageNumberSelect.value ? parseInt(newMessageNumberSelect.value, 10) : null;
        const waId = newMessageWaId.value.trim();
        const idx = newMessageTemplateSelect.value;
        newMessageError.classList.add('hidden');
        if (!numberId || !waId || idx === '') {
            newMessageError.textContent = 'Please select a number, enter a recipient, and choose a template.';
            newMessageError.classList.remove('hidden');
            return;
        }
        const template = newMessageTemplatesForNumber[parseInt(idx, 10)];
        const variables = collectTemplateVariables(newMessageVars);
        const renderedBody = renderTemplateBody(template, variables);

        const submitBtn = newMessageForm.querySelector('button[type="submit"]');
        submitBtn.disabled = true;
        try {
            const createRes = await fetch('/api/conversations', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    whatsapp_number_id: numberId, contact_wa_id: waId,
                    contact_name: newMessageContactName.value.trim() || null,
                }),
            });
            if (!createRes.ok) {
                const err = await createRes.json().catch(() => ({}));
                newMessageError.textContent = err.detail || 'Failed to start the conversation.';
                newMessageError.classList.remove('hidden');
                return;
            }
            const conversation = await createRes.json();

            const sendRes = await fetch(`/api/conversations/${conversation.id}/reply`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    type: 'template', template_name: template.name, language: template.language,
                    variables, text: renderedBody,
                }),
            });
            if (!sendRes.ok) {
                const err = await sendRes.json().catch(() => ({}));
                newMessageError.textContent = err.detail || 'Failed to send template message.';
                newMessageError.classList.remove('hidden');
                return;
            }

            closeNewMessageModal();
            await loadConversations();
            await openConversation(conversation.id);
        } finally {
            submitBtn.disabled = false;
        }
    });

    loadNumberFilter();
    loadConversations();
    setInterval(loadConversations, 5000);
})();
