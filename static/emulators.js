/* Shared emulator-settings form helpers. */
export function emulatorFieldLabel(key) {
  return String(key).replaceAll('_', ' ').replace(/\b\w/g, letter => letter.toUpperCase());
}

export function readEmulatorSettingsValues(root = document) {
  const values = {};
  root.querySelectorAll('[data-emulator-field]').forEach(input => {
    const key = input.dataset.emulatorField;
    if (input.type === 'checkbox') values[key] = input.checked;
    else if (input.type === 'number') values[key] = Number(input.value);
    else values[key] = input.value;
  });
  return values;
}
