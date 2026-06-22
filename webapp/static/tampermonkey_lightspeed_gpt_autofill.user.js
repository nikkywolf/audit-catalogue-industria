// ==UserScript==
// @name         Lightspeed GPT Autofill
// @namespace    industria
// @version      2.2
// @match        https://industria-coiffure-641699.shoplightspeed.com/*
// @match        https://*.shoplightspeed.com/*
// @downloadURL  https://dashboardindustria.com/static/tampermonkey_lightspeed_gpt_autofill.user.js
// @updateURL    https://dashboardindustria.com/static/tampermonkey_lightspeed_gpt_autofill.user.js
// @run-at       document-idle
// @grant        none
// ==/UserScript==

(function () {
  "use strict";

  var STORAGE_KEY = "industria_lightspeed_gpt_json_v2";

  var FIELD_MAP = {
    FR_Title_Short: ['input[name="product[fr][title]"]', '[data-bind="fr.title"]'],
    FR_Title_Long: ['input[name="product[fr][fulltitle]"]', '[data-bind="fr.fulltitle"]'],
    US_Title_Short: ['input[name="product[us][title]"]', '[data-bind="us.title"]'],
    US_Title_Long: ['input[name="product[us][fulltitle]"]', '[data-bind="us.fulltitle"]'],
    FC_Title_Short: ['input[name="product[fc][title]"]', '[data-bind="fc.title"]'],
    FC_Title_Long: ['input[name="product[fc][fulltitle]"]', '[data-bind="fc.fulltitle"]'],
    FR_Description_Short: ['textarea[name="product[fr][description]"]', '[data-bind="fr.description"]'],
    US_Description_Short: ['textarea[name="product[us][description]"]', '[data-bind="us.description"]'],
    FC_Description_Short: ['textarea[name="product[fc][description]"]', '[data-bind="fc.description"]']
  };

  var LONG_DESCRIPTION_MAP = {
    FR_Description_Long: { textareaSelector: 'textarea[name="product[fr][content]"]', dataBindSelector: '[data-bind="fr.content"]', fieldName: "product[fr][content]" },
    US_Description_Long: { textareaSelector: 'textarea[name="product[us][content]"]', dataBindSelector: '[data-bind="us.content"]', fieldName: "product[us][content]" },
    FC_Description_Long: { textareaSelector: 'textarea[name="product[fc][content]"]', dataBindSelector: '[data-bind="fc.content"]', fieldName: "product[fc][content]" }
  };

  var SEO_CONFIG = {
    fr: {
      label: "Français",
      keys: { url: "FR_URL", title: "FR_Meta_Title", description: "FR_Meta_Description", keywords: "FR_Meta_Keywords", google: "FR_Google_Category" },
      hidden: {
        url: ['input[name="product[fr][slug]"]'],
        title: ['input[name="product[metafields][meta_title_fr]"]'],
        description: ['input[name="product[metafields][meta_description_fr]"]', 'textarea[name="product[metafields][meta_description_fr]"]'],
        keywords: ['input[name="product[metafields][meta_keywords_fr]"]'],
        google: ['input[name="product[metafields][google_product_category_fr]"]']
      }
    },
    us: {
      label: "English (US)",
      keys: { url: "US_URL", title: "US_Meta_Title", description: "US_Meta_Description", keywords: "US_Meta_Keywords", google: "US_Google_Category" },
      hidden: {
        url: ['input[name="product[us][slug]"]'],
        title: ['input[name="product[metafields][meta_title_us]"]'],
        description: ['input[name="product[metafields][meta_description_us]"]', 'textarea[name="product[metafields][meta_description_us]"]'],
        keywords: ['input[name="product[metafields][meta_keywords_us]"]'],
        google: ['input[name="product[metafields][google_product_category_us]"]']
      }
    },
    fc: {
      label: "Français (CA)",
      keys: { url: "FC_URL", title: "FC_Meta_Title", description: "FC_Meta_Description", keywords: "FC_Meta_Keywords", google: "FC_Google_Category" },
      hidden: {
        url: ['input[name="product[fc][slug]"]'],
        title: ['input[name="product[metafields][meta_title_fc]"]'],
        description: ['input[name="product[metafields][meta_description_fc]"]', 'textarea[name="product[metafields][meta_description_fc]"]'],
        keywords: ['input[name="product[metafields][meta_keywords_fc]"]'],
        google: ['input[name="product[metafields][google_product_category_fc]"]']
      }
    }
  };

  function isVisible(element) {
    return !!(element && (element.offsetWidth || element.offsetHeight || element.getClientRects().length));
  }

  function triggerEvents(element) {
    if (!element) return;
    element.dispatchEvent(new Event("input", { bubbles: true }));
    element.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function hasValue(element) {
    if (!element) return false;
    return String(element.value || element.textContent || "").trim() !== "";
  }

  function setElementValue(element, value, onlyEmpty) {
    var text = value == null ? "" : String(value);
    if (!element) return false;
    if (onlyEmpty && hasValue(element)) return "skipped_existing";

    element.focus();

    if (element.isContentEditable) {
      element.textContent = text;
    } else {
      element.value = text;
    }

    triggerEvents(element);
    return true;
  }

  function setAnyField(selectors, value, onlyEmpty) {
    var i, element, result;

    for (i = 0; i < selectors.length; i += 1) {
      element = document.querySelector(selectors[i]);
      if (!element) continue;

      result = setElementValue(element, value, onlyEmpty);
      if (result === true) return "filled";
      if (result === "skipped_existing") return "skipped_existing";
    }

    return "missing";
  }

  function setHiddenFields(selectors, value, onlyEmpty) {
    var count = 0;
    var skipped = 0;
    var i, j, elements, result;

    for (i = 0; i < selectors.length; i += 1) {
      elements = document.querySelectorAll(selectors[i]);

      for (j = 0; j < elements.length; j += 1) {
        result = setElementValue(elements[j], value, onlyEmpty);
        if (result === true) count += 1;
        if (result === "skipped_existing") skipped += 1;
      }
    }

    return { filled: count, skipped: skipped };
  }

  function clickSeoModifierButtons(callback) {
    var clickableElements = document.querySelectorAll("button, a, span");
    var clicked = 0;
    var i, element, text;

    for (i = 0; i < clickableElements.length; i += 1) {
      element = clickableElements[i];
      text = (element.textContent || "").replace(/\s+/g, " ").trim();

      if (text === "Modifier" && isVisible(element) && clicked < 3) {
        element.click();
        clicked += 1;
      }
    }

    window.setTimeout(function () {
      callback(clicked);
    }, 900);
  }

  function findSeoBlock(languageLabel) {
    var all = document.querySelectorAll("div, section, article");
    var candidates = [];
    var i, element, text;

    for (i = 0; i < all.length; i += 1) {
      element = all[i];
      text = (element.textContent || "").replace(/\s+/g, " ").trim();

      if (
        text.indexOf(languageLabel) !== -1 &&
        text.indexOf("Titre de la page") !== -1 &&
        text.indexOf("Adresse URL") !== -1 &&
        text.indexOf("Meta description") !== -1
      ) {
        candidates.push(element);
      }
    }

    candidates.sort(function (a, b) {
      return a.textContent.length - b.textContent.length;
    });

    return candidates[0] || null;
  }

  function setVisibleSeoFieldsInBlock(block, values, onlyEmpty) {
    var count = 0;
    var skipped = 0;
    var result;
    var inputs;

    function apply(element, value) {
      result = setElementValue(element, value, onlyEmpty);
      if (result === true) count += 1;
      if (result === "skipped_existing") skipped += 1;
    }

    if (!block) return { filled: 0, skipped: 0 };

    if (values.title != null && block.querySelector("input#title")) apply(block.querySelector("input#title"), values.title);
    if (values.url != null && block.querySelector("input#url")) apply(block.querySelector("input#url"), values.url);
    if (values.description != null && block.querySelector("textarea#description")) apply(block.querySelector("textarea#description"), values.description);

    inputs = block.querySelectorAll("input");

    Array.prototype.forEach.call(inputs, function (input) {
      var placeholder = input.getAttribute("placeholder") || "";

      if (values.keywords != null && isVisible(input) && placeholder.indexOf("mots-clés") !== -1) {
        apply(input, values.keywords);
      }

      if (values.google != null && isVisible(input) && placeholder.indexOf("Google") !== -1) {
        apply(input, values.google);
      }
    });

    return { filled: count, skipped: skipped };
  }

  function fillSeoForLanguage(data, config, summary, onlyEmpty) {
    var values = {};
    var hasAny = false;
    var key;
    var hiddenResult;
    var visibleResult;
    var block;

    for (key in config.keys) {
      if (Object.prototype.hasOwnProperty.call(config.keys, key)) {
        if (Object.prototype.hasOwnProperty.call(data, config.keys[key])) {
          values[key] = data[config.keys[key]];
          hasAny = true;
        } else {
          summary.skipped.push(config.keys[key]);
        }
      }
    }

    if (!hasAny) return;

    block = findSeoBlock(config.label);
    visibleResult = setVisibleSeoFieldsInBlock(block, values, onlyEmpty);

    for (key in values) {
      if (Object.prototype.hasOwnProperty.call(values, key)) {
        hiddenResult = setHiddenFields(config.hidden[key] || [], values[key], onlyEmpty);

        if (hiddenResult.filled > 0 || visibleResult.filled > 0) summary.filled.push(config.keys[key]);
        else if (hiddenResult.skipped > 0 || visibleResult.skipped > 0) summary.skippedExisting.push(config.keys[key]);
        else summary.missing.push(config.keys[key]);
      }
    }
  }

  function getTinyMceEditorForField(fieldName, textarea) {
    var editors, i, editor, target;

    if (!window.tinymce || !window.tinymce.editors) return null;

    editors = window.tinymce.editors;

    for (i = 0; i < editors.length; i += 1) {
      editor = editors[i];
      if (!editor) continue;

      target = editor.getElement ? editor.getElement() : null;

      if (target && target === textarea) return editor;
      if (target && target.getAttribute && target.getAttribute("name") === fieldName) return editor;
      if (textarea && editor.id === textarea.id) return editor;
      if (textarea && editor.targetElm === textarea) return editor;
    }

    return null;
  }

  function setTinyMCE(config, value, onlyEmpty) {
    var textarea = document.querySelector(config.textareaSelector);
    var fallbackElement = textarea || document.querySelector(config.dataBindSelector);
    var editor = getTinyMceEditorForField(config.fieldName, textarea);
    var current;
    var target;

    if (editor) {
      current = String(editor.getContent({ format: "text" }) || "").trim();
      if (onlyEmpty && current !== "") return "skipped_existing";

      editor.setContent(String(value || ""));
      editor.save();

      target = editor.getElement ? editor.getElement() : null;
      triggerEvents(target);
      return "filled";
    }

    if (fallbackElement) {
      return setElementValue(fallbackElement, value, onlyEmpty) === true ? "filled" : "skipped_existing";
    }

    return "missing";
  }

  function setTagsField(value, onlyEmpty) {
    var selectors = [
      'input[name="product[tags]"]',
      'input[name="tags"]',
      'input[data-bind*="tags"]',
      'input[data-bind*="tag"]'
    ];

    var i, j, element, blocks, block, text, inputs, candidates = [];
    var result;

    for (i = 0; i < selectors.length; i += 1) {
      element = document.querySelector(selectors[i]);
      if (element && isVisible(element)) {
        result = setElementValue(element, value, onlyEmpty);
        return result === true ? "filled" : "skipped_existing";
      }
    }

    blocks = document.querySelectorAll("div, section");

    for (i = 0; i < blocks.length; i += 1) {
      block = blocks[i];
      text = (block.textContent || "").replace(/\s+/g, " ").trim();

      if (
        text.indexOf("BALISES") !== -1 ||
        text.indexOf("Les balises peuvent") !== -1 ||
        text.indexOf("Séparez les balises") !== -1
      ) {
        candidates.push(block);
      }
    }

    candidates.sort(function (a, b) {
      return a.textContent.length - b.textContent.length;
    });

    for (i = 0; i < candidates.length; i += 1) {
      inputs = candidates[i].querySelectorAll("input");

      for (j = 0; j < inputs.length; j += 1) {
        if (isVisible(inputs[j])) {
          result = setElementValue(inputs[j], value, onlyEmpty);
          return result === true ? "filled" : "skipped_existing";
        }
      }
    }

    return "missing";
  }

  function fillFieldsFromJson(data, onlyEmpty) {
    var summary = { filled: [], missing: [], skipped: [], skippedExisting: [], unknown: [] };
    var known = {};
    var key, result;

    for (key in FIELD_MAP) {
      if (Object.prototype.hasOwnProperty.call(FIELD_MAP, key)) {
        known[key] = true;

        if (!Object.prototype.hasOwnProperty.call(data, key)) summary.skipped.push(key);
        else {
          result = setAnyField(FIELD_MAP[key], data[key], onlyEmpty);
          if (result === "filled") summary.filled.push(key);
          else if (result === "skipped_existing") summary.skippedExisting.push(key);
          else summary.missing.push(key);
        }
      }
    }

    for (key in LONG_DESCRIPTION_MAP) {
      if (Object.prototype.hasOwnProperty.call(LONG_DESCRIPTION_MAP, key)) {
        known[key] = true;

        if (!Object.prototype.hasOwnProperty.call(data, key)) summary.skipped.push(key);
        else {
          result = setTinyMCE(LONG_DESCRIPTION_MAP[key], data[key], onlyEmpty);
          if (result === "filled") summary.filled.push(key);
          else if (result === "skipped_existing") summary.skippedExisting.push(key);
          else summary.missing.push(key);
        }
      }
    }

    ["fr", "us", "fc"].forEach(function (lang) {
      var config = SEO_CONFIG[lang];
      var sectionKey;

      for (sectionKey in config.keys) {
        if (Object.prototype.hasOwnProperty.call(config.keys, sectionKey)) {
          known[config.keys[sectionKey]] = true;
        }
      }

      fillSeoForLanguage(data, config, summary, onlyEmpty);
    });

    known.Tags = true;

    for (key in data) {
      if (Object.prototype.hasOwnProperty.call(data, key) && !known[key]) {
        summary.unknown.push(key);
      }
    }

    return summary;
  }

  function summaryText(summary) {
    return [
      "Résumé",
      "",
      "Champs remplis (" + summary.filled.length + ")",
      summary.filled.join("\n") || "Aucun",
      "",
      "Champs ignorés car déjà remplis (" + summary.skippedExisting.length + ")",
      summary.skippedExisting.join("\n") || "Aucun",
      "",
      "Champs non trouvés (" + summary.missing.length + ")",
      summary.missing.join("\n") || "Aucun",
      "",
      "Clés absentes du JSON (" + summary.skipped.length + ")",
      summary.skipped.join("\n") || "Aucun",
      "",
      "Clés inconnues dans le JSON (" + summary.unknown.length + ")",
      summary.unknown.join("\n") || "Aucune",
      "",
      "Aucun clic sur Enregistrer n'a été effectué."
    ].join("\n");
  }

  function showAutoStatus(message, isError) {
    var existing = document.getElementById("industria-autofill-status");
    var box = existing || document.createElement("div");
    box.id = "industria-autofill-status";
    box.textContent = message;
    box.style.cssText = [
      "position:fixed",
      "bottom:82px",
      "right:20px",
      "z-index:999999999",
      "max-width:420px",
      "background:" + (isError ? "#991b1b" : "#111827"),
      "color:white",
      "border-radius:8px",
      "padding:12px 14px",
      "font:13px/1.35 Arial,sans-serif",
      "box-shadow:0 8px 24px rgba(0,0,0,.25)"
    ].join(";");
    if (!existing) document.body.appendChild(box);
  }

  function getDashboardAutofillParams() {
    var params = new URLSearchParams(window.location.search);
    var autofillMode = params.get("industria_autofill") || "";
    if (autofillMode !== "1" && autofillMode !== "batch") return null;
    return {
      mode: autofillMode,
      variantId: params.get("industria_variant") || "",
      apiBase: (params.get("industria_api") || "").replace(/\/$/, "")
    };
  }

  function waitForProductForm(callback, attempts) {
    attempts = attempts || 0;
    if (
      document.querySelector('input[name="product[fr][title]"]') ||
      document.querySelector('input[name="product[fc][title]"]') ||
      document.querySelector('[data-bind="fr.title"]') ||
      document.querySelector('[data-bind="fc.title"]')
    ) {
      callback();
      return;
    }
    if (attempts > 80) {
      callback();
      return;
    }
    window.setTimeout(function () {
      waitForProductForm(callback, attempts + 1);
    }, 250);
  }

  function cleanAutofillUrl() {
    var url = new URL(window.location.href);
    url.searchParams.delete("industria_autofill");
    url.searchParams.delete("industria_variant");
    url.searchParams.delete("industria_api");
    window.history.replaceState({}, document.title, url.toString());
  }

  function runDashboardAutofill() {
    var params = getDashboardAutofillParams();
    var runKey;

    if (!params) return;
    if (!params.variantId || !params.apiBase) {
      showAutoStatus("Autofill Industria: paramètres manquants.", true);
      return;
    }

    runKey = "industria_autofill_done_" + params.variantId;
    if (sessionStorage.getItem(runKey) === "1") return;
    sessionStorage.setItem(runKey, "1");

    showAutoStatus(params.mode === "batch" ? "Autofill Industria: récupération du JSON batch..." : "Autofill Industria: génération du JSON en cours...");

    fetch(
      params.apiBase +
        "/api/products/" +
        encodeURIComponent(params.variantId) +
        (params.mode === "batch" ? "/batch-json" : "/autofill-json"),
      { credentials: "include" }
    )
      .then(function (response) {
        if (!response.ok) {
          return response.text().then(function (text) {
            throw new Error(text || response.statusText);
          });
        }
        return response.json();
      })
      .then(function (data) {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(data, null, 2));
        showAutoStatus("Autofill Industria: ouverture des sections SEO...");
        clickSeoModifierButtons(function () {
          waitForProductForm(function () {
            var summary = fillFieldsFromJson(data, false);
            showAutoStatus(
              "Autofill Industria terminé. Champs remplis: " +
                summary.filled.length +
                ". Aucun clic sur Enregistrer.",
              false
            );
            cleanAutofillUrl();
          });
        });
      })
      .catch(function (error) {
        console.error(error);
        sessionStorage.removeItem(runKey);
        showAutoStatus("Autofill Industria erreur: " + error.message, true);
      });
  }

  function sampleJson() {
    return "{\n" +
      '  "FR_Title_Short": "Titre court FR",\n' +
      '  "FR_Title_Long": "Titre long FR",\n' +
      '  "US_Title_Short": "Short title US",\n' +
      '  "US_Title_Long": "Long title US",\n' +
      '  "FC_Title_Short": "Titre court FC",\n' +
      '  "FC_Title_Long": "Titre long FC",\n' +
      '  "FR_Description_Short": "Description courte FR",\n' +
      '  "US_Description_Short": "Short description US",\n' +
      '  "FC_Description_Short": "Description courte FC",\n' +
      '  "FR_Description_Long": "<h2>DESCRIPTION</h2><p>Texte FR...</p>",\n' +
      '  "US_Description_Long": "<h2>DESCRIPTION</h2><p>Text US...</p>",\n' +
      '  "FC_Description_Long": "<h2>DESCRIPTION</h2><p>Texte FC...</p>",\n' +
      '  "FR_URL": "slug-fr",\n' +
      '  "US_URL": "slug-us",\n' +
      '  "FC_URL": "slug-fc",\n' +
      '  "FR_Meta_Title": "Meta title FR",\n' +
      '  "FR_Meta_Description": "Meta description FR",\n' +
      '  "FR_Meta_Keywords": "mots cles",\n' +
      '  "FR_Google_Category": "Health & Beauty > Personal Care > Hair Care",\n' +
      '  "US_Meta_Title": "Meta title US",\n' +
      '  "US_Meta_Description": "Meta description US",\n' +
      '  "US_Meta_Keywords": "keywords",\n' +
      '  "US_Google_Category": "Health & Beauty > Personal Care > Hair Care",\n' +
      '  "FC_Meta_Title": "Meta title FC",\n' +
      '  "FC_Meta_Description": "Meta description FC",\n' +
      '  "FC_Meta_Keywords": "mots cles",\n' +
      '  "FC_Google_Category": "Health & Beauty > Personal Care > Hair Care"\n' +
      "}";
  }

  function openJsonModal() {
    var existing = document.getElementById("gpt-json-modal");
    var textarea;

    if (existing) {
      existing.style.display = "block";
      textarea = document.getElementById("gpt-json-textarea");
      if (textarea) textarea.focus();
      return;
    }

    var modal = document.createElement("div");
    modal.id = "gpt-json-modal";
    modal.style.cssText = "position:fixed;left:0;top:0;right:0;bottom:0;z-index:999999998;background:rgba(0,0,0,0.45);";

    var box = document.createElement("div");
    box.style.cssText = "position:fixed;left:50%;top:50%;transform:translate(-50%,-50%);width:860px;max-width:92vw;background:white;padding:16px;border-radius:8px;box-shadow:0 10px 40px rgba(0,0,0,.35);font-family:Arial,sans-serif;";

    var title = document.createElement("div");
    title.textContent = "Remplir avec GPT";
    title.style.cssText = "font-weight:bold;margin-bottom:10px;font-size:16px;";

    textarea = document.createElement("textarea");
    textarea.id = "gpt-json-textarea";
    textarea.style.cssText = "width:100%;height:380px;box-sizing:border-box;font-family:monospace;font-size:13px;";
    textarea.value = localStorage.getItem(STORAGE_KEY) || sampleJson();

    var onlyEmptyLabel = document.createElement("label");
    onlyEmptyLabel.style.cssText = "display:block;margin-top:10px;font-size:13px;";

    var onlyEmpty = document.createElement("input");
    onlyEmpty.type = "checkbox";
    onlyEmpty.id = "gpt-only-empty";

    onlyEmptyLabel.appendChild(onlyEmpty);
    onlyEmptyLabel.appendChild(document.createTextNode(" Ne remplir que les champs vides"));

    var fillButton = document.createElement("button");
    fillButton.type = "button";
    fillButton.textContent = "Remplir les champs";
    fillButton.style.cssText = "margin-top:10px;margin-right:8px;background:#2563eb;color:white;border:0;border-radius:6px;padding:10px 14px;font-weight:bold;cursor:pointer;";

    var validateButton = document.createElement("button");
    validateButton.type = "button";
    validateButton.textContent = "Valider le JSON";
    validateButton.style.cssText = "margin-top:10px;margin-right:8px;background:#475569;color:white;border:0;border-radius:6px;padding:10px 14px;font-weight:bold;cursor:pointer;";

    var closeButton = document.createElement("button");
    closeButton.type = "button";
    closeButton.textContent = "Fermer";
    closeButton.style.cssText = "margin-top:10px;padding:10px 14px;cursor:pointer;";

    var output = document.createElement("pre");
    output.id = "gpt-json-summary";
    output.style.cssText = "white-space:pre-wrap;background:#f1f5f9;margin-top:12px;padding:10px;border-radius:6px;max-height:220px;overflow:auto;font-size:12px;";

    textarea.addEventListener("input", function () {
      localStorage.setItem(STORAGE_KEY, textarea.value);
    });

    validateButton.addEventListener("click", function () {
      try {
        JSON.parse(textarea.value);
        localStorage.setItem(STORAGE_KEY, textarea.value);
        output.textContent = "JSON valide. Tu peux remplir les champs.";
      } catch (error) {
        output.textContent = "JSON invalide : " + error.message;
      }
    });

    fillButton.addEventListener("click", function () {
      try {
        var data = JSON.parse(textarea.value);
        var summary = fillFieldsFromJson(data, onlyEmpty.checked);
        localStorage.setItem(STORAGE_KEY, textarea.value);
        output.textContent = summaryText(summary);
      } catch (error) {
        output.textContent = "JSON invalide : " + error.message;
      }
    });

    closeButton.addEventListener("click", function () {
      modal.style.display = "none";
    });

    box.appendChild(title);
    box.appendChild(textarea);
    box.appendChild(onlyEmptyLabel);
    box.appendChild(fillButton);
    box.appendChild(validateButton);
    box.appendChild(closeButton);
    box.appendChild(output);
    modal.appendChild(box);
    document.body.appendChild(modal);
    textarea.focus();
  }

  function createMainButton() {
    if (document.getElementById("gpt-autofill-button")) return;

    var button = document.createElement("button");
    button.id = "gpt-autofill-button";
    button.type = "button";
    button.textContent = "Remplir avec GPT";
    button.style.cssText = "position:fixed;bottom:20px;right:20px;z-index:999999999;background:#2563eb;color:white;border:0;border-radius:8px;padding:14px 18px;font-size:15px;font-weight:bold;cursor:pointer;box-shadow:0 8px 24px rgba(0,0,0,.25);";

    button.addEventListener("click", function () {
      clickSeoModifierButtons(function () {
        openJsonModal();
      });
    });

    document.body.appendChild(button);
  }

  function init() {
    if (!document.body) {
      window.setTimeout(init, 250);
      return;
    }

    createMainButton();
    runDashboardAutofill();
  }

  init();
})();
