document.addEventListener('DOMContentLoaded', () => {
    const state = {
        units: [],
        numbers: [],
        templatesByNumber: {},
        freeTemplates: [],
        paidTemplates: [],
        formMappings: []
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
            if (n.label.includes('—')) {
                return n.label.split('—').pop().trim();
            }
            if (n.label.includes('-')) {
                return n.label.split('-').pop().trim();
            }
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

    async function initData() {
        const [units, nums, formMaps, freeTmpls, paidTmpls] = await Promise.all([
            safeFetch('/api/automations/units'),
            safeFetch('/api/numbers'),
            safeFetch('/api/automations/form-mappings'),
            safeFetch('/api/automations/registration-templates?template_type=free_acknowledgment'),
            safeFetch('/api/automations/registration-templates?template_type=payment_reminder')
        ]);

        state.units = units;
        state.numbers = nums;
        state.formMappings = formMaps;
        state.freeTemplates = freeTmpls;
        state.paidTemplates = paidTmpls;

        console.log('Dashboard Initialized:', state);

        ['free', 'paid', 'form'].forEach(prefix => {
            fillSelect(
                document.getElementById(`${prefix}-unit-select`), 
                state.units, 
                'id', 
                'name', 
                'Select Unit...'
            );
            fillSelect(
                document.getElementById(`${prefix}-number-select`), 
                state.numbers, 
                'id', 
                formatNumberLabel, 
                'Select Number...'
            );

            const numSelect = document.getElementById(`${prefix}-number-select`);
            const tmplSelect = document.getElementById(`${prefix}-template-select`);

            if (numSelect && tmplSelect) {
                numSelect.addEventListener('change', async () => {
                    const numberId = numSelect.value;
                    if (!numberId) {
                        fillSelect(tmplSelect, [], 'name', 'name', 'Select Number First...');
                        return;
                    }
                    tmplSelect.innerHTML = `<option value="">Loading templates...</option>`;
                    const tmpls = await fetchTemplatesForNumber(numberId);
                    fillGroupedTemplateSelect(tmplSelect, tmpls, 'Select Template...');
                });
            }
        });

        if (state.numbers.length > 0) {
            const defaultNumId = state.numbers[0].id;
            const tmpls = await fetchTemplatesForNumber(defaultNumId);
            ['free', 'paid', 'form'].forEach(prefix => {
                const numSelect = document.getElementById(`${prefix}-number-select`);
                const tmplSelect = document.getElementById(`${prefix}-template-select`);
                if (numSelect && tmplSelect) {
                    numSelect.value = defaultNumId;
                    fillGroupedTemplateSelect(tmplSelect, tmpls, 'Select Template...');
                }
            });
        }

        renderTables();
    }

    function renderTables() {
        renderRegTable('free', state.freeTemplates);
        renderRegTable('paid', state.paidTemplates);
        renderFormTable();
    }

    function renderRegTable(type, items) {
        const tbody = document.getElementById(`${type}-list-body`);
        if (!tbody) return;

        if (!items || items.length === 0) {
            tbody.innerHTML = `<tr><td colspan="5" class="px-5 py-4 text-center text-slate-500">No ${type} automations configured yet.</td></tr>`;
            return;
        }

        tbody.innerHTML = items.map(item => {
            const u = state.units.find(un => un.id === item.unit_id)?.name || item.unit_id || '-';
            const numObj = state.numbers.find(n => n.id === item.whatsapp_number_id);
            const num = numObj ? formatNumberLabel(numObj) : (item.whatsapp_number_id || '-');
            const isActive = item.active ?? true;
            const statusBg = isActive ? 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900/30 dark:text-emerald-400' : 'bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-400';

            return `
                <tr class="hover:bg-slate-50 dark:hover:bg-slate-800/50 transition">
                    <td class="px-5 py-3.5 font-medium text-slate-900 dark:text-white">${u}</td>
                    <td class="px-5 py-3.5">${num}</td>
                    <td class="px-5 py-3.5 font-mono text-xs">${item.template_name || '-'}</td>
                    <td class="px-5 py-3.5">
                        <span class="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium ${statusBg}">
                            ${isActive ? 'Active' : 'Disabled'}
                        </span>
                    </td>
                    <td class="px-5 py-3.5 text-right space-x-2">
                        <button onclick="window.editRegAutomation('${type}', ${item.id})" class="text-brand-primary hover:underline font-medium text-xs">Edit</button>
                    </td>
                </tr>
            `;
        }).join('');
    }

    function renderFormTable() {
        const tbody = document.getElementById('form-list-body');
        if (!tbody) return;

        const items = state.formMappings;

        if (!items || items.length === 0) {
            tbody.innerHTML = `<tr><td colspan="6" class="px-5 py-4 text-center text-slate-500">No form response automations configured yet.</td></tr>`;
            return;
        }

        tbody.innerHTML = items.map(item => {
            const u = state.units.find(un => un.id === item.unit_id)?.name || item.unit_id || '-';
            const numObj = state.numbers.find(n => n.id === item.whatsapp_number_id);
            const num = numObj ? formatNumberLabel(numObj) : (item.whatsapp_number_id || '-');
            const isActive = item.active ?? item.is_active ?? true;
            const statusBg = isActive ? 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900/30 dark:text-emerald-400' : 'bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-400';

            return `
                <tr class="hover:bg-slate-50 dark:hover:bg-slate-800/50 transition">
                    <td class="px-5 py-3.5 font-medium text-slate-900 dark:text-white">${u}</td>
                    <td class="px-5 py-3.5 font-mono text-xs">${item.pco_form_id || '-'}</td>
                    <td class="px-5 py-3.5">${num}</td>
                    <td class="px-5 py-3.5 font-mono text-xs">${item.template_name || '-'}</td>
                    <td class="px-5 py-3.5">
                        <span class="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium ${statusBg}">
                            ${isActive ? 'Active' : 'Disabled'}
                        </span>
                    </td>
                    <td class="px-5 py-3.5 text-right space-x-2">
                        <button onclick="window.editFormAutomation(${item.id})" class="text-brand-primary hover:underline font-medium text-xs">Edit</button>
                    </td>
                </tr>
            `;
        }).join('');
    }

    function setupDynamicFormAndPreview(prefix) {
        const numSelect = document.getElementById(`${prefix}-number-select`);
        const tmplSelect = document.getElementById(`${prefix}-template-select`);
        const varWrap = document.getElementById(`${prefix}-variable-fields-container`);
        const varHint = document.getElementById(`${prefix}-variable-fields-hint`);

        const waBody = document.getElementById(`${prefix}-wa-body`);
        const waHeader = document.getElementById(`${prefix}-wa-header`);
        const waFooter = document.getElementById(`${prefix}-wa-footer`);

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

            // Render optional image upload directly inside the variable fields container
            if (isImageHeader) {
                const imgGroup = document.createElement('div');
                imgGroup.className = 'mb-4 p-3 bg-slate-50 dark:bg-slate-800/50 border border-slate-200 dark:border-slate-700 rounded-lg';
                imgGroup.innerHTML = `
                    <label class="block text-xs font-semibold text-slate-700 dark:text-slate-300 mb-1">
                        Header Image <span class="text-slate-400 font-normal">(Optional)</span>
                    </label>
                    <div class="flex items-center gap-2">
                        <input type="text" id="${prefix}-header-image-url" placeholder="Enter image URL or leave blank..." class="focus-brand text-xs bg-white dark:bg-slate-900 border border-slate-300 dark:border-slate-700 rounded-md px-2.5 py-1.5 dark:text-white flex-1">
                        <input type="file" id="${prefix}-header-image-file" accept="image/*" class="hidden">
                        <button type="button" onclick="document.getElementById('${prefix}-header-image-file').click()" class="px-3 py-1.5 text-xs bg-slate-200 dark:bg-slate-700 text-slate-800 dark:text-slate-200 rounded hover:bg-slate-300 dark:hover:bg-slate-600 transition font-medium">
                            Upload...
                        </button>
                    </div>
                `;
                varWrap.appendChild(imgGroup);

                if (waHeader) {
                    waHeader.classList.add('hidden');
                    waHeader.innerHTML = `<div id="${prefix}-wa-header-img" class="w-full h-32 bg-slate-100 dark:bg-slate-800 rounded flex items-center justify-center text-xs text-slate-400">Image Preview</div>`;
                }

                const fileInput = imgGroup.querySelector(`#${prefix}-header-image-file`);
                const urlInput = imgGroup.querySelector(`#${prefix}-header-image-url`);
                
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
                    const imgBox = document.getElementById(`${prefix}-wa-header-img`);
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

    ['free', 'paid', 'form'].forEach(setupDynamicFormAndPreview);

    initData();
});
