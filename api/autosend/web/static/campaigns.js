document.addEventListener('DOMContentLoaded', () => {
    const state = {
        units: [],
        numbers: [],
        templatesByNumber: {}
    };

    const COMMON_VARIABLES = [
        { label: '-- Select Variable --', value: '' },
        { label: 'First Name', value: '{{first_name}}' },
        { label: 'Last Name', value: '{{last_name}}' },
        { label: 'Event / Form Name', value: '{{event_name}}' },
        { label: 'Amount Due', value: '{{amount_due}}' },
        { label: 'System Reference', value: '{{reference}}' },
        { label: 'Registration Link', value: '{{registration_url}}' },
        { label: 'Custom / Static Text', value: '__CUSTOM__' }
    ];

    async function safeFetch(url, fallback = []) {
        try {
            const res = await fetch(url);
            if (!res.ok) {
                console.warn(`[API] ${url} returned ${res.status}. Using fallback.`);
                return fallback;
            }
            return await res.json();
        } catch (err) {
            console.error(`[API Error] Failed fetching ${url}:`, err);
            return fallback;
        }
    }

    function formatNumberLabel(n) {
        if (!n) return '-';
        const rawPhone = n.display_phone_number || n.phone_number;
        if (rawPhone) return rawPhone;

        if (n.label) {
            if (n.label.includes('—')) return n.label.split('—').pop().trim();
            if (n.label.includes('-')) return n.label.split('-').pop().trim();
            return n.label;
        }
        return `Number #${n.id}`;
    }

    function fillSelect(selectEl, items, valueKey, textKey, placeholder) {
        if (!selectEl) return;
        selectEl.innerHTML = `<option value="">${placeholder}</option>`;
        if (!Array.isArray(items)) return;
        
        items.forEach(item => {
            const opt = document.createElement('option');
            opt.value = item[valueKey];
            opt.textContent = typeof textKey === 'function' ? textKey(item) : item[textKey];
            selectEl.appendChild(opt);
        });
    }

    function fillGroupedTemplateSelect(selectEl, templates, placeholder) {
        if (!selectEl) return;
        selectEl.innerHTML = `<option value="">${placeholder}</option>`;
        if (!Array.isArray(templates) || templates.length === 0) return;

        const groups = {};
        templates.forEach(t => {
            const category = (t.category || 'OTHER').toUpperCase();
            if (!groups[category]) groups[category] = [];
            groups[category].push(t);
        });

        Object.keys(groups).sort().forEach(cat => {
            const optgroup = document.createElement('optgroup');
            optgroup.label = cat;

            groups[cat].forEach(tmpl => {
                const opt = document.createElement('option');
                opt.value = tmpl.name;
                
                const statusStr = tmpl.status ? ` [${tmpl.status.toUpperCase()}]` : '';
                const langStr = tmpl.language ? ` (${tmpl.language})` : '';
                opt.textContent = `${tmpl.name}${langStr}${statusStr}`;

                if (tmpl.status && tmpl.status.toUpperCase() !== 'APPROVED') {
                    opt.disabled = true;
                }

                optgroup.appendChild(opt);
            });

            selectEl.appendChild(optgroup);
        });
    }

    async function fetchTemplatesForNumber(numberId) {
        if (!numberId) return [];
        if (state.templatesByNumber[numberId]) return state.templatesByNumber[numberId];
        
        const tmpls = await safeFetch(`/api/templates?number_id=${numberId}`);
        state.templatesByNumber[numberId] = tmpls;
        return tmpls;
    }

    async function initCampaigns() {
        const [units, nums] = await Promise.all([
            safeFetch('/api/automations/units'),
            safeFetch('/api/numbers')
        ]);

        state.units = units;
        state.numbers = nums;

        const numSelect = document.getElementById('campaign-number-select') || document.getElementById('number-select');
        const tmplSelect = document.getElementById('campaign-template-select') || document.getElementById('template-select');

        if (numSelect) {
            fillSelect(numSelect, state.numbers, 'id', formatNumberLabel, 'Select Number...');
            
            numSelect.addEventListener('change', async () => {
                const numberId = numSelect.value;
                if (!numberId) {
                    fillSelect(tmplSelect, [], 'name', 'name', 'Select Number First...');
                    return;
                }
                if (tmplSelect) {
                    tmplSelect.innerHTML = `<option value="">Loading templates...</option>`;
                    const tmpls = await fetchTemplatesForNumber(numberId);
                    fillGroupedTemplateSelect(tmplSelect, tmpls, 'Select Template...');
                }
            });

            if (state.numbers.length > 0) {
                numSelect.value = state.numbers[0].id;
                const tmpls = await fetchTemplatesForNumber(state.numbers[0].id);
                if (tmplSelect) fillGroupedTemplateSelect(tmplSelect, tmpls, 'Select Template...');
            }
        }

        setupCampaignTemplateAndPreview();
    }

    function setupCampaignTemplateAndPreview() {
        const numSelect = document.getElementById('campaign-number-select') || document.getElementById('number-select');
        const tmplSelect = document.getElementById('campaign-template-select') || document.getElementById('template-select');
        const varWrap = document.getElementById('campaign-variable-fields-container') || document.getElementById('variable-fields-container');
        const varHint = document.getElementById('campaign-variable-fields-hint') || document.getElementById('variable-fields-hint');

        const waBody = document.getElementById('campaign-wa-body') || document.getElementById('wa-body');
        const waHeader = document.getElementById('campaign-wa-header') || document.getElementById('wa-header');
        const waFooter = document.getElementById('campaign-wa-footer') || document.getElementById('wa-footer');

        if (!tmplSelect) return;

        tmplSelect.addEventListener('change', () => {
            const tmplName = tmplSelect.value;
            const numberId = numSelect ? numSelect.value : null;
            const numberTemplates = state.templatesByNumber[numberId] || [];
            const tmpl = numberTemplates.find(t => t.name === tmplName);

            if (!tmpl) {
                if (varWrap) varWrap.innerHTML = '';
                if (varHint) varHint.classList.remove('hidden');
                if (waBody) waBody.textContent = 'Select a template above to see preview.';
                if (waHeader) waHeader.classList.add('hidden');
                if (waFooter) waFooter.classList.add('hidden');
                return;
            }

            if (varHint) varHint.classList.add('hidden');
            if (varWrap) varWrap.innerHTML = '';

            const headerComp = tmpl.components?.find(c => c.type === 'HEADER');
            const isImageHeader = headerComp && (headerComp.format === 'IMAGE' || headerComp.format === 'MEDIA');

            if (isImageHeader) {
                const imgGroup = document.createElement('div');
                imgGroup.className = 'mb-4 p-3 bg-slate-50 dark:bg-slate-800/50 border border-slate-200 dark:border-slate-700 rounded-lg';
                imgGroup.innerHTML = `
                    <label class="block text-xs font-semibold text-slate-700 dark:text-slate-300 mb-1">
                        Header Image <span class="text-slate-400 font-normal">(Optional)</span>
                    </label>
                    <div class="flex items-center gap-2">
                        <input type="text" id="campaign-header-image-url" placeholder="Enter image URL or leave blank..." class="focus-brand text-xs bg-white dark:bg-slate-900 border border-slate-300 dark:border-slate-700 rounded-md px-2.5 py-1.5 dark:text-white flex-1">
                        <input type="file" id="campaign-header-image-file" accept="image/*" class="hidden">
                        <button type="button" onclick="document.getElementById('campaign-header-image-file').click()" class="px-3 py-1.5 text-xs bg-slate-200 dark:bg-slate-700 text-slate-800 dark:text-slate-200 rounded hover:bg-slate-300 dark:hover:bg-slate-600 transition font-medium">
                            Upload...
                        </button>
                    </div>
                `;
                varWrap.appendChild(imgGroup);

                if (waHeader) {
                    waHeader.classList.add('hidden');
                    waHeader.innerHTML = `<div id="campaign-wa-header-img" class="w-full h-32 bg-slate-100 dark:bg-slate-800 rounded flex items-center justify-center text-xs text-slate-400">Image Preview</div>`;
                }

                const fileInput = imgGroup.querySelector('#campaign-header-image-file');
                const urlInput = imgGroup.querySelector('#campaign-header-image-url');
                
                fileInput.addEventListener('change', (e) => {
                    const file = e.target.files[0];
                    if (file) {
                        const reader = new FileReader();
                        reader.onload = (evt) => {
                            urlInput.value = evt.target.result;
                            updateHeaderImgPreview(evt.target.result);
                        };
                        reader.readAsDataURL(file);
                    }
                });

                urlInput.addEventListener('input', () => {
                    updateHeaderImgPreview(urlInput.value);
                });

                function updateHeaderImgPreview(src) {
                    const imgBox = document.getElementById('campaign-wa-header-img');
                    if (src && src.trim()) {
                        if (waHeader) waHeader.classList.remove('hidden');
                        if (imgBox) {
                            imgBox.innerHTML = `<img src="${src}" class="w-full h-32 object-cover rounded" alt="Header Preview" onerror="this.outerHTML='<div class=\'w-full h-32 bg-red-50 text-red-400 text-xs flex items-center justify-center rounded\'>Invalid Image URL</div>'">`;
                        }
                    } else {
                        if (waHeader) waHeader.classList.add('hidden');
                    }
                }
            } else if (waHeader) {
                waHeader.classList.add('hidden');
            }

            const bodyComp = tmpl.components?.find(c => c.type === 'BODY');
            const bodyText = bodyComp ? bodyComp.text : (tmpl.body_text || tmpl.text || '');
            const matches = [...new Set(bodyText.match(/\{\{([^}]+)\}\}/g) || [])];

            matches.forEach(m => {
                const varName = m.replace(/[\{\}]/g, '').trim();
                const fieldGroup = document.createElement('div');
                fieldGroup.className = 'flex flex-col sm:flex-row items-stretch sm:items-center gap-2 bg-white dark:bg-slate-800 p-2 rounded border border-slate-200 dark:border-slate-700 mb-2';

                const optionsHtml = COMMON_VARIABLES.map(v => `<option value="${v.value}">${v.label}</option>`).join('');

                fieldGroup.innerHTML = `
                    <span class="text-xs font-mono bg-slate-100 dark:bg-slate-700/80 px-2.5 py-1 rounded text-slate-700 dark:text-slate-300 border border-slate-200 dark:border-slate-600 shrink-0">
                        {{${varName}}}
                    </span>
                    <select data-var="${varName}" class="tmpl-var-select focus-brand text-xs bg-slate-50 dark:bg-slate-900 border border-slate-300 dark:border-slate-700 rounded-md px-2 py-1.5 dark:text-white flex-1">
                        ${optionsHtml}
                    </select>
                    <input type="text" data-var="${varName}" placeholder="Static value" class="tmpl-var-custom hidden focus-brand text-xs bg-slate-50 dark:bg-slate-900 border border-slate-300 dark:border-slate-700 rounded-md px-2 py-1.5 dark:text-white flex-1">
                `;

                if (varWrap) varWrap.appendChild(fieldGroup);

                const sel = fieldGroup.querySelector('.tmpl-var-select');
                const inp = fieldGroup.querySelector('.tmpl-var-custom');

                sel.addEventListener('change', () => {
                    if (sel.value === '__CUSTOM__') {
                        inp.classList.remove('hidden');
                        inp.focus();
                    } else {
                        inp.classList.add('hidden');
                    }
                });
            });

            const updatePreview = () => {
                let updatedBody = bodyText;
                const groups = varWrap ? varWrap.querySelectorAll('div.flex') : [];
                
                groups.forEach(group => {
                    const sel = group.querySelector('.tmpl-var-select');
                    const inp = group.querySelector('.tmpl-var-custom');
                    if (!sel) return;

                    const key = sel.getAttribute('data-var');
                    let val = sel.value;

                    if (val === '__CUSTOM__') {
                        val = inp.value.trim() || `{{${key}}}`;
                    } else if (!val) {
                        val = `{{${key}}}`;
                    }

                    updatedBody = updatedBody.replaceAll(`{{${key}}}`, val);
                });

                if (waBody) waBody.innerHTML = updatedBody.replace(/\n/g, '<br>');
            };

            if (varWrap) {
                varWrap.addEventListener('change', updatePreview);
                varWrap.addEventListener('input', updatePreview);
            }
            updatePreview();
        });
    }

    initCampaigns();
});
