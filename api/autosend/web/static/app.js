function togglePasswordVisibility(btn) {
    const input = btn.previousElementSibling;
    const showing = input.type === 'text';
    input.type = showing ? 'password' : 'text';
    btn.textContent = showing ? 'Show' : 'Hide';
}

// Shared WhatsApp bubble preview rendering, used by dashboard.html (campaigns),
// automations.html (all three automation types), and templates.html (template
// builder + "view template" modal). Each page still gathers its own preview
// data (CSV row values, selected variable values, example values) - this just
// turns a normalized data shape into DOM, and renders individual button pills,
// so that markup/formatting changes only need to happen here.
window.WAPreview = (function () {
    function escapeHtml(str) {
        return (str ?? '').toString().replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    }

    function escapeAttr(str) {
        return String(str || '').replace(/"/g, '&quot;');
    }

    // Applies WhatsApp's inline markup (*bold*, _italic_, ~strike~, `code`) to
    // an already-HTML-escaped line/fragment. Used both for plain lines and for
    // the text inside list items / quote lines, since WhatsApp still honours
    // inline styles nested inside those.
    function inlineWhatsappMarkup(str) {
        return str
            .replace(/\*([^*\n]+)\*/g, '<strong>$1</strong>')
            .replace(/_([^_\n]+)_/g, '<em>$1</em>')
            .replace(/~([^~\n]+)~/g, '<s>$1</s>')
            .replace(/`([^`\n]+)`/g, '<code class="bg-black/25 px-1 py-0.5 rounded text-[10px]">$1</code>');
    }

    // Converts WhatsApp's markup into HTML, escaping the raw text first.
    // Covers every style WhatsApp's formatting menu supports:
    //   *bold*   _italic_   ~strike~   ```monospace block```   `inline code`
    //   "* " / "- " bulleted list      "1. " numbered list      "> " quote
    // Monospace blocks are pulled out before anything else runs, since
    // WhatsApp doesn't apply any other formatting (or list/quote parsing)
    // inside them - triple backticks always win.
    function whatsappMarkupToHtml(text) {
        const escaped = escapeHtml(text || '');

        const codeBlocks = [];
        const working = escaped.replace(/```([\s\S]+?)```/g, (m, code) => {
            codeBlocks.push(code);
            return `\u0000CB${codeBlocks.length - 1}\u0000`;
        });

        const lines = working.split('\n');
        const parts = [];
        let list = null;   // { tag: 'ul'|'ol', items: [] }
        let quote = null;  // array of raw (still-escaped) lines

        function flushList() {
            if (!list) return;
            const cls = list.tag === 'ul' ? 'list-disc' : 'list-decimal';
            parts.push(`<${list.tag} class="${cls} pl-4 my-1 space-y-0.5">${list.items.map(i => `<li>${inlineWhatsappMarkup(i)}</li>`).join('')}</${list.tag}>`);
            list = null;
        }
        function flushQuote() {
            if (!quote) return;
            parts.push(`<div class="border-l-[3px] border-slate-500/70 pl-2 my-1 text-slate-300/90">${quote.map(inlineWhatsappMarkup).join('<br>')}</div>`);
            quote = null;
        }

        lines.forEach((line, i) => {
            const bullet = line.match(/^[*-]\s+(.*)$/);
            const numbered = line.match(/^\d+\.\s+(.*)$/);
            const quoted = line.match(/^&gt;\s?(.*)$/);

            if (bullet) {
                flushQuote();
                if (!list || list.tag !== 'ul') { flushList(); list = { tag: 'ul', items: [] }; }
                list.items.push(bullet[1]);
                return;
            }
            if (numbered) {
                flushQuote();
                if (!list || list.tag !== 'ol') { flushList(); list = { tag: 'ol', items: [] }; }
                list.items.push(numbered[1]);
                return;
            }
            if (quoted) {
                flushList();
                if (!quote) quote = [];
                quote.push(quoted[1]);
                return;
            }

            flushList();
            flushQuote();
            parts.push(inlineWhatsappMarkup(line));
            if (i < lines.length - 1) parts.push('<br>');
        });
        flushList();
        flushQuote();

        let html = parts.join('');

        html = html.replace(/\u0000CB(\d+)\u0000/g, (m, idx) =>
            `<pre class="whitespace-pre-wrap font-mono text-[10px] leading-snug bg-black/25 rounded px-1.5 py-1 my-1">${codeBlocks[idx]}</pre>`);

        return html;
    }

    // Same styling rules as inlineWhatsappMarkup, but for the composer's live
    // preview: the marker characters are kept (dimmed) rather than stripped,
    // since the composer's underlying value - what literally gets sent - must
    // still contain them.
    function inlineWhatsappMarkupLive(str) {
        const marker = (c) => `<span class="opacity-40">${c}</span>`;
        return str
            .replace(/\*([^*\n]+)\*/g, (m, inner) => `${marker('*')}<strong>${inner}</strong>${marker('*')}`)
            .replace(/_([^_\n]+)_/g, (m, inner) => `${marker('_')}<em>${inner}</em>${marker('_')}`)
            .replace(/~([^~\n]+)~/g, (m, inner) => `${marker('~')}<s>${inner}</s>${marker('~')}`)
            .replace(/`([^`\n]+)`/g, (m, inner) => `${marker('`')}<code class="bg-slate-500/20 px-1 rounded">${inner}</code>${marker('`')}`);
    }

    // Live-preview counterpart of whatsappMarkupToHtml, used directly as the
    // Inbox composer's own contenteditable rendering while staff type (see
    // the "Composer editing model" comment block in inbox.js) - not a
    // separate preview pane. Recognises every style the real renderer does
    // (bold/italic/strike/inline-code, ```monospace blocks```, bulleted and
    // numbered lists, "> " quotes) so what's rendered never falls short of
    // what the sent message will actually look like, but - unlike the sent-
    // message renderer - keeps every marker character visible (dimmed)
    // instead of stripping it, since this text is still being edited.
    function liveMarkupToHtml(text) {
        const marker = (c) => `<span class="opacity-40">${escapeHtml(c)}</span>`;
        const escaped = escapeHtml(text || '');

        const codeBlocks = [];
        const working = escaped.replace(/```([\s\S]+?)```/g, (m, code) => {
            codeBlocks.push(code);
            return ` CB${codeBlocks.length - 1} `;
        });

        const lines = working.split('\n');
        const parts = [];
        let list = null;   // { tag: 'ul'|'ol', items: [{ prefix, text }] }
        let quote = null;  // array of raw (still-escaped) lines

        function flushList() {
            if (!list) return;
            const cls = list.tag === 'ul' ? 'list-disc' : 'list-decimal';
            parts.push(`<${list.tag} class="${cls} pl-4 my-1 space-y-0.5">${list.items.map(i => `<li>${marker(i.prefix)}${inlineWhatsappMarkupLive(i.text)}</li>`).join('')}</${list.tag}>`);
            list = null;
        }
        function flushQuote() {
            if (!quote) return;
            // No text-color utility here (unlike the sent-bubble renderer's
            // matching block, which assumes a colored/dark bubble backdrop) -
            // this composer sits in a plain box whose own color already
            // adapts to light/dark mode, so the quote text just inherits it.
            // The "> " marker's own optional space (q.space) is emitted as
            // plain text rather than through marker(), which re-escapes its
            // argument - the space needs no escaping, and passing it through
            // marker() as if it were a raw character would be harmless here,
            // but keeping the rule "marker() takes exactly one raw markup
            // character" simple avoids it being reused wrong elsewhere.
            parts.push(`<div class="border-l-[3px] border-slate-400/70 pl-2 my-1">${quote.map(q => `${marker('>')}${q.space}${inlineWhatsappMarkupLive(q.text)}`).join('<br>')}</div>`);
            quote = null;
        }

        lines.forEach((line, i) => {
            const bullet = line.match(/^([*-]\s+)(.*)$/);
            const numbered = line.match(/^(\d+\.\s+)(.*)$/);
            // The space after ">" is optional in WhatsApp's own syntax, so it
            // must be captured (rather than consumed by a bare \s?) and
            // reproduced exactly - composerPlainText (inbox.js) reconstructs
            // the composer's live-typed text from this same rendered output,
            // and dropping/always-adding that space would silently corrupt
            // ">text" into "> text" (or vice versa) on every re-render.
            const quoted = line.match(/^&gt;(\s?)(.*)$/);

            if (bullet) {
                flushQuote();
                if (!list || list.tag !== 'ul') { flushList(); list = { tag: 'ul', items: [] }; }
                list.items.push({ prefix: bullet[1], text: bullet[2] });
                return;
            }
            if (numbered) {
                flushQuote();
                if (!list || list.tag !== 'ol') { flushList(); list = { tag: 'ol', items: [] }; }
                list.items.push({ prefix: numbered[1], text: numbered[2] });
                return;
            }
            if (quoted) {
                flushList();
                if (!quote) quote = [];
                quote.push({ space: quoted[1], text: quoted[2] });
                return;
            }

            const wasBlockOpen = !!(list || quote);
            flushList();
            flushQuote();
            if (wasBlockOpen && line === '' && i === lines.length - 1) {
                // The line just before this one closed out a list/quote
                // block, and this final "line" is empty - i.e. the real text
                // ends with a newline right after that block. A flushed
                // <ul>/<ol>/<div> has no content of its own to carry that
                // trailing newline the way a plain line's own <br> below
                // does, so without a marker here, composerPlainText
                // (inbox.js) can't tell "block, then nothing more" apart
                // from "block, then one more empty line" - the composer
                // would silently swallow that final newline on every
                // re-render. A zero-width space is invisible but non-empty,
                // so it counts as a real sibling for isLastMeaningfulSibling
                // while contributing zero characters of its own once
                // composerPlainText's matching ZWSP-stripping reads it back.
                parts.push('​');
            } else {
                parts.push(inlineWhatsappMarkupLive(line));
                if (i < lines.length - 1) parts.push('<br>');
            }
        });
        flushList();
        flushQuote();

        let html = parts.join('');

        html = html.replace(/ CB(\d+) /g, (m, idx) =>
            // The leading "\n" is deliberate, not the code's own: per the
            // HTML spec, a <pre> silently swallows exactly one newline if
            // it's the very first character of its content, so a code block
            // whose captured text starts with its own real newline (the
            // overwhelmingly common case - a fence on its own line followed
            // by the code) would otherwise lose that first line every time
            // this gets parsed back into the DOM - critical here since,
            // unlike whatsappMarkupToHtml's read-only sent-message display,
            // the composer (inbox.js) reconstructs its live text FROM this
            // rendered DOM on every keystroke, so a lost line would be a
            // real, compounding data loss rather than just a display glitch.
            // Adding one extra newline gives the parser something to
            // swallow instead, so the real content survives untouched
            // either way (whether or not it happens to start with "\n").
            `${marker('```')}<pre class="whitespace-pre-wrap font-mono text-[10px] leading-snug bg-black/25 rounded px-1.5 py-1 my-1">\n${codeBlocks[idx]}</pre>${marker('```')}`);

        return html;
    }

    // Substitutes a URL button's {{1}} placeholder with an example/preview value.
    // If the example value is itself already a full URL, it replaces the whole thing.
    function fillButtonUrl(url, example) {
        if (!url) return url;
        const exampleValue = example && example.length ? example[0] : '';
        if (/^https?:\/\//i.test(exampleValue)) return exampleValue;
        return url.replace(/\{\{\s*\d+\s*\}\}/g, () => exampleValue);
    }

    // Renders one WhatsApp-style button pill.
    // b: { text, type, url, phone_number, example }
    // opts.clickable: render a real <a href> for URL/PHONE_NUMBER buttons.
    //                 templates.html passes false - a template being built has
    //                 no real recipient yet, so its buttons are static pills.
    // opts.previewValue: a resolved value to use in place of the button's stored
    //                    example (e.g. a variable's live value, or a CSV column's
    //                    first-row value), used only when clickable is true.
    function renderButtonPill(b, opts) {
        opts = opts || {};
        const label = escapeHtml(b.text || b.type || '(button)');
        const baseClasses = 'flex items-center justify-center gap-1.5 w-full py-2 px-3 bg-[#1f2c34] hover:bg-[#2a3942] text-[#00a884] font-medium text-center rounded text-xs';
        const icon = b.type === 'URL' ? '<i class="fa-solid fa-arrow-up-right-from-square text-[10px]"></i>'
            : (b.type === 'PHONE_NUMBER' || b.type === 'VOICE_CALL') ? '<i class="fa-solid fa-phone text-[10px]"></i>'
            : '';

        if (opts.clickable && b.type === 'URL' && b.url) {
            const example = opts.previewValue ? [opts.previewValue] : b.example;
            const resolvedUrl = fillButtonUrl(b.url, example);
            return `<a class="${baseClasses} hover:underline" href="${escapeAttr(resolvedUrl)}" target="_blank" rel="noopener noreferrer">${icon}${label}</a>`;
        }

        if (opts.clickable && b.type === 'PHONE_NUMBER' && b.phone_number) {
            return `<a class="${baseClasses} hover:underline" href="tel:${escapeAttr(b.phone_number)}">${icon}${label}</a>`;
        }

        return `<div class="${baseClasses}">${icon}${label}</div>`;
    }

    // Paints a WhatsApp bubble (header/media/body/footer/buttons) into the elements
    // identified by `ids` ({ media, header, body, footer, buttons }), given a
    // normalized `data` shape:
    //   headerType: 'text'|'TEXT'|'image'|'IMAGE'|'MEDIA' (case-insensitive)
    //   headerText, headerTextAlwaysShow, headerImageUrl, headerImagePlaceholder
    //   body, bodyPlaceholder
    //   footer
    //   buttonsHtml: array of pre-rendered button HTML strings (see renderButtonPill)
    function paintBubble(ids, data) {
        const mediaEl = document.getElementById(ids.media);
        const headerEl = document.getElementById(ids.header);
        const bodyEl = document.getElementById(ids.body);
        const footerEl = document.getElementById(ids.footer);
        const buttonsEl = document.getElementById(ids.buttons);

        mediaEl.classList.add('hidden');
        mediaEl.innerHTML = '';
        headerEl.classList.add('hidden');
        headerEl.textContent = '';

        const headerType = (data.headerType || '').toLowerCase();
        if (headerType === 'image' || headerType === 'media') {
            if (data.headerImageUrl) {
                mediaEl.classList.remove('hidden');
                mediaEl.innerHTML = `<img src="${data.headerImageUrl}" alt="Header image" class="w-full max-h-48 object-cover rounded-md">`;
            } else if (data.headerImagePlaceholder) {
                mediaEl.classList.remove('hidden');
                mediaEl.innerHTML = data.headerImagePlaceholder;
            }
        } else if (headerType === 'text' && (data.headerTextAlwaysShow || data.headerText)) {
            headerEl.classList.remove('hidden');
            headerEl.textContent = data.headerText || '';
        }

        bodyEl.innerHTML = whatsappMarkupToHtml(data.body || data.bodyPlaceholder || '');

        if (data.footer) {
            footerEl.classList.remove('hidden');
            footerEl.textContent = data.footer;
        } else {
            footerEl.classList.add('hidden');
            footerEl.textContent = '';
        }

        if (data.buttonsHtml && data.buttonsHtml.length) {
            buttonsEl.classList.remove('hidden');
            buttonsEl.innerHTML = data.buttonsHtml.join('');
        } else {
            buttonsEl.classList.add('hidden');
            buttonsEl.innerHTML = '';
        }
    }

    return { escapeHtml, escapeAttr, whatsappMarkupToHtml, liveMarkupToHtml, fillButtonUrl, renderButtonPill, paintBubble };
})();

// Shared WhatsApp template-structure helpers, used by dashboard.html,
// automations.html, and templates.html. These read/interpret a template's
// `components` array from the Meta Graph API - separate from WAPreview,
// which is about painting a bubble once you already have body/header/etc.
window.WATemplates = (function () {
    const CATEGORY_LABELS = { MARKETING: 'Marketing', UTILITY: 'Utility', AUTHENTICATION: 'Authentication' };
    const CATEGORY_ORDER = ['MARKETING', 'UTILITY', 'AUTHENTICATION'];

    // Accepts either a template object ({ components: [...] }) or a components
    // array directly - templates.html's viewer already has just the array.
    function getComponent(templateOrComponents, type) {
        const components = Array.isArray(templateOrComponents) ? templateOrComponents : (templateOrComponents.components || []);
        return components.find(c => c.type === type);
    }

    function countBodyVariables(bodyText) {
        if (!bodyText) return 0;
        const matches = [...bodyText.matchAll(/\{\{\s*(\d+)\s*\}\}/g)].map(m => parseInt(m[1], 10));
        return matches.length ? Math.max(...matches) : 0;
    }

    function isDynamicUrlButton(b) {
        return b.type === 'URL' && /\{\{\s*\d+\s*\}\}/.test(b.url || '');
    }

    // Builds <optgroup>-grouped <option>s for a template-picker <select>.
    // opts.includePlaceholder: prepend a "Select a template…" option
    //                          (automations.html wants this, dashboard.html doesn't).
    function buildTemplateOptions(templates, opts) {
        opts = opts || {};
        const groups = new Map();
        templates.forEach((t, i) => {
            const cat = t.category || 'OTHER';
            if (!groups.has(cat)) groups.set(cat, []);
            groups.get(cat).push(i);
        });

        const orderedCats = [
            ...CATEGORY_ORDER.filter(c => groups.has(c)),
            ...Array.from(groups.keys()).filter(c => !CATEGORY_ORDER.includes(c)).sort(),
        ];

        const placeholder = opts.includePlaceholder ? `<option value="">Select a template…</option>` : '';
        return placeholder + orderedCats.map(cat => {
            const label = CATEGORY_LABELS[cat] || (cat.charAt(0) + cat.slice(1).toLowerCase());
            const options = groups.get(cat).map(i => {
                const t = templates[i];
                return `<option value="${i}">${WAPreview.escapeHtml(t.name)} (${WAPreview.escapeHtml(t.language)}) - ${WAPreview.escapeHtml(t.status)}</option>`;
            }).join('');
            return `<optgroup label="${WAPreview.escapeAttr(label)}">${options}</optgroup>`;
        }).join('');
    }

    // Fetches the full numbers list and populates a <select> with it - the
    // shared first step of dashboard.html's and templates.html's loadNumbers()
    // (each page still does its own thing afterwards: dashboard.html also loads
    // usage/quality, templates.html loads that number's existing templates).
    // Not used by automations.html, whose numbers are unit-scoped
    // (a different endpoint/query entirely, not just a cosmetic difference).
    async function fetchAndPopulateNumbers(selectEl) {
        const res = await fetch('/api/numbers');
        const numbers = await res.json();
        selectEl.innerHTML = numbers.map(n => `<option value="${WAPreview.escapeAttr(n.id)}">${WAPreview.escapeHtml(n.label)}</option>`).join('');
        return numbers;
    }

    // Turns a failed fetch Response into a short, user-facing message.
    // Our own API errors are JSON ({detail: "..."}); anything else (e.g. a
    // Cloudflare/nginx gateway page returned before the request ever reaches
    // the app) is HTML or plain text and must never be dumped in raw - it's
    // long, and it isn't meant for end users.
    async function extractErrorMessage(res) {
        const contentType = res.headers.get('content-type') || '';
        if (contentType.includes('application/json')) {
            try {
                const data = await res.json();
                if (data && typeof data.detail === 'string') return data.detail;
            } catch (e) { /* fall through to generic message */ }
        }
        return `Server error (${res.status}). Please try again in a moment.`;
    }

    return { getComponent, countBodyVariables, isDynamicUrlButton, buildTemplateOptions, fetchAndPopulateNumbers, extractErrorMessage };
})();

// Generic pagination helpers, used by dashboard.html (campaign progress/
// history/modal tables) and automations.html (automation history list).
function paginateSlice(items, page, size) {
    const totalItems = items.length;
    const totalPages = Math.max(1, Math.ceil(totalItems / size));
    page = Math.min(Math.max(1, page), totalPages);
    const start = (page - 1) * size;
    return { page, totalPages, start, end: Math.min(start + size, totalItems), totalItems, pageItems: items.slice(start, start + size) };
}

// Generic sort+paginate helper for admin list tables/cards that rebuild
// their contents from an in-memory JS array on every load/save/delete
// (as opposed to paginateSlice/renderPaginationControls above, which page
// over rows already sitting in the DOM). Used by automations.html's
// registration/form/serving/provider automation lists and
// auto_reply_rules.html's rule list - each fetches its own row array and
// wants the same sort/paginate behaviour without several near-identical
// copies of it. Deliberately matches the Prev/[page numbers]/Next pill
// style already used by automations.html's Recent Automation History
// card and the server-rendered /history and /usage pages, not
// paginateSlice's arrow style above (dashboard.html's own convention) -
// two pagination looks already coexist in this codebase, each kept local
// to where it was first introduced.
//
// config:
//   container: element whose innerHTML is replaced each render (a
//     <tbody> for a table, or any container for a non-table list like
//     auto_reply_rules.html's card list).
//   renderRow(row): returns the HTML string for one row/card.
//   emptyHtml: HTML shown when the full row array is empty.
//   pageSize: rows per page (default 10).
//   paginationEl: element to render Prev/[n]/Next controls into.
//   pageLabelEl (optional): element to show "Page X of Y · N total".
//   afterRender(pageRows) (optional): called after each render with the
//     current page's row slice, for wiring up per-row button handlers
//     against the freshly-rendered DOM.
//   theadEl (optional): enables click-to-sort - any descendant with a
//     [data-sort-key] attribute (and a nested .sort-indicator element
//     for the arrow) becomes a sortable column header.
//   getSortValue(row, key) (optional): defaults to row[key]; override
//     when the sortable value isn't a plain property (e.g. falling back
//     to a second field when the first is null).
function makeListPager(config) {
    const pageSize = config.pageSize || 10;
    let rows = [];
    let currentPage = 1;
    let sortKey = null;
    let sortDir = 'asc';

    function sortedRows() {
        if (!sortKey) return rows;
        const getValue = config.getSortValue || ((row, key) => row[key]);
        const sorted = rows.slice().sort((a, b) => {
            const av = getValue(a, sortKey);
            const bv = getValue(b, sortKey);
            if (av == null && bv == null) return 0;
            if (av == null) return -1;
            if (bv == null) return 1;
            if (typeof av === 'boolean' || typeof bv === 'boolean') return av === bv ? 0 : (av ? 1 : -1);
            if (typeof av === 'number' && typeof bv === 'number') return av - bv;
            return String(av).localeCompare(String(bv), undefined, { sensitivity: 'base' });
        });
        return sortDir === 'desc' ? sorted.reverse() : sorted;
    }

    function updateSortIndicators() {
        if (!config.theadEl) return;
        config.theadEl.querySelectorAll('[data-sort-key]').forEach(th => {
            const indicator = th.querySelector('.sort-indicator');
            if (!indicator) return;
            indicator.textContent = th.dataset.sortKey === sortKey ? (sortDir === 'asc' ? ' ▲' : ' ▼') : '';
        });
    }

    function renderPills(totalPages) {
        const el = config.paginationEl;
        if (!el) return;
        el.innerHTML = '';
        if (totalPages <= 1) return;
        const makeBtn = (label, page, disabled, active) => {
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.textContent = label;
            btn.className = active
                ? 'px-3 py-1.5 rounded-lg text-xs font-semibold bg-brand-primary text-white'
                : 'px-3 py-1.5 rounded-lg text-xs font-semibold border border-slate-300 dark:border-slate-600 hover:border-brand-primary disabled:opacity-40 disabled:cursor-not-allowed disabled:hover:border-slate-300';
            btn.disabled = disabled;
            if (!disabled && !active) btn.addEventListener('click', () => { currentPage = page; render(); });
            return btn;
        };
        el.appendChild(makeBtn('Prev', currentPage - 1, currentPage <= 1, false));
        for (let p = 1; p <= totalPages; p++) el.appendChild(makeBtn(String(p), p, false, p === currentPage));
        el.appendChild(makeBtn('Next', currentPage + 1, currentPage >= totalPages, false));
    }

    function render() {
        const sorted = sortedRows();
        const totalPages = Math.max(1, Math.ceil(sorted.length / pageSize));
        currentPage = Math.min(Math.max(1, currentPage), totalPages);
        const start = (currentPage - 1) * pageSize;
        const pageRows = sorted.slice(start, start + pageSize);

        config.container.innerHTML = rows.length ? pageRows.map(config.renderRow).join('') : config.emptyHtml;

        if (config.afterRender) config.afterRender(pageRows);
        if (config.pageLabelEl) {
            config.pageLabelEl.textContent = rows.length
                ? `Page ${currentPage} of ${totalPages} · ${rows.length} total`
                : '';
        }
        renderPills(totalPages);
        updateSortIndicators();
    }

    if (config.theadEl) {
        config.theadEl.querySelectorAll('[data-sort-key]').forEach(th => {
            th.addEventListener('click', () => {
                const key = th.dataset.sortKey;
                sortDir = (sortKey === key && sortDir === 'asc') ? 'desc' : 'asc';
                sortKey = key;
                currentPage = 1;
                render();
            });
        });
    }

    return {
        setRows(newRows) {
            rows = newRows || [];
            currentPage = 1;
            render();
        },
        render,
    };
}

function renderPaginationControls(wrap, page, totalPages, onPageChange) {
    if (totalPages <= 1) {
        wrap.innerHTML = '';
        return;
    }
    let html = `<button type="button" data-page="${page - 1}" ${page === 1 ? 'disabled' : ''} class="px-2.5 py-1 text-xs rounded border border-slate-300 dark:border-slate-700 disabled:opacity-40 hover:bg-slate-100 dark:hover:bg-slate-700 transition">&larr;</button>`;
    for (let p = 1; p <= totalPages; p++) {
        if (p === 1 || p === totalPages || (p >= page - 1 && p <= page + 1)) {
            html += `<button type="button" data-page="${p}" class="px-2.5 py-1 text-xs rounded border ${p === page ? 'bg-brand-primary text-white border-brand-primary' : 'border-slate-300 dark:border-slate-700 hover:bg-slate-100 dark:hover:bg-slate-700'} transition">${p}</button>`;
        } else if (p === page - 2 || p === page + 2) {
            html += `<span class="px-1 text-xs text-slate-400 self-center">...</span>`;
        }
    }
    html += `<button type="button" data-page="${page + 1}" ${page === totalPages ? 'disabled' : ''} class="px-2.5 py-1 text-xs rounded border border-slate-300 dark:border-slate-700 disabled:opacity-40 hover:bg-slate-100 dark:hover:bg-slate-700 transition">&rarr;</button>`;
    wrap.innerHTML = html;
    wrap.querySelectorAll('button[data-page]').forEach(btn => {
        btn.addEventListener('click', () => onPageChange(parseInt(btn.dataset.page, 10)));
    });
}
