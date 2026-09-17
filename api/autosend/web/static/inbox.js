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
    const composerTextarea = document.getElementById('composer-textarea');
    const composerPreview = document.getElementById('composer-preview');
    const composerSend = document.getElementById('composer-send');
    const composerError = document.getElementById('composer-error');
    const threadBack = document.getElementById('thread-back');

    let conversations = [];
    let activeConversationId = null;
    let lastThreadSignature = null;
    let threadPollTimer = null;

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

        if (m.message_type === 'text' || !m.message_type) {
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
            else statusIcon = '<i class="fa-solid fa-check text-[10px] opacity-60"></i>';
        }

        return `
            <div class="flex flex-col ${align} max-w-[75%]">
                <div class="${bubbleColor} rounded-lg px-3 py-2 text-sm break-words">${bodyHtml}</div>
                <div class="text-[10px] text-slate-400 mt-0.5 flex items-center gap-1">${timeAgo(m.created_at)} ${statusIcon}</div>
            </div>
        `;
    }

    function threadSignature(messages) {
        return JSON.stringify(messages.map(m => [m.id, m.status, m.delivery_status, m.media_download_status]));
    }

    async function loadThread(forceScrollToBottom) {
        if (!activeConversationId) return;
        try {
            const res = await fetch(`/api/conversations/${activeConversationId}/messages`);
            if (!res.ok) return;
            const data = await res.json();

            sessionClosedBanner.classList.toggle('hidden', data.session_window_open);

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
            if (forceScrollToBottom || wasNearBottom) {
                messageThread.scrollTop = messageThread.scrollHeight;
            }
        } catch (e) {
            // Silent - the next 3s poll retries.
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

    composerTextarea.addEventListener('input', () => {
        composerTextarea.style.height = 'auto';
        composerTextarea.style.height = `${Math.min(composerTextarea.scrollHeight, 128)}px`;

        const text = composerTextarea.value;
        if (text) {
            composerPreview.innerHTML = WAPreview.liveMarkupToHtml(text);
            composerPreview.classList.remove('hidden');
        } else {
            composerPreview.classList.add('hidden');
        }
    });

    composerTextarea.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            sendReply();
        }
    });

    composerSend.addEventListener('click', sendReply);

    async function sendReply() {
        const text = composerTextarea.value.trim();
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
            composerTextarea.value = '';
            composerTextarea.style.height = 'auto';
            composerPreview.classList.add('hidden');
            await loadThread(true);
            await loadConversations();
        } catch (e) {
            composerError.textContent = 'Failed to send message.';
            composerError.classList.remove('hidden');
        } finally {
            composerSend.disabled = false;
        }
    }

    loadNumberFilter();
    loadConversations();
    setInterval(loadConversations, 5000);
})();
