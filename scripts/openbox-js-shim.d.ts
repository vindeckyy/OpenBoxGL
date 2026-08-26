interface Element {
  [key: string]: any;
  onclick: ((event: Event) => void) | null;
  dataset: DOMStringMap;
  hidden: boolean;
}

interface HTMLElement {
  [key: string]: any;
  showModal?: () => void;
  close?: () => void;
  open?: boolean;
}

interface EventTarget {
  [key: string]: any;
}

interface NodeListOf<TNode> extends Array<TNode> {}

interface FormData {
  [Symbol.iterator](): IterableIterator<[string, FormDataEntryValue]>;
}

declare function fetch(input: RequestInfo, init?: RequestInit): Promise<Response>;

declare const App: any;
declare const Library: any;
declare const Setup: any;
declare const Activity: any;
declare const BigBox: any;
declare const Imports: any;
declare const Media: any;
declare const Settings: any;
declare const Explorer: any;
declare const Notifications: any;
declare const state: any;
declare const api: any;
declare const toast: any;
declare const openDialog: any;
declare const closeDialog: any;
