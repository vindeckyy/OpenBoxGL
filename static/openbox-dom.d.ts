// Incremental checkJs support for legacy DOM-heavy static modules.
// Narrows common element property access without requiring full strict typing yet.

interface Element {
  checked?: boolean;
  value?: string;
  showModal?: () => void;
  close?: () => void;
  selectedIndex?: number;
  disabled?: boolean;
  hidden?: boolean;
  open?: boolean;
}
