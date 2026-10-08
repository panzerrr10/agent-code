// agent-code 的 CodeMirror 6 打包入口。
//
// 为什么需要打包：CM6 是 ESM 多包结构，不能像 marked / DOMPurify 那样直接下一个
// UMD 文件用 <script> 引入。这里用 esbuild 打成单个自包含 IIFE（挂 window.AgentCodeCM），
// 打完即与 node_modules 无关，浏览器端零构建步骤。
//
// 对外只暴露两个函数（便于审计）：
//   AgentCodeCM.create(parentEl, opts) -> 编辑器句柄
//   AgentCodeCM.detectLang(filename)   -> 语言 id
//
// 见 scripts/build-codemirror.sh 与 web/static/vendor/README.md。
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
  python,
  javascript: () => javascript(),
  typescript: () => javascript({ typescript: true }),
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
 *
 * 支持多文档：调用方可以为每个标签各调一次 createState()，
 * 切换时用 getState()/setState() 整体换掉。每个 EditorState 自带文档、
 * 撤销栈、语言与只读态，所以标签之间天然隔离，且只占一个 DOM。
 */
function create(parent, opts = {}) {
  const langComp = new Compartment();
  const themeComp = new Compartment();
  const historyComp = new Compartment();
  // 只读开关也必须走 Compartment：`EditorView.editable` 是 Facet，
  // Facet 上没有 reconfigure()（只有 Compartment 有），直接调会抛 TypeError。
  const readOnlyComp = new Compartment();

  const buildLang = (id) => (LANGS[id] ? LANGS[id]() : []);

  const host = {
    save: opts.onSave || (() => {}),
    onChange: opts.onChange || (() => {}),
    onStateChange: () => {
      if (opts.onStateChange) opts.onStateChange(host);
    },
  };

  /** 造一个完整扩展集的 EditorState。 */
  const makeState = (doc, langId, readOnly) => EditorState.create({
    doc: doc || "",
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
        // Ctrl/Cmd+S 交给宿主页面（必须 preventDefault，否则会被"保存网页"截胡）
        { key: "Mod-s", preventDefault: true, run: () => { host.save(); return true; } },
      ]),
      langComp.of(buildLang(langId)),
      themeComp.of(opts.dark ? oneDark : []),
      EditorView.updateListener.of((u) => {
        if (u.docChanged) host.onChange();
        if (u.docChanged || u.selectionSet) host.onStateChange();
      }),
      readOnlyComp.of(EditorView.editable.of(!readOnly)),
    ],
  });

  const view = new EditorView({
    parent,
    state: makeState(opts.doc, opts.lang, opts.readOnly),
  });

  Object.assign(host, {
    view,
    // ---- 内容读写 ----
    getValue: () => view.state.doc.toString(),
    /**
     * 载入新内容。**不记进撤销历史** —— 否则打开 B 文件后按 Ctrl+Z 会「撤销」回
     * A 文件的内容，那是不可接受的。
     */
    setValue: (text) => view.dispatch({
      changes: { from: 0, to: view.state.doc.length, insert: text },
      annotations: Transaction.addToHistory.of(false),
      selection: { anchor: 0 },
    }),
    /** 清空撤销 / 重做栈。 */
    resetHistory: () => view.dispatch({ effects: historyComp.reconfigure(history()) }),

    // ---- 多文档 ----
    createState: (doc, langId, readOnly) => makeState(doc, langId, readOnly),
    getState: () => view.state,
    setState: (state) => view.setState(state),

    // ---- 外观 / 能力 ----
    setLanguage: (id) => view.dispatch({ effects: langComp.reconfigure(buildLang(id)) }),
    setTheme: (dark) => view.dispatch({ effects: themeComp.reconfigure(dark ? oneDark : []) }),
    setReadOnly: (ro) => view.dispatch({
      effects: readOnlyComp.reconfigure(EditorView.editable.of(!ro)),
    }),

    // ---- 撤销 / 重做（按钮与 Ctrl+Z / Ctrl+Shift+Z 走同一条）----
    undo: () => undo(view),
    redo: () => redo(view),
    canUndo: () => undoDepth(view.state) > 0,
    canRedo: () => redoDepth(view.state) > 0,

    focus: () => view.focus(),
    requestMeasure: () => view.requestMeasure(),
  });

  return host;
}

window.AgentCodeCM = { create, detectLang, LANGS: Object.keys(LANGS) };
