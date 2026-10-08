// agent-code 的 CodeMirror 6 打包入口。
// 打成单个 IIFE 挂到 window.AgentCodeCM，供单文件前端直接 <script> 使用（无构建步骤）。
import { EditorState, Compartment, Transaction } from "@codemirror/state";
import {
  EditorView, keymap, lineNumbers, highlightActiveLine, highlightActiveLineGutter,
  drawSelection, dropCursor, rectangularSelection, crosshairCursor, highlightSpecialChars,
} from "@codemirror/view";
import {
  defaultKeymap, history, historyKeymap, indentWithTab, undo, redo, undoDepth, redoDepth,
} from "@codemirror/commands";
import { searchKeymap, highlightSelectionMatches } from "@codemirror/search";
import {
  autocompletion, completionKeymap, closeBrackets, closeBracketsKeymap,
} from "@codemirror/autocomplete";
import {
  bracketMatching, indentOnInput, syntaxHighlighting, defaultHighlightStyle,
  foldGutter, foldKeymap, StreamLanguage,
} from "@codemirror/language";
import { oneDark } from "@codemirror/theme-one-dark";

import { python } from "@codemirror/lang-python";
import { javascript } from "@codemirror/lang-javascript";
import { json } from "@codemirror/lang-json";
import { html } from "@codemirror/lang-html";
import { css } from "@codemirror/lang-css";
import { markdown } from "@codemirror/lang-markdown";
import { xml } from "@codemirror/lang-xml";
import { sql } from "@codemirror/lang-sql";
import { yaml } from "@codemirror/lang-yaml";
import { shell } from "@codemirror/legacy-modes/mode/shell";
import { toml } from "@codemirror/legacy-modes/mode/toml";
import { dockerFile } from "@codemirror/legacy-modes/mode/dockerfile";

const LANGS = {
  python, javascript: () => javascript(), typescript: () => javascript({ typescript: true }),
  json, html, css, markdown, xml, sql, yaml,
  shell: () => StreamLanguage.define(shell),
  toml: () => StreamLanguage.define(toml),
  dockerfile: () => StreamLanguage.define(dockerFile),
};

// 扩展名 → 语言。未知后缀按纯文本处理。
const EXT_MAP = {
  py: "python", pyw: "python", js: "javascript", mjs: "javascript", cjs: "javascript",
  jsx: "javascript", ts: "typescript", tsx: "typescript", json: "json",
  html: "html", htm: "html", vue: "html", css: "css", scss: "css", less: "css",
  md: "markdown", markdown: "markdown", xml: "xml", svg: "xml",
  sql: "sql", yaml: "yaml", yml: "yaml", sh: "shell", bash: "shell", zsh: "shell",
  toml: "toml", dockerfile: "dockerfile",
};

function detectLang(filename) {
  const base = String(filename || "").split("/").pop().toLowerCase();
  if (base === "dockerfile" || base.startsWith("dockerfile.")) return "dockerfile";
  const ext = base.includes(".") ? base.split(".").pop() : "";
  return EXT_MAP[ext] || "plain";
}

/**
 * 创建一个编辑器。
 * @returns 句柄对象（供宿主页面调用）
 */
function create(parent, opts = {}) {
  const langComp = new Compartment();
  const themeComp = new Compartment();
  const historyComp = new Compartment();
  // 只读开关也必须走 Compartment：`EditorView.editable` 是 Facet，
  // Facet 上没有 reconfigure()（只有 Compartment 有），直接调会抛 TypeError。
  const readOnlyComp = new Compartment();

  const buildLang = (id) => (LANGS[id] ? LANGS[id]() : []);

  const view = new EditorView({
    parent,
    state: EditorState.create({
      doc: opts.doc || "",
      extensions: [
        lineNumbers(), highlightActiveLineGutter(), highlightSpecialChars(),
        historyComp.of(history()), foldGutter(), drawSelection(), dropCursor(),
        EditorState.allowMultipleSelections.of(true),
        indentOnInput(),
        syntaxHighlighting(defaultHighlightStyle, { fallback: true }),
        bracketMatching(), closeBrackets(), autocompletion(),
        rectangularSelection(), crosshairCursor(),
        highlightActiveLine(), highlightSelectionMatches(),
        keymap.of([
          ...closeBracketsKeymap, ...defaultKeymap, ...searchKeymap,
          ...historyKeymap, ...foldKeymap, ...completionKeymap, indentWithTab,
          // Ctrl/Cmd+S 交给宿主页面（浏览器里必须 preventDefault 才不会被"保存网页"截胡）
          { key: "Mod-s", preventDefault: true, run: () => { host.save(); return true; } },
        ]),
        langComp.of(buildLang(opts.lang)),
        themeComp.of(opts.dark ? oneDark : []),
        EditorView.updateListener.of((u) => {
          if (u.docChanged) host.onChange();
          if (u.docChanged || u.selectionSet) host.onStateChange();
        }),
        readOnlyComp.of(EditorView.editable.of(!opts.readOnly)),
      ],
    }),
  });

  const host = {
    view,
    save: opts.onSave || (() => {}),
    onChange: opts.onChange || (() => {}),
    onStateChange: () => {
      if (opts.onStateChange) opts.onStateChange(api);
    },
    getValue: () => view.state.doc.toString(),
    /**
     * 载入新内容。**不记进撤销历史** —— 否则打开 B 文件后按 Ctrl+Z 会「撤销」回
     * A 文件的内容，那是不可接受的。调用方通常紧接着调 resetHistory() 清干净。
     */
    setValue: (text) => view.dispatch({
      changes: { from: 0, to: view.state.doc.length, insert: text },
      annotations: Transaction.addToHistory.of(false),
      selection: { anchor: 0 },
    }),
    /** 清空撤销 / 重做栈（切换文件时用）。 */
    resetHistory: () => view.dispatch({ effects: historyComp.reconfigure(history()) }),
    setLanguage: (id) => view.dispatch({ effects: langComp.reconfigure(buildLang(id)) }),
    setTheme: (dark) => view.dispatch({ effects: themeComp.reconfigure(dark ? oneDark : []) }),
    setReadOnly: (ro) => view.dispatch({
      effects: readOnlyComp.reconfigure(EditorView.editable.of(!ro)),
    }),
    undo: () => undo(view),
    redo: () => redo(view),
    canUndo: () => undoDepth(view.state) > 0,
    canRedo: () => redoDepth(view.state) > 0,
    focus: () => view.focus(),
    requestMeasure: () => view.requestMeasure(),
  };
  const api = host;
  return host;
}

window.AgentCodeCM = { create, detectLang, LANGS: Object.keys(LANGS) };
