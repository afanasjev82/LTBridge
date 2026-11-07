/* ltbridge frontend JS */

(async () => {
  const sourceSelect  = document.getElementById("source-lang");
  const targetSelect  = document.getElementById("target-lang");
  const sourceText    = document.getElementById("source-text");
  const targetText    = document.getElementById("target-text");
  const translateBtn  = document.getElementById("translate-btn");
  const swapBtn       = document.getElementById("swap-btn");
  const clearBtn      = document.getElementById("clear-btn");
  const pasteBtn      = document.getElementById("paste-btn");
  const copySourceBtn = document.getElementById("copy-source-btn");
  const copyTargetBtn = document.getElementById("copy-target-btn");
  const charCount     = document.getElementById("char-count");
  const charLimitEl   = document.getElementById("char-limit");
  const detectedEl    = document.getElementById("detected-lang");
  const statusMsg     = document.getElementById("status-msg");
  const statsEl       = document.getElementById("stats");

  let charLimit = 5000;

  // ---------------------------------------------------------------------------
  // Load settings + languages
  // ---------------------------------------------------------------------------

  async function loadSettings() {
    try {
      const res = await fetch("/frontend/settings");
      const data = await res.json();
      charLimit = data.charLimit || 5000;
      charLimitEl.textContent = charLimit;
    } catch (_) { /* ignore */ }
  }

  async function loadLanguages() {
    const res = await fetch("/languages");
    const langs = await res.json();

    // Populate target select
    targetSelect.innerHTML = "";
    for (const lang of langs) {
      const opt = document.createElement("option");
      opt.value = lang.code;
      opt.textContent = lang.name;
      if (lang.code === "en") opt.selected = true;
      targetSelect.appendChild(opt);
    }

    // Populate source select (keep "Auto Detect" at top)
    for (const lang of langs) {
      const opt = document.createElement("option");
      opt.value = lang.code;
      opt.textContent = lang.name;
      sourceSelect.appendChild(opt);
    }
  }

  // ---------------------------------------------------------------------------
  // Stats line (time spent + speed, measured client-side)
  // ---------------------------------------------------------------------------

  function formatDuration(ms) {
    if (ms < 1000) return `${Math.round(ms)} ms`;
    const s = ms / 1000;
    if (s < 60) return `${s.toFixed(s < 10 ? 2 : 1)} s`;
    return `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`;
  }

  function formatSpeed(chars, ms) {
    if (ms <= 0) return "";
    const perSec = (chars * 1000) / ms;
    return `${perSec < 10 ? perSec.toFixed(1) : Math.round(perSec)} chars/s`;
  }

  let ticker = null;

  function stopTicker() {
    if (ticker !== null) {
      clearInterval(ticker);
      ticker = null;
    }
  }

  function startTicker(startedAt) {
    stopTicker();
    statsEl.textContent = "";
    ticker = setInterval(() => {
      statsEl.textContent = `${((performance.now() - startedAt) / 1000).toFixed(1)} s`;
    }, 100);
  }

  function clearStats() {
    stopTicker();
    statsEl.textContent = "";
    statsEl.title = "";
  }

  function showStats(ms, inChars, outChars) {
    stopTicker();
    const speed = formatSpeed(outChars, ms);
    statsEl.textContent =
      `${formatDuration(ms)} · ${inChars} → ${outChars} chars` + (speed ? ` · ${speed}` : "");
    statsEl.title = "Round-trip time measured in the browser; speed = translated characters per second";
  }

  // ---------------------------------------------------------------------------
  // Translation
  // ---------------------------------------------------------------------------

  async function doTranslate() {
    const q = sourceText.value.trim();
    if (!q) return;

    translateBtn.disabled = true;
    statusMsg.textContent = "Translating…";
    statusMsg.className = "";
    targetText.value = "";
    detectedEl.textContent = "";
    updateButtons();

    const startedAt = performance.now();
    startTicker(startedAt);

    try {
      const res = await fetch("/translate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          q,
          source: sourceSelect.value,
          target: targetSelect.value,
        }),
      });

      const data = await res.json();

      if (!res.ok) {
        throw new Error(data.error || `HTTP ${res.status}`);
      }

      const elapsed = performance.now() - startedAt;
      targetText.value = data.translatedText || "";
      statusMsg.textContent = "";
      showStats(elapsed, q.length, targetText.value.length);

      if (data.detectedLanguage) {
        const { language, confidence } = data.detectedLanguage;
        detectedEl.textContent = `Detected: ${language} (${confidence}%)`;
      }
    } catch (err) {
      const elapsed = performance.now() - startedAt;
      stopTicker();
      statsEl.textContent = `failed after ${formatDuration(elapsed)}`;
      statsEl.title = "";
      statusMsg.textContent = err.message;
      statusMsg.className = "error";
    } finally {
      translateBtn.disabled = false;
      updateButtons();
    }
  }

  // ---------------------------------------------------------------------------
  // Swap languages
  // ---------------------------------------------------------------------------

  function swapLanguages() {
    const srcVal = sourceSelect.value;
    const tgtVal = targetSelect.value;
    const srcText = targetText.value;

    if (srcVal === "auto") return; // can't swap auto-detect

    // Swap select values
    sourceSelect.value = tgtVal;
    targetSelect.value = srcVal;

    // Swap text content
    sourceText.value = srcText;
    targetText.value = "";
    detectedEl.textContent = "";
    clearStats();
    updateCharCount();
  }

  // ---------------------------------------------------------------------------
  // Clipboard: copy / paste / clear
  // ---------------------------------------------------------------------------

  // The async Clipboard API only exists in secure contexts (HTTPS or
  // localhost). LTBridge is often reached over plain HTTP on a LAN, so copy
  // falls back to the legacy execCommand path, and Paste — which has no
  // fallback (browsers never let a page read the clipboard otherwise) — is
  // only shown where it can work. Ctrl+V into the box always works.
  const canReadClipboard =
    window.isSecureContext && !!(navigator.clipboard && navigator.clipboard.readText);

  async function writeClipboard(text) {
    if (window.isSecureContext && navigator.clipboard && navigator.clipboard.writeText) {
      try {
        await navigator.clipboard.writeText(text);
        return;
      } catch (_) { /* fall through to the legacy path */ }
    }
    const helper = document.createElement("textarea");
    helper.value = text;
    helper.setAttribute("readonly", "");
    helper.style.position = "fixed";
    helper.style.top = "-1000px";
    helper.style.opacity = "0";
    document.body.appendChild(helper);
    const prevFocus = document.activeElement;
    helper.select();
    let ok = false;
    try {
      ok = document.execCommand("copy");
    } finally {
      helper.remove();
      if (prevFocus && prevFocus.focus) prevFocus.focus({ preventScroll: true });
    }
    if (!ok) throw new Error("Copy failed");
  }

  // Briefly swap a footer button's label ("Copied" / "Failed"), then restore.
  function flashButton(btn, text, cls) {
    const label = btn.querySelector(".btn-label");
    if (!btn.dataset.label) btn.dataset.label = label.textContent;
    clearTimeout(btn._flashTimer);
    label.textContent = text;
    btn.classList.remove("done", "failed");
    btn.classList.add(cls);
    btn._flashTimer = setTimeout(() => {
      label.textContent = btn.dataset.label;
      btn.classList.remove(cls);
    }, 1500);
  }

  async function copyFrom(textarea, btn) {
    if (!textarea.value) return;
    try {
      await writeClipboard(textarea.value);
      flashButton(btn, "Copied", "done");
    } catch (_) {
      flashButton(btn, "Failed", "failed");
    }
  }

  async function pasteIntoSource() {
    let text;
    try {
      text = await navigator.clipboard.readText();
    } catch (_) {
      flashButton(pasteBtn, "Blocked", "failed");
      return;
    }
    if (!text) return;
    // Insert at the caret / over the selection, exactly like Ctrl+V would.
    sourceText.focus();
    sourceText.setRangeText(text, sourceText.selectionStart, sourceText.selectionEnd, "end");
    updateCharCount();
  }

  function clearSource() {
    sourceText.value = "";
    targetText.value = "";
    detectedEl.textContent = "";
    statusMsg.textContent = "";
    statusMsg.className = "";
    clearStats();
    updateCharCount();
    sourceText.focus();
  }

  // ---------------------------------------------------------------------------
  // Char counter + button state
  // ---------------------------------------------------------------------------

  function updateButtons() {
    const hasSource = sourceText.value.length > 0;
    clearBtn.hidden = !hasSource;
    copySourceBtn.disabled = !hasSource;
    copyTargetBtn.disabled = targetText.value.length === 0;
  }

  function updateCharCount() {
    const len = sourceText.value.length;
    charCount.textContent = len;
    charCount.style.color = len > charLimit ? "#dc2626" : "";
    updateButtons();
  }

  // ---------------------------------------------------------------------------
  // Keyboard shortcut: Ctrl+Enter / Cmd+Enter to translate
  // ---------------------------------------------------------------------------

  sourceText.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      doTranslate();
    }
  });

  sourceText.addEventListener("input", updateCharCount);
  translateBtn.addEventListener("click", doTranslate);
  swapBtn.addEventListener("click", swapLanguages);
  clearBtn.addEventListener("click", clearSource);
  copySourceBtn.addEventListener("click", () => copyFrom(sourceText, copySourceBtn));
  copyTargetBtn.addEventListener("click", () => copyFrom(targetText, copyTargetBtn));
  pasteBtn.addEventListener("click", pasteIntoSource);
  pasteBtn.hidden = !canReadClipboard;

  // ---------------------------------------------------------------------------
  // Init
  // ---------------------------------------------------------------------------

  updateCharCount();
  await Promise.all([loadSettings(), loadLanguages()]);
  updateCharCount();
})();
