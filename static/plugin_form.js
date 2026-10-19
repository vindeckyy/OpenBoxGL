/* Shared plugin settings renderer: JSON-Schema subset used by local plugins. */
import { escapeHtml } from './util.js';

function fieldValue(values, name, field) {
  return values[name] !== undefined ? values[name] : field.default;
}

/**
 * Render schema fields into HTML. Each row carries `data-setting`, so the
 * Settings search sees dynamically rendered plugin settings.
 */
export function pluginSettingsFields(schema = {}, values = {}, { pluginName = '', idPrefix = 'plugin' } = {}) {
  const properties = schema.properties || {};
  const required = new Set(schema.required || []);
  return Object.entries(properties).map(([name, field]) => {
    const value = fieldValue(values, name, field);
    const dataSetting = escapeHtml([name, field.title || '', pluginName].filter(Boolean).join(' '));
    const label = `<span>${escapeHtml(field.title || name)}${required.has(name) ? ' *' : ''}</span>`;
    const desc = field.description ? `<small>${escapeHtml(field.description)}</small>` : '';
    const fieldName = escapeHtml(name);
    const type = escapeHtml(field.type || 'string');
    let input;
    if (field.enum) {
      input = `<select name="${fieldName}" data-plugin-setting-type="${type}">${field.enum.map(option => `<option value="${escapeHtml(String(option))}"${String(option) === String(value ?? '') ? ' selected' : ''}>${escapeHtml(String(option))}</option>`).join('')}</select>`;
    } else if (field.type === 'boolean') {
      input = `<input type="checkbox" name="${fieldName}" data-plugin-setting-type="boolean"${value ? ' checked' : ''}>`;
    } else if (field.type === 'integer' || field.type === 'number') {
      input = `<input type="number" name="${fieldName}" data-plugin-setting-type="${type}" value="${escapeHtml(String(value ?? ''))}"${field.minimum !== undefined ? ` min="${field.minimum}"` : ''}${field.maximum !== undefined ? ` max="${field.maximum}"` : ''}${field.type === 'integer' ? ' step="1"' : ' step="any"'}>`;
    } else if (field.format === 'password') {
      const placeholder = 'Stored; leave blank to keep';
      input = `<span class="path-input-row"><input type="password" name="${fieldName}" data-plugin-setting-type="password" value="" placeholder="${escapeHtml(placeholder)}" autocomplete="new-password"><label class="check"><input type="checkbox" data-plugin-clear-secret="${fieldName}"> Clear stored</label></span>`;
    } else {
      input = `<input type="text" name="${fieldName}" data-plugin-setting-type="string" value="${escapeHtml(String(value ?? ''))}"${field.maxLength ? ` maxlength="${field.maxLength}"` : ''}>`;
    }
    return `<label class="field wide settings-field" data-settings-panel="plugins" data-setting="${dataSetting}" data-plugin-setting-row="${fieldName}" data-plugin-settings-prefix="${escapeHtml(idPrefix)}">${label}${input}${desc}</label>`;
  }).join('');
}

/** Collect a rendered form back into the values payload. */
export function collectPluginSettings(container) {
  const values = {};
  container.querySelectorAll('[name][data-plugin-setting-type]').forEach(input => {
    const type = input.dataset.pluginSettingType;
    if (type === 'boolean') values[input.name] = input.checked;
    else if (type === 'integer') { if (input.value !== '') values[input.name] = parseInt(input.value, 10); }
    else if (type === 'number') { if (input.value !== '') values[input.name] = parseFloat(input.value); }
    else values[input.name] = input.value;
  });
  return values;
}

/**
 * Render fields plus an optional Save button. `onSave` receives the collected
 * values and the container. The caller owns the request and success behavior.
 */
export function renderPluginSettings(container, schema, values, { onSave, pluginName = '', idPrefix = 'plugin', saveLabel = 'Save plugin settings' } = {}) {
  const fields = pluginSettingsFields(schema, values, { pluginName, idPrefix });
  container.innerHTML = `<div class="form-grid" data-plugin-settings-fields>${fields || '<p class="description">This plugin has no settings.</p>'}</div>${onSave ? `<div class="extras"><button type="button" class="icon-button" data-plugin-settings-save>${escapeHtml(saveLabel)}</button></div>` : ''}`;
  if (onSave) {
    container.querySelector('[data-plugin-settings-save]').onclick = () => onSave(collectPluginSettings(container), container);
  }
  return container;
}
