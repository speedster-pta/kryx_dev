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
        const baseClasses = 'block w-full py-2 px-3 bg-[#1f2c34] hover:bg-[#2a3942] text-[#00a884] font-medium text-center rounded text-xs';

        if (opts.clickable && b.type === 'URL' && b.url) {
            const example = opts.previewValue ? [opts.previewValue] : b.example;
            const resolvedUrl = fillButtonUrl(b.url, example);
            return `<a class="${baseClasses} hover:underline" href="${escapeAttr(resolvedUrl)}" target="_blank" rel="noopener noreferrer">${label} &#8599;</a>`;
        }

        if (opts.clickable && b.type === 'PHONE_NUMBER' && b.phone_number) {
            return `<a class="${baseClasses} hover:underline" href="tel:${escapeAttr(b.phone_number)}">${label} &#9742;</a>`;
        }

        return `<div class="${baseClasses}">${label}</div>`;
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

    return { escapeHtml, escapeAttr, whatsappMarkupToHtml, fillButtonUrl, renderButtonPill, paintBubble };
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

    return { getComponent, countBodyVariables, isDynamicUrlButton, buildTemplateOptions, fetchAndPopulateNumbers };
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
