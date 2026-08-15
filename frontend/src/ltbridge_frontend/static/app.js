/* ltbridge frontend JS */

(async () => {
  const sourceSelect = document.getElementById("source-lang");
  const targetSelect = document.getElementById("target-lang");
  const sourceText   = document.getElementById("source-text");
  const targetText   = document.getElementById("target-text");
  const translateBtn = document.getElementById("translate-btn");
  const swapBtn      = document.getElementById("swap-btn");
  const charCount    = document.getElementById("char-count");
  const charLimitEl  = document.getElementById("char-limit");
  const detectedEl   = document.getElementById("detected-lang");
  const statusMsg    = document.getElementById("status-msg");

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

      targetText.value = data.translatedText || "";
      statusMsg.textContent = "";

      if (data.detectedLanguage) {
        const { language, confidence } = data.detectedLanguage;
        detectedEl.textContent = `Detected: ${language} (${confidence}%)`;
      }
    } catch (err) {
      statusMsg.textContent = err.message;
      statusMsg.className = "error";
    } finally {
      translateBtn.disabled = false;
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
    updateCharCount();
  }

  // ---------------------------------------------------------------------------
  // Char counter
  // ---------------------------------------------------------------------------

  function updateCharCount() {
    const len = sourceText.value.length;
    charCount.textContent = len;
    charCount.style.color = len > charLimit ? "#dc2626" : "";
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

  // ---------------------------------------------------------------------------
  // Init
  // ---------------------------------------------------------------------------

  await Promise.all([loadSettings(), loadLanguages()]);
  updateCharCount();
})();
