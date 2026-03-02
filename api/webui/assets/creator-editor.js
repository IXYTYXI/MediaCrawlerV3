/**
 * Creator list editor — injected into the Dashboard SPA.
 * Watches for the "Excel 作者列表" section and overlays add/edit/delete controls.
 */
(function () {
  'use strict';

  const API_BASE = '/api/dashboard';
  const CHECK_INTERVAL = 2000;
  let injected = false;

  function getAuthHeaders() {
    const token = localStorage.getItem('auth_token') || localStorage.getItem('token') || '';
    const h = { 'Content-Type': 'application/json' };
    if (token) h['Authorization'] = 'Bearer ' + token;
    return h;
  }

  async function apiFetch(path, opts = {}) {
    opts.headers = { ...getAuthHeaders(), ...(opts.headers || {}) };
    const r = await fetch(API_BASE + path, opts);
    return r.json();
  }

  function $(sel, parent) { return (parent || document).querySelector(sel); }
  function $$(sel, parent) { return [...(parent || document).querySelectorAll(sel)]; }

  const STYLE = `
    .ce-bar{display:flex;gap:8px;margin-bottom:12px;align-items:center}
    .ce-bar input{background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.12);border-radius:6px;color:#e2e8f0;padding:6px 10px;font-size:12px;outline:none}
    .ce-bar input:focus{border-color:rgba(99,102,241,.5)}
    .ce-bar input::placeholder{color:rgba(255,255,255,.3)}
    .ce-btn{padding:5px 14px;border-radius:6px;font-size:12px;cursor:pointer;border:none;white-space:nowrap;transition:background .15s}
    .ce-btn-add{background:#6366f1;color:#fff}.ce-btn-add:hover{background:#818cf8}
    .ce-btn-add:disabled{opacity:.4;cursor:default}
    .ce-btn-edit{background:rgba(255,255,255,.08);color:#e2e8f0;border:1px solid rgba(255,255,255,.12);padding:2px 8px}
    .ce-btn-edit:hover{background:rgba(255,255,255,.14)}
    .ce-btn-del{background:rgba(239,68,68,.15);color:#fca5a5;border:1px solid rgba(239,68,68,.2);padding:2px 8px}
    .ce-btn-del:hover{background:rgba(239,68,68,.3)}
    .ce-btn-save{background:#22c55e;color:#fff;padding:2px 8px}.ce-btn-save:hover{background:#16a34a}
    .ce-btn-cancel{background:rgba(255,255,255,.08);color:#e2e8f0;border:1px solid rgba(255,255,255,.12);padding:2px 8px}
    .ce-actions{display:flex;gap:4px;justify-content:center}
    .ce-edit-input{background:rgba(255,255,255,.06);border:1px solid rgba(99,102,241,.3);border-radius:4px;color:#e2e8f0;padding:2px 6px;font-size:12px;width:100%;outline:none}
    .ce-toast{position:fixed;top:20px;right:20px;padding:10px 20px;border-radius:8px;font-size:13px;color:#fff;z-index:99999;animation:ce-fadein .2s}
    .ce-toast-ok{background:#22c55e}.ce-toast-err{background:#ef4444}
    @keyframes ce-fadein{from{opacity:0;transform:translateY(-8px)}to{opacity:1;transform:translateY(0)}}
  `;

  function toast(msg, ok = true) {
    const el = document.createElement('div');
    el.className = 'ce-toast ' + (ok ? 'ce-toast-ok' : 'ce-toast-err');
    el.textContent = msg;
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 2500);
  }

  function findCreatorSection() {
    const headings = $$('h2, h3, [class*="heading"], [class*="title"]');
    for (const h of headings) {
      if (h.textContent.includes('作者列表')) return h;
    }
    const all = $$('*');
    for (const el of all) {
      if (el.children.length === 0 && el.textContent.includes('Excel 作者列表')) return el;
    }
    return null;
  }

  function findCreatorTable() {
    const tables = $$('table');
    for (const t of tables) {
      const ths = $$('th', t);
      const texts = ths.map(th => th.textContent);
      if (texts.some(x => x.includes('名称')) && texts.some(x => x.includes('链接'))) return t;
    }
    return null;
  }

  function injectAddBar(anchor) {
    if ($('.ce-bar')) return;
    const bar = document.createElement('div');
    bar.className = 'ce-bar';
    bar.innerHTML = `
      <input class="ce-input-name" placeholder="名称" style="flex:1" />
      <input class="ce-input-id" placeholder="ID（可选）" style="flex:1" />
      <input class="ce-input-url" placeholder="小红书主页链接" style="flex:2.5" />
      <button class="ce-btn ce-btn-add" disabled>添加</button>
    `;
    const urlInput = bar.querySelector('.ce-input-url');
    const btn = bar.querySelector('.ce-btn-add');
    urlInput.addEventListener('input', () => { btn.disabled = !urlInput.value.trim(); });
    urlInput.addEventListener('keydown', e => { if (e.key === 'Enter') btn.click(); });
    btn.addEventListener('click', async () => {
      const name = bar.querySelector('.ce-input-name').value.trim();
      const id = bar.querySelector('.ce-input-id').value.trim();
      const url = urlInput.value.trim();
      if (!url) return;
      btn.disabled = true; btn.textContent = '...';
      try {
        const res = await apiFetch('/creators', { method: 'POST', body: JSON.stringify({ name, id, url }) });
        if (res.success) {
          toast('已添加');
          bar.querySelector('.ce-input-name').value = '';
          bar.querySelector('.ce-input-id').value = '';
          urlInput.value = '';
          setTimeout(refreshPage, 400);
        } else {
          toast(res.detail || res.error || '添加失败', false);
        }
      } catch (e) { toast('请求失败', false); }
      btn.disabled = false; btn.textContent = '添加';
    });

    const parent = anchor.closest('div') || anchor.parentElement;
    if (parent) {
      const nextSibling = anchor.nextElementSibling;
      if (nextSibling) parent.insertBefore(bar, nextSibling);
      else parent.appendChild(bar);
    }
  }

  function injectTableActions(table) {
    if (table.dataset.ceInjected) return;
    table.dataset.ceInjected = '1';

    const headerRow = $('thead tr', table);
    if (headerRow && !headerRow.querySelector('.ce-th-actions')) {
      const th = document.createElement('th');
      th.className = 'ce-th-actions';
      th.textContent = '操作';
      th.style.cssText = 'width:80px;text-align:center';
      headerRow.appendChild(th);
    }

    const rows = $$('tbody tr', table);
    rows.forEach((tr, idx) => {
      if (tr.querySelector('.ce-actions')) return;
      const td = document.createElement('td');
      td.innerHTML = `<div class="ce-actions">
        <button class="ce-btn ce-btn-edit" data-idx="${idx}">改</button>
        <button class="ce-btn ce-btn-del" data-idx="${idx}">删</button>
      </div>`;

      td.querySelector('.ce-btn-edit').addEventListener('click', () => startEdit(table, tr, idx));
      td.querySelector('.ce-btn-del').addEventListener('click', () => deleteRow(idx));
      tr.appendChild(td);
    });
  }

  function startEdit(table, tr, idx) {
    const tds = $$('td', tr);
    if (tds.length < 4) return;
    const nameTd = tds[1], idTd = tds[2], urlTd = tds[3], actionTd = tds[tds.length - 1];
    const origName = nameTd.textContent.trim();
    const origId = idTd.textContent.trim();
    const linkEl = $('a', urlTd);
    const origUrl = linkEl ? linkEl.href : urlTd.textContent.trim();

    nameTd.innerHTML = `<input class="ce-edit-input" value="${origName}" />`;
    idTd.innerHTML = `<input class="ce-edit-input" value="${origId}" />`;
    urlTd.innerHTML = `<input class="ce-edit-input" value="${origUrl}" />`;
    actionTd.innerHTML = `<div class="ce-actions">
      <button class="ce-btn ce-btn-save">存</button>
      <button class="ce-btn ce-btn-cancel">取消</button>
    </div>`;

    actionTd.querySelector('.ce-btn-save').addEventListener('click', async () => {
      const name = nameTd.querySelector('input').value.trim();
      const id = idTd.querySelector('input').value.trim();
      const url = urlTd.querySelector('input').value.trim();
      try {
        const res = await apiFetch('/creators/' + idx, { method: 'PUT', body: JSON.stringify({ name, id, url }) });
        if (res.success) { toast('已更新'); setTimeout(refreshPage, 400); }
        else toast(res.detail || '更新失败', false);
      } catch (e) { toast('请求失败', false); }
    });

    actionTd.querySelector('.ce-btn-cancel').addEventListener('click', () => {
      setTimeout(refreshPage, 100);
    });
  }

  async function deleteRow(idx) {
    if (!confirm('确认删除该作者？')) return;
    try {
      const res = await apiFetch('/creators/' + idx, { method: 'DELETE' });
      if (res.success) { toast(res.message || '已删除'); setTimeout(refreshPage, 400); }
      else toast(res.detail || '删除失败', false);
    } catch (e) { toast('请求失败', false); }
  }

  function refreshPage() {
    const refreshBtn = $$('button').find(b => b.textContent.includes('刷新'));
    if (refreshBtn) { refreshBtn.click(); setTimeout(tryInject, 600); return; }
    location.reload();
  }

  function tryInject() {
    const section = findCreatorSection();
    const table = findCreatorTable();
    if (section) injectAddBar(section);
    if (table) {
      injectTableActions(table);
      injected = true;
    }
  }

  function init() {
    const style = document.createElement('style');
    style.textContent = STYLE;
    document.head.appendChild(style);

    const observer = new MutationObserver(() => {
      if (!injected || !findCreatorTable()?.dataset.ceInjected) tryInject();
    });
    observer.observe(document.body, { childList: true, subtree: true });

    setInterval(() => {
      const table = findCreatorTable();
      if (table && !table.dataset.ceInjected) tryInject();
    }, CHECK_INTERVAL);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
