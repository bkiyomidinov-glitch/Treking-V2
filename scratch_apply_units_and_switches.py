with open(r"c:\Users\behzod.q\Sz\static\index.html", "r", encoding="utf-8") as f:
    content = f.read()

# 1. Add "Единицы измерения" to Sidebar
old_sidebar_units = """      <button class="nav-item" data-view="categories">
        <i data-lucide="folder-tree" width="16"></i>
        <span>Категории</span>
      </button>"""

new_sidebar_units = """      <button class="nav-item" data-view="categories">
        <i data-lucide="folder-tree" width="16"></i>
        <span>Категории</span>
      </button>
      <button class="nav-item" data-view="units">
        <i data-lucide="scale" width="16"></i>
        <span>Единицы измерения</span>
      </button>"""

if 'data-view="units"' not in content:
    content = content.replace(old_sidebar_units, new_sidebar_units)

# 2. Add switchView handling for 'units'
old_switch_units = """      else if (view === 'categories') root.innerHTML = renderCategories();"""
new_switch_units = """      else if (view === 'categories') root.innerHTML = renderCategories();
      else if (view === 'units') root.innerHTML = renderUnits();"""

if "renderUnits()" not in content:
    content = content.replace(old_switch_units, new_switch_units)

# 3. Add getProductFlagBadges helper and renderUnits and unit/switch functions
unit_and_switch_js = """
    // --- Dynamic Product Flag Badges Helper ---
    function getProductFlagBadges(p) {
      const badges = [];
      const boolAttrs = (state.data.custom_attributes || []).filter(a => a.type === 'boolean');
      const attrNameMap = {};
      for (const a of boolAttrs) attrNameMap[a.key] = a.name;

      if (p.attributes) {
        for (const [key, val] of Object.entries(p.attributes)) {
          if (val === true || val === 'true' || val === 1) {
            const name = attrNameMap[key] || key;
            badges.push(`<span class="badge badge-emerald" style="font-size:9px; padding:1px 5px;">✓ ${esc(name)}</span>`);
          }
        }
      }

      // Fallback for legacy columns if not in attributes
      if (p.is_certified && !badges.some(b => b.includes('Сертификат'))) {
        badges.push('<span class="badge badge-emerald" style="font-size:9px; padding:1px 5px;">✓ Сертификат</span>');
      }
      if (p.is_temperature_controlled && !badges.some(b => b.includes('Термо'))) {
        badges.push('<span class="badge badge-blue" style="font-size:9px; padding:1px 5px;">❄ Термо</span>');
      }
      if (p.is_fragile && !badges.some(b => b.includes('Хрупк'))) {
        badges.push('<span class="badge badge-amber" style="font-size:9px; padding:1px 5px;">⚠ Хрупкое</span>');
      }

      return badges.length > 0 ? `<div style="display:flex; flex-wrap:wrap; gap:4px; margin-bottom:4px;">${badges.join('')}</div>` : '';
    }

    // --- Units of Measurement Management Functions ---
    async function quickAddUnitPrompt(targetInputId = null) {
      const name = prompt('Введите обозначение новой единицы измерения (например: "км", "рулон", "бобина", "тонна", "литр", "компл"):');
      if (!name || !name.trim()) return;
      const cleanName = name.trim();
      const desc = prompt(`Описание для единицы «${cleanName}» (необязательно, например: "Километр" или "Рулон ткани"):`, '') || '';

      try {
        const res = await api('/units', { method: 'POST', body: JSON.stringify({ name: cleanName, description: desc.trim() }) });
        if (!state.data.units) state.data.units = [];
        if (!state.data.units.some(u => (typeof u==='string'?u:u.name) === cleanName)) {
          state.data.units.push(res);
        }
        if (targetInputId) {
          const el = document.getElementById(targetInputId);
          if (el) el.value = cleanName;
        }
        toast(`Единица измерения «${cleanName}» успешно добавлена!`, 'success');
        if (state.currentView === 'units') await switchView('units');
      } catch (err) {
        toast(err.message, 'error');
      }
    }

    async function promptRenameUnit(unitId, oldName) {
      const newName = prompt(`Введите новое название для единицы «${oldName}»:`, oldName);
      if (!newName || !newName.trim() || newName.trim() === oldName) return;
      const desc = prompt(`Описание для единицы «${newName.trim()}» (необязательно):`, '') || '';

      try {
        if (unitId) {
          await api(`/units/${unitId}`, { method: 'PUT', body: JSON.stringify({ name: newName.trim(), description: desc.trim() }) });
        } else {
          await api('/units', { method: 'POST', body: JSON.stringify({ name: newName.trim(), description: desc.trim() }) });
        }
        toast(`Единица измерения переименована в «${newName.trim()}»!`, 'success');
        await loadData();
        await switchView(state.currentView);
      } catch (err) {
        toast(err.message, 'error');
      }
    }

    async function promptDeleteUnit(unitId, name) {
      if (!confirm(`Вы действительно хотите удалить единицу измерения «${name}» из справочника?`)) return;
      try {
        if (unitId) {
          await api(`/units/${unitId}`, { method: 'DELETE' });
        } else {
          const uObj = (state.data.units || []).find(u => (typeof u==='string'?u:u.name) === name);
          if (uObj && uObj.id) {
            await api(`/units/${uObj.id}`, { method: 'DELETE' });
          }
        }
        toast(`Единица измерения «${name}» удалена!`, 'info');
        await loadData();
        await switchView(state.currentView);
      } catch (err) {
        toast(err.message, 'error');
      }
    }

    function openUnitsManagerModal() {
      const units = state.data.units || [];
      document.getElementById('modal-container').innerHTML = `
        <div class="modal-backdrop" onclick="if(event.target===this)closeModal()">
          <div class="modal-box" role="dialog" aria-modal="true" style="width:min(580px, 100%);">
            <div class="modal-head">
              <div style="font-weight:700; font-size:15px; color:#fff; display:flex; align-items:center; gap:8px;">
                <i data-lucide="scale" width="18" style="color:var(--primary)"></i>
                <span>Справочник единиц измерения</span>
              </div>
              <button class="btn btn-sm btn-icon" onclick="closeModal()"><i data-lucide="x" width="16"></i></button>
            </div>
            <div class="modal-body" style="display:grid; gap:14px;">
              <div style="display:flex; justify-content:space-between; align-items:center;">
                <div style="font-size:12px; color:var(--text-muted);">
                  Полный список единиц: добавляйте свои (км, рулон, тонна...), переименовывайте или удаляйте.
                </div>
                <button class="btn btn-sm btn-primary" onclick="quickAddUnitPrompt()"><i data-lucide="plus" width="13"></i>Добавить</button>
              </div>

              <div class="table-container" style="max-height:360px; overflow-y:auto; border:1px solid var(--border); border-radius:8px;">
                <table>
                  <thead>
                    <tr>
                      <th>Обозначение</th>
                      <th>Полное название</th>
                      <th style="text-align:right;">Действия</th>
                    </tr>
                  </thead>
                  <tbody>
                    ${units.length === 0 ? `
                      <tr><td colspan="3" style="text-align:center; padding:24px; color:var(--text-dim);">Единицы измерения пока не созданы</td></tr>
                    ` : units.map(u => {
                      const uId = typeof u === 'string' ? null : u.id;
                      const uName = typeof u === 'string' ? u : u.name;
                      const uDesc = typeof u === 'string' ? '' : (u.description || '');
                      return `
                        <tr>
                          <td><span class="mono" style="font-weight:700; color:#fff; font-size:13px; background:rgba(255,255,255,0.06); padding:2px 8px; border-radius:4px;">${esc(uName)}</span></td>
                          <td style="color:var(--text-muted);">${esc(uDesc || '—')}</td>
                          <td style="text-align:right;">
                            <div style="display:inline-flex; gap:6px;">
                              <button class="btn btn-sm btn-icon" title="Переименовать" onclick="promptRenameUnit(${uId || 'null'}, '${esc(uName)}')"><i data-lucide="edit-2" width="12"></i></button>
                              <button class="btn btn-sm btn-icon" style="color:var(--red);" title="Удалить" onclick="promptDeleteUnit(${uId || 'null'}, '${esc(uName)}')"><i data-lucide="trash-2" width="12"></i></button>
                            </div>
                          </td>
                        </tr>
                      `;
                    }).join('')}
                  </tbody>
                </table>
              </div>
            </div>
            <div class="modal-footer">
              <button type="button" class="btn btn-primary" onclick="closeModal()">Закрыть</button>
            </div>
          </div>
        </div>
      `;
      if (window.lucide) lucide.createIcons();
    }

    function renderUnits() {
      const units = state.data.units || [];
      return `
        <div class="page-header">
          <div>
            <h1 class="page-title"><i data-lucide="scale" width="22" style="color:var(--primary)"></i>Единицы измерения номенклатуры</h1>
            <div class="page-desc">Полная свобода: создавайте любые свои единицы (километры, рулоны, тонны, метры, штуки), переименовывайте или удаляйте ненужные</div>
          </div>
          <button class="btn btn-primary" onclick="quickAddUnitPrompt()"><i data-lucide="plus" width="14"></i>Добавить единицу измерения</button>
        </div>

        <div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(260px, 1fr)); gap:16px;">
          ${units.map(u => {
            const uId = typeof u === 'string' ? null : u.id;
            const uName = typeof u === 'string' ? u : u.name;
            const uDesc = typeof u === 'string' ? '' : (u.description || '');
            return `
              <div class="card">
                <div class="card-head">
                  <div style="display:flex; align-items:center; gap:10px;">
                    <div style="width:34px; height:34px; border-radius:8px; background:rgba(16,185,129,0.1); color:var(--accent); display:grid; place-items:center; font-family:'JetBrains Mono',monospace; font-weight:800; font-size:12px;">
                      ${esc(uName)}
                    </div>
                    <div>
                      <div class="card-title" style="color:#fff; font-size:14px;">${esc(uName)}</div>
                      <div style="font-size:11px; color:var(--text-dim); margin-top:1px;">${esc(uDesc || 'Пользовательская единица')}</div>
                    </div>
                  </div>
                  <div style="display:flex; align-items:center; gap:6px;">
                    <button class="btn btn-sm btn-icon" title="Переименовать единицу" onclick="promptRenameUnit(${uId || 'null'}, '${esc(uName)}')"><i data-lucide="edit-2" width="13"></i></button>
                    <button class="btn btn-sm btn-icon" style="color:var(--red);" title="Удалить единицу" onclick="promptDeleteUnit(${uId || 'null'}, '${esc(uName)}')"><i data-lucide="trash-2" width="13"></i></button>
                  </div>
                </div>
              </div>
            `;
          }).join('')}
        </div>
      `;
    }

    // --- Dynamic Switch Flags (Parameters and Flags) Functions ---
    async function promptAddNewSwitchFlag() {
      const name = prompt('Введите название нового переключателя (например: "Экспортный контроль", "Водоотталкивающий", "Эко-хлопок", "Двойной шов", "Хрупкое"):');
      if (!name || !name.trim()) return;
      const cleanName = name.trim();

      const translit = (str) => {
        const ru = 'абвгдеёжзийклмнопрстуфхцчшщъыьэюя';
        const en = ['a','b','v','g','d','e','yo','zh','z','i','y','k','l','m','n','o','p','r','s','t','u','f','kh','ts','ch','sh','shch','','y','','e','yu','ya'];
        return str.toLowerCase().split('').map(c => {
          const idx = ru.indexOf(c);
          return idx >= 0 ? en[idx] : c;
        }).join('');
      };
      let safeKey = 'flag_' + translit(cleanName).replace(/[^a-z0-9_]/g, '_').replace(/_+/g, '_').replace(/^_|_$/g, '');
      if (state.data.custom_attributes.some(a => a.key === safeKey)) {
        safeKey += '_' + Math.floor(Math.random() * 1000);
      }

      try {
        const res = await api('/attributes', {
          method: 'POST',
          body: JSON.stringify({
            name: cleanName,
            key: safeKey,
            type: 'boolean',
            options: [],
            unit: ''
          })
        });
        state.data.custom_attributes.push(res);
        toast(`Переключатель «${cleanName}» создан в системе!`, 'success');

        // Dynamically add to modal grid if open
        const grid = document.getElementById('modal-switches-grid') || document.getElementById('modal-edit-switches-grid');
        if (grid) {
          const emptyPlaceholder = grid.querySelector('div[style*="grid-column:1/-1"]');
          if (emptyPlaceholder) emptyPlaceholder.remove();

          const row = document.createElement('div');
          row.className = 'switch-row';
          row.id = `switch-card-${res.id}`;
          row.innerHTML = `
            <div class="switch-info" style="flex:1;">
              <div style="display:flex; align-items:center; gap:6px;">
                <div class="switch-title">${esc(cleanName)}</div>
                <button type="button" class="btn btn-sm btn-icon" style="width:20px; height:20px; padding:0; border:none; background:transparent; color:var(--text-dim);" title="Переименовать переключатель" onclick="promptRenameSwitch(${res.id}, '${esc(cleanName)}')"><i data-lucide="edit-2" width="11"></i></button>
                <button type="button" class="btn btn-sm btn-icon" style="width:20px; height:20px; padding:0; border:none; background:transparent; color:var(--red);" title="Удалить переключатель из системы" onclick="promptDeleteSwitch(${res.id}, '${esc(cleanName)}')"><i data-lucide="trash-2" width="11"></i></button>
              </div>
              <div class="switch-desc mono" style="font-size:10px; color:var(--text-dim);">ключ: ${esc(safeKey)}</div>
            </div>
            <label class="switch">
              <input type="checkbox" data-switch-key="${esc(safeKey)}" checked>
              <span class="switch-slider"></span>
            </label>
          `;
          grid.appendChild(row);
          if (window.lucide) lucide.createIcons();
        }
      } catch (err) {
        toast(err.message, 'error');
      }
    }

    async function promptRenameSwitch(attrId, oldName) {
      const newName = prompt(`Введите новое название для переключателя «${oldName}»:`, oldName);
      if (!newName || !newName.trim() || newName.trim() === oldName) return;
      const cleanName = newName.trim();

      try {
        await api(`/attributes/${attrId}`, {
          method: 'PUT',
          body: JSON.stringify({ name: cleanName, type: 'boolean' })
        });
        const attr = state.data.custom_attributes.find(a => a.id === attrId);
        if (attr) attr.name = cleanName;
        toast(`Переключатель переименован в «${cleanName}»!`, 'success');

        const card = document.getElementById(`switch-card-${attrId}`) || document.getElementById(`edit-switch-card-${attrId}`);
        if (card) {
          const title = card.querySelector('.switch-title');
          if (title) title.textContent = cleanName;
        }
      } catch (err) {
        toast(err.message, 'error');
      }
    }

    async function promptDeleteSwitch(attrId, name) {
      if (!confirm(`Вы действительно хотите удалить переключатель «${name}» из системы?`)) return;
      try {
        await api(`/attributes/${attrId}`, { method: 'DELETE' });
        state.data.custom_attributes = state.data.custom_attributes.filter(a => a.id !== attrId);
        toast(`Переключатель «${name}» удален!`, 'info');

        const card = document.getElementById(`switch-card-${attrId}`) || document.getElementById(`edit-switch-card-${attrId}`);
        if (card) card.remove();
      } catch (err) {
        toast(err.message, 'error');
      }
    }
"""

if "function quickAddUnitPrompt" not in content:
    target = "    // --- Category Management Functions ---"
    content = content.replace(target, unit_and_switch_js + "\n    " + target)

with open(r"c:\Users\behzod.q\Sz\static\index.html", "w", encoding="utf-8") as f:
    f.write(content)

print("Part 1: Functions for units and dynamic switches added to index.html!")
