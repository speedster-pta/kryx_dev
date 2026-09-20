// Knowledge Base page (see admin_pages.KnowledgeBaseView / web/knowledge_router.py).
//
// Ported from the single-tenant parent project's knowledge_base.js, adapted
// to this app's org/unit model: a "congregation" there is a "unit" here, and
// there's no cross-org "Global" concept - the nearest equivalent is an
// org-wide entry (unit_id === null), scoped to the caller's own org just
// like every unit-specific one (see storage/knowledge_base.py's docstring).
// The API here also returns one row per chunk rather than pre-aggregated
// documents, so groupEntries() below does client-side what the parent
// project's /api/knowledge endpoint did server-side.
(function () {
  const unitSelect = document.getElementById('kb-unit-select');
  const visibilityFilter = document.getElementById('kb-visibility-filter');
  const statusEl = document.getElementById('kb-status');
  const tabButtons = document.querySelectorAll('.kb-tab-btn');
  const tabPanels = document.querySelectorAll('.kb-tab-panel');

  const manualForm = document.getElementById('kb-manual-form');
  const manualQuestion = document.getElementById('kb-manual-question');
  const manualAnswer = document.getElementById('kb-manual-answer');

  const scrapeForm = document.getElementById('kb-scrape-form');
  const scrapeUrl = document.getElementById('kb-scrape-url');
  const scrapeTitle = document.getElementById('kb-scrape-title');
  const scrapePreview = document.getElementById('kb-scrape-preview');
  const scrapeSubmit = document.getElementById('kb-scrape-submit');
  const scrapeSubmitLabel = scrapeSubmit.querySelector('.kb-scrape-submit-label');

  const uploadForm = document.getElementById('kb-upload-form');
  const uploadFile = document.getElementById('kb-upload-file');
  const uploadTitle = document.getElementById('kb-upload-title');
  const uploadPreview = document.getElementById('kb-upload-preview');
  const uploadSubmit = document.getElementById('kb-upload-submit');
  const uploadSubmitLabel = uploadSubmit.querySelector('.kb-upload-submit-label');

  const entriesBody = document.getElementById('kb-entries-body');
  const entriesPageInfo = document.getElementById('kb-entries-page-info');
  const entriesPagination = document.getElementById('kb-entries-pagination');
  const sortableHeaders = document.querySelectorAll('#kb-entries-table th.kb-sortable');
  const ENTRIES_PAGE_SIZE = 10;

  // Every grouped document (one row per source, not per chunk) currently
  // loaded for the selected scope, unsorted/unpaginated - renderEntriesPage()
  // derives what's actually shown (filtered, sorted, then sliced to one
  // page) from this on every render, the same split dashboard.html's
  // createPaginatedTable uses (see paginateSlice/renderPaginationControls
  // in app.js).
  const entriesState = { items: [], page: 1, sortKey: null, sortDir: 1, currentPageGroups: [] };

  const chunksOverlay = document.getElementById('kb-chunks-overlay');
  const chunksTitle = document.getElementById('kb-chunks-title');
  const chunksSubtitle = document.getElementById('kb-chunks-subtitle');
  const chunksBody = document.getElementById('kb-chunks-body');
  const chunksCloseBtn = document.getElementById('kb-chunks-close-btn');

  // 'org-wide' is a legitimate selection here (any staff member can view
  // org-wide entries; only an org admin can create/edit/delete them, which
  // the server re-checks on every write), so hasUnits - not
  // currentUnitId's truthiness - is what guards "nothing to load yet"
  // below.
  let currentUnitId = null; // null while scope === 'org-wide'
  let currentScope = 'org-wide';
  let hasUnits = false;
  let units = [];

  function showStatus(message, isError) {
    statusEl.textContent = message;
    statusEl.classList.remove('hidden', 'bg-rose-50', 'text-rose-700', 'dark:bg-rose-900/30', 'dark:text-rose-400',
      'bg-emerald-50', 'text-emerald-700', 'dark:bg-emerald-900/30', 'dark:text-emerald-400');
    statusEl.classList.add(...(isError
      ? ['bg-rose-50', 'text-rose-700', 'dark:bg-rose-900/30', 'dark:text-rose-400']
      : ['bg-emerald-50', 'text-emerald-700', 'dark:bg-emerald-900/30', 'dark:text-emerald-400']));
  }

  function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str == null ? '' : String(str);
    return div.innerHTML;
  }

  tabButtons.forEach((btn) => {
    btn.addEventListener('click', () => {
      tabButtons.forEach((b) => b.classList.toggle('active', b === btn));
      tabPanels.forEach((p) => p.classList.toggle('hidden', p.dataset.tab !== btn.dataset.tab));
    });
  });

  async function loadUnits() {
    // /api/ai/units (not /api/automations/units) - the latter lives on the
    // PCO automations router and 403s for any org that has AI Assistant
    // enabled without also having the (unrelated) PCO module enabled.
    const res = await fetch('/api/ai/units');
    units = res.ok ? await res.json() : [];
    const unitOptions = units.map((u) => `<option value="${u.id}">${escapeHtml(u.name)}</option>`).join('');
    unitSelect.innerHTML = '<option value="org-wide">Org-wide (all units)</option>' + unitOptions;
    hasUnits = true; // org-wide is always a valid scope, even with zero units
    currentScope = 'org-wide';
    currentUnitId = null;
    unitSelect.value = 'org-wide';
  }

  function unitName(unitId) {
    const unit = units.find((u) => u.id === unitId);
    return unit ? unit.name : `Unit ${unitId}`;
  }

  function sourceLabel(entry) {
    if (entry.source_type === 'manual') return 'Manual';
    if (entry.source_type === 'url') return 'URL';
    if (entry.source_type === 'pdf') return 'PDF';
    return entry.source_type;
  }

  // storage.list_knowledge_base_entries returns one row per chunk; group
  // into one row per document here so a scraped page/PDF with many chunks
  // shows as a single table row with a FAQ count, "View FAQs", and
  // document-level toggle/delete/re-scrape actions. A manual Q&A entry is
  // already its own document (source_ref is always null, and its own id
  // makes the group key unique), so it keeps the original single-entry
  // actions instead.
  function groupEntries(entries) {
    const groups = [];
    const byKey = new Map();
    for (const e of entries) {
      const key = `${e.source_type}|${e.unit_id}|${e.source_type === 'manual' ? e.id : e.source_ref}`;
      let group = byKey.get(key);
      if (!group) {
        group = {
          source_type: e.source_type,
          source_ref: e.source_ref,
          unit_id: e.unit_id,
          entry_id: e.source_type === 'manual' ? e.id : null,
          title: e.document_title || e.title,
          answer: e.content,
          is_active: e.is_active,
          last_refreshed_at: e.last_refreshed_at,
          chunk_count: 0,
        };
        byKey.set(key, group);
        groups.push(group);
      }
      group.chunk_count += 1;
      // A group only reads as active if every chunk in it is - a
      // partially-deactivated group (which the bulk toggle below never
      // itself produces, but an individual chunk edit could) should
      // surface that inconsistency rather than hide it.
      group.is_active = group.is_active && e.is_active;
      if (e.last_refreshed_at > group.last_refreshed_at) group.last_refreshed_at = e.last_refreshed_at;
    }
    return groups;
  }

  function formatDate(iso) {
    if (!iso) return '—';
    try {
      return new Date(iso).toLocaleString();
    } catch (e) {
      return iso;
    }
  }

  function renderEntries(groups) {
    if (!groups.length) {
      entriesBody.innerHTML = '<tr><td colspan="6" class="px-6 py-6 text-center text-slate-400">No knowledge base entries yet.</td></tr>';
      return;
    }
    entriesBody.innerHTML = groups.map((g, i) => {
      const statusBadge = `<span class="px-2 py-1 rounded-full text-xs font-semibold ${g.is_active ? 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900/30 dark:text-emerald-400' : 'bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-400'}">${g.is_active ? 'Active' : 'Inactive'}</span>`;
      const toggleLabel = g.is_active ? 'Deactivate' : 'Activate';
      const scopeBadge = g.unit_id === null
        ? '<span class="ml-2 px-2 py-0.5 rounded-full text-xs font-semibold bg-indigo-100 text-indigo-700 dark:bg-indigo-900/30 dark:text-indigo-400">Org-wide</span>'
        : '';
      const rescrapeBtn = g.source_type === 'url'
        ? `<button type="button" class="kb-rescrape-btn text-slate-500 hover:text-brand-primary mr-3" data-index="${i}" title="Re-scrape"><i class="fa-solid fa-rotate"></i></button>`
        : '';
      // Manual entries are already a single chunk fully shown as the
      // answer preview below - only url/pdf groups need a way to inspect
      // every chunk behind the aggregated row.
      const viewChunksBtn = g.source_type !== 'manual'
        ? `<button type="button" class="kb-view-chunks-btn text-slate-500 hover:text-brand-primary mr-3" data-index="${i}" title="View FAQs"><i class="fa-solid fa-list"></i></button>`
        : '';
      const toggleBtn = `<button type="button" class="kb-toggle-btn text-slate-500 hover:text-brand-primary mr-3" data-index="${i}" title="${toggleLabel}">
              <i class="fa-solid ${g.is_active ? 'fa-toggle-on' : 'fa-toggle-off'}"></i>
            </button>`;
      const deleteBtn = `<button type="button" class="kb-delete-btn text-slate-500 hover:text-rose-600" data-index="${i}" title="Delete">
              <i class="fa-solid fa-trash"></i>
            </button>`;
      return `
        <tr>
          <td class="px-6 py-4">${sourceLabel(g)}</td>
          <td class="px-6 py-4 max-w-md">
            <div class="font-medium">${escapeHtml(g.title)}${scopeBadge}</div>
            <div class="text-xs text-slate-400 truncate">${escapeHtml(g.source_ref || g.answer || '')}</div>
          </td>
          <td class="px-6 py-4">${g.chunk_count}</td>
          <td class="px-6 py-4">${formatDate(g.last_refreshed_at)}</td>
          <td class="px-6 py-4">${statusBadge}</td>
          <td class="px-6 py-4 text-right whitespace-nowrap">
            ${viewChunksBtn}
            ${rescrapeBtn}
            ${toggleBtn}
            ${deleteBtn}
          </td>
        </tr>`;
    }).join('');
  }

  function sortValue(group, key) {
    switch (key) {
      case 'source': return sourceLabel(group).toLowerCase();
      case 'title': return (group.title || '').toLowerCase();
      case 'faqs': return group.chunk_count;
      case 'last_refreshed': return group.last_refreshed_at ? new Date(group.last_refreshed_at).getTime() : 0;
      case 'status': return group.is_active ? 1 : 0;
      default: return '';
    }
  }

  function updateSortIndicators() {
    sortableHeaders.forEach((th) => {
      const arrow = th.querySelector('.kb-sort-arrow');
      const active = th.dataset.sort === entriesState.sortKey;
      arrow.innerHTML = active && entriesState.sortDir === -1 ? '&#9660;' : '&#9650;';
      arrow.classList.toggle('opacity-0', !active);
    });
  }

  // "This scope only" / "Org-wide only" mirrors the parent project's
  // local/global split, renamed for this app's unit-based scoping: when
  // the selected scope is a specific unit, its fetched entries already
  // include that unit's own rows *and* org-wide ones (the storage layer's
  // "OR unit_id IS NULL" relaxation) - this filter narrows between them.
  // When the selected scope is org-wide itself, every fetched entry is
  // already org-wide, so "This scope only" is empty by construction.
  function visibleGroups() {
    const mode = visibilityFilter.value;
    if (mode === 'global') return entriesState.items.filter((g) => g.unit_id === null);
    if (mode === 'local') return entriesState.items.filter((g) => g.unit_id !== null);
    return entriesState.items;
  }

  function renderEntriesPage() {
    const filtered = visibleGroups();
    const items = entriesState.sortKey
      ? [...filtered].sort((a, b) => {
          const av = sortValue(a, entriesState.sortKey);
          const bv = sortValue(b, entriesState.sortKey);
          if (av < bv) return -1 * entriesState.sortDir;
          if (av > bv) return 1 * entriesState.sortDir;
          return 0;
        })
      : filtered;
    const { page, totalPages, start, end, totalItems, pageItems } = paginateSlice(items, entriesState.page, ENTRIES_PAGE_SIZE);
    entriesState.page = page;
    entriesState.currentPageGroups = pageItems;
    renderEntries(pageItems);
    entriesPageInfo.textContent = totalItems ? `Showing ${start + 1}–${end} of ${totalItems}` : '';
    renderPaginationControls(entriesPagination, page, totalPages, (p) => { entriesState.page = p; renderEntriesPage(); });
  }

  sortableHeaders.forEach((th) => {
    th.addEventListener('click', () => {
      const key = th.dataset.sort;
      if (entriesState.sortKey === key) {
        entriesState.sortDir *= -1;
      } else {
        entriesState.sortKey = key;
        entriesState.sortDir = 1;
      }
      entriesState.page = 1;
      updateSortIndicators();
      renderEntriesPage();
    });
  });

  async function loadEntries() {
    if (!hasUnits) return;
    entriesBody.innerHTML = '<tr><td colspan="6" class="px-6 py-6 text-center text-slate-400">Loading...</td></tr>';
    entriesPageInfo.textContent = '';
    entriesPagination.innerHTML = '';
    const params = new URLSearchParams();
    if (currentUnitId !== null) params.set('unit_id', currentUnitId);
    const res = await fetch(`/api/knowledge/entries${params.toString() ? '?' + params : ''}`);
    if (!res.ok) {
      entriesBody.innerHTML = '<tr><td colspan="6" class="px-6 py-6 text-center text-rose-500">Failed to load entries.</td></tr>';
      return;
    }
    let entries = await res.json();
    // Selecting the org-wide scope has no direct server-side filter (an
    // unset unit_id fetches every entry the caller can see) - narrow to
    // org-wide-only client-side, same as the pre-existing unit-filter
    // behaviour this page replaces.
    if (currentScope === 'org-wide') entries = entries.filter((e) => e.unit_id === null);
    entriesState.items = groupEntries(entries);
    entriesState.page = 1;
    renderEntriesPage();
  }

  unitSelect.addEventListener('change', () => {
    currentScope = unitSelect.value;
    currentUnitId = currentScope === 'org-wide' ? null : parseInt(currentScope, 10);
    loadEntries();
  });

  visibilityFilter.addEventListener('change', () => {
    entriesState.page = 1;
    renderEntriesPage();
  });

  manualForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    const res = await fetch('/api/knowledge/entries/manual', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        unit_id: currentUnitId,
        title: manualQuestion.value,
        content: manualAnswer.value,
      }),
    });
    const result = await res.json().catch(() => ({}));
    if (!res.ok) {
      showStatus(result.detail || 'Failed to save Q&A.', true);
      return;
    }
    showStatus('Saved.', false);
    manualForm.reset();
    loadEntries();
  });

  async function runScrape(url, title) {
    const res = await fetch('/api/knowledge/entries/url', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ unit_id: currentUnitId, url, title: title || null }),
    });
    const result = await res.json().catch(() => ({}));
    if (!res.ok) {
      showStatus(result.detail || 'Failed to scrape URL.', true);
      return null;
    }
    return result;
  }

  // The ingest endpoints here only return the new entry_ids (unlike the
  // parent project's, which returned a ready-made {title, chunk_count,
  // preview} summary) - re-fetch and pick out the freshly (re-)ingested
  // group by source_ref to build the same preview box from real data.
  async function describeSource(sourceType, sourceRef) {
    const params = new URLSearchParams({ source_type: sourceType, source_ref: sourceRef });
    if (currentUnitId !== null) params.set('unit_id', currentUnitId);
    const res = await fetch(`/api/knowledge/sources/chunks?${params}`);
    if (!res.ok) return null;
    const chunks = await res.json();
    if (!chunks.length) return null;
    return {
      title: chunks[0].document_title || chunks[0].title,
      chunk_count: chunks.length,
      preview: chunks[0].content,
    };
  }

  scrapeForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    scrapePreview.classList.add('hidden');
    scrapeSubmit.disabled = true;
    scrapeSubmitLabel.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Scraping...';
    showStatus('Scraping page and generating FAQs - this can take a moment...', false);
    try {
      const result = await runScrape(scrapeUrl.value, scrapeTitle.value);
      if (!result) return;
      const summary = await describeSource('url', scrapeUrl.value.trim());
      if (summary) {
        scrapePreview.innerHTML = `<div class="font-semibold mb-1">${escapeHtml(summary.title)}</div>
          <div class="text-xs text-slate-400 mb-2">${summary.chunk_count} FAQ(s) saved</div>
          <div class="text-slate-600 dark:text-slate-300">${escapeHtml(summary.preview)}</div>`;
        scrapePreview.classList.remove('hidden');
      }
      showStatus('Saved.', false);
      // Only the title is cleared (not the URL) - leaving a just-used title
      // in place would silently reapply it to whatever different URL staff
      // scrape next, since it's a distinct field from the URL this form
      // otherwise leaves untouched after a successful scrape.
      scrapeTitle.value = '';
      loadEntries();
    } finally {
      scrapeSubmit.disabled = false;
      scrapeSubmitLabel.textContent = 'Scrape & Save';
    }
  });

  uploadForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    uploadPreview.classList.add('hidden');
    const file = uploadFile.files[0];
    if (!file) return;
    const formData = new FormData();
    formData.append('file', file);
    const params = new URLSearchParams();
    if (currentUnitId !== null) params.set('unit_id', currentUnitId);
    if (uploadTitle.value.trim()) params.set('title', uploadTitle.value.trim());
    uploadSubmit.disabled = true;
    uploadSubmitLabel.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Uploading...';
    showStatus('Uploading PDF and generating FAQs - this can take a moment...', false);
    try {
      const res = await fetch(`/api/knowledge/entries/pdf?${params}`, {
        method: 'POST',
        body: formData,
      });
      const result = await res.json().catch(() => ({}));
      if (!res.ok) {
        showStatus(result.detail || 'Failed to upload PDF.', true);
        return;
      }
      const summary = await describeSource('pdf', file.name);
      if (summary) {
        uploadPreview.innerHTML = `<div class="font-semibold mb-1">${escapeHtml(summary.title)}</div>
          <div class="text-xs text-slate-400 mb-2">${summary.chunk_count} FAQ(s) saved</div>
          <div class="text-slate-600 dark:text-slate-300">${escapeHtml(summary.preview)}</div>`;
        uploadPreview.classList.remove('hidden');
      }
      showStatus('Saved.', false);
      uploadForm.reset();
      loadEntries();
    } finally {
      uploadSubmit.disabled = false;
      uploadSubmitLabel.textContent = 'Upload & Save';
    }
  });

  // Tracks the currently open modal's group so edit/delete handlers can
  // reload it afterwards without the caller having to re-thread these
  // through every action.
  let currentChunksGroup = null;

  async function openChunksModal(group) {
    currentChunksGroup = group;
    chunksTitle.textContent = group.title;
    chunksSubtitle.textContent = group.source_ref || '';
    chunksBody.innerHTML = '<p class="text-sm text-slate-400 text-center">Loading...</p>';
    chunksOverlay.classList.remove('hidden');

    const params = new URLSearchParams({ source_type: group.source_type, source_ref: group.source_ref });
    if (group.unit_id !== null) params.set('unit_id', group.unit_id);
    const res = await fetch(`/api/knowledge/sources/chunks?${params}`);
    if (!res.ok) {
      chunksBody.innerHTML = '<p class="text-sm text-rose-500 text-center">Failed to load FAQs.</p>';
      return;
    }
    const chunks = await res.json();
    chunksSubtitle.textContent = `${group.source_ref || ''} — ${chunks.length} FAQ(s)`;
    chunksBody.innerHTML = chunks.map((c) => `
      <div class="kb-chunk-row border border-slate-200 dark:border-slate-700 rounded-lg p-3" data-chunk-id="${c.id}">
        <div class="flex items-center justify-between mb-1">
          <div class="text-xs font-semibold text-slate-400">FAQ ${c.chunk_index + 1} of ${chunks.length}${c.is_active ? '' : ' (inactive)'}</div>
          <div class="flex gap-3">
            <button type="button" class="kb-chunk-edit-btn text-slate-400 hover:text-brand-primary" data-chunk-id="${c.id}" data-title="${escapeHtml(c.title)}" title="Edit"><i class="fa-solid fa-pen"></i></button>
            <button type="button" class="kb-chunk-delete-btn text-slate-400 hover:text-rose-600" data-chunk-id="${c.id}" title="Delete"><i class="fa-solid fa-trash"></i></button>
          </div>
        </div>
        <div class="kb-chunk-content text-sm text-slate-700 dark:text-slate-200 whitespace-pre-wrap">${escapeHtml(c.content)}</div>
      </div>
    `).join('') || '<p class="text-sm text-slate-400 text-center">No FAQs found.</p>';
  }

  function refreshChunksModal() {
    if (!currentChunksGroup) return;
    openChunksModal(currentChunksGroup);
  }

  function closeChunksModal() {
    chunksOverlay.classList.add('hidden');
    currentChunksGroup = null;
  }

  chunksCloseBtn.addEventListener('click', closeChunksModal);
  chunksOverlay.addEventListener('click', (event) => {
    if (event.target === chunksOverlay) closeChunksModal();
  });

  chunksBody.addEventListener('click', async (event) => {
    const editBtn = event.target.closest('.kb-chunk-edit-btn');
    const deleteBtn = event.target.closest('.kb-chunk-delete-btn');
    const saveBtn = event.target.closest('.kb-chunk-save-btn');
    const cancelBtn = event.target.closest('.kb-chunk-cancel-btn');

    if (editBtn) {
      const wrapper = event.target.closest('.kb-chunk-row');
      const contentEl = wrapper.querySelector('.kb-chunk-content');
      const currentText = contentEl.textContent;
      contentEl.outerHTML = `
        <div class="kb-chunk-content space-y-2">
          <textarea class="w-full text-sm rounded-lg border border-slate-300 dark:border-slate-600 bg-slate-50 dark:bg-slate-900 p-2.5" rows="4">${escapeHtml(currentText)}</textarea>
          <div class="flex gap-2">
            <button type="button" class="kb-chunk-save-btn bg-brand-primary hover:bg-brand-primary-hover text-white text-xs font-semibold py-1.5 px-3 rounded-lg" data-chunk-id="${editBtn.dataset.chunkId}" data-title="${editBtn.dataset.title}">Save</button>
            <button type="button" class="kb-chunk-cancel-btn text-xs text-slate-500 py-1.5 px-3" data-chunk-id="${editBtn.dataset.chunkId}">Cancel</button>
          </div>
        </div>`;
      return;
    }

    if (cancelBtn) {
      refreshChunksModal();
      return;
    }

    if (saveBtn) {
      const wrapper = event.target.closest('.kb-chunk-row');
      const textarea = wrapper.querySelector('textarea');
      const content = textarea.value.trim();
      if (!content) return;
      // The Knowledge Base API's PATCH takes the full entry (title +
      // content + is_active), unlike the parent project's content-only
      // PATCH - the row's own question/title is preserved here, only the
      // answer/content text is editable from this modal.
      const res = await fetch(`/api/knowledge/entries/${saveBtn.dataset.chunkId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: saveBtn.dataset.title, content, is_active: true }),
      });
      if (!res.ok) {
        const result = await res.json().catch(() => ({}));
        showStatus(result.detail || 'Failed to save FAQ.', true);
        return;
      }
      showStatus('Saved.', false);
      refreshChunksModal();
      loadEntries();
      return;
    }

    if (deleteBtn) {
      if (!confirm('Delete this FAQ entry? This cannot be undone.')) return;
      const res = await fetch(`/api/knowledge/entries/${deleteBtn.dataset.chunkId}`, { method: 'DELETE' });
      if (!res.ok) {
        const result = await res.json().catch(() => ({}));
        showStatus(result.detail || 'Failed to delete FAQ.', true);
        return;
      }
      showStatus('Deleted.', false);
      refreshChunksModal();
      loadEntries();
    }
  });

  entriesBody.addEventListener('click', async (event) => {
    const viewChunksBtn = event.target.closest('.kb-view-chunks-btn');
    const rescrapeBtn = event.target.closest('.kb-rescrape-btn');
    const toggleBtn = event.target.closest('.kb-toggle-btn');
    const deleteBtn = event.target.closest('.kb-delete-btn');

    // Must reference exactly what's currently on screen (sorted, filtered,
    // and paginated) - recomputing the filter/slice here without also
    // reapplying the active sort would resolve data-index against a
    // differently-ordered array than the one actually rendered.
    const groupFromIndex = (el) => entriesState.currentPageGroups[parseInt(el.dataset.index, 10)];

    if (viewChunksBtn) {
      openChunksModal(groupFromIndex(viewChunksBtn));
      return;
    }

    if (rescrapeBtn) {
      const group = groupFromIndex(rescrapeBtn);
      const icon = rescrapeBtn.querySelector('i');
      rescrapeBtn.disabled = true;
      icon.className = 'fa-solid fa-spinner fa-spin';
      showStatus('Re-scraping...', false);
      try {
        const priorUnitId = currentUnitId;
        currentUnitId = group.unit_id;
        const result = await runScrape(group.source_ref);
        currentUnitId = priorUnitId;
        if (result) {
          showStatus('Re-scraped.', false);
          loadEntries();
        }
      } finally {
        rescrapeBtn.disabled = false;
        icon.className = 'fa-solid fa-rotate';
      }
      return;
    }

    if (toggleBtn) {
      const group = groupFromIndex(toggleBtn);
      if (group.entry_id !== null) {
        // /entries/{id} has no GET - reuse the group's own known title/
        // content rather than an extra round trip, matching the toggle
        // logic the pre-existing page already used for manual entries.
        await fetch(`/api/knowledge/entries/${group.entry_id}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ title: group.title, content: group.answer, is_active: !group.is_active }),
        });
      } else {
        await fetch('/api/knowledge/sources/active', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            unit_id: group.unit_id, source_type: group.source_type,
            source_ref: group.source_ref, is_active: !group.is_active,
          }),
        });
      }
      loadEntries();
      return;
    }

    if (deleteBtn) {
      const group = groupFromIndex(deleteBtn);
      if (!confirm(group.chunk_count > 1 ? 'Delete this document and all its FAQs? This cannot be undone.' : 'Delete this entry? This cannot be undone.')) return;
      if (group.entry_id !== null) {
        await fetch(`/api/knowledge/entries/${group.entry_id}`, { method: 'DELETE' });
      } else {
        const params = new URLSearchParams({ source_type: group.source_type, source_ref: group.source_ref });
        if (group.unit_id !== null) params.set('unit_id', group.unit_id);
        await fetch(`/api/knowledge/sources?${params}`, { method: 'DELETE' });
      }
      loadEntries();
    }
  });

  (async function init() {
    await loadUnits();
    await loadEntries();
  })();
})();
