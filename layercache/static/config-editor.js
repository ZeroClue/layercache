// Config editor — CodeMirror 6 from CDN
// Falls back to plain textarea if CDN is unavailable.
import { EditorView, basicSetup } from "https://esm.sh/codemirror@6.0.1";
import { yaml } from "https://esm.sh/@codemirror/lang-yaml@6.1.0";
import { oneDark } from "https://esm.sh/@codemirror/theme-one-dark@6.1.2";

function initEditor() {
  const textarea = document.getElementById("config-editor");
  if (!textarea || textarea.dataset.editorInitialized) return;

  const host = document.getElementById("editor-host");
  if (!host) return;

  textarea.dataset.editorInitialized = "true";

  const lineCount = textarea.value.split("\n").length;
  const minHeight = Math.max(lineCount * 20, 400);

  try {
    const view = new EditorView({
      doc: textarea.value,
      extensions: [
        basicSetup,
        yaml(),
        oneDark,
        EditorView.updateListener.of((update) => {
          textarea.value = update.state.doc.toString();
        }),
        EditorView.theme({
          "&": {
            fontFamily: '"SF Mono", "Fira Code", monospace',
            fontSize: "0.8rem",
            minHeight: minHeight + "px",
            borderRadius: "var(--radius)",
            border: "1px solid var(--border)",
          },
          ".cm-scroller": { overflow: "auto" },
          ".cm-gutters": {
            borderRight: "1px solid var(--border)",
            backgroundColor: "var(--bg)",
          },
          ".cm-activeLineGutter": {
            backgroundColor: "rgba(59,130,246,0.12)",
          },
        }),
      ],
      parent: host,
    });

    textarea.style.display = "none";
  } catch (e) {
    console.warn("CodeMirror init failed, using plain textarea:", e);
    textarea.style.display = "";
  }
}

document.addEventListener("htmx:afterSwap", function (evt) {
  if (evt.detail?.target?.id === "main-content") {
    setTimeout(initEditor, 50);
  }
});

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", initEditor);
} else {
  initEditor();
}
