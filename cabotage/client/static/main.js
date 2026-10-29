/* Slugify */
function slugify(text) {
  return text
    .toString()
    .toLowerCase()
    .replace(/\s+/g, '-')
    .replace(/[^\w\-]+/g, '')
    .replace(/\-\-+/g, '-')
    .replace(/^-+/, '')
    .replace(/-+$/, '')
    .replace(/[\s_-]+/g, '-');
}

function applySlugify(sourceSelector, destinationSelector) {
  var dest = document.querySelector(destinationSelector);
  var source = document.querySelector(sourceSelector);
  if (!dest || !source) return;
  dest.addEventListener('keyup', function () {
    if (!dest.classList.contains('user-has-edited')) {
      dest.classList.add('user-has-edited');
    }
  });
  source.addEventListener('keyup', function () {
    if (!dest.classList.contains('user-has-edited')) {
      dest.value = slugify(source.value);
    }
  });
}

/* Tab Navigation */
function initTabs(containerSelector) {
  var container = document.querySelector(containerSelector || '[data-tabs]');
  if (!container) return;

  var tabs = container.querySelectorAll('[data-tab]');
  var panels = document.querySelectorAll('[data-tab-panel]');

  function activateTab(tabId, pushHistory) {
    tabs.forEach(function (t) {
      t.classList.toggle('tab-active', t.getAttribute('data-tab') === tabId);
    });

    panels.forEach(function (p) {
      var panelId = p.getAttribute('data-tab-panel');
      if (panelId === tabId) {
        p.classList.add('tab-panel-active');
        p.dispatchEvent(new CustomEvent('tab-activated'));
      } else if (p.classList.contains('tab-panel-active')) {
        p.dispatchEvent(new CustomEvent('tab-deactivated'));
        p.classList.remove('tab-panel-active');
      }
    });

    if (pushHistory) {
      history.pushState(null, null, '#' + tabId);
    }
  }

  tabs.forEach(function (tab) {
    // URL tabs (href not starting with #) navigate normally — don't intercept
    var href = tab.getAttribute('href');
    if (href && href.charAt(0) !== '#') return;
    // Disabled tabs don't participate in SPA navigation
    if (tab.classList.contains('tab-disabled')) return;

    tab.addEventListener('click', function (e) {
      e.preventDefault();
      activateTab(tab.getAttribute('data-tab'), true);
    });
  });

  window.addEventListener('popstate', function () {
    var hash = window.location.hash.replace('#', '');
    var validTab = false;
    tabs.forEach(function (t) {
      if (t.getAttribute('data-tab') === hash) validTab = true;
    });
    if (validTab) {
      activateTab(hash, false);
    }
  });

  var hash = window.location.hash.replace('#', '');
  var validTab = false;
  tabs.forEach(function (t) {
    if (t.getAttribute('data-tab') === hash) validTab = true;
  });

  if (validTab) {
    activateTab(hash, false);
  } else if (tabs.length > 0) {
    activateTab(tabs[0].getAttribute('data-tab'), false);
  }
}

/* Increment/Decrement (Process Scaling) */
function initCountInputs() {
  document.querySelectorAll('.incr-btn').forEach(function (button) {
    button.addEventListener('click', function (e) {
      e.preventDefault();
      var parent = button.closest('.count-input');
      if (!parent) return;
      var input = parent.querySelector('.quantity');
      if (!input) return;

      var oldValue = parseFloat(input.value) || 0;
      var decrBtn = parent.querySelector('.incr-btn[data-action="decrease"]');
      if (decrBtn) decrBtn.classList.remove('inactive');

      if (button.getAttribute('data-action') === 'increase') {
        input.value = oldValue + 1;
      } else {
        input.value = Math.max(0, oldValue - 1);
        if (input.value == 0 && decrBtn) decrBtn.classList.add('inactive');
      }

      document.querySelectorAll('.update_process_settings').forEach(function (el) {
        el.classList.remove('hidden');
      });
    });
  });

  document.querySelectorAll('.pod-size').forEach(function (select) {
    select.addEventListener('change', function () {
      document.querySelectorAll('.update_process_settings').forEach(function (el) {
        el.classList.remove('hidden');
      });
    });
  });
}

/* Job Toggle (CronJob suspend/resume) */
function initJobToggles() {
  document.querySelectorAll('.job-toggle').forEach(function (toggle) {
    toggle.addEventListener('change', function () {
      var fieldName = toggle.getAttribute('data-count-field');
      var hidden = document.querySelector('input[type="hidden"][name="' + fieldName + '"]');
      if (hidden) hidden.value = toggle.checked ? '1' : '0';
      document.querySelectorAll('.update_process_settings').forEach(function (el) {
        el.classList.remove('hidden');
      });
    });
  });
}

/* Env Var Reveal */
function initEnvReveal() {
  document.querySelectorAll('[data-reveal]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var target = document.getElementById(btn.getAttribute('data-reveal'));
      if (!target) return;
      var hidden = target.querySelector('.env-hidden');
      var shown = target.querySelector('.env-shown');
      if (hidden && shown) {
        hidden.classList.toggle('hidden');
        shown.classList.toggle('hidden');
        btn.textContent = hidden.classList.contains('hidden') ? 'Hide' : 'Reveal';
      }
    });
  });
}

/* Dropdown Close */
function initDropdowns() {
  document.addEventListener('click', function (e) {
    if (!e.target.closest('.dropdown')) {
      document.querySelectorAll('.dropdown [tabindex]').forEach(function (el) {
        el.blur();
      });
    }
  });
}

/* Mobile Nav Toggle */
function initMobileNav() {
  var toggle = document.getElementById('mobile-nav-toggle');
  var menu = document.getElementById('mobile-nav-menu');
  if (toggle && menu) {
    toggle.addEventListener('click', function () {
      menu.classList.toggle('hidden');
    });
  }

  var tabBar = document.querySelector('[data-tabs]');
  var mobileTabsContainer = document.getElementById('mobile-nav-tabs');
  var mobileDivider = document.getElementById('mobile-nav-divider');
  if (tabBar && mobileTabsContainer) {
    var tabs = tabBar.querySelectorAll('.tab-item');
    if (tabs.length) {
      tabs.forEach(function (tab) {
        var a = document.createElement('a');
        a.href = tab.getAttribute('href') || '#';
        a.className = 'btn btn-ghost btn-sm justify-start text-sm';
        a.textContent = tab.textContent.trim().replace(/\s*\d+$/, '');
        a.setAttribute('data-mobile-tab', tab.getAttribute('data-tab') || '');
        a.addEventListener('click', function (e) {
          e.preventDefault();
          tab.click();
          if (menu) menu.classList.add('hidden');
        });
        mobileTabsContainer.appendChild(a);
      });
      if (mobileDivider) mobileDivider.classList.remove('hidden');
    }
  }
}

/* Theme Toggle (click cycles, long-hover reveals dropdown) */
function initThemeToggle() {
  function resolveSystem() {
    return window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
  }

  function applyPref(pref) {
    var resolved = pref === 'system' ? resolveSystem() : pref;
    document.documentElement.setAttribute('data-theme', resolved);
    document.documentElement.setAttribute('data-theme-pref', pref);
    localStorage.setItem('theme-pref', pref);
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta) {
      var metaColors = {
        light: '#fafafe', terminal: '#0a0a0a',
        'contrast-dark': '#010409', 'contrast-light': '#ffffff',
        'cb-protanopia': '#0d0e16', 'cb-deuteranopia': '#140f0a', 'cb-tritanopia': '#170f0f'
      };
      meta.content = metaColors[resolved] || '#0f0f17';
    }
    var root = document.documentElement;
    var accent = localStorage.getItem('accent-color') || root.getAttribute('data-accent-default') || 'purple';
    // Terminal only takes the mono accents. Not saved, so leaving terminal brings back the user's pick or the cluster's color.
    if (resolved === 'terminal' && accent !== 'white' && accent !== 'dark') accent = 'white';
    root.setAttribute('data-accent', accent);
    document.querySelectorAll('.accent-opt').forEach(function (b) {
      b.style.borderColor = b.getAttribute('data-accent') === accent ? 'var(--color-base-content)' : 'transparent';
    });
    if (window.__applyAccent) window.__applyAccent(accent, resolved);
  }

  var cycleThemes = ['light', 'dark', 'system'];

  document.querySelectorAll('.theme-toggle-wrap').forEach(function (wrap) {
    var btn = wrap.querySelector('[role="button"]');
    var dropdown = wrap.querySelector('.theme-dropdown');

    function cycleTheme() {
      var current = localStorage.getItem('theme-pref') || 'system';
      var idx = cycleThemes.indexOf(current);
      var next = cycleThemes[(idx + 1) % cycleThemes.length];
      applyPref(next);
    }

    btn.addEventListener('click', function (e) {
      e.stopPropagation();
      btn.blur();
      cycleTheme();
    });

    dropdown.querySelectorAll('.theme-opt').forEach(function (opt) {
      opt.addEventListener('click', function (e) {
        e.stopPropagation();
        applyPref(opt.getAttribute('data-theme-val'));
        btn.blur();
      });
    });
  });

  window.matchMedia('(prefers-color-scheme: light)').addEventListener('change', function () {
    var pref = localStorage.getItem('theme-pref') || 'system';
    if (pref === 'system') {
      applyPref('system');
    }
  });

  var pref = localStorage.getItem('theme-pref') || 'system';
  document.documentElement.setAttribute('data-theme-pref', pref);
}

/* Accent Color Picker (lives inside theme dropdown) */
function initAccentPicker() {
  function getResolvedTheme() {
    var pref = localStorage.getItem('theme-pref') || 'system';
    if (pref === 'system') {
      return window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
    }
    return pref;
  }

  function markActive(accent) {
    document.querySelectorAll('.accent-opt').forEach(function (btn) {
      if (btn.getAttribute('data-accent') === accent) {
        btn.style.borderColor = 'var(--color-base-content)';
      } else {
        btn.style.borderColor = 'transparent';
      }
    });
  }

  var current = document.documentElement.getAttribute('data-accent') || 'purple';
  markActive(current);

  document.querySelectorAll('.accent-opt').forEach(function (opt) {
    opt.addEventListener('click', function (e) {
      e.stopPropagation();
      var name = opt.getAttribute('data-accent');
      localStorage.setItem('accent-color', name);
      document.documentElement.setAttribute('data-accent', name);
      var theme = getResolvedTheme();
      if (window.__applyAccent) window.__applyAccent(name, theme);
      markActive(name);
    });
  });
}

/* Raw Editor Modal */
function initRawEditor() {
  var modal = document.getElementById('raw-editor-modal');
  if (!modal) return;

  var openBtn = document.getElementById('raw-editor-open');
  var closeBtn = document.getElementById('raw-editor-close');
  var cancelBtn = document.getElementById('raw-editor-cancel');
  var backdrop = modal.querySelector('.raw-editor-backdrop');
  var textarea = document.getElementById('raw-editor-textarea');
  var formatInput = document.getElementById('raw-editor-format');
  var copyBtn = document.getElementById('raw-editor-copy');
  var tabs = modal.querySelectorAll('[data-editor-tab]');
  var panels = modal.querySelectorAll('[data-editor-panel]');

  function openModal() {
    modal.style.display = 'flex';
    if (textarea) textarea.focus();
  }
  function closeModal() {
    modal.style.display = 'none';
  }

  if (openBtn) openBtn.addEventListener('click', openModal);
  if (closeBtn) closeBtn.addEventListener('click', closeModal);
  if (cancelBtn) cancelBtn.addEventListener('click', closeModal);
  if (backdrop) backdrop.addEventListener('click', closeModal);

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && modal.style.display !== 'none') {
      closeModal();
    }
  });

  tabs.forEach(function (tab) {
    tab.addEventListener('click', function () {
      var tabId = tab.getAttribute('data-editor-tab');
      tabs.forEach(function (t) {
        t.classList.toggle('raw-editor-tab-active', t.getAttribute('data-editor-tab') === tabId);
      });
      panels.forEach(function (p) {
        p.style.display = p.getAttribute('data-editor-panel') === tabId ? '' : 'none';
      });
      if (formatInput) formatInput.value = tabId;

      if (textarea) {
        if (tabId === 'json') {
          textarea.placeholder = '{\n  "DATABASE_URL": "postgres://...",\n  "REDIS_URL": "redis://..."\n}';
        } else {
          textarea.placeholder =
            '# Paste your environment variables here\nDATABASE_URL=postgres://...\nREDIS_URL=redis://...';
        }
      }
    });
  });

  if (copyBtn) {
    copyBtn.addEventListener('click', function () {
      var dataEl = document.getElementById('env-export-data');
      if (!dataEl) return;
      try {
        var configs = JSON.parse(dataEl.textContent);
        var lines = configs.map(function (c) {
          if (c.secret) return c.name + '=**secure**';
          return c.name + '=' + c.value;
        });
        var text = lines.join('\n');
        navigator.clipboard.writeText(text).then(function () {
          var orig = copyBtn.innerHTML;
          copyBtn.textContent = 'Copied!';
          setTimeout(function () {
            copyBtn.innerHTML = orig;
          }, 1500);
        });
      } catch (e) {
      }
    });
  }
}

/* Fade-scroll: update fade classes based on scroll position */
function initFadeScroll(el) {
  function update() {
    var atLeft = el.scrollLeft <= 1;
    var atRight = el.scrollLeft + el.clientWidth >= el.scrollWidth - 1;
    el.classList.remove('fade-left', 'fade-right', 'fade-both');
    if (!atLeft && !atRight) el.classList.add('fade-both');
    else if (!atLeft) el.classList.add('fade-left');
    else if (!atRight) el.classList.add('fade-right');
  }
  el.addEventListener('scroll', update);
  // Use ResizeObserver to catch content/size changes
  if (window.ResizeObserver) {
    new ResizeObserver(update).observe(el);
  }
  update();
  return update;
}

function bindKeyValuePaste(nameInput, valueInput) {
  if (!nameInput || !valueInput) return;

  nameInput.addEventListener('paste', function (e) {
    if (!e.clipboardData) return;

    var text = e.clipboardData.getData('text/plain').replace(/(?:\r\n|\r|\n)$/, '');
    if (/[\r\n]/.test(text)) return;

    var separator = text.indexOf('=');
    if (separator < 1 || valueInput.value) return;

    var name = text.slice(0, separator);
    if (!/^[a-zA-Z_][a-zA-Z0-9_]*$/.test(name)) return;

    e.preventDefault();
    nameInput.value = name.toUpperCase();
    valueInput.value = text.slice(separator + 1);
    nameInput.dispatchEvent(new Event('input', { bubbles: true }));
    valueInput.dispatchEvent(new Event('input', { bubbles: true }));
    valueInput.focus();
    if (valueInput.setSelectionRange) {
      valueInput.setSelectionRange(valueInput.value.length, valueInput.value.length);
    }
  });
}

/* Add Variable Modal */
function initAddVarModal() {
  var modal = document.getElementById('add-var-modal');
  if (!modal) return;

  var _addVarResetHooks = [];
  function openModal() {
    modal.style.display = 'flex';
    var nameInput = modal.querySelector('input[name="name"]');
    if (nameInput) {
      nameInput.value = '';
      nameInput.focus();
    }
    var valueInput = modal.querySelector('input[name="value"]');
    if (valueInput) valueInput.value = '';
    _addVarResetHooks.forEach(function (fn) { fn(); });
  }
  function closeModal() {
    modal.style.display = 'none';
  }

  document.querySelectorAll('#add-var-open, [data-add-var-open]').forEach(function (btn) {
    btn.addEventListener('click', openModal);
  });

  modal.querySelectorAll('[data-add-var-close]').forEach(function (el) {
    el.addEventListener('click', closeModal);
  });

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && modal.style.display !== 'none') {
      closeModal();
    }
  });

  var nameField = modal.querySelector('input[name="name"]');
  if (nameField) {
    nameField.addEventListener('input', function () {
      var pos = this.selectionStart;
      this.value = this.value.toUpperCase().replace(/[^A-Z0-9_]/g, '_');
      this.selectionStart = this.selectionEnd = pos;
    });
  }

  // Init fade-scroll on all elements in the modal
  var fadeScrollEls = modal.querySelectorAll('.fade-scroll');
  fadeScrollEls.forEach(function (el) { initFadeScroll(el); });

  // Template preview — resolve ${...} references client-side for preview
  var valueInput = modal.querySelector('input[name="value"]');
  bindKeyValuePaste(nameField, valueInput);
  var previewEl = document.getElementById('add-var-preview');
  var previewFadeUpdate = previewEl ? initFadeScroll(previewEl) : null;
  var siblingDataEl = document.getElementById('sibling-ref-data');
  var siblingRefs = null;
  var siblingTcpDataEl = document.getElementById('sibling-tcp-ref-data');
  var siblingTcpRefs = null;

  if (siblingDataEl) {
    try { siblingRefs = JSON.parse(siblingDataEl.textContent); } catch (e) { /* ignore */ }
  }
  if (siblingTcpDataEl) {
    try { siblingTcpRefs = JSON.parse(siblingTcpDataEl.textContent); } catch (e) { /* ignore */ }
  }

  var sharedDataEl = document.getElementById('shared-ref-data');
  var sharedRefs = null;
  if (sharedDataEl) {
    try { sharedRefs = JSON.parse(sharedDataEl.textContent); } catch (e) { /* ignore */ }
  }

  function resolvePreview(val) {
    if (!val || val.indexOf('${') === -1) return null;
    if (!siblingRefs && !siblingTcpRefs && !sharedRefs) return null;
    var pattern = /\$\{([a-zA-Z0-9_-]+)(?:\.([a-zA-Z0-9_-]+))?\.(url|host|svc|hostname|port)\}/g;
    var sharedRefPattern = /\$\{shared\.([a-zA-Z_][a-zA-Z0-9_]*)\}/g;
    var hasMatch = false;

    // Resolve shared references first
    if (sharedRefs) {
      val = val.replace(sharedRefPattern, function (match, varName) {
        var ref = sharedRefs[varName];
        if (ref) { hasMatch = true; return ref; }
        return match;
      });
    }

    var resolved = val.replace(pattern, function (match, appSlug, middle, prop) {
      if (prop === 'svc' || prop === 'hostname' || prop === 'port') {
        if (!siblingTcpRefs || !middle) return match;
        var tcpSib = null;
        for (var i = 0; i < siblingTcpRefs.length; i++) {
          if (siblingTcpRefs[i].slug === appSlug) { tcpSib = siblingTcpRefs[i]; break; }
        }
        if (!tcpSib) return match;
        var found = false;
        for (var j = 0; j < tcpSib.processes.length; j++) {
          if (tcpSib.processes[j] === middle) { found = true; break; }
        }
        if (!found) return match;
        hasMatch = true;
        if (prop === 'port') return '8000';
        var fqdn = appSlug + '-' + middle + '.<namespace>.svc.cluster.local';
        if (prop === 'hostname') return fqdn;
        return fqdn + ':8000';
      }
      if (!siblingRefs) return match;
      var sib = null;
      for (var i = 0; i < siblingRefs.length; i++) {
        if (siblingRefs[i].slug === appSlug) { sib = siblingRefs[i]; break; }
      }
      if (!sib) return match;
      var ing = null;
      if (middle) {
        for (var j = 0; j < sib.ingresses.length; j++) {
          if (sib.ingresses[j].name === middle) { ing = sib.ingresses[j]; break; }
        }
      } else if (sib.ingresses.length === 1) {
        ing = sib.ingresses[0];
      }
      if (!ing) return match;
      hasMatch = true;
      if (prop === 'host') return ing.hostname;
      return (ing.tls ? 'https://' : 'http://') + ing.hostname;
    });
    return hasMatch ? resolved : null;
  }

  var secureCheckbox = modal.querySelector('input[name="secure"]');
  var templatePattern = /\$\{[a-zA-Z0-9_-]+(?:\.[a-zA-Z0-9_-]+)?\.(url|host|svc|hostname|port)\}/;
  var sharedPattern = /\$\{shared\.[a-zA-Z_][a-zA-Z0-9_]*\}/;

  function hasTemplateVars(val) {
    return val && (templatePattern.test(val) || sharedPattern.test(val));
  }

  function updatePreview() {
    var val = valueInput ? valueInput.value : '';
    var isTemplate = hasTemplateVars(val);

    if (previewEl) {
      var resolved = resolvePreview(val);
      if (resolved) {
        previewEl.textContent = resolved;
        previewEl.title = resolved;
        previewEl.style.display = '';
        if (previewFadeUpdate) previewFadeUpdate();
      } else {
        previewEl.style.display = 'none';
      }
    }

    if (secureCheckbox) {
      if (isTemplate) {
        secureCheckbox.checked = false;
        secureCheckbox.disabled = true;
      } else {
        secureCheckbox.disabled = false;
      }
    }
  }

  if (valueInput) {
    valueInput.addEventListener('input', updatePreview);
  }

  // Reference picker — inserts template strings into value field at cursor
  modal.querySelectorAll('.add-var-ref-insert').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var ref = btn.getAttribute('data-ref');
      var pos = valueInput.selectionStart || valueInput.value.length;
      var before = valueInput.value.slice(0, pos);
      var after = valueInput.value.slice(pos);
      valueInput.value = before + ref + after;
      valueInput.focus();
      var newPos = pos + ref.length;
      valueInput.selectionStart = valueInput.selectionEnd = newPos;
      updatePreview();
    });
  });

  var refCheck = document.getElementById('add-var-ref-check');
  var refPicker = document.getElementById('add-var-ref-picker');

  if (refCheck && refPicker) {
    refCheck.addEventListener('change', function () {
      refPicker.style.display = this.checked ? '' : 'none';
      if (this.checked && secureCheckbox) {
        secureCheckbox.checked = false;
      }
    });

    _addVarResetHooks.push(function () {
      refCheck.checked = false;
      refPicker.style.display = 'none';
      if (previewEl) previewEl.style.display = 'none';
    });
  }

  var sharedCheck = document.getElementById('add-var-shared-check');
  var sharedPicker = document.getElementById('add-var-shared-picker');

  if (sharedCheck && sharedPicker) {
    sharedCheck.addEventListener('change', function () {
      sharedPicker.style.display = this.checked ? '' : 'none';
      if (this.checked && secureCheckbox) {
        secureCheckbox.checked = false;
      }
    });

    _addVarResetHooks.push(function () {
      sharedCheck.checked = false;
      sharedPicker.style.display = 'none';
    });
  }

  // Floating Add Variable button — appears when the original scrolls out of view
  var addVarBtn = document.getElementById('add-var-open');
  var addVarFab = document.getElementById('add-var-fab');
  if (addVarBtn && addVarFab && typeof IntersectionObserver !== 'undefined') {
    var configPanel = addVarBtn.closest('[data-tab-panel="config"]');
    var btnOutOfView = false;
    var observer = new IntersectionObserver(function (entries) {
      btnOutOfView = !entries[0].isIntersecting;
      var panelActive = configPanel && configPanel.classList.contains('tab-panel-active');
      addVarFab.style.display = (btnOutOfView && panelActive) ? '' : 'none';
    }, { threshold: 0 });
    observer.observe(addVarBtn);

    // Hide FAB when switching away from config tab
    document.querySelectorAll('[data-tab]').forEach(function (tab) {
      tab.addEventListener('click', function () {
        setTimeout(function () {
          var panelActive = configPanel && configPanel.classList.contains('tab-panel-active');
          addVarFab.style.display = (btnOutOfView && panelActive) ? '' : 'none';
        }, 0);
      });
    });
  }
}

/* Environment Add Variable Modal */
function initEnvAddVarModal() {
  var modal = document.getElementById('env-add-var-modal');
  if (!modal) return;

  function openModal() {
    modal.style.display = 'flex';
    var nameInput = modal.querySelector('input[name="name"]');
    if (nameInput) {
      nameInput.value = '';
      nameInput.focus();
    }
    var valueInput = modal.querySelector('input[name="value"]');
    if (valueInput) valueInput.value = '';
    var refCheck = document.getElementById('env-add-var-ref-check');
    var refPicker = document.getElementById('env-add-var-ref-picker');
    if (refCheck) refCheck.checked = false;
    if (refPicker) refPicker.style.display = 'none';
  }
  function closeModal() {
    modal.style.display = 'none';
  }

  document.querySelectorAll('#env-add-var-open, #env-add-var-open-empty, #env-add-var-fab').forEach(function (btn) {
    btn.addEventListener('click', openModal);
  });
  modal.querySelectorAll('[data-env-add-var-close]').forEach(function (el) {
    el.addEventListener('click', closeModal);
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && modal.style.display !== 'none') closeModal();
  });

  var nameField = modal.querySelector('input[name="name"]');
  if (nameField) {
    nameField.addEventListener('input', function () {
      var pos = this.selectionStart;
      this.value = this.value.toUpperCase().replace(/[^A-Z0-9_]/g, '_');
      this.selectionStart = this.selectionEnd = pos;
    });
  }

  var valueInput = document.getElementById('env-add-var-value');
  bindKeyValuePaste(nameField, valueInput);
  var refCheck = document.getElementById('env-add-var-ref-check');
  var refPicker = document.getElementById('env-add-var-ref-picker');
  var secureCheckbox = modal.querySelector('input[name="secure"]');

  if (refCheck && refPicker) {
    refCheck.addEventListener('change', function () {
      refPicker.style.display = this.checked ? '' : 'none';
      if (this.checked && secureCheckbox) {
        secureCheckbox.checked = false;
      }
    });

    modal.querySelectorAll('.env-add-var-ref-insert').forEach(function (btn) {
      btn.addEventListener('click', function () {
        if (!valueInput) return;
        var ref = btn.getAttribute('data-ref');
        var pos = valueInput.selectionStart || valueInput.value.length;
        var before = valueInput.value.slice(0, pos);
        var after = valueInput.value.slice(pos);
        valueInput.value = before + ref + after;
        valueInput.focus();
        var newPos = pos + ref.length;
        valueInput.selectionStart = valueInput.selectionEnd = newPos;
      });
    });
  }

  // Floating Add Variable button for the environment page
  var envAddVarBtn = document.getElementById('env-add-var-open');
  var envAddVarFab = document.getElementById('env-add-var-fab');
  if (envAddVarBtn && envAddVarFab && typeof IntersectionObserver !== 'undefined') {
    var envObserver = new IntersectionObserver(function (entries) {
      envAddVarFab.style.display = entries[0].isIntersecting ? 'none' : '';
    }, { threshold: 0 });
    envObserver.observe(envAddVarBtn);
  }
}

/* Environment Edit Variable Modal */
function initEnvEditVarModal() {
  var modal = document.getElementById('env-edit-var-modal');
  if (!modal) return;

  var form = document.getElementById('env-edit-var-form');
  var configIdField = document.getElementById('env-edit-config-id');
  var configNameField = document.getElementById('env-edit-config-name');
  var secureField = document.getElementById('env-edit-secure');
  var nameDisplay = document.getElementById('env-edit-name-display');
  var currentValueText = document.getElementById('env-edit-current-value-text');
  var currentValueSecret = document.getElementById('env-edit-current-value-secret');
  var valueInput = document.getElementById('env-edit-value');
  var buildtimeCheckbox = document.getElementById('env-edit-buildtime');
  var secretStatus = document.getElementById('env-edit-secret-status');

  function openModal(btn) {
    var configId = btn.getAttribute('data-config-id');
    var name = btn.getAttribute('data-config-name');
    var value = btn.getAttribute('data-config-value');
    var isSecret = btn.getAttribute('data-config-secret') === 'true';
    var isBuildtime = btn.getAttribute('data-config-buildtime') === 'true';
    var editUrl = btn.getAttribute('data-edit-url');

    form.action = editUrl;
    configIdField.value = configId;
    configNameField.value = name;
    secureField.value = isSecret ? 'y' : '';
    nameDisplay.textContent = name;

    if (isSecret) {
      currentValueText.style.display = 'none';
      currentValueSecret.style.display = '';
      secretStatus.innerHTML = '<svg class="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg> <span>Secret (encrypted)</span>';
    } else {
      currentValueText.textContent = value;
      currentValueText.style.display = '';
      currentValueSecret.style.display = 'none';
      secretStatus.innerHTML = '<svg class="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 5-5 5 5 0 0 1 3 1"/></svg> <span>Not secret</span>';
    }

    valueInput.value = isSecret ? '' : value;
    buildtimeCheckbox.checked = isBuildtime;
    modal.style.display = 'flex';
    valueInput.focus();
  }

  function closeModal() {
    modal.style.display = 'none';
  }

  document.querySelectorAll('.env-edit-btn').forEach(function (btn) {
    btn.addEventListener('click', function () { openModal(btn); });
  });

  modal.querySelectorAll('[data-env-edit-var-close]').forEach(function (el) {
    el.addEventListener('click', closeModal);
  });

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && modal.style.display !== 'none') closeModal();
  });
}

/* Application Edit Variable Modal */
function initAppEditVarModal() {
  var modal = document.getElementById('app-edit-var-modal');
  if (!modal) return;

  var form = document.getElementById('app-edit-var-form');
  var configNameField = document.getElementById('app-edit-config-name');
  var secureField = document.getElementById('app-edit-secure');
  var nameDisplay = document.getElementById('app-edit-name-display');
  var currentValueText = document.getElementById('app-edit-current-value-text');
  var currentValueSecret = document.getElementById('app-edit-current-value-secret');
  var valueInput = document.getElementById('app-edit-value');
  var buildtimeCheckbox = document.getElementById('app-edit-buildtime');
  var secretStatus = document.getElementById('app-edit-secret-status');

  function openModal(btn) {
    var name = btn.getAttribute('data-config-name');
    var value = btn.getAttribute('data-config-value');
    var isSecret = btn.getAttribute('data-config-secret') === 'true';
    var isBuildtime = btn.getAttribute('data-config-buildtime') === 'true';
    var editUrl = btn.getAttribute('data-edit-url');

    form.action = editUrl;
    configNameField.value = name;
    secureField.value = isSecret ? 'y' : '';
    nameDisplay.textContent = name;

    if (isSecret) {
      currentValueText.style.display = 'none';
      currentValueSecret.style.display = '';
      secretStatus.innerHTML = '<svg class="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg> <span>Secret (encrypted)</span>';
    } else {
      currentValueText.textContent = value;
      currentValueText.style.display = '';
      currentValueSecret.style.display = 'none';
      secretStatus.innerHTML = '<svg class="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 5-5 5 5 0 0 1 3 1"/></svg> <span>Not secret</span>';
    }

    valueInput.value = isSecret ? '' : value;
    buildtimeCheckbox.checked = isBuildtime;
    modal.style.display = 'flex';
    valueInput.focus();
  }

  function closeModal() {
    modal.style.display = 'none';
  }

  document.querySelectorAll('.app-config-edit-btn').forEach(function (btn) {
    btn.addEventListener('click', function () { openModal(btn); });
  });

  modal.querySelectorAll('[data-app-edit-var-close]').forEach(function (el) {
    el.addEventListener('click', closeModal);
  });

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && modal.style.display !== 'none') closeModal();
  });
}

/* Generic History Modal — fetches detail page and extracts content */
function initHistoryModal(btnClass, modalId, contentId, closeAttr) {
  var modal = document.getElementById(modalId);
  if (!modal) return;
  var contentEl = document.getElementById(contentId);

  function openModal(btn) {
    var url = btn.getAttribute('data-history-url');
    contentEl.innerHTML = '<span class="text-base-content/30">Loading…</span>';
    modal.style.display = 'flex';

    fetch(url, { headers: { 'X-Requested-With': 'XMLHttpRequest' } })
      .then(function (r) { return r.text(); })
      .then(function (html) {
        var parser = new DOMParser();
        var doc = parser.parseFromString(html, 'text/html');
        var cards = doc.querySelectorAll('.card');
        var out = '';
        cards.forEach(function (card) { out += card.outerHTML; });
        contentEl.innerHTML = out || '<p class="text-base-content/40">No history available.</p>';
      })
      .catch(function () {
        contentEl.innerHTML = '<p class="text-error">Failed to load history.</p>';
      });
  }

  function closeModal() { modal.style.display = 'none'; }

  document.querySelectorAll('.' + btnClass).forEach(function (btn) {
    btn.addEventListener('click', function () { openModal(btn); });
  });
  modal.querySelectorAll('[' + closeAttr + ']').forEach(function (el) {
    el.addEventListener('click', closeModal);
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && modal.style.display !== 'none') closeModal();
  });
}

/* Generic Delete Modal */
function initDeleteModal(btnClass, modalId, formId, nameDisplayId, configIdFieldId, configNameFieldId, configValueFieldId, confirmFieldId, closeAttr) {
  var modal = document.getElementById(modalId);
  if (!modal) return;
  var form = document.getElementById(formId);
  var nameDisplay = document.getElementById(nameDisplayId);
  var configIdField = document.getElementById(configIdFieldId);
  var configNameField = document.getElementById(configNameFieldId);
  var configValueField = document.getElementById(configValueFieldId);
  var confirmField = document.getElementById(confirmFieldId);

  function openModal(btn) {
    var configId = btn.getAttribute('data-config-id');
    var name = btn.getAttribute('data-config-name');
    var value = btn.getAttribute('data-config-value');
    var deleteUrl = btn.getAttribute('data-delete-url');

    form.action = deleteUrl;
    nameDisplay.textContent = name;
    configIdField.value = configId;
    configNameField.value = name;
    configValueField.value = value;
    confirmField.value = '';
    confirmField.placeholder = name;
    modal.style.display = 'flex';
    confirmField.focus();
  }

  function closeModal() { modal.style.display = 'none'; }

  document.querySelectorAll('.' + btnClass).forEach(function (btn) {
    btn.addEventListener('click', function () { openModal(btn); });
  });
  modal.querySelectorAll('[' + closeAttr + ']').forEach(function (el) {
    el.addEventListener('click', closeModal);
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && modal.style.display !== 'none') closeModal();
  });
}

/* Auto-growing textareas */
function initAutoGrowTextareas() {
  document.querySelectorAll('textarea.auto-grow').forEach(function (ta) {
    ta.style.overflow = 'hidden';
    ta.style.resize = 'none';
    function adjust() {
      ta.style.height = 'auto';
      ta.style.height = (ta.scrollHeight + parseInt(getComputedStyle(ta).lineHeight)) + 'px';
    }
    ta.addEventListener('input', adjust);
    adjust();
  });
}

/* Flash Messages — dismiss + auto-fade */
function initFlashMessages() {
  document.querySelectorAll('.flash-message').forEach(function (el) {
    function dismiss() {
      el.classList.add('flash-fade-out');
      setTimeout(function () { el.remove(); }, 400);
    }

    var btn = el.querySelector('.flash-dismiss');
    if (btn) btn.addEventListener('click', dismiss);

    if (el.getAttribute('data-flash-auto') !== 'false') {
      setTimeout(dismiss, 5000);
    }
  });
}

/* Environment Config Reference Insert (edit page) */
function initEnvConfigRefInsert() {
  var buttons = document.querySelectorAll('.env-config-ref-insert');
  if (!buttons.length) return;
  var valueInput = document.querySelector('input[name="value"]');
  if (!valueInput) return;

  buttons.forEach(function (btn) {
    btn.addEventListener('click', function () {
      var ref = btn.getAttribute('data-ref');
      var pos = valueInput.selectionStart || valueInput.value.length;
      var before = valueInput.value.slice(0, pos);
      var after = valueInput.value.slice(pos);
      valueInput.value = before + ref + after;
      valueInput.focus();
      var newPos = pos + ref.length;
      valueInput.selectionStart = valueInput.selectionEnd = newPos;
    });
  });
}

/* Expand Modal */
function initExpandModal() {
  var modal = document.getElementById('expand-modal');
  if (!modal) return;

  var titleEl = modal.querySelector('.expand-modal-title');
  var bodyEl = modal.querySelector('.expand-modal-body');
  var copyBtn = modal.querySelector('.expand-modal-copy');
  var closeBtn = modal.querySelector('.expand-modal-close');
  var backdrop = modal.querySelector('.raw-editor-backdrop');

  function openModal(title, content) {
    titleEl.textContent = title;
    bodyEl.innerHTML = content;
    modal.classList.remove('hidden');
    document.body.style.overflow = 'hidden';
  }

  function closeModal() {
    modal.classList.add('hidden');
    document.body.style.overflow = '';
  }

  if (closeBtn) closeBtn.addEventListener('click', closeModal);
  if (backdrop) backdrop.addEventListener('click', closeModal);

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && !modal.classList.contains('hidden')) {
      closeModal();
    }
  });

  if (copyBtn) {
    copyBtn.addEventListener('click', function () {
      var text = bodyEl.textContent;
      navigator.clipboard.writeText(text).then(function () {
        var orig = copyBtn.innerHTML;
        copyBtn.textContent = 'Copied!';
        setTimeout(function () {
          copyBtn.innerHTML = orig;
        }, 1500);
      });
    });
  }

  document.querySelectorAll('[data-expand]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var targetId = btn.getAttribute('data-expand');
      var target = document.getElementById(targetId);
      if (!target) return;
      var title = btn.getAttribute('data-expand-title') || 'Details';
      openModal(title, target.innerHTML);
    });
  });
}

/* Detail Log Height Sync */
function getColumnNaturalHeight(col) {
  var children = col.children;
  var gap = parseFloat(getComputedStyle(col).rowGap) || 16;
  var h = 0;
  for (var i = 0; i < children.length; i++) {
    h += children[i].offsetHeight;
  }
  h += gap * Math.max(0, children.length - 1);
  return h;
}

function autoExpandCollapsibleCards() {
  var left = document.querySelector('[data-log-left]');
  var logCol = document.getElementById('log-column');
  if (!left || !logCol) return;
  if (window.innerWidth < 1024) return;

  var cards = left.querySelectorAll('details[data-collapsible-card]');
  if (!cards.length) return;

  var logHeight = getColumnNaturalHeight(logCol);

  for (var i = 0; i < cards.length; i++) {
    if (getColumnNaturalHeight(left) >= logHeight) break;
    cards[i].open = true;
  }
}

function syncDetailLogHeight() {
  var left = document.querySelector('[data-log-left]');
  var logViewer = document.querySelector('[data-log-viewer]');
  if (!left || !logViewer) return;
  if (window.innerWidth < 1024) {
    logViewer.style.maxHeight = '';
    return;
  }
  var naturalH = getColumnNaturalHeight(left);
  var minH = window.innerHeight * 0.7;
  var cardPad = 32; /* card-body !p-4 top+bottom */
  var headerH = 48; /* log header row approx */
  var h = Math.max(naturalH, minH) - cardPad - headerH;
  logViewer.style.maxHeight = Math.max(h, 200) + 'px';
}

/* Build Progress Tracker */
function BuildProgressTracker(barFill, phaseLabel, type, stepsContainer, elapsedEl, serverStartTime) {
  this.barFill = barFill;
  this.phaseLabel = phaseLabel;
  this.stepsContainer = stepsContainer;
  this.elapsedEl = elapsedEl;
  this.type = type || 'build';
  this.progress = 0;
  this.maxStep = 0;
  this.totalSteps = 0;
  this.activated = false;
  this.currentStepIdx = -1;
  this.startTime = serverStartTime ? new Date(serverStartTime).getTime() : Date.now();
  this.timerInterval = null;
  this.errored = false;
  this.errorStepIdx = -1;
  this.linesReceived = 0;
  this.phaseStartTimes = {};
  this.phaseDurations = {};

  if (this.type === 'deploy') {
    this.steps = [
      { id: 'setup', label: 'Setup', patterns: [/Constructing API Clients/i], progress: 5 },
      { id: 'namespace', label: 'Namespace', patterns: [/Fetching Namespace/i], progress: 10 },
      {
        id: 'account',
        label: 'Account',
        patterns: [/Fetching ServiceAccount/i, /Patching ServiceAccount/i],
        progress: 20,
      },
      { id: 'enrollment', label: 'Enrollment', patterns: [/Fetching CabotageEnrollment/i], progress: 25 },
      { id: 'secrets', label: 'Secrets', patterns: [/Fetching ImagePullSecrets/i], progress: 32 },
      { id: 'release', label: 'Release', patterns: [/Running release command/i], progress: 45 },
      { id: 'deploy', label: 'Deploy', patterns: [/Creating deployment for/i, /Creating Service for/i], progress: 58 },
      { id: 'rollout', label: 'Rollout', patterns: [/Waiting on deployment to rollout/i], progress: 72 },
      { id: 'postdeploy', label: 'Post-deploy', patterns: [/Running postdeploy/i], progress: 88 },
      { id: 'complete', label: 'Done', patterns: [/Deployment .* complete/i], progress: 100 },
    ];
  } else {
    this.steps = [
      { id: 'resolve', label: 'Resolve', patterns: [/load build definition/i, /resolve image config/i, /load remote build context/i], progress: 5 },
      { id: 'build', label: 'Build', patterns: [/\[(?:\S+\s+)?\d+\/\d+\]/], progress: 40, substep: true },
      { id: 'export', label: 'Export', patterns: [/exporting to image/i], progress: 78 },
      { id: 'push', label: 'Push', patterns: [/pushing manifest/i, /pushing layers/i], progress: 92 },
      { id: 'complete', label: 'Done', patterns: [], progress: 100 },
    ];
  }

  this.renderSteps();
  this.startTimer();
}

BuildProgressTracker.prototype.renderSteps = function () {
  if (!this.stepsContainer) return;
  this.stepsContainer.innerHTML = '';
  var checkSvg =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';
  for (var i = 0; i < this.steps.length; i++) {
    var step = this.steps[i];
    var el = document.createElement('div');
    el.className = 'progress-step';
    el.setAttribute('data-step', step.id);
    el.innerHTML =
      '<div class="step-dot">' +
      checkSvg +
      '<div class="step-dot-spinner"></div></div>' +
      '<span class="step-label">' +
      step.label +
      '</span>' +
      '<span class="step-duration" data-step-duration></span>' +
      (step.substep ? '<div class="step-stages" data-stages></div>' : '');
    this.stepsContainer.appendChild(el);
  }
  this.stepEls = this.stepsContainer.querySelectorAll('.progress-step');
};

BuildProgressTracker.prototype.startTimer = function () {
  if (!this.elapsedEl) return;
  var self = this;
  this.timerInterval = setInterval(function () {
    var elapsed = Math.floor((Date.now() - self.startTime) / 1000);
    var min = Math.floor(elapsed / 60);
    var sec = elapsed % 60;
    self.elapsedEl.textContent = (min < 10 ? '0' : '') + min + ':' + (sec < 10 ? '0' : '') + sec;
  }, 1000);
};

BuildProgressTracker.prototype.stopTimer = function () {
  if (this.timerInterval) {
    clearInterval(this.timerInterval);
    this.timerInterval = null;
  }
};

BuildProgressTracker.prototype.setStep = function (idx) {
  if (idx <= this.currentStepIdx) return;
  var prevIdx = this.currentStepIdx;
  this.currentStepIdx = idx;
  if (prevIdx >= 0 && this.phaseStartTimes[prevIdx] && !this.phaseDurations[prevIdx]) {
    this.phaseDurations[prevIdx] = (Date.now() - this.phaseStartTimes[prevIdx]) / 1000;
    this.showStepDuration(prevIdx);
  }
  this.phaseStartTimes[idx] = Date.now();
  if (!this.stepEls) return;
  for (var i = 0; i < this.stepEls.length; i++) {
    this.stepEls[i].classList.remove('step-done', 'step-active');
    if (i < idx) {
      this.stepEls[i].classList.add('step-done');
    } else if (i === idx) {
      this.stepEls[i].classList.add('step-active');
    }
  }
};

BuildProgressTracker.prototype.formatStepDuration = function (seconds) {
  var s = Math.round(seconds);
  var m = Math.floor(s / 60);
  s = s % 60;
  return '(' + (m < 10 ? '0' : '') + m + ':' + (s < 10 ? '0' : '') + s + ')';
};

BuildProgressTracker.prototype.showStepDuration = function (idx) {
  if (!this.stepEls || !this.phaseDurations[idx]) return;
  var durEl = this.stepEls[idx].querySelector('[data-step-duration]');
  if (durEl) durEl.textContent = this.formatStepDuration(this.phaseDurations[idx]);
};

BuildProgressTracker.prototype.activate = function () {
  if (this.activated) return;
  this.activated = true;
  this.barFill.classList.add('build-progress-bar-determinate');
  this.barFill.style.width = '0%';
};

BuildProgressTracker.prototype.setProgress = function (pct) {
  if (pct <= this.progress) return;
  this.progress = pct;
  this.barFill.style.width = Math.min(pct, 100) + '%';
};

BuildProgressTracker.prototype.setPhase = function (text) {
  if (this.phaseLabel) {
    this.phaseLabel.textContent = text;
  }
};

BuildProgressTracker.prototype.processLine = function (line) {
  this.linesReceived++;
  if (this.type === 'deploy') {
    this.processDeployLine(line);
  } else {
    this.processBuildLine(line);
  }
};

BuildProgressTracker.prototype.processBuildLine = function (line) {
  if (/error|failed|failure|exception|traceback/i.test(line) && !/no error/i.test(line) && !/warning/i.test(line) && !/cache importer/i.test(line)) {
    this.errored = true;
    if (this.errorStepIdx < 0) this.errorStepIdx = Math.max(this.currentStepIdx, 0);
  }

  var stepMatch = line.match(/\[(?:(\S+)\s+)?(\d+)\/(\d+)\]/);
  if (stepMatch) {
    this.activate();
    var stageName = stepMatch[1] || null;
    var current = parseInt(stepMatch[2], 10);
    var total = parseInt(stepMatch[3], 10);
    if (!this.stages) this.stages = {};
    if (!this.stageOrder) this.stageOrder = [];
    var key = stageName || '_default';
    if (!this.stages[key]) {
      this.stages[key] = { maxStep: 0, totalSteps: 0, name: stageName };
      this.stageOrder.push(key);
    }
    var stage = this.stages[key];
    if (total > stage.totalSteps) stage.totalSteps = total;
    if (current > stage.maxStep) stage.maxStep = current;
    var completed = 0, grandTotal = 0;
    for (var i = 0; i < this.stageOrder.length; i++) {
      var s = this.stages[this.stageOrder[i]];
      completed += s.maxStep;
      grandTotal += s.totalSteps;
    }
    var pct = 5 + (completed / grandTotal) * 70;
    this.setProgress(pct);
    var phaseText = 'Building';
    if (this.stageOrder.length > 1) {
      var activeNames = [];
      for (var i = 0; i < this.stageOrder.length; i++) {
        var s = this.stages[this.stageOrder[i]];
        if (s.maxStep < s.totalSteps) activeNames.push(s.name || 'build');
      }
      phaseText = activeNames.length > 0
        ? 'Building ' + activeNames.join(', ')
        : 'Building (' + this.stageOrder.length + ' stages)';
    } else {
      phaseText = 'Building step ' + current + '/' + total;
      if (stageName) phaseText += ' (' + stageName + ')';
    }
    this.setPhase(phaseText);
    this.setStep(1);
    this.renderStageProgress();
    return;
  }

  if (/exporting to image/i.test(line)) {
    this.activate();
    this.setProgress(78);
    this.setPhase('Exporting image');
    this.setStep(2);
    return;
  }

  if (/pushing manifest/i.test(line) || /pushing layers/i.test(line)) {
    this.activate();
    this.setProgress(92);
    this.setPhase('Pushing image to registry');
    this.setStep(3);
    return;
  }

  if (/load build definition/i.test(line) || /resolve image config/i.test(line) || /load remote build context/i.test(line)) {
    this.activate();
    this.setProgress(2);
    this.setPhase('Resolving build definition');
    this.setStep(0);
    return;
  }
};

BuildProgressTracker.prototype.renderStageProgress = function () {
  var container = this.stepsContainer && this.stepsContainer.querySelector('[data-stages]');
  if (!container || !this.stages || !this.stageOrder) return;
  // Single unnamed stage: just show the count
  if (this.stageOrder.length === 1 && !this.stages[this.stageOrder[0]].name) {
    var s = this.stages[this.stageOrder[0]];
    container.innerHTML = '<span class="stage-count-solo">' + s.maxStep + '/' + s.totalSteps + '</span>';
    return;
  }
  var html = '';
  for (var i = 0; i < this.stageOrder.length; i++) {
    var s = this.stages[this.stageOrder[i]];
    if (s.maxStep >= s.totalSteps) continue;
    var pct = s.totalSteps > 0 ? Math.round((s.maxStep / s.totalSteps) * 100) : 0;
    var name = s.name || 'stage ' + (i + 1);
    html += '<div class="stage-row stage-active">' +
      '<span class="stage-name">' + name + '</span>' +
      '<span class="stage-bar"><span class="stage-bar-fill" style="width:' + pct + '%"></span></span>' +
      '<span class="stage-count">' + s.maxStep + '/' + s.totalSteps + '</span>' +
      '</div>';
  }
  container.innerHTML = html;
};

BuildProgressTracker.prototype.processDeployLine = function (line) {
  if (/error|failed|failure|exception|traceback|timed?\s*out/i.test(line) && !/no error/i.test(line)) {
    this.errored = true;
    if (this.errorStepIdx < 0) this.errorStepIdx = Math.max(this.currentStepIdx, 0);
  }

  for (var i = 0; i < this.steps.length; i++) {
    var step = this.steps[i];
    for (var j = 0; j < step.patterns.length; j++) {
      if (step.patterns[j].test(line)) {
        this.activate();
        this.setProgress(step.progress);
        this.setPhase(step.label === 'Done' ? 'Deployment complete' : step.label + '\u2026');
        this.setStep(i);
        return;
      }
    }
  }

  if (!this.activated && line.trim().length > 0) {
    this.activate();
    this.setProgress(2);
    this.setPhase('Starting deployment\u2026');
  }
};

BuildProgressTracker.prototype.complete = function () {
  this.stopTimer();

  if (this.linesReceived === 0) {
    this.setPhase('No logs available');
    return;
  }

  this.activate();

  if (
    this.currentStepIdx >= 0 &&
    this.phaseStartTimes[this.currentStepIdx] &&
    !this.phaseDurations[this.currentStepIdx]
  ) {
    this.phaseDurations[this.currentStepIdx] = (Date.now() - this.phaseStartTimes[this.currentStepIdx]) / 1000;
    this.showStepDuration(this.currentStepIdx);
  }

  if (this.errored) {
    var failedAt = this.errorStepIdx >= 0 ? this.errorStepIdx : this.currentStepIdx;
    this.setPhase('Failed');
    if (this.barFill) this.barFill.classList.add('deploy-progress-bar-error');
    if (this.stepEls) {
      for (var i = 0; i < this.stepEls.length; i++) {
        this.stepEls[i].classList.remove('step-active');
        if (i < failedAt) {
          this.stepEls[i].classList.add('step-done');
        } else if (i === failedAt) {
          this.stepEls[i].classList.add('step-error');
        }
      }
    }
    return;
  }

  this.setProgress(100);
  this.setPhase('Complete');
  this.setStep(this.steps.length - 1);
  if (this.stepEls) {
    for (var i = 0; i < this.stepEls.length; i++) {
      this.stepEls[i].classList.remove('step-active');
      this.stepEls[i].classList.add('step-done');
      this.showStepDuration(i);
    }
  }
};



/* Commit Popup */
var _commitPopup = null;
var _commitCache = {};

function initCommitPopup() {
  var commitEl = document.getElementById('liveCommitStatus');
  if (!commitEl) return;

  commitEl.classList.add('commit-popup-anchor');

  var hoverTimeout = null;
  var leaveTimeout = null;

  function showPopup() {
    clearTimeout(leaveTimeout);
    if (_commitPopup) return;
    hoverTimeout = setTimeout(function () {
      toggleCommitPopup(commitEl);
    }, 200);
  }

  function hidePopup() {
    clearTimeout(hoverTimeout);
    leaveTimeout = setTimeout(function () {
      closeCommitPopup();
    }, 300);
  }

  commitEl.addEventListener('mouseenter', showPopup);
  commitEl.addEventListener('mouseleave', hidePopup);

  commitEl.addEventListener('mouseover', function (e) {
    if (e.target.closest && e.target.closest('.commit-popup')) {
      clearTimeout(leaveTimeout);
    }
  });

  commitEl.addEventListener('click', function (e) {
    if (e.ctrlKey || e.metaKey) return;
    var link = e.target.closest('a.live-commit-sha');
    if (link) {
      e.preventDefault();
      e.stopPropagation();
      clearTimeout(hoverTimeout);
      if (!_commitPopup) toggleCommitPopup(commitEl);
    }
  });

  document.addEventListener('click', function (e) {
    if (_commitPopup && !commitEl.contains(e.target)) {
      closeCommitPopup();
    }
  });

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && _commitPopup) closeCommitPopup();
  });
}

function toggleCommitPopup(el) {
  if (_commitPopup) {
    closeCommitPopup();
    return;
  }

  var sha = el.getAttribute('data-commit-sha');
  var repo = el.getAttribute('data-github-repo');
  if (!sha) return;

  var deployTime = el.getAttribute('data-deploy-time') || '';
  var releaseVer = el.getAttribute('data-release-version') || '';
  var imageVer = el.getAttribute('data-image-version') || '';
  var ref = el.getAttribute('data-commit-ref') || '';
  var author = el.getAttribute('data-commit-author') || '';
  var releaseId = el.getAttribute('data-release-id') || '';
  var imageId = el.getAttribute('data-image-id') || '';
  var deploysUrl = el.getAttribute('data-deploys-url') || '';

  var html = '<div class="commit-popup-header">';
  html +=
    '<svg class="commit-popup-github-icon" viewBox="0 0 16 16" fill="currentColor"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"/></svg>';
  html += '<span class="commit-popup-header-text">Deployed via GitHub</span>';
  html += '</div>';

  html += '<div class="commit-popup-message commit-popup-loading">';
  html += '<span class="commit-popup-author-area" data-commit-author-area></span>';
  html += '<span data-commit-msg-text>Loading...</span>';
  html += '</div>';

  html += '<div class="commit-popup-meta">';
  if (repo) {
    var repoShort = repo.split('/').pop() || repo;
    html += '<div class="commit-popup-row"><span class="commit-popup-label">' + escapeHtml(repo) + '</span>';
    if (ref) {
      html +=
        '<span class="commit-popup-value commit-popup-ref"><svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="6" y1="3" x2="6" y2="15"/><circle cx="18" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M18 9a9 9 0 01-9 9"/></svg> ' +
        escapeHtml(ref) +
        '</span>';
    }
    html += '</div>';
  }
  if (imageVer || releaseVer) {
    html += '<div class="commit-popup-row">';
    if (imageVer) {
      if (imageId) {
        html += '<span class="commit-popup-label">Image <a href="/image/' + escapeHtml(imageId) + '" class="dpl-meta-chip-link" onclick="event.stopPropagation()"><code>#' + escapeHtml(imageVer) + '</code></a></span>';
      } else {
        html += '<span class="commit-popup-label">Image <code>#' + escapeHtml(imageVer) + '</code></span>';
      }
    }
    if (releaseVer) {
      if (releaseId) {
        html += '<span class="commit-popup-value">Package <a href="/release/' + escapeHtml(releaseId) + '" class="dpl-meta-chip-link" onclick="event.stopPropagation()"><code>v' + escapeHtml(releaseVer) + '</code></a></span>';
      } else {
        html += '<span class="commit-popup-value">Package <code>v' + escapeHtml(releaseVer) + '</code></span>';
      }
    }
    html += '</div>';
  }
  if (deployTime) {
    html +=
      '<div class="commit-popup-row"><span class="commit-popup-label">Deployed</span><span class="commit-popup-value">' +
      escapeHtml(deployTime) +
      '</span></div>';
  }
  html += '</div>';

  html += '<div class="commit-popup-sha">';
  html += '<code title="' + sha + '">' + sha + '</code>';
  html += '<button class="commit-popup-copy" title="Copy SHA" data-copy-sha="' + sha + '">';
  html +=
    '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>';
  html += '</button>';
  html += '</div>';

  html += '<div class="commit-popup-links">';
  if (repo) {
    html +=
      '<a href="https://github.com/' +
      repo +
      '/commit/' +
      sha +
      '" target="_blank" rel="noopener">View on GitHub &rarr;</a>';
  }
  if (deploysUrl) {
    html += '<a href="' + escapeHtml(deploysUrl) + '">View Pipeline &rarr;</a>';
  }
  html += '</div>';

  var popup = document.createElement('div');
  popup.className = 'commit-popup';
  popup.innerHTML = html;
  el.appendChild(popup);
  _commitPopup = popup;

  var copyBtn = popup.querySelector('[data-copy-sha]');
  if (copyBtn) {
    copyBtn.addEventListener('click', function (e) {
      e.stopPropagation();
      navigator.clipboard.writeText(sha).then(function () {
        copyBtn.innerHTML =
          '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="20 6 9 17 4 12"/></svg>';
        setTimeout(function () {
          copyBtn.innerHTML =
            '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>';
        }, 2000);
      });
    });
  }

  requestAnimationFrame(function () {
    popup.classList.add('commit-popup-open');
  });

  if (repo && sha) {
    fetchCommitMessage(repo, sha, popup.querySelector('.commit-popup-message'));
  }
}

function closeCommitPopup() {
  if (_commitPopup) {
    _commitPopup.classList.remove('commit-popup-open');
    var el = _commitPopup;
    setTimeout(function () {
      if (el.parentNode) el.parentNode.removeChild(el);
    }, 150);
    _commitPopup = null;
  }
}

function fetchCommitMessage(repo, sha, msgEl) {
  var cacheKey = repo + '/' + sha;
  if (_commitCache[cacheKey]) {
    renderCommitMessage(msgEl, _commitCache[cacheKey]);
    return;
  }

  fetch('https://api.github.com/repos/' + repo + '/commits/' + sha, {
    headers: { Accept: 'application/vnd.github.v3+json' },
  })
    .then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    })
    .then(function (data) {
      var info = {
        message: data.commit.message || '',
        author: data.commit.author.name || '',
        login: data.author ? data.author.login : '',
        avatar: data.author ? data.author.avatar_url : '',
        date: data.commit.author.date || '',
      };
      _commitCache[cacheKey] = info;
      renderCommitMessage(msgEl, info);
    })
    .catch(function () {
      if (msgEl) {
        var msgText = msgEl.querySelector('[data-commit-msg-text]');
        if (msgText) {
          msgText.textContent = 'Could not load commit message';
        } else {
          msgEl.textContent = 'Could not load commit message';
        }
      }
    });
}

function renderCommitMessage(el, info) {
  if (!el) return;
  el.classList.remove('commit-popup-loading');

  var authorArea = el.querySelector('[data-commit-author-area]');
  if (authorArea && (info.avatar || info.author)) {
    var avatarHtml = '';
    if (info.avatar) {
      avatarHtml += '<img src="' + info.avatar + '&s=32" class="commit-popup-avatar" alt="" />';
    }
    avatarHtml += '<span class="commit-popup-author-name">' + escapeHtml(info.login || info.author) + '</span>';
    if (info.date) {
      var d = new Date(info.date);
      var dateStr = d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
      avatarHtml += '<span class="commit-popup-author-date">' + dateStr + '</span>';
    }
    authorArea.innerHTML = avatarHtml;
  }

  var msgText = el.querySelector('[data-commit-msg-text]');
  var target = msgText || el;
  var lines = info.message.split('\n');
  var firstLine = lines[0] || '';
  var rest = lines.slice(1).join('\n').trim();
  var html = '<div class="commit-popup-first-line">' + escapeHtml(firstLine) + '</div>';
  if (rest) {
    html += '<div class="commit-popup-body">' + escapeHtml(rest) + '</div>';
  }
  target.innerHTML = html;
}

function escapeHtml(str) {
  var div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

/* Live Timestamp Ticker */

function timeago(date) {
  var now = Date.now();
  var diff = Math.max(0, Math.floor((now - date.getTime()) / 1000));
  if (diff < 2) return 'just now';
  if (diff < 60) return diff + ' seconds ago';
  var m = Math.floor(diff / 60);
  if (m === 1) return 'a minute ago';
  if (m < 60) return m + ' minutes ago';
  var h = Math.floor(m / 60);
  if (h === 1) return 'an hour ago';
  if (h < 24) return h + ' hours ago';
  var d = Math.floor(h / 24);
  if (d === 1) return 'a day ago';
  return d + ' days ago';
}

var _timestampTickerInterval = null;

function tickTimestamps() {
  var els = document.querySelectorAll('time[data-timestamp]');
  for (var i = 0; i < els.length; i++) {
    var iso = els[i].getAttribute('data-timestamp');
    if (!iso) continue;
    var d = new Date(iso);
    if (isNaN(d.getTime())) continue;
    els[i].textContent = timeago(d);
  }
}

function startTimestampTicker() {
  if (_timestampTickerInterval) return;
  tickTimestamps(); // immediate tick
  _timestampTickerInterval = setInterval(tickTimestamps, 1000);
}

function stopTimestampTicker() {
  if (_timestampTickerInterval) {
    clearInterval(_timestampTickerInterval);
    _timestampTickerInterval = null;
  }
}

function isLowDataMode() {
  return localStorage.getItem('low-data-mode') === 'true';
}


function initTimestampsAndDeployForm() {
  if (document.querySelector('time[data-timestamp]')) {
    startTimestampTicker();
  }

  var deployForm = document.querySelector('[data-full-deploy-form]');
  if (deployForm) {
    deployForm.addEventListener('submit', function () {
      var btn = deployForm.querySelector('button[type="submit"]');
      if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<span class="loading loading-spinner loading-xs"></span> Deploying\u2026';
      }
    });
  }
}

/* Structured Live Log (build/release/deploy) */
var STRUCT_LOG_ERROR_RE = /error|failed|failure|exception|traceback|timed?\s*out/i;
var STRUCT_LOG_ERROR_EXCLUDE_RE = /no error|warning|cache importer/i;
var STRUCT_LOG_VERTEX_RE = /^#(\d+)\s(.*)$/;
var STRUCT_LOG_CACHED_RE = /^CACHED\s*$/i;
var STRUCT_LOG_DONE_RE = /^DONE(?:\s+([\d.]+)s)?\s*$/i;
var STRUCT_LOG_VERTEX_ERROR_RE = /^ERROR:?\s*(.*)$/i;
var STRUCT_LOG_BRACKET_RE = /\[(?:\S+\s+)?(\d+)\/(\d+)\]/;
var STRUCT_LOG_PHASE_START_PATTERNS = [
  /^Constructing API Clients$/i,
  /^Fetching Namespace$/i,
  /^Fetching Cabotage CA Cert ConfigMap$/i,
  /^Fetching ServiceAccount$/i,
  /^Fetching CabotageEnrollment$/i,
  /^Fetching (?:web|tcp) Service\(s\)$/i,
  /^Fetching \S+ Service$/i,
  /^Fetching ImagePullSecrets$/i,
  /^Patching ServiceAccount with ImagePullSecrets/i,
  /^Running release command /i,
  /^Creating deployment for /i,
  /^Creating CronJob for /i,
  /^Waiting on deployment to rollout/i,
  /^Running postdeploy command /i,
  /^Waiting for TLS certificate/i,
  /^Deployment \S+ complete$/i
];
var STRUCT_LOG_PHASE_TERMINAL_RE = /^Deployment \S+ complete$/i;
var STRUCT_LOG_PHASE_FINISH_RE = /complete!\s*$/i;
var STRUCT_LOG_CHEVRON_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6" /></svg>';

function structLogLooksLikeError(trimmed) {
  return STRUCT_LOG_ERROR_RE.test(trimmed) && !STRUCT_LOG_ERROR_EXCLUDE_RE.test(trimmed);
}
function structLogFmtLive(seconds) {
  seconds = Math.floor(seconds);
  if (seconds < 60) return seconds + 's';
  var m = Math.floor(seconds / 60), s = seconds % 60;
  return m + ':' + (s < 10 ? '0' : '') + s;
}
function structLogFmtFinal(seconds) {
  if (seconds < 60) return (Math.round(seconds * 10) / 10) + 's';
  var m = Math.floor(seconds / 60), s = Math.round(seconds % 60);
  return m + ':' + (s < 10 ? '0' : '') + s;
}
function structLogAttachTailTracking(scrollEl, chipEl) {
  var state = { follow: true };
  scrollEl.addEventListener('scroll', function () {
    var atBottom = scrollEl.scrollHeight - scrollEl.scrollTop - scrollEl.clientHeight < 32;
    state.follow = atBottom;
    chipEl.hidden = atBottom;
  });
  chipEl.addEventListener('click', function () {
    state.follow = true;
    chipEl.hidden = true;
    scrollEl.scrollTop = scrollEl.scrollHeight;
  });
  return state;
}

function StructuredLogViewer(container, kind) {
  this.container = container;
  this.kind = kind; /* 'buildkit' | 'phases' */
  this.allLines = [];
  this.steps = [];
  this.vertexIndex = {};
  this.currentPhaseStep = null;
  this.leadingFinalized = false;
  this.hasError = false;
  this.firstErrorLineEl = null;
  this.maxBracketTotal = 0;
  this.bracketCurrent = 0;
  this.scrollScheduled = false;
  this.rawMode = false;
  container.classList.add('struct-log-active');
  container.innerHTML = '';
  this.buildDom();
}

StructuredLogViewer.prototype.buildDom = function () {
  var self = this;
  var root = document.createElement('div');
  root.className = 'struct-log';

  var header = document.createElement('div');
  header.className = 'struct-log-header';

  var stepCount = document.createElement('span');
  stepCount.className = 'struct-log-stepcount';
  stepCount.hidden = true;

  var jumpError = document.createElement('button');
  jumpError.type = 'button';
  jumpError.className = 'struct-log-btn struct-log-jump-error';
  jumpError.textContent = 'Jump to error';
  jumpError.hidden = true;

  var spacer = document.createElement('span');
  spacer.className = 'struct-log-spacer';

  var rawToggle = document.createElement('button');
  rawToggle.type = 'button';
  rawToggle.className = 'struct-log-btn';
  rawToggle.setAttribute('aria-pressed', 'false');
  rawToggle.textContent = 'Raw';

  header.appendChild(stepCount);
  header.appendChild(jumpError);
  header.appendChild(spacer);
  header.appendChild(rawToggle);

  var body = document.createElement('div');
  body.className = 'struct-log-body';

  var waiting = document.createElement('div');
  waiting.className = 'struct-log-waiting';
  waiting.textContent = 'Waiting for output\u2026';
  body.appendChild(waiting);

  var leadingGroup = document.createElement('div');
  leadingGroup.className = 'log-output-group';
  leadingGroup.hidden = true;
  leadingGroup.innerHTML = '<div class="log-output-label">Output</div><div class="log-output-lines"></div>';
  body.appendChild(leadingGroup);

  var trailingGroup = document.createElement('div');
  trailingGroup.className = 'log-output-group';
  trailingGroup.hidden = true;
  trailingGroup.innerHTML = '<div class="log-output-label">Output</div><div class="log-output-lines"></div>';
  body.appendChild(trailingGroup);

  var bodyChip = document.createElement('button');
  bodyChip.type = 'button';
  bodyChip.className = 'struct-log-latest-chip';
  bodyChip.textContent = 'Jump to latest';
  bodyChip.hidden = true;
  body.appendChild(bodyChip);

  var rawText = document.createElement('pre');
  rawText.className = 'struct-log-raw-text';

  var rawChip = document.createElement('button');
  rawChip.type = 'button';
  rawChip.className = 'struct-log-latest-chip';
  rawChip.textContent = 'Jump to latest';
  rawChip.hidden = true;

  var raw = document.createElement('div');
  raw.className = 'struct-log-raw';
  raw.hidden = true;
  raw.appendChild(rawText);
  raw.appendChild(rawChip);

  root.appendChild(header);
  root.appendChild(body);
  root.appendChild(raw);
  this.container.appendChild(root);

  this.stepCountEl = stepCount;
  this.jumpErrorEl = jumpError;
  this.rawToggleEl = rawToggle;
  this.bodyEl = body;
  this.waitingEl = waiting;
  this.leadingGroupEl = leadingGroup;
  this.trailingGroupEl = trailingGroup;
  this.rawEl = raw;
  this.rawTextEl = rawText;

  this.bodyFollow = structLogAttachTailTracking(body, bodyChip);
  this.rawFollow = structLogAttachTailTracking(raw, rawChip);

  jumpError.addEventListener('click', function () { self.jumpToError(); });
  rawToggle.addEventListener('click', function () {
    self.rawMode = !self.rawMode;
    rawToggle.setAttribute('aria-pressed', self.rawMode ? 'true' : 'false');
    rawToggle.textContent = self.rawMode ? 'Structured' : 'Raw';
    body.hidden = self.rawMode;
    raw.hidden = !self.rawMode;
    if (self.rawMode) raw.scrollTop = raw.scrollHeight;
  });
};

StructuredLogViewer.prototype.scheduleScroll = function () {
  if (this.scrollScheduled) return;
  this.scrollScheduled = true;
  var self = this;
  requestAnimationFrame(function () {
    self.scrollScheduled = false;
    if (self.bodyFollow.follow) self.bodyEl.scrollTop = self.bodyEl.scrollHeight;
    if (self.rawFollow.follow) self.rawEl.scrollTop = self.rawEl.scrollHeight;
  });
};

StructuredLogViewer.prototype.processLine = function (rawLine) {
  this.waitingEl.hidden = true;
  this.allLines.push(rawLine);
  this.rawTextEl.appendChild(document.createTextNode(rawLine + '\n'));
  var trimmed = rawLine.replace(/^\s+/, '');
  if (/^Job Pod /.test(trimmed)) {
    this.pushOutputLine(rawLine, false);
    this.scheduleScroll();
    return;
  }
  var isError = structLogLooksLikeError(trimmed);
  if (this.kind === 'buildkit') {
    this.processBuildkitLine(trimmed, rawLine, isError);
  } else {
    this.processPhaseLine(trimmed, rawLine, isError);
  }
  this.scheduleScroll();
};

StructuredLogViewer.prototype.processBuildkitLine = function (trimmed, rawLine, isError) {
  var m = trimmed.match(STRUCT_LOG_VERTEX_RE);
  if (!m) {
    this.pushOutputLine(rawLine, isError);
    return;
  }
  var vid = m[1];
  var rest = m[2];
  var step = this.vertexIndex[vid];
  if (!step) {
    step = this.createStep('bk-' + vid, rest);
    this.vertexIndex[vid] = step;
    var br = rest.match(STRUCT_LOG_BRACKET_RE);
    if (br) {
      var cur = parseInt(br[1], 10), tot = parseInt(br[2], 10);
      if (tot > this.maxBracketTotal) this.maxBracketTotal = tot;
      if (cur > this.bracketCurrent) this.bracketCurrent = cur;
      this.updateHeader();
    }
    return;
  }
  var cachedM = STRUCT_LOG_CACHED_RE.test(rest);
  var doneM = rest.match(STRUCT_LOG_DONE_RE);
  var errM = rest.match(STRUCT_LOG_VERTEX_ERROR_RE);
  if (cachedM) {
    this.closeStep(step, 'cached', null);
  } else if (doneM) {
    this.closeStep(step, 'done', doneM[1] ? parseFloat(doneM[1]) : null);
  } else if (errM) {
    this.addStepLine(step, rawLine, true);
    this.flagStepError(step);
    this.closeStep(step, 'failed', null);
  } else {
    this.addStepLine(step, rawLine, isError);
  }
};

StructuredLogViewer.prototype.processPhaseLine = function (trimmed, rawLine, isError) {
  if (STRUCT_LOG_PHASE_FINISH_RE.test(trimmed) && !STRUCT_LOG_PHASE_TERMINAL_RE.test(trimmed)) {
    if (this.currentPhaseStep) {
      this.addStepLine(this.currentPhaseStep, rawLine, isError);
      this.closeStep(this.currentPhaseStep, isError ? 'failed' : 'done', null);
      this.currentPhaseStep = null;
    } else {
      this.pushOutputLine(rawLine, isError);
    }
    return;
  }
  for (var i = 0; i < STRUCT_LOG_PHASE_START_PATTERNS.length; i++) {
    if (STRUCT_LOG_PHASE_START_PATTERNS[i].test(trimmed)) {
      if (this.currentPhaseStep) this.closeStep(this.currentPhaseStep, 'done', null);
      var step = this.createStep('ph-' + this.steps.length, trimmed);
      this.currentPhaseStep = step;
      if (STRUCT_LOG_PHASE_TERMINAL_RE.test(trimmed)) {
        this.closeStep(step, 'done', 0);
        this.currentPhaseStep = null;
      }
      return;
    }
  }
  if (this.currentPhaseStep) {
    this.addStepLine(this.currentPhaseStep, rawLine, isError);
    if (isError) this.flagStepError(this.currentPhaseStep);
  } else {
    this.pushOutputLine(rawLine, isError);
  }
};

StructuredLogViewer.prototype.pushOutputLine = function (rawLine, isError) {
  var group = this.leadingFinalized ? this.trailingGroupEl : this.leadingGroupEl;
  group.hidden = false;
  var linesEl = group.querySelector('.log-output-lines');
  var lineEl = document.createElement('div');
  lineEl.className = 'log-step-line';
  lineEl.textContent = rawLine.replace(/^\s+/, '');
  if (isError) this.markErrorLine(lineEl);
  linesEl.appendChild(lineEl);
};

StructuredLogViewer.prototype.markErrorLine = function (lineEl) {
  lineEl.classList.add('log-line-error');
  lineEl.tabIndex = -1;
  if (!this.firstErrorLineEl) {
    this.firstErrorLineEl = lineEl;
    this.hasError = true;
    this.updateHeader();
  }
};

StructuredLogViewer.prototype.createStep = function (key, label) {
  if (!this.leadingFinalized) this.leadingFinalized = true;
  var step = {
    key: key,
    label: label,
    status: 'running',
    startTime: Date.now(),
    duration: null,
    autoManaged: true,
    lines: []
  };
  this.steps.push(step);
  this.renderStep(step);
  this.startStepTimer(step);
  this.updateHeader();
  return step;
};

StructuredLogViewer.prototype.renderStep = function (step) {
  var self = this;
  var row = document.createElement('div');
  row.className = 'log-step log-step-running';

  var toggle = document.createElement('button');
  toggle.type = 'button';
  toggle.className = 'log-step-toggle';
  toggle.setAttribute('aria-expanded', 'true');

  var chevron = document.createElement('span');
  chevron.className = 'log-step-chevron';
  chevron.innerHTML = STRUCT_LOG_CHEVRON_SVG;

  var dot = document.createElement('span');
  dot.className = 'log-step-dot';

  var label = document.createElement('span');
  label.className = 'log-step-label';
  label.textContent = step.label;
  label.title = step.label;

  var duration = document.createElement('span');
  duration.className = 'log-step-duration';

  toggle.appendChild(chevron);
  toggle.appendChild(dot);
  toggle.appendChild(label);
  toggle.appendChild(duration);

  var lines = document.createElement('div');
  lines.className = 'log-step-lines';

  row.appendChild(toggle);
  row.appendChild(lines);

  toggle.addEventListener('click', function () {
    var expanded = toggle.getAttribute('aria-expanded') === 'true';
    self.setStepExpanded(step, !expanded, true);
  });

  this.bodyEl.insertBefore(row, this.trailingGroupEl);
  step.el = row;
  step.toggleEl = toggle;
  step.durationEl = duration;
  step.linesEl = lines;
};

StructuredLogViewer.prototype.setStepExpanded = function (step, expanded, isManual) {
  if (isManual) step.autoManaged = false;
  step.linesEl.hidden = !expanded;
  step.toggleEl.setAttribute('aria-expanded', expanded ? 'true' : 'false');
  step.el.classList.toggle('log-step-collapsed', !expanded);
};

StructuredLogViewer.prototype.addStepLine = function (step, rawLine, isError) {
  step.lines.push(rawLine);
  var lineEl = document.createElement('div');
  lineEl.className = 'log-step-line';
  lineEl.textContent = rawLine.replace(/^\s+/, '');
  if (isError) this.markErrorLine(lineEl);
  step.linesEl.appendChild(lineEl);
};

StructuredLogViewer.prototype.flagStepError = function (step) {
  if (step.status === 'failed') return;
  step.status = 'failed';
  this.hasError = true;
  this.updateStepVisual(step);
  this.setStepExpanded(step, true, false);
  this.updateHeader();
};

StructuredLogViewer.prototype.closeStep = function (step, statusIfNotFailed, explicitDuration) {
  if (step.status !== 'failed') step.status = statusIfNotFailed;
  step.endTime = Date.now();
  step.duration = (explicitDuration != null) ? explicitDuration : (step.endTime - step.startTime) / 1000;
  this.stopStepTimer(step);
  this.updateStepVisual(step);
  if (step.status !== 'failed' && step.autoManaged) this.setStepExpanded(step, false, false);
  this.updateHeader();
};

StructuredLogViewer.prototype.updateStepVisual = function (step) {
  var collapsed = step.el.classList.contains('log-step-collapsed');
  step.el.className = 'log-step log-step-' + step.status + (collapsed ? ' log-step-collapsed' : '');
  if (step.status === 'cached') {
    step.durationEl.textContent = 'cached';
  } else if (step.status === 'running') {
    /* left to the live timer */
  } else {
    step.durationEl.textContent = structLogFmtFinal(step.duration);
  }
};

StructuredLogViewer.prototype.startStepTimer = function (step) {
  var self = this;
  step.timerId = setInterval(function () {
    step.durationEl.textContent = structLogFmtLive((Date.now() - step.startTime) / 1000);
  }, 1000);
  step.durationEl.textContent = '0s';
};
StructuredLogViewer.prototype.stopStepTimer = function (step) {
  if (step.timerId) { clearInterval(step.timerId); step.timerId = null; }
};

StructuredLogViewer.prototype.updateHeader = function () {
  if (this.kind === 'buildkit' && this.maxBracketTotal > 0) {
    this.stepCountEl.hidden = false;
    this.stepCountEl.textContent = 'Step ' + Math.min(this.bracketCurrent, this.maxBracketTotal) + ' of ' + this.maxBracketTotal;
  } else {
    this.stepCountEl.hidden = true;
  }
  this.jumpErrorEl.hidden = !this.hasError;
};

StructuredLogViewer.prototype.jumpToError = function () {
  if (!this.firstErrorLineEl) return;
  var stepRow = this.firstErrorLineEl.closest ? this.firstErrorLineEl.closest('.log-step') : null;
  if (stepRow) {
    for (var i = 0; i < this.steps.length; i++) {
      if (this.steps[i].el === stepRow) { this.setStepExpanded(this.steps[i], true, false); break; }
    }
  }
  if (this.rawMode) {
    this.rawMode = false;
    this.rawToggleEl.setAttribute('aria-pressed', 'false');
    this.rawToggleEl.textContent = 'Raw';
    this.bodyEl.hidden = false;
    this.rawEl.hidden = true;
  }
  var el = this.firstErrorLineEl;
  el.scrollIntoView({ block: 'center' });
  el.focus();
  el.classList.add('log-line-flash');
  setTimeout(function () { el.classList.remove('log-line-flash'); }, 1200);
};

StructuredLogViewer.prototype.complete = function (errored) {
  for (var i = 0; i < this.steps.length; i++) {
    var step = this.steps[i];
    if (step.status === 'running') this.closeStep(step, errored ? 'failed' : 'done', null);
  }
  this.currentPhaseStep = null;
  if (errored) { this.hasError = true; this.updateHeader(); }
  this.waitingEl.hidden = true;
};

/* Build/Release Detail Page */
function initBuildDetailPage(opts) {
  var nextStepUrl = opts.nextStepUrl;
  var nextStepBanner = document.getElementById('nextStepBanner');
  if (nextStepUrl && nextStepBanner) {
    nextStepBanner.querySelector('a').href = nextStepUrl;
    nextStepBanner.hidden = false;
  }
  var logsPre = document.getElementById(opts.logElementId);
  if (!logsPre) return;
  var structLog = new StructuredLogViewer(logsPre, 'buildkit');
  var barFill = document.getElementById('buildProgressFill');
  var phaseLabel = document.getElementById('buildPhase');
  var stepsEl = document.getElementById('buildSteps');
  var elapsedEl = document.getElementById('buildElapsed');
  var tracker = barFill ? new BuildProgressTracker(barFill, phaseLabel, 'build', stepsEl, elapsedEl) : null;
  var logsFinished = false;
  var linesReceived = 0;
  var emptyEndAttempts = 0;
  var maxEmptyEndAttempts = 20;
  var progressBanner = document.querySelector('.build-progress-banner');
  var pendingLines = [];
  var flushScheduled = false;
  function flushPendingLines() {
    if (pendingLines.length === 0) return;
    for (var i = 0; i < pendingLines.length; i++) structLog.processLine(pendingLines[i]);
    pendingLines = [];
    flushScheduled = false;
  }
  var protocol = (window.location.protocol === 'https:') ? 'wss://' : 'ws://';
  var wsUrl = opts.wsUrl || (window.location.pathname + '/livelogs');
  function connectWebSocket() {
    var socket = new WebSocket(protocol + window.location.host + wsUrl);
    socket.addEventListener('message', function(ev) {
      if (ev.data === '=================END OF LOGS=================') {
        flushPendingLines();
        socket.close();
        if (linesReceived === 0 && emptyEndAttempts < maxEmptyEndAttempts) {
          emptyEndAttempts++;
          if (tracker) tracker.setPhase('Waiting for pod to start\u2026');
          setTimeout(connectWebSocket, 3000);
          return;
        }
        logsFinished = true;
        if (tracker) {
          tracker.complete();
          if (tracker.errored && progressBanner) progressBanner.classList.add('deploy-progress-banner-error');
        }
        structLog.complete(tracker ? tracker.errored : false);
        return;
      }
      linesReceived++;
      if (tracker) tracker.processLine(ev.data);
      pendingLines.push(ev.data);
      if (!flushScheduled) {
        flushScheduled = true;
        requestAnimationFrame(flushPendingLines);
      }
    });
    socket.addEventListener('close', function() {
      if (logsFinished) {
        fetchBuildStatus();
      } else if (linesReceived > 0) {
        if (tracker) tracker.setPhase('Waiting for build logs\u2026');
        setTimeout(function() { window.location.reload(); }, 10000);
      }
    });
  }
  var statusPollAttempts = 0;
  var maxStatusPolls = 30;
  function fetchBuildStatus() {
    statusPollAttempts++;
    fetch(window.location.pathname + '?_t=' + Date.now(), {
      credentials: 'same-origin',
      headers: { 'Accept': 'text/html', 'Cache-Control': 'no-cache' }
    })
      .then(function(r) { return r.text(); })
      .then(function(html) {
        var doc = new DOMParser().parseFromString(html, 'text/html');
        var newOrb = doc.querySelector('.build-status-orb');
        if (newOrb && newOrb.classList.contains('build-status-building') && statusPollAttempts < maxStatusPolls) {
          setTimeout(fetchBuildStatus, 2000);
          return;
        }
        var newHeader = newOrb ? newOrb.closest('.mb-6') : null;
        var curHeader = document.querySelector('.build-status-orb');
        curHeader = curHeader ? curHeader.closest('.mb-6') : null;
        if (newHeader && curHeader) curHeader.innerHTML = newHeader.innerHTML;
        var newInfo = doc.querySelector('[data-log-left]');
        var curInfo = document.querySelector('[data-log-left]');
        if (newInfo && curInfo) curInfo.innerHTML = newInfo.innerHTML;
        var newError = doc.querySelector('.build-error-banner');
        if (newError && !document.querySelector('.build-error-banner')) {
          if (curHeader) curHeader.insertAdjacentElement('afterend', newError);
        }
        if (progressBanner) progressBanner.style.display = 'none';
        // TODO(#180): restore pollForNextStep when pipeline_status endpoint exists
      })
      .catch(function() {
        if (statusPollAttempts < maxStatusPolls) setTimeout(fetchBuildStatus, 2000);
      });
  }
  connectWebSocket();
}
function copyBuildLog() {
  var raw = document.querySelector('#buildLog .struct-log-raw-text');
  var text = raw ? raw.textContent : document.getElementById('buildLog').textContent;
  navigator.clipboard.writeText(text).then(function() {
    var btn = event.currentTarget;
    var orig = btn.innerHTML;
    btn.innerHTML = '<svg class="w-3 h-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="20 6 9 17 4 12" /></svg> Copied';
    setTimeout(function() { btn.innerHTML = orig; }, 2000);
  });
}

/* Deploy Detail Page */
function initDeployDetailPage(opts) {
  var logsPre = document.getElementById(opts.logElementId);
  if (!logsPre) return;
  var structLog = new StructuredLogViewer(logsPre, 'phases');
  var barFill = document.getElementById('deployProgressFill');
  var phaseLabel = document.getElementById('deployPhase');
  var stepsEl = document.getElementById('deploySteps');
  var elapsedEl = document.getElementById('deployElapsed');
  var tracker = barFill ? new BuildProgressTracker(barFill, phaseLabel, 'deploy', stepsEl, elapsedEl, opts.startTime) : null;
  var progressBanner = document.querySelector('.deploy-progress-banner');
  var logsFinished = false;
  var linesReceived = 0;
  var reconnectAttempts = 0;
  var maxReconnectAttempts = 30;
  var emptyEndAttempts = 0;
  var maxEmptyEndAttempts = 20;
  var pendingLines = [];
  var flushScheduled = false;
  function flushPendingLines() {
    if (pendingLines.length === 0) return;
    for (var i = 0; i < pendingLines.length; i++) structLog.processLine(pendingLines[i]);
    pendingLines = [];
    flushScheduled = false;
  }
  var protocol = (window.location.protocol === 'https:') ? 'wss://' : 'ws://';
  var wsUrl = protocol + window.location.host + window.location.pathname + '/livelogs';
  function connectWebSocket() {
    var socket = new WebSocket(wsUrl);
    socket.addEventListener('message', function(ev) {
      if (ev.data === '=================END OF LOGS=================') {
        flushPendingLines();
        socket.close();
        if (linesReceived === 0 && emptyEndAttempts < maxEmptyEndAttempts) {
          emptyEndAttempts++;
          if (tracker) tracker.setPhase('Waiting for pod to start\u2026');
          setTimeout(connectWebSocket, 3000);
          return;
        }
        logsFinished = true;
        reconnectAttempts = 0;
        if (tracker) {
          tracker.complete();
          if (tracker.errored && progressBanner) progressBanner.classList.add('deploy-progress-banner-error');
        }
        structLog.complete(tracker ? tracker.errored : false);
        fetchDeploymentStatus();
        return;
      }
      reconnectAttempts = 0;
      linesReceived++;
      if (tracker) tracker.processLine(ev.data);
      pendingLines.push(ev.data);
      if (!flushScheduled) {
        flushScheduled = true;
        requestAnimationFrame(flushPendingLines);
      }
    });
    socket.addEventListener('close', function() {
      if (logsFinished) return;
      reconnectAttempts++;
      if (reconnectAttempts >= maxReconnectAttempts) {
        if (tracker) tracker.setPhase('Checking status\u2026');
        fetchDeploymentStatus();
        return;
      }
      if (tracker) tracker.setPhase('Reconnecting\u2026');
      setTimeout(connectWebSocket, Math.min(2000, 500 * reconnectAttempts));
    });
  }
  var statusPollAttempts = 0;
  var maxStatusPolls = 30;
  function fetchDeploymentStatus() {
    statusPollAttempts++;
    fetch(window.location.pathname + '?_t=' + Date.now(), {
      credentials: 'same-origin',
      headers: { 'Accept': 'text/html', 'Cache-Control': 'no-cache' }
    })
      .then(function(r) { return r.text(); })
      .then(function(html) {
        var doc = new DOMParser().parseFromString(html, 'text/html');
        var newOrb = doc.querySelector('.deploy-status-orb');
        if (newOrb && newOrb.classList.contains('deploy-status-deploying') && statusPollAttempts < maxStatusPolls) {
          setTimeout(fetchDeploymentStatus, 2000);
          return;
        }
        var newHeader = newOrb ? newOrb.closest('.mb-6') : null;
        var curHeader = document.querySelector('.deploy-status-orb');
        curHeader = curHeader ? curHeader.closest('.mb-6') : null;
        if (newHeader && curHeader) curHeader.innerHTML = newHeader.innerHTML;
        var newInfo = doc.querySelector('[data-log-left]');
        var curInfo = document.querySelector('[data-log-left]');
        if (newInfo && curInfo) curInfo.innerHTML = newInfo.innerHTML;
        var newError = doc.querySelector('.deploy-error-banner');
        if (newError && !document.querySelector('.deploy-error-banner')) {
          if (curHeader) curHeader.insertAdjacentElement('afterend', newError);
        }
        if (progressBanner) progressBanner.style.display = 'none';
      })
      .catch(function() {
        if (statusPollAttempts < maxStatusPolls) setTimeout(fetchDeploymentStatus, 2000);
      });
  }
  connectWebSocket();
}
function copyDeployLog() {
  var raw = document.querySelector('#deployLog .struct-log-raw-text');
  var text = raw ? raw.textContent : document.getElementById('deployLog').textContent;
  navigator.clipboard.writeText(text).then(function() {
    var btn = event.currentTarget;
    var orig = btn.innerHTML;
    btn.innerHTML = '<svg class="w-3 h-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="20 6 9 17 4 12" /></svg> Copied';
    setTimeout(function() { btn.innerHTML = orig; }, 2000);
  });
}

/* App Logs Page */
function initAppLogs() {
  var logsPre = document.getElementById('appLogs');
  if (!logsPre) return;
  var autoScroll = true;
  var paused = false;
  var filterKubeProbe = true;
  var backLog = [];
  function appendLog(data) {
    if (/\bkube-probe\/\d+.\d+\b/.test(data) && filterKubeProbe) return;
    var span = document.createElement('div');
    span.className = 'log-line';
    span.innerHTML = renderLogLine(data);
    logsPre.appendChild(span);
    while (logsPre.childElementCount > 1000) logsPre.removeChild(logsPre.firstChild);
    if (autoScroll) scrollToBottom();
  }
  function stringToColor(str) {
    var hash = 0;
    for (var i = 0; i < str.length; i++) hash = str.charCodeAt(i) + ((hash << 5) - hash);
    var colour = '#';
    for (var i = 0; i < 3; i++) colour += ('00' + ((hash >> (i * 8)) & 0xFF).toString(16)).substr(-2);
    return colour;
  }
  function renderLogLine(logLine) {
    var logArray = logLine.split(' ');
    return '<span style="color:' + stringToColor(logArray[0]) + '">' + logLine + '</span>';
  }
  function scrollToBottom() {
    var anchor = document.getElementById('scroll-anchor');
    if (anchor) anchor.scrollIntoView({ block: 'end' });
  }
  window.toggleKubeProbe = function() {
    filterKubeProbe = !filterKubeProbe;
    document.getElementById('toggleKubeProbeButton').textContent = filterKubeProbe ? 'Show kube-probe' : 'Hide kube-probe';
  };
  window.toggleAutoScroll = function() {
    autoScroll = !autoScroll;
    document.getElementById('toggleAutoScrollButton').textContent = autoScroll ? 'Disable auto-scroll' : 'Enable auto-scroll';
    document.getElementById('scrollToBottomButton').classList.toggle('hidden', autoScroll);
  };
  window.togglePaused = function() {
    paused = !paused;
    var btn = document.getElementById('togglePausedButton');
    if (paused) {
      btn.textContent = 'Resume';
      appendLog('**** paused ****');
    } else {
      appendLog('**** resumed ****');
      while (backLog.length > 0) appendLog(backLog.shift());
      btn.textContent = 'Pause';
    }
  };
  window.scrollToBottom = scrollToBottom;
  var protocol = (window.location.protocol === 'https:') ? 'wss://' : 'ws://';
  var socket = new WebSocket(protocol + window.location.host + window.location.pathname + '/live');
  socket.addEventListener('message', function(ev) {
    if (ev.data === '=================END OF LOGS=================') { socket.close(); return; }
    if (paused) {
      backLog.push(ev.data);
      while (backLog.length > 1000) backLog.shift();
    } else {
      appendLog(ev.data);
    }
  });
  socket.addEventListener('close', function() { setTimeout(function() { window.location.reload(); }, 3000); });
}

/* App Shell Page */
function initAppShell() {
  function wrap(object, method, wrapper) {
    var fn = object[method];
    return object[method] = function() {
      return wrapper.apply(this, [fn.bind(this)].concat(Array.prototype.slice.call(arguments)));
    };
  }
  var protocol = (window.location.protocol === 'https:') ? 'wss://' : 'ws://';
  var socket = new WebSocket(protocol + window.location.host + window.location.pathname + '/socket');
  var term = new Terminal({
    fontFamily: '"JetBrains Mono", "Cascadia Code", Menlo, monospace',
    fontSize: 13,
    cursorBlink: true,
    allowProposedApi: true,
    theme: { background: '#000000', foreground: '#e2e2e9', cursor: '#7c3aed', selectionBackground: 'rgba(124, 58, 237, 0.3)' }
  });
  var attachAddon = new AttachAddon.AttachAddon(socket, true);
  wrap(attachAddon, '_sendData', function(original, data) { original('\x00' + data); });
  var fitAddon = new FitAddon.FitAddon(socket, true);
  term.loadAddon(attachAddon);
  term.loadAddon(fitAddon);
  term.open(document.getElementById('terminal'));
  fitAddon.fit();
  var debounce = function(callback, wait) {
    var timeoutId = null;
    return function() {
      var args = arguments;
      window.clearTimeout(timeoutId);
      timeoutId = window.setTimeout(function() { callback.apply(null, args); }, wait);
    };
  };
  var sendResize = debounce(function() {
    fitAddon.fit();
    socket.send('\x01' + JSON.stringify({ Width: term.cols, Height: term.rows }));
  }, 100);
  window.addEventListener('resize', sendResize);
  socket.addEventListener('open', function() { sendResize(); });
}

/* Loki Log Viewer Component */
function initLokiLogViewer() {
  document.querySelectorAll('.loki-log-component').forEach(function(root) {
    var queryUrl = root.getAttribute('data-query-url');
    if (!queryUrl) return;

    var logLines = root.querySelector('.js-log-lines');
    var logWrap = root.querySelector('.js-log-wrap');
    var loading = root.querySelector('.js-loading');
    var empty = root.querySelector('.js-empty');
    var errorEl = root.querySelector('.js-error');
    var errorMsg = root.querySelector('.js-error-msg');
    var searchInput = root.querySelector('.js-search');
    var processFilter = root.querySelector('.js-process-filter');
    var showProbes = root.querySelector('.js-show-probes');
    var logDisplay = root.querySelector('.js-log-display');
    var scrollAnchor = root.querySelector('.js-scroll-anchor');
    var logNav = root.querySelector('.js-log-nav');
    var scrollTopBtn = root.querySelector('.js-scroll-top');
    var scrollBottomBtn = root.querySelector('.js-scroll-bottom');
    var noPoll = root.getAttribute('data-no-poll') === 'true';

    // Optional fixed time window (set when opened from an Observe panel click/zoom).
    // Validated client-side; any parse failure falls back to today's live-tail behavior.
    var startTsAttr = root.getAttribute('data-start-ts');
    var endTsAttr = root.getAttribute('data-end-ts');
    var windowStartNs = null, windowEndNs = null, windowStartMs = null, windowEndMs = null;
    if (startTsAttr && endTsAttr) {
      var parsedStartMs = Date.parse(startTsAttr);
      var parsedEndMs = Date.parse(endTsAttr);
      if (!isNaN(parsedStartMs) && !isNaN(parsedEndMs) && parsedEndMs > parsedStartMs) {
        windowStartMs = parsedStartMs;
        windowEndMs = parsedEndMs;
        windowStartNs = String(Math.round(parsedStartMs * 1e6));
        windowEndNs = String(Math.round(parsedEndMs * 1e6));
      }
    }
    var hasWindow = !!(windowStartNs && windowEndNs);
    var windowLabel = root.querySelector('.js-window-label');
    if (hasWindow && windowLabel) {
      var fmtOpts = { hour: '2-digit', minute: '2-digit' };
      windowLabel.textContent = new Date(windowStartMs).toLocaleTimeString(undefined, fmtOpts) +
        '\u2013' + new Date(windowEndMs).toLocaleTimeString(undefined, fmtOpts);
    }

    var newestTs = null;
    var oldestTs = null;
    var olderExhausted = false;
    var fetchingOlder = false;
    var debounceTimer = null;
    var pollTimer = null;
    var abortCtrl = null;
    var userScrolled = false;
    var MAX_LINES = 1000;
    var POLL_FAST = 2000;
    var POLL_SLOW = 10000;
    var pollInterval = POLL_FAST;
    var idleCount = 0;

    // Process colors
    var COLORS = ['#8b5cf6','#06b6d4','#f59e0b','#3b82f6','#10b981','#ef4444'];
    var colorMap = {}, colorIdx = 0;
    function procColor(p) {
      if (!colorMap[p]) { colorMap[p] = COLORS[colorIdx++ % COLORS.length]; }
      return colorMap[p];
    }

    // Timestamp formatting
    function formatTs(tsStr) {
      var ms = tsStr.length > 6 ? Number(tsStr.slice(0, -6)) : 0;
      var d = new Date(ms);
      return d.toISOString().replace('T', ' ').replace('Z', '');
    }

    // Escape HTML
    var escEl = document.createElement('span');
    function esc(s) { escEl.textContent = s; return escEl.innerHTML; }

    // Increment nanosecond timestamp string by 1
    function tsIncrement(tsStr) {
      var digits = tsStr.split('');
      for (var i = digits.length - 1; i >= 0; i--) {
        var d = parseInt(digits[i], 10) + 1;
        if (d < 10) { digits[i] = String(d); return digits.join(''); }
        digits[i] = '0';
      }
      return '1' + digits.join('');
    }

    // Decrement nanosecond timestamp string by 1
    function tsDecrement(tsStr) {
      var digits = tsStr.split('');
      for (var i = digits.length - 1; i >= 0; i--) {
        var d = parseInt(digits[i], 10) - 1;
        if (d >= 0) { digits[i] = String(d); return digits.join(''); }
        digits[i] = '9';
      }
      return digits.join('');
    }

    function parseLogObject(message) {
      if (message.trim().charAt(0) !== '{') return null;
      try {
        var value = JSON.parse(message);
        return value && typeof value === 'object' && !Array.isArray(value) ? value : null;
      } catch (_) {
        return null;
      }
    }

    function logValue(value) {
      return typeof value === 'string' ? value : JSON.stringify(value, null, 2);
    }

    function firstField(fields, keys) {
      for (var i = 0; i < keys.length; i++) {
        if (fields[keys[i]] != null) return fields[keys[i]];
      }
      return null;
    }

    function logSpan(className, text) {
      var span = document.createElement('span');
      span.className = className;
      span.textContent = text;
      return span;
    }

    function structuredLine(e, fields) {
      var line = document.createElement('details');
      line.className = 'log-line log-line-structured' + (e.stream === 'stderr' ? ' log-line-stderr' : '');
      var summary = document.createElement('summary');
      summary.className = 'log-entry-summary';
      summary.appendChild(logSpan('log-entry-chevron', ''));
      summary.appendChild(logSpan('log-line-ts', formatTs(e.ts)));
      summary.appendChild(logSpan('log-line-process', e.process));

      var level = firstField(fields, ['level', 'severity', 'levelname']);
      level = typeof level === 'string' ? level.toLowerCase() : '';
      var severity = 'unknown';
      switch (level) {
        case 'warn': case 'warning': severity = 'warning'; break;
        case 'error': case 'critical': case 'fatal': severity = 'error'; break;
        case 'info': severity = 'info'; break;
        case 'debug': case 'trace': severity = 'debug'; break;
      }
      summary.appendChild(logSpan('log-entry-level log-entry-level-' + severity, level || '\u2014'));

      var content = document.createElement('span');
      content.className = 'log-entry-content';
      var message = firstField(fields, ['message', 'event', 'msg']);
      content.appendChild(logSpan('log-entry-message', message == null ? 'Structured entry' : logValue(message)));
      var context = document.createElement('span');
      context.className = 'log-entry-context';
      ['method', 'path', 'status', 'duration_ms', 'logger'].forEach(function(key) {
        var value = fields[key];
        if (value == null || value === '' || typeof value === 'object') return;
        var text = key === 'duration_ms' && typeof value === 'number' ? String(Math.round(value * 100) / 100) + ' ms' : String(value);
        var field = logSpan('log-entry-context-field', text);
        field.title = key;
        context.appendChild(field);
      });
      content.appendChild(context);
      summary.appendChild(content);
      summary.appendChild(logSpan('log-entry-field-count', Object.keys(fields).length + ' fields'));
      summary.appendChild(logSpan('log-entry-raw-message log-line-msg', e.message));
      line.appendChild(summary);

      // Build the inspector only when opened; most live-tail rows stay collapsed.
      line.addEventListener('toggle', function() {
        if (!line.open || line.querySelector('.log-entry-inspector')) return;
        var inspector = document.createElement('div');
        inspector.className = 'log-entry-inspector';
        var heading = document.createElement('h3');
        heading.className = 'log-entry-heading';
        heading.textContent = 'Fields';
        inspector.appendChild(heading);
        var list = document.createElement('dl');
        list.className = 'log-entry-fields';
        Object.keys(fields).forEach(function(key) {
          var term = document.createElement('dt');
          term.textContent = key;
          var value = document.createElement('dd');
          value.textContent = logValue(fields[key]);
          list.appendChild(term);
          list.appendChild(value);
        });
        inspector.appendChild(list);
        var raw = document.createElement('details');
        raw.className = 'log-entry-original';
        var label = document.createElement('summary');
        label.textContent = 'Original line';
        raw.appendChild(label);
        var pre = document.createElement('pre');
        pre.textContent = e.message;
        raw.appendChild(pre);
        inspector.appendChild(raw);
        line.appendChild(inspector);
      });
      summary.addEventListener('click', function(event) {
        if (root.classList.contains('log-display-raw')) event.preventDefault();
      });
      return line;
    }

    // Build a document fragment from entries
    function buildFragment(entries) {
      var frag = document.createDocumentFragment();
      for (var i = 0; i < entries.length; i++) {
        var e = entries[i];
        var fields = parseLogObject(e.message);
        if (fields) {
          frag.appendChild(structuredLine(e, fields));
          continue;
        }
        var line = document.createElement('div');
        line.className = e.stream === 'stderr' ? 'log-line log-line-stderr' : 'log-line';

        var ts = document.createElement('span');
        ts.className = 'log-line-ts';
        ts.textContent = formatTs(e.ts);

        var proc = document.createElement('span');
        proc.className = 'log-line-process';
        if (e.process) proc.style.color = procColor(e.process);
        proc.textContent = e.process;

        var msg = document.createElement('span');
        msg.className = 'log-line-msg';
        msg.textContent = e.message;

        line.appendChild(ts);
        line.appendChild(proc);
        line.appendChild(msg);
        frag.appendChild(line);
      }
      return frag;
    }

    function appendEntries(entries) {
      logLines.appendChild(buildFragment(entries));
    }

    function prependEntries(entries) {
      var prevHeight = logWrap.scrollHeight;
      var prevTop = logWrap.scrollTop;
      logLines.insertBefore(buildFragment(entries), logLines.firstChild);
      // Restore scroll position so the view doesn't jump
      logWrap.scrollTop = prevTop + (logWrap.scrollHeight - prevHeight);
    }

    // Trim oldest lines from top (when auto-scrolling with new entries)
    function trimTop() {
      while (logLines.children.length > MAX_LINES) {
        logLines.removeChild(logLines.firstChild);
      }
    }

    // Trim newest lines from bottom (when prepending older entries)
    function trimBottom() {
      while (logLines.children.length > MAX_LINES) {
        logLines.removeChild(logLines.lastChild);
      }
    }

    function scrollToTop() {
      logWrap.scrollTop = 0;
    }

    function scrollToBottom() {
      if (scrollAnchor) scrollAnchor.scrollIntoView({ block: 'end' });
    }

    scrollTopBtn.addEventListener('click', scrollToTop);
    scrollBottomBtn.addEventListener('click', function() {
      userScrolled = false;
      scrollToBottom();
    });

    logWrap.addEventListener('scroll', function() {
      userScrolled = (logWrap.scrollHeight - logWrap.scrollTop - logWrap.clientHeight) > 40;
      // Prefetch older entries when approaching the top
      var prefetchZone = Math.max(logWrap.clientHeight * 0.5, 200);
      if (logWrap.scrollTop < prefetchZone && oldestTs && !olderExhausted && !fetchingOlder) {
        fetchLogs('older');
      }
    }, { passive: true });

    function cancelFetch() {
      if (abortCtrl) { abortCtrl.abort(); abortCtrl = null; }
    }

    function fetchLogs(mode) {
      if (mode === 'older') {
        if (fetchingOlder || olderExhausted || !oldestTs) return;
        fetchingOlder = true;
      } else {
        cancelFetch();
      }
      if (mode === 'reset') {
        userScrolled = false;
        logLines.innerHTML = '';
        newestTs = null;
        oldestTs = null;
        olderExhausted = false;
        idleCount = 0;
        pollInterval = POLL_FAST;
      }
      if (mode !== 'newer' && mode !== 'older') {
        loading.classList.remove('hidden');
        errorEl.classList.add('hidden');
        empty.classList.add('hidden');
        if (mode === 'reset') { logWrap.classList.add('hidden'); logNav.classList.add('hidden'); }
      }

      var params = new URLSearchParams();
      params.set('limit', '500');
      if (searchInput.value.trim()) params.set('search', searchInput.value.trim());
      if (processFilter.value) params.set('process', processFilter.value);
      if (!showProbes.checked) params.set('hide_probes', '1');
      var pageParams = new URLSearchParams(window.location.search);
      if (pageParams.get('job_name')) params.set('job_name', pageParams.get('job_name'));

      if (mode === 'newer' && newestTs) {
        params.set('start', tsIncrement(newestTs));
        params.set('direction', 'forward');
      } else if (mode === 'older' && oldestTs) {
        params.set('end', tsDecrement(oldestTs));
        params.set('direction', 'backward');
        // Never page further back than the active window's lower bound.
        if (hasWindow) params.set('start', windowStartNs);
      } else if (mode === 'reset' && hasWindow) {
        params.set('start', windowStartNs);
        params.set('end', windowEndNs);
        params.set('direction', 'backward');
      }

      var ctrl = new AbortController();
      if (mode !== 'older') abortCtrl = ctrl;
      fetch(queryUrl + '?' + params.toString(), { signal: ctrl.signal })
        .then(function(r) {
          if (!r.ok) throw new Error('HTTP ' + r.status);
          return r.json();
        })
        .then(function(data) {
          if (mode !== 'older') abortCtrl = null;
          loading.classList.add('hidden');

          if (mode === 'older') {
            fetchingOlder = false;
            var entries = data.entries || [];
            if (entries.length === 0) { olderExhausted = true; return; }
            oldestTs = entries[0].ts;
            prependEntries(entries);
            trimBottom();
            return;
          }

          if (data.error) {
            if (mode !== 'newer') { errorMsg.textContent = data.error; errorEl.classList.remove('hidden'); }
            schedulePoll();
            return;
          }
          var entries = data.entries || [];

          if (mode === 'newer') {
            if (entries.length === 0) {
              idleCount++;
              if (idleCount > 3) pollInterval = Math.min(pollInterval * 2, POLL_SLOW);
              schedulePoll();
              return;
            }
            idleCount = 0;
            pollInterval = POLL_FAST;
            newestTs = entries[entries.length - 1].ts;
            appendEntries(entries);
            logWrap.classList.remove('hidden'); logNav.classList.remove('hidden');
            empty.classList.add('hidden');
            if (!userScrolled) { trimTop(); scrollToBottom(); }
            schedulePoll();
            return;
          }

          if (entries.length === 0 && mode === 'reset') { empty.classList.remove('hidden'); schedulePoll(); return; }
          logWrap.classList.remove('hidden');
          appendEntries(entries);
          if (entries.length > 0) {
            newestTs = entries[entries.length - 1].ts;
            oldestTs = entries[0].ts;
          }
          scrollToBottom();
          schedulePoll();
        })
        .catch(function(err) {
          if (mode === 'older') { fetchingOlder = false; return; }
          abortCtrl = null;
          if (err.name === 'AbortError') return;
          loading.classList.add('hidden');
          if (mode !== 'newer') { errorMsg.textContent = err.message || 'Failed to fetch logs'; errorEl.classList.remove('hidden'); }
          schedulePoll();
        });
    }

    function schedulePoll() {
      if (noPoll || hasWindow) return;
      stopPolling();
      pollTimer = setTimeout(function() { fetchLogs('newer'); }, pollInterval);
    }
    function stopPolling() { if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; } }
    function resetAndPoll() { stopPolling(); cancelFetch(); fetchLogs('reset'); }

    processFilter.addEventListener('change', function() {
      var url = new URL(window.location);
      if (processFilter.value) { url.searchParams.set('process', processFilter.value); }
      else { url.searchParams.delete('process'); }
      history.replaceState(null, '', url);
      resetAndPoll();
    });
    searchInput.addEventListener('input', function() {
      clearTimeout(debounceTimer);
      debounceTimer = setTimeout(function() { resetAndPoll(); }, 300);
    });
    showProbes.addEventListener('change', function() { resetAndPoll(); });
    logDisplay.addEventListener('change', function() {
      var top = logWrap.scrollTop;
      root.classList.toggle('log-display-raw', logDisplay.value === 'raw');
      logWrap.scrollTop = top;
    });
    document.addEventListener('visibilitychange', function() {
      if (document.hidden) { stopPolling(); cancelFetch(); } else { idleCount = 0; pollInterval = POLL_FAST; schedulePoll(); }
    });

    fetchLogs('reset');
  });
}

/* Compact Topbar (scroll collapse) */
function initCompactTopbar() {
  var topbar = document.querySelector('[data-topbar]');
  var tabBarWrapper = document.querySelector('[data-tab-bar-wrapper]');
  var inlineTabs = document.querySelector('[data-inline-tabs]');
  var tabBar = document.querySelector('[data-tabs]');

  if (!topbar || !tabBarWrapper || !inlineTabs || !tabBar) return;

  // The tab bar's parent inside the wrapper (e.g. .app-content-padded)
  var tabBarParent = tabBar.parentNode;

  function moveTabBar(compact) {
    if (compact) {
      inlineTabs.appendChild(tabBar);
    } else if (tabBar.parentNode !== tabBarParent) {
      tabBarParent.appendChild(tabBar);
    }
  }

  var THRESHOLD = 20;
  var isCompact = false;
  var compactPref = localStorage.getItem('compact-mode') === 'true';

  requestAnimationFrame(function () {
    document.documentElement.classList.remove('compact-mode-pref');
  });

  function applyCompact(compact) {
    if (compact !== isCompact) {
      isCompact = compact;
      topbar.classList.toggle('topbar-compact', isCompact);
      moveTabBar(isCompact);
    }
  }

  function checkScroll() {
    if (compactPref) {
      applyCompact(true);
      return;
    }
    applyCompact(window.scrollY > THRESHOLD);
  }

  window.addEventListener('scroll', checkScroll, { passive: true });
  checkScroll();

  var toggles = document.querySelectorAll('.compact-mode-toggle');
  toggles.forEach(function (toggle) {
    toggle.checked = compactPref;
    toggle.addEventListener('change', function () {
      compactPref = toggle.checked;
      localStorage.setItem('compact-mode', compactPref);
      toggles.forEach(function (t) {
        t.checked = compactPref;
      });
      checkScroll();
    });
  });
}

/* Preference Toggles (reduce motion, pipeline toasts, low data) */
function initPreferenceToggles() {
  var motionToggles = document.querySelectorAll('.reduce-motion-toggle');
  var motionPref = localStorage.getItem('reduce-motion') === 'true';
  motionToggles.forEach(function (toggle) {
    toggle.checked = motionPref;
    toggle.addEventListener('change', function () {
      motionPref = toggle.checked;
      localStorage.setItem('reduce-motion', motionPref);
      document.documentElement.classList.toggle('reduce-motion', motionPref);
      motionToggles.forEach(function (t) { t.checked = motionPref; });
    });
  });

  var toastToggles = document.querySelectorAll('.pipeline-toasts-toggle');
  var toastPref = localStorage.getItem('pipeline-toasts') !== 'false';
  toastToggles.forEach(function (toggle) {
    toggle.checked = toastPref;
    toggle.addEventListener('change', function () {
      toastPref = toggle.checked;
      localStorage.setItem('pipeline-toasts', toastPref);
      toastToggles.forEach(function (t) { t.checked = toastPref; });
    });
  });

  var dataToggles = document.querySelectorAll('.low-data-toggle');
  var dataPref = localStorage.getItem('low-data-mode') === 'true';
  dataToggles.forEach(function (toggle) {
    toggle.checked = dataPref;
    toggle.addEventListener('change', function () {
      dataPref = toggle.checked;
      localStorage.setItem('low-data-mode', dataPref);
      dataToggles.forEach(function (t) { t.checked = dataPref; });
      // TODO(#179/#180): restore stopAllPollers() when pollers are re-enabled
      if (!dataPref) {
        window.location.reload();
      }
    });
  });
}

document.addEventListener('click', function (e) {
  var el = e.target.closest('[data-href]');
  if (el) {
    e.preventDefault();
    e.stopPropagation();
    window.open(el.getAttribute('data-href'), '_blank', 'noopener');
  }
});

document.addEventListener('DOMContentLoaded', function () {
  initTabs();
  initCompactTopbar();
  initCountInputs();
  initJobToggles();
  initEnvReveal();
  initDropdowns();
  initMobileNav();
  initThemeToggle();
  initRawEditor();
  initAddVarModal();
  initEnvAddVarModal();
  initEnvEditVarModal();
  initAppEditVarModal();
  initHistoryModal('env-history-btn', 'env-history-modal', 'env-history-content', 'data-env-history-close');
  initHistoryModal('app-history-btn', 'app-history-modal', 'app-history-content', 'data-app-history-close');
  initDeleteModal('env-delete-btn', 'env-delete-var-modal', 'env-delete-var-form', 'env-delete-name-display', 'env-delete-config-id', 'env-delete-config-name', 'env-delete-config-value', 'env-delete-confirm', 'data-env-delete-var-close');
  initDeleteModal('app-delete-btn', 'app-delete-var-modal', 'app-delete-var-form', 'app-delete-name-display', 'app-delete-config-id', 'app-delete-config-name', 'app-delete-config-value', 'app-delete-confirm', 'data-app-delete-var-close');
  initFlashMessages();
  initAutoGrowTextareas();
  initEnvConfigRefInsert();
  initExpandModal();
  initAccentPicker();
  initPreferenceToggles();
  initCommitPopup();
  initLokiLogViewer();
  initTimestampsAndDeployForm();
  autoExpandCollapsibleCards();
  syncDetailLogHeight();
  initAnnotationTables();
  initIngressForms();
  initConfirmDialogs();
  initTfSelect();
  initSecuritySettings();
  initHdrActionTips();
  initClusterSwitcher();
  window.addEventListener('resize', function () {
    autoExpandCollapsibleCards();
    syncDetailLogHeight();
  });
});

/* Header action disabled popovers — fixed-positioned to escape overflow clip */
function initHdrActionTips() {
  document.querySelectorAll('.hdr-action-disabled').forEach(function (el) {
    var tip = el.querySelector('.hdr-action-tip'), t;
    if (!tip) return;
    function show() {
      clearTimeout(t);
      var r = el.getBoundingClientRect();
      tip.style.cssText = 'display:block;top:' + (r.bottom + 8) + 'px;right:' + (window.innerWidth - r.right) + 'px';
    }
    function hide() { t = setTimeout(function () { tip.style.display = ''; }, 80); }
    [el, tip].forEach(function (n) {
      n.addEventListener('mouseenter', show);
      n.addEventListener('mouseleave', hide);
    });
    el.addEventListener('focusin', show);
    el.addEventListener('focusout', hide);
  });
}

/* Ingress: dynamic annotation key/value tables */
function initAnnotationTables() {
  document.querySelectorAll('[data-annotations-toggle]').forEach(function (checkbox) {
    var name = checkbox.getAttribute('data-annotations-toggle');
    var target = document.getElementById('annotations-table-' + name);
    if (!target) return;
    checkbox.addEventListener('change', function () {
      target.classList.toggle('hidden', !this.checked);
    });
  });

  document.querySelectorAll('[data-annotations-add]').forEach(function (addBtn) {
    var name = addBtn.getAttribute('data-annotations-add');
    var table = document.getElementById('annotations-list-' + name);
    if (!table) return;

    table.addEventListener('click', function (e) {
      if (e.target.closest('.annotation-remove')) {
        e.target.closest('tr').remove();
      }
    });

    var nextIdx = table.querySelectorAll('tbody tr').length;
    addBtn.addEventListener('click', function () {
      var tbody = table.querySelector('tbody');
      var idx = nextIdx++;
      var tr = document.createElement('tr');

      var keyTd = document.createElement('td');
      var keyInput = document.createElement('input');
      keyInput.type = 'text';
      keyInput.name = '_annotation_key_' + idx;
      keyInput.className = 'input input-bordered input-xs w-full';
      keyInput.placeholder = 'nginx.ingress.kubernetes.io/...';
      keyTd.appendChild(keyInput);

      var valTd = document.createElement('td');
      var valInput = document.createElement('input');
      valInput.type = 'text';
      valInput.name = '_annotation_value_' + idx;
      valInput.className = 'input input-bordered input-xs w-full';
      valTd.appendChild(valInput);

      var rmTd = document.createElement('td');
      var rmBtn = document.createElement('button');
      rmBtn.type = 'button';
      rmBtn.className = 'btn btn-ghost btn-xs text-error p-0';
      rmBtn.textContent = '\u00d7';
      rmBtn.addEventListener('click', function () { tr.remove(); });
      rmTd.appendChild(rmBtn);

      tr.appendChild(keyTd);
      tr.appendChild(valTd);
      tr.appendChild(rmTd);
      tbody.appendChild(tr);
    });
  });
}

/* Ingress: unified form host/path add/remove */
function initIngressForms() {
  // Remove host or path row (generic)
  document.querySelectorAll('.btn-remove-row').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var row = btn.closest('.host-row') || btn.closest('.path-row');
      if (row) row.remove();
    });
  });

  // Add host
  document.querySelectorAll('[data-add-host]').forEach(function (btn) {
    var name = btn.getAttribute('data-add-host');
    var container = document.getElementById('new-hosts-' + name);
    if (!container) return;
    var nextHostIdx = 0;

    btn.addEventListener('click', function () {
      var idx = nextHostIdx++;
      var row = document.createElement('div');
      row.className = 'host-row flex items-center gap-2 mb-0.5';

      var input = document.createElement('input');
      input.type = 'text';
      input.name = '_new_host_' + idx + '_name';
      input.className = 'input input-bordered input-xs flex-1';
      input.placeholder = 'app.example.com';

      var tlsLabel = document.createElement('label');
      tlsLabel.className = 'flex items-center gap-1 cursor-pointer';
      tlsLabel.title = 'Fetch certificate for this hostname';
      var tlsCheckbox = document.createElement('input');
      tlsCheckbox.type = 'checkbox';
      tlsCheckbox.name = '_new_host_' + idx + '_tls';
      tlsCheckbox.className = 'checkbox checkbox-xs checkbox-primary';
      tlsCheckbox.checked = true;
      var tlsIcon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      tlsIcon.setAttribute('class', 'w-3 h-3');
      tlsIcon.setAttribute('viewBox', '0 0 24 24');
      tlsIcon.setAttribute('fill', 'none');
      tlsIcon.setAttribute('stroke', 'currentColor');
      tlsIcon.setAttribute('stroke-width', '2');
      tlsIcon.setAttribute('stroke-linecap', 'round');
      tlsIcon.setAttribute('stroke-linejoin', 'round');
      tlsIcon.innerHTML = '<rect x="3" y="11" width="18" height="11" rx="2" ry="2"></rect><path d="M7 11V7a5 5 0 0 1 10 0v4"></path>';
      tlsLabel.appendChild(tlsCheckbox);
      tlsLabel.appendChild(tlsIcon);

      var rmBtn = document.createElement('button');
      rmBtn.type = 'button';
      rmBtn.className = 'btn btn-ghost btn-xs text-error p-0';
      rmBtn.textContent = '\u00d7';
      rmBtn.addEventListener('click', function () { row.remove(); });

      row.appendChild(tlsLabel);
      row.appendChild(input);
      row.appendChild(rmBtn);
      container.appendChild(row);
      input.focus();
    });
  });

  // Add path
  document.querySelectorAll('[data-add-path]').forEach(function (btn) {
    var name = btn.getAttribute('data-add-path');
    var container = document.getElementById('new-paths-' + name);
    if (!container) return;
    var choices = JSON.parse(container.getAttribute('data-web-processes') || '[]');
    var nextIdx = 0;

    btn.addEventListener('click', function () {
      var idx = nextIdx++;
      var row = document.createElement('div');
      row.className = 'path-row flex items-center gap-2 mb-1';

      var pathInput = document.createElement('input');
      pathInput.type = 'text';
      pathInput.name = '_new_path_' + idx + '_path';
      pathInput.className = 'input input-bordered input-xs flex-1';
      pathInput.placeholder = '/';

      var typeSelect = document.createElement('select');
      typeSelect.name = '_new_path_' + idx + '_type';
      typeSelect.className = 'select select-bordered select-xs';
      ['Prefix', 'Exact', 'ImplementationSpecific'].forEach(function (t) {
        var opt = document.createElement('option');
        opt.value = t; opt.textContent = t;
        typeSelect.appendChild(opt);
      });

      var targetSelect = document.createElement('select');
      targetSelect.name = '_new_path_' + idx + '_target';
      targetSelect.className = 'select select-bordered select-xs';
      choices.forEach(function (p) {
        var opt = document.createElement('option');
        opt.value = p; opt.textContent = p;
        targetSelect.appendChild(opt);
      });

      var rmBtn = document.createElement('button');
      rmBtn.type = 'button';
      rmBtn.className = 'btn btn-ghost btn-xs text-error p-0';
      rmBtn.textContent = '\u00d7';
      rmBtn.addEventListener('click', function () { row.remove(); });

      row.appendChild(pathInput);
      row.appendChild(typeSelect);
      row.appendChild(targetSelect);
      row.appendChild(rmBtn);
      container.appendChild(row);
      pathInput.focus();
    });
  });
}

/* Confirm-to-act dialogs (type username/slug to enable action button) */
function initConfirmDialogs() {
  document.querySelectorAll('.confirm-modal').forEach(function (dialog) {
    var expected = dialog.getAttribute('data-confirm-value');
    var input = dialog.querySelector('.confirm-input');
    var btn = dialog.querySelector('.confirm-btn');
    if (!input || !btn) return;
    input.addEventListener('input', function () {
      btn.disabled = input.value !== expected;
    });
    dialog.addEventListener('close', function () {
      input.value = '';
      btn.disabled = true;
    });
  });
}

/* Two-Factor Select — inline TOTP + WebAuthn on tf_select page */
function initTfSelect() {
  var container = document.getElementById('tf-select-page');
  if (!container) return;

  var csrfToken = container.querySelector('input[name="csrf_token"]').value;
  var selectUrl = container.getAttribute('data-select-url');
  var validateUrl = container.getAttribute('data-validate-url');
  var signinUrl = container.getAttribute('data-signin-url');
  var nextUrl = container.getAttribute('data-next-url') || '';
  var rememberCheckbox = document.getElementById('tf-remember-checkbox');

  // TOTP inline submit
  var totpForm = document.getElementById('totp-form');
  if (totpForm) {
    var fullValidateUrl = validateUrl;
    if (nextUrl) fullValidateUrl += '?next=' + encodeURIComponent(nextUrl);

    var codeField = totpForm.querySelector('input[name="code"]');
    codeField.addEventListener('input', function() {
      codeField.value = codeField.value.replace(/[^0-9]/g, '').slice(0, 6);
    });

    totpForm.addEventListener('submit', function(e) {
      e.preventDefault();
      var code = codeField.value;
      var btn = totpForm.querySelector('button[type="submit"]');
      btn.disabled = true;
      btn.innerHTML = '<span class="loading loading-spinner loading-xs"></span>';

      var selectData = new FormData();
      selectData.append('csrf_token', csrfToken);
      selectData.append('which', 'authenticator');

      fetch(selectUrl, {
        method: 'POST',
        body: selectData,
        redirect: 'manual',
        credentials: 'same-origin',
      }).then(function() {
        var form = document.createElement('form');
        form.method = 'POST';
        form.action = fullValidateUrl;
        form.style.display = 'none';

        var csrfInput = document.createElement('input');
        csrfInput.type = 'hidden';
        csrfInput.name = 'csrf_token';
        csrfInput.value = csrfToken;
        form.appendChild(csrfInput);

        var codeInput = document.createElement('input');
        codeInput.type = 'hidden';
        codeInput.name = 'code';
        codeInput.value = code;
        form.appendChild(codeInput);

        if (rememberCheckbox && rememberCheckbox.checked) {
          var rememberInput = document.createElement('input');
          rememberInput.type = 'hidden';
          rememberInput.name = 'remember';
          rememberInput.value = 'y';
          form.appendChild(rememberInput);
        }

        document.body.appendChild(form);
        form.submit();
      });
    });
  }

  // WebAuthn inline flow
  var wanBtn = document.getElementById('wan-btn');
  if (wanBtn) {
    var wanMethod = wanBtn.getAttribute('data-method');

    wanBtn.addEventListener('click', function() {
      wanBtn.disabled = true;
      wanBtn.innerHTML = '<span class="loading loading-spinner loading-xs"></span> Waiting for key...';
      var errEl = document.getElementById('wan-error');
      errEl.classList.add('hidden');

      var selectData = new FormData();
      selectData.append('csrf_token', csrfToken);
      selectData.append('which', wanMethod);

      fetch(selectUrl, {
        method: 'POST',
        body: selectData,
        redirect: 'manual',
        credentials: 'same-origin',
      }).then(function() {
        var signinData = new FormData();
        signinData.append('csrf_token', csrfToken);

        return fetch(signinUrl, {
          method: 'POST',
          body: signinData,
          credentials: 'same-origin',
          headers: { 'Accept': 'application/json' },
        });
      }).then(function(resp) {
        return resp.json();
      }).then(function(data) {
        if (!data.response || !data.response.credential_options) {
          throw new Error(data.response && data.response.errors ? JSON.stringify(data.response.errors) : 'Failed to get credential options');
        }
        var credOpts = JSON.stringify(data.response.credential_options);
        var wanState = data.response.wan_state;

        return handleSignin(credOpts).then(function(result) {
          return { result: result, wanState: wanState };
        });
      }).then(function(ctx) {
        if (ctx.result.error_msg) {
          throw new Error(ctx.result.error_msg);
        }
        var responseUrl = signinUrl + '/' + ctx.wanState;
        if (nextUrl) responseUrl += '?next=' + encodeURIComponent(nextUrl);

        var form = document.getElementById('wan-response-form');
        form.action = responseUrl;
        document.getElementById('wan-credential').value = ctx.result.credential;
        document.getElementById('wan-remember').value = (rememberCheckbox && rememberCheckbox.checked) ? 'y' : '';
        form.submit();
      }).catch(function(err) {
        wanBtn.disabled = false;
        wanBtn.textContent = 'Use Security Key';
        errEl.textContent = '';
        var alertDiv = document.createElement('div');
        alertDiv.className = 'alert alert-error text-sm py-2';
        var msgSpan = document.createElement('span');
        msgSpan.textContent = err.message;
        alertDiv.appendChild(msgSpan);
        errEl.appendChild(alertDiv);
        errEl.classList.remove('hidden');
      });
    });
  }
}

/* TOTP Authenticator Setup Modal */
function initTotpSetup() {
  var container = document.getElementById('totp-setup-container');
  if (!container) return;

  var setupUrl = container.getAttribute('data-setup-url');
  var validateUrl = container.getAttribute('data-validate-url');
  var successUrl = container.getAttribute('data-success-url');
  var csrfToken = container.getAttribute('data-csrf-token');

  var stepLoading = document.getElementById('totp-step-loading');
  var stepVerify = document.getElementById('totp-step-verify');
  var stepError = document.getElementById('totp-step-error');
  var errorMsg = document.getElementById('totp-setup-error-msg');

  // Reset to loading state
  stepLoading.classList.remove('hidden');
  stepVerify.classList.add('hidden');
  stepError.classList.add('hidden');

  // Step 1: POST to tf-setup to generate secret
  var setupData = new FormData();
  setupData.append('csrf_token', csrfToken);
  setupData.append('setup', 'authenticator');

  fetch(setupUrl, {
    method: 'POST',
    body: setupData,
    credentials: 'same-origin',
    headers: { 'Accept': 'application/json' },
  }).then(function(resp) {
    return resp.json();
  }).then(function(data) {
    var r = data.response || {};
    var key = r.tf_authr_key;
    var username = r.tf_authr_username || '';
    var issuer = r.tf_authr_issuer || '';
    var stateToken = r.tf_state_token;

    if (!key) {
      throw new Error('Failed to generate authenticator secret');
    }

    // Show key
    document.getElementById('totp-manual-key').textContent = key;

    // Generate otpauth URI and QR code
    var rawKey = key.replace(/-/g, '');
    var otpauthUri = 'otpauth://totp/' + encodeURIComponent(issuer + ':' + username)
      + '?secret=' + rawKey + '&issuer=' + encodeURIComponent(issuer);
    var qrContainer = document.getElementById('totp-qr-container');
    qrContainer.textContent = '';
    generateQrImg(otpauthUri, qrContainer);

    stepLoading.classList.add('hidden');
    stepVerify.classList.remove('hidden');

    // Digit-only filtering
    var codeInput = document.getElementById('totp-verify-code');
    codeInput.value = '';
    codeInput.addEventListener('input', function() {
      codeInput.value = codeInput.value.replace(/[^0-9]/g, '').slice(0, 6);
    });
    codeInput.focus();

    // Step 2: verify code
    var verifyForm = document.getElementById('totp-verify-form');
    verifyForm.onsubmit = function(e) {
      e.preventDefault();
      var code = codeInput.value;
      if (code.length !== 6) return;

      var btn = verifyForm.querySelector('button[type="submit"]');
      btn.disabled = true;
      btn.textContent = 'Verifying...';
      var verifyErr = document.getElementById('totp-verify-error');
      verifyErr.classList.add('hidden');

      var url = stateToken ? (setupUrl + '/' + stateToken) : validateUrl;

      var verifyData = new FormData();
      verifyData.append('csrf_token', csrfToken);
      verifyData.append('code', code);

      fetch(url, {
        method: 'POST',
        body: verifyData,
        credentials: 'same-origin',
        headers: { 'Accept': 'application/json' },
      }).then(function(resp) {
        return resp.json();
      }).then(function(result) {
        var meta = result.response || {};
        if (meta.errors || meta.field_errors) {
          var msg = '';
          if (meta.field_errors) {
            for (var field in meta.field_errors) {
              msg += meta.field_errors[field].join(', ');
            }
          }
          if (meta.errors) {
            msg = meta.errors.join(', ');
          }
          throw new Error(msg || 'Invalid code');
        }
        // Success — redirect to security settings
        window.location.href = successUrl;
      }).catch(function(err) {
        btn.disabled = false;
        btn.textContent = 'Verify';
        verifyErr.textContent = '';
        var alertDiv = document.createElement('div');
        alertDiv.className = 'alert alert-error text-sm py-2';
        var msgSpan = document.createElement('span');
        msgSpan.textContent = err.message;
        alertDiv.appendChild(msgSpan);
        verifyErr.appendChild(alertDiv);
        verifyErr.classList.remove('hidden');
      });
    };
  }).catch(function(err) {
    stepLoading.classList.add('hidden');
    errorMsg.textContent = err.message;
    stepError.classList.remove('hidden');
  });
}

/* Generate QR code as an img element using the server-side endpoint */
function generateQrImg(otpauthUri, container) {
  var img = document.createElement('img');
  img.alt = 'Scan with authenticator app';
  img.className = 'w-48 h-48 rounded-lg border border-base-content/10 bg-white p-2';
  img.src = '/account/security/qr?uri=' + encodeURIComponent(otpauthUri);
  container.appendChild(img);
}

/* Security Settings — WebAuthn key deletion via fetch */
function initSecuritySettings() {
  var container = document.getElementById('security-settings');
  if (!container) return;

  var deleteUrl = container.getAttribute('data-wan-delete-url');
  var csrfToken = container.getAttribute('data-csrf-token');
  if (!deleteUrl) return;

  container.querySelectorAll('.wan-delete-btn').forEach(function(btn) {
    btn.addEventListener('click', function() {
      var keyName = btn.getAttribute('data-key-name');
      btn.disabled = true;
      btn.textContent = 'Removing...';

      var data = new FormData();
      data.append('csrf_token', csrfToken);
      data.append('name', keyName);

      fetch(deleteUrl, {
        method: 'POST',
        body: data,
        credentials: 'same-origin',
        headers: { 'Accept': 'application/json' },
      }).then(function(resp) {
        return resp.json();
      }).then(function(result) {
        var meta = result.response || {};
        if (meta.errors || meta.field_errors) {
          throw new Error('Failed to remove key');
        }
        // Reload — Flask-Security doesn't flash on JSON, so use a param
        window.location.href = window.location.pathname + '?deleted=' + encodeURIComponent(keyName);
      }).catch(function() {
        btn.disabled = false;
        btn.textContent = 'Remove';
      });
    });
  });
}

/* ====================================================================== */
/* Cluster switcher: ⌘J jump palette across Cabotage installs             */
/* ====================================================================== */

var CLUSTER_STORE_KEY = 'cabotage-clusters';
var CLUSTER_NAME_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$/;

// A jump lands with #from-cluster=<name>. Consume it before initTabs reads the hash.
var _clusterArrival = (function () {
  var m = /^#from-cluster=([A-Za-z0-9._-]{1,40})$/.exec(window.location.hash);
  if (!m) return null;
  history.replaceState(history.state, '', window.location.pathname + window.location.search);
  return m[1];
})();

function initClusterSwitcher() {
  var dataEl = document.getElementById('cluster-switcher-data');
  if (!dataEl) return;
  var data = JSON.parse(dataEl.textContent);
  var crumbWrap = document.querySelector('[data-cluster-crumb]');
  var face = document.querySelector('[data-cluster-face]');
  var topbar = document.querySelector('[data-topbar]');
  var horizon = document.querySelector('[data-cluster-horizon]');
  var appShell = document.querySelector('.app-shell');
  var isMac = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
  var shortcut = isMac ? '⌘J' : 'Ctrl J';
  var reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  var DEST_ORDER = ['shared', 'exact', 'home'];
  var DEST_LABEL = { shared: 'Nearest shared page', exact: 'Same page', home: 'Home' };

  document.querySelectorAll('[data-cluster-kbd]').forEach(function (k) { k.textContent = shortcut; });
  document.querySelectorAll('.cluster-crumb').forEach(function (b) { b.title = 'Switch cluster (' + shortcut + ')'; });

  // ---- Data ----------------------------------------------------------------
  function originOf(value) {
    var raw = String(value || '').trim();
    if (!raw) return null;
    if (!/^[a-z][a-z0-9+.-]*:\/\//i.test(raw)) raw = 'https://' + raw;
    try {
      var u = new URL(raw);
      if (u.protocol !== 'https:' && u.protocol !== 'http:') return null;
      return u.origin;
    } catch {
      return null;
    }
  }

  function readPersonal() {
    var list;
    try { list = JSON.parse(localStorage.getItem(CLUSTER_STORE_KEY) || '[]'); } catch { return []; }
    if (!Array.isArray(list)) return [];
    return list.filter(function (c) {
      return c && CLUSTER_NAME_RE.test(c.name) && originOf(c.url) === c.url && data.colors.indexOf(c.color) >= 0;
    });
  }

  function writePersonal(list) {
    localStorage.setItem(CLUSTER_STORE_KEY, JSON.stringify(list.map(function (c) {
      return { name: c.name, url: c.url, color: c.color };
    })));
  }

  function clusters() {
    var configured = data.clusters.map(function (c) {
      return { name: c.name, url: c.url, region: c.region, color: c.color, personal: false };
    });
    var taken = configured.map(function (c) { return c.url; });
    var list = configured.concat(readPersonal().filter(function (c) {
      return taken.indexOf(c.url) < 0;
    }).map(function (c) {
      return { name: c.name, url: c.url, region: null, color: c.color, personal: true };
    }));
    var here = currentIn(list);
    if (!here) {
      // An install outside the operators' list still appears, so "you are here" is never missing.
      list.unshift({ name: window.location.host, url: window.location.origin, region: null, color: 'current', personal: false, unnamed: true });
    }
    return list;
  }

  function currentIn(list) {
    for (var i = 0; i < list.length; i++) {
      if (data.current ? list[i].name === data.current && !list[i].personal : list[i].url === window.location.origin) return list[i];
    }
    return null;
  }

  function isCurrent(c) {
    return c.unnamed || (data.current ? c.name === data.current && !c.personal : c.url === window.location.origin);
  }

  function destPath(mode) {
    if (mode === 'home') return '/';
    if (mode === 'exact') return window.location.pathname + window.location.search;
    var path = window.location.pathname, best = '/';
    data.shared.forEach(function (root) {
      var r = root.replace(/\/$/, '');
      if ((path === r || path.indexOf(r + '/') === 0) && root.length > best.length) best = root;
    });
    return best;
  }

  function syncCrumb() {
    var others = clusters().filter(function (c) { return !isCurrent(c); });
    if (crumbWrap) crumbWrap.hidden = !data.current && others.length === 0;
  }

  // ---- Live link: preconnect + reachability --------------------------------
  var reach = {};
  var preconnected = {};
  var pingTimer = null;

  function warm(c) {
    if (preconnected[c.url]) return;
    preconnected[c.url] = true;
    var link = document.createElement('link');
    link.rel = 'preconnect';
    link.href = c.url;
    document.head.appendChild(link);
  }

  function ping(c) {
    if (window.location.protocol === 'https:' && c.url.indexOf('http:') === 0) {
      reach[c.url] = { unchecked: true };
      return;
    }
    var started = performance.now();
    var ctl = typeof AbortController === 'function' ? new AbortController() : null;
    var timer = setTimeout(function () { if (ctl) ctl.abort(); }, 4000);
    // no-cors: an opaque answer proves the host is up, and needs no CORS on older installs.
    fetch(c.url + '/_cabotage/ping', { mode: 'no-cors', cache: 'no-store', credentials: 'omit', signal: ctl ? ctl.signal : undefined })
      .then(function () { reach[c.url] = { ms: Math.round(performance.now() - started) }; })
      .catch(function () { reach[c.url] = { down: true }; })
      .then(function () { clearTimeout(timer); paintReach(c); });
  }

  function pingAll() {
    clusters().forEach(function (c) {
      if (isCurrent(c)) return;
      warm(c);
      ping(c);
    });
  }

  // ---- DOM helpers ---------------------------------------------------------
  function h(tag, attrs, children) {
    var el = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === 'text') el.textContent = attrs[k];
      else if (k === 'className') el.className = attrs[k];
      else if (attrs[k] === true) el.setAttribute(k, '');
      else if (attrs[k] !== false && attrs[k] != null) el.setAttribute(k, attrs[k]);
    });
    (children || []).forEach(function (child) {
      if (child != null) el.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
    });
    return el;
  }
  function svg(paths, cls) {
    var s = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    s.setAttribute('viewBox', '0 0 24 24');
    s.setAttribute('fill', 'none');
    s.setAttribute('stroke', 'currentColor');
    s.setAttribute('stroke-width', '2');
    s.setAttribute('stroke-linecap', 'round');
    s.setAttribute('stroke-linejoin', 'round');
    s.setAttribute('aria-hidden', 'true');
    if (cls) s.setAttribute('class', cls);
    s.innerHTML = paths;
    return s;
  }
  function kbd(text) { return h('kbd', { text: text }); }
  var ICON_SEARCH = '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>';
  var ICON_PLUS = '<path d="M12 5v14M5 12h14"/>';
  var ICON_X = '<path d="M18 6 6 18M6 6l12 12"/>';
  var ICON_BACK = '<path d="m15 18-6-6 6-6"/>';
  var ICON_FROM = '<path d="M9 14 4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11"/>';

  // ---- Palette -------------------------------------------------------------
  var root, input, listEl, scopeBtn, addForm, footNote;
  var state = { open: false, query: '', sel: 0, mode: 'shared', rows: [], adding: false, returnFocus: null };

  function build() {
    input = h('input', {
      type: 'text', className: 'cp-input', role: 'combobox', 'aria-expanded': 'true',
      'aria-controls': 'cp-list', 'aria-autocomplete': 'list', placeholder: 'Jump to cluster…',
      autocomplete: 'off', spellcheck: 'false', 'aria-label': 'Filter clusters',
    });
    scopeBtn = h('button', { type: 'button', className: 'cp-scope', title: 'Where the jump lands (Tab)' });
    listEl = h('div', { className: 'cp-list', id: 'cp-list', role: 'listbox', 'aria-label': 'Clusters' });
    footNote = h('span', { className: 'cp-foot-note' });
    addForm = buildAddForm();
    var palette = h('div', { className: 'cluster-palette', role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Jump to cluster' }, [
      h('div', { className: 'cp-search' }, [svg(ICON_SEARCH, 'cp-search-icon'), input, scopeBtn]),
      listEl,
      addForm,
      h('div', { className: 'cp-foot' }, [
        footNote,
        h('span', { className: 'cp-keys' }, [
          h('span', {}, [kbd('↑'), kbd('↓'), ' move']),
          h('span', {}, [kbd('tab'), ' lands on']),
          h('span', {}, [kbd('↵'), ' jump']),
          h('span', {}, [kbd(isMac ? '⌘↵' : 'Ctrl ↵'), ' new tab']),
        ]),
      ]),
    ]);
    root = h('div', { className: 'cluster-palette-root', hidden: true }, [h('div', { className: 'cp-scrim', 'data-cp-close': true }), palette]);
    document.body.appendChild(root);

    input.addEventListener('input', function () {
      state.query = input.value;
      state.sel = 0;
      renderList();
    });
    input.addEventListener('keydown', onListKey);
    scopeBtn.addEventListener('click', function () { cycleMode(1); input.focus(); });
    root.addEventListener('click', function (e) {
      if (e.target.closest('[data-cp-close]')) { close(); return; }
      var remove = e.target.closest('[data-cp-remove]');
      if (remove) { removePersonal(remove.getAttribute('data-cp-remove')); return; }
      var row = e.target.closest('.cp-row');
      if (row) activate(Number(row.getAttribute('data-index')), e.metaKey || e.ctrlKey);
    });
    listEl.addEventListener('mousemove', function (e) {
      var row = e.target.closest('.cp-row');
      if (!row) return;
      var i = Number(row.getAttribute('data-index'));
      if (i !== state.sel) { state.sel = i; paintSelection(); }
    });
    root.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') {
        e.preventDefault();
        if (state.adding) showList(); else close();
      }
    });
  }

  function visibleRows() {
    var q = state.query.trim().toLowerCase();
    var rows = clusters().filter(function (c) {
      if (!q) return true;
      var host = c.url.replace(/^https?:\/\//, '');
      return c.name.toLowerCase().indexOf(q) >= 0 || host.indexOf(q) >= 0 || (c.region || '').toLowerCase().indexOf(q) >= 0;
    }).map(function (c) { return { cluster: c }; });
    rows.push({ action: 'add' });
    return rows;
  }

  function renderList() {
    state.rows = visibleRows();
    var firstJumpable = 0;
    for (var k = 0; k < state.rows.length; k++) {
      if (state.rows[k].cluster && !isCurrent(state.rows[k].cluster)) { firstJumpable = k; break; }
    }
    if (state.sel === -1) state.sel = firstJumpable;
    if (state.sel >= state.rows.length) state.sel = state.rows.length - 1;

    var path = destPath(state.mode);
    listEl.textContent = '';
    var number = 0;
    state.rows.forEach(function (row, i) {
      var id = 'cp-opt-' + i;
      if (row.action === 'add') {
        if (state.query && state.rows.length === 1) {
          listEl.appendChild(h('div', { className: 'cp-empty', text: 'No cluster matches “' + state.query.trim() + '”.' }));
        }
        listEl.appendChild(h('div', { className: 'cp-row cp-row-action', role: 'option', id: id, 'data-index': i }, [
          h('span', { className: 'cp-action-icon' }, [svg(ICON_PLUS)]),
          h('span', { className: 'cp-name', text: 'Add a cluster…' }),
          h('span', { className: 'cp-dest', text: 'Saved in this browser' }),
        ]));
        return;
      }
      var c = row.cluster;
      var here = isCurrent(c);
      number += 1;
      var host = c.url.replace(/^https?:\/\//, '');
      var dest = here
        ? h('span', { className: 'cp-dest', text: 'You are here' })
        : h('span', { className: 'cp-dest' }, [c.url.slice(0, c.url.length - host.length), h('span', { className: 'cp-host', text: host }), h('b', { text: path })]);
      var meta = h('span', { className: 'cp-meta' }, [
        c.personal ? h('span', { className: 'cp-tag', text: 'personal' }) : null,
        here ? h('span', { className: 'cp-tag', text: 'current' }) : h('span', { className: 'cp-reach', 'data-reach': c.url }),
        c.personal ? h('button', { type: 'button', className: 'cp-remove', tabindex: '-1', 'data-cp-remove': c.url, 'aria-label': 'Remove ' + c.name, title: 'Remove ' + c.name + ' (' + (isMac ? '⌘⌫' : 'Ctrl ⌫') + ')' }, [svg(ICON_X)]) : null,
        h('span', { className: 'cp-num', text: number <= 9 ? String(number) : '' }),
      ]);
      listEl.appendChild(h('div', {
        className: 'cp-row' + (here ? ' cp-row-current' : ''), role: 'option', id: id, 'data-index': i,
        'aria-disabled': here ? 'true' : null,
      }, [
        // Here, the swatch is the accent actually in use; elsewhere, the cluster's configured one.
        h('span', { className: 'cluster-swatch', 'data-cluster-color': here ? 'current' : c.color, 'aria-hidden': 'true' }),
        h('span', { className: 'cp-name' }, [c.name, c.region ? h('span', { className: 'cp-region', text: c.region }) : null]),
        dest,
        meta,
      ]));
      if (!here) paintReach(c);
    });
    scopeBtn.textContent = '';
    scopeBtn.appendChild(document.createTextNode(DEST_LABEL[state.mode] + ' '));
    scopeBtn.appendChild(kbd('tab'));
    footNote.textContent = data.clusters.length ? 'Clusters come from your operators' : 'Clusters you add live in this browser';
    paintSelection();
  }

  function paintSelection() {
    listEl.querySelectorAll('.cp-row').forEach(function (row) {
      row.setAttribute('aria-selected', String(Number(row.getAttribute('data-index')) === state.sel));
    });
    var selected = document.getElementById('cp-opt-' + state.sel);
    if (selected) {
      input.setAttribute('aria-activedescendant', selected.id);
      selected.scrollIntoView({ block: 'nearest' });
    }
  }

  function paintReach(c) {
    if (!listEl) return;
    var r = reach[c.url];
    listEl.querySelectorAll('[data-reach]').forEach(function (el) {
      if (el.getAttribute('data-reach') !== c.url) return;
      var row = el.closest('.cp-row');
      row.classList.toggle('cp-row-down', !!(r && r.down));
      el.className = 'cp-reach' + (r ? (r.down ? ' is-down' : r.unchecked ? ' is-unchecked' : ' is-up') : ' is-pending');
      el.textContent = !r ? '' : r.down ? 'unreachable' : r.unchecked ? 'not checked' : r.ms + ' ms';
      el.title = !r ? 'Checking…' : r.down ? 'No answer within 4 seconds' : r.unchecked ? 'An https page can’t probe an http cluster' : 'Round trip from this browser';
    });
  }

  function cycleMode(step) {
    var i = DEST_ORDER.indexOf(state.mode);
    state.mode = DEST_ORDER[(i + step + DEST_ORDER.length) % DEST_ORDER.length];
    renderList();
  }

  function move(step) {
    var n = state.rows.length;
    if (!n) return;
    state.sel = (state.sel + step + n) % n;
    paintSelection();
  }

  function onListKey(e) {
    if (e.isComposing) return;
    var k = e.key;
    if (k === 'ArrowDown') { e.preventDefault(); move(1); }
    else if (k === 'ArrowUp') { e.preventDefault(); move(-1); }
    else if (k === 'Tab') { e.preventDefault(); cycleMode(e.shiftKey ? -1 : 1); }
    else if (k === 'Enter') { e.preventDefault(); activate(state.sel, e.metaKey || e.ctrlKey); }
    else if (k === 'Backspace' && (e.metaKey || e.ctrlKey)) {
      var row = state.rows[state.sel];
      if (row && row.cluster && row.cluster.personal) { e.preventDefault(); removePersonal(row.cluster.url); }
    } else if (/^[1-9]$/.test(k) && !state.query && !e.metaKey && !e.ctrlKey && !e.altKey) {
      var jumpable = state.rows.filter(function (r) { return r.cluster; });
      var target = jumpable[Number(k) - 1];
      if (target) { e.preventDefault(); activate(state.rows.indexOf(target), false); }
    }
  }

  function activate(index, newTab) {
    var row = state.rows[index];
    if (!row) return;
    if (row.action === 'add') { showAdd(); return; }
    if (isCurrent(row.cluster)) { close(); return; }
    jump(row.cluster, newTab);
  }

  function open() {
    if (state.open) return;
    if (!root) build();
    state.open = true;
    state.query = '';
    state.sel = -1;
    state.mode = 'shared';
    state.returnFocus = document.activeElement;
    input.value = '';
    showList();
    root.hidden = false;
    if (appShell) appShell.inert = true;
    document.querySelectorAll('[data-cluster-open][aria-haspopup]').forEach(function (b) { b.setAttribute('aria-expanded', 'true'); });
    input.focus();
    pingAll();
    pingTimer = setInterval(pingAll, 5000);
  }

  function close(restoreFocus) {
    if (!state.open) return;
    state.open = false;
    root.hidden = true;
    if (appShell) appShell.inert = false;
    clearInterval(pingTimer);
    document.querySelectorAll('[data-cluster-open][aria-haspopup]').forEach(function (b) { b.setAttribute('aria-expanded', 'false'); });
    if (restoreFocus !== false && state.returnFocus && state.returnFocus.focus) state.returnFocus.focus();
  }

  // ---- Personal clusters ---------------------------------------------------
  function buildAddForm() {
    var swatches = h('div', { className: 'cp-swatches', role: 'radiogroup', 'aria-label': 'Color' });
    data.colors.forEach(function (color) {
      swatches.appendChild(h('label', { className: 'cp-swatch-opt', title: color }, [
        h('input', { type: 'radio', name: 'color', value: color, 'aria-label': color }),
        h('span', { className: 'cluster-swatch', 'data-cluster-color': color, 'aria-hidden': 'true' }),
      ]));
    });
    var form = h('form', { className: 'cp-add', hidden: true, novalidate: true }, [
      h('div', { className: 'cp-add-head' }, [
        h('button', { type: 'button', className: 'cp-back', 'data-cp-back': true, 'aria-label': 'Back to clusters' }, [svg(ICON_BACK)]),
        h('span', { text: 'Add a cluster' }),
      ]),
      h('label', { className: 'cp-field' }, [h('span', { text: 'URL' }), h('input', { name: 'url', type: 'url', inputmode: 'url', placeholder: 'https://cabotage.example.com', autocomplete: 'off', spellcheck: 'false', required: true })]),
      h('label', { className: 'cp-field' }, [h('span', { text: 'Name' }), h('input', { name: 'name', type: 'text', autocomplete: 'off', spellcheck: 'false', maxlength: '40' })]),
      h('div', { className: 'cp-field' }, [h('span', { text: 'Color' }), swatches]),
      h('p', { className: 'cp-add-error', role: 'alert' }),
      h('div', { className: 'cp-add-actions' }, [
        h('span', { className: 'cp-add-note', text: 'Saved in this browser, for ' + window.location.host + ' only.' }),
        h('button', { type: 'button', className: 'btn btn-ghost btn-sm', 'data-cp-back': true, text: 'Cancel' }),
        h('button', { type: 'submit', className: 'btn btn-primary btn-sm', text: 'Add cluster' }),
      ]),
    ]);
    var urlField = form.querySelector('input[name="url"]');
    var nameField = form.querySelector('input[name="name"]');
    form.addEventListener('click', function (e) { if (e.target.closest('[data-cp-back]')) showList(); });
    urlField.addEventListener('input', function () {
      var origin = originOf(urlField.value);
      nameField.placeholder = origin ? suggestName(origin) : '';
    });
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      var err = form.querySelector('.cp-add-error');
      var origin = originOf(urlField.value);
      var name = (nameField.value || (origin ? suggestName(origin) : '')).trim();
      var list = clusters();
      var clash = origin && list.filter(function (c) { return c.url === origin; })[0];
      var problem = !origin ? 'Enter the cluster’s address, like https://cabotage.example.com.'
        : clash ? 'Already listed as ' + clash.name + '.'
        : !CLUSTER_NAME_RE.test(name) ? 'Names use letters, digits, “.”, “_” and “-”, up to 40 characters.'
        : list.some(function (c) { return c.name.toLowerCase() === name.toLowerCase(); }) ? 'A cluster named ' + name + ' already exists.'
        : null;
      err.textContent = problem || '';
      if (problem) { (origin && !clash ? nameField : urlField).focus(); return; }
      var personal = readPersonal();
      personal.push({ name: name, url: origin, color: form.querySelector('input[name="color"]:checked').value });
      writePersonal(personal);
      syncCrumb();
      showList();
      state.query = '';
      input.value = '';
      renderList();
      pingAll();
    });
    return form;
  }

  function suggestName(origin) {
    var host = origin.replace(/^https?:\/\//, '').replace(/:\d+$/, '');
    if (/^[\d.]+$/.test(host) || host.indexOf('.') < 0) return host.replace(/[^A-Za-z0-9._-]/g, '-').slice(0, 40);
    var labels = host.split('.');
    return labels[labels.length - 2].slice(0, 40);
  }

  function showAdd() {
    state.adding = true;
    listEl.hidden = true;
    input.closest('.cp-search').hidden = true;
    root.querySelector('.cp-foot').hidden = true;
    addForm.hidden = false;
    addForm.reset();
    addForm.querySelector('input[name="name"]').placeholder = '';
    addForm.querySelector('.cp-add-error').textContent = '';
    var used = clusters().map(function (c) { return c.color; });
    var free = data.colors.filter(function (c) { return used.indexOf(c) < 0; })[0] || data.colors[0];
    addForm.querySelector('input[value="' + free + '"]').checked = true;
    addForm.querySelector('input[name="url"]').focus();
  }

  function showList() {
    state.adding = false;
    addForm.hidden = true;
    listEl.hidden = false;
    input.closest('.cp-search').hidden = false;
    root.querySelector('.cp-foot').hidden = false;
    renderList();
    input.focus();
  }

  function removePersonal(url) {
    writePersonal(readPersonal().filter(function (c) { return c.url !== url; }));
    syncCrumb();
    renderList();
    input.focus();
  }

  // ---- Handoff: the jump itself --------------------------------------------
  function jump(c, newTab) {
    var url = c.url + destPath(state.mode);
    if (newTab) {
      window.open(url, '_blank', 'noopener');
      close();
      return;
    }
    var here = currentIn(clusters());
    // Only operator-listed clusters share this list and consume the marker; personal ones may run anything.
    var marker = here && !c.personal ? '#from-cluster=' + encodeURIComponent(here.name) : '';
    close(false);
    if (!reduceMotion.matches) playHandoff(c);
    // Two frames so the handoff paints before the browser starts unloading.
    requestAnimationFrame(function () {
      requestAnimationFrame(function () { window.location.assign(url + marker); });
    });
  }

  var EASE_OUT = 'cubic-bezier(0.16, 1, 0.3, 1)';
  function playHandoff(c) {
    document.body.classList.add('cluster-leaving');
    if (face) {
      var next = face.cloneNode(true);
      next.removeAttribute('data-cluster-face');
      next.setAttribute('data-cluster-color', c.color);
      next.querySelector('[data-cluster-name]').textContent = c.name;
      next.classList.add('cluster-crumb-face-next');
      face.parentNode.appendChild(next);
      face.animate([{ transform: 'translateY(0)', opacity: 1 }, { transform: 'translateY(-105%)', opacity: 0 }], { duration: 280, easing: EASE_OUT, fill: 'forwards' });
      next.animate([{ transform: 'translateY(105%)', opacity: 0 }, { transform: 'translateY(0)', opacity: 1 }], { duration: 340, delay: 40, easing: EASE_OUT, fill: 'both' });
    }
    if (horizon) {
      var sweep = h('div', { className: 'cluster-horizon-sweep', 'data-cluster-color': c.color });
      horizon.appendChild(sweep);
      // Runs to 85% and holds; the destination's arrival finishes the line.
      sweep.animate([
        { transform: 'scaleX(0)' },
        { transform: 'scaleX(0.6)', offset: 0.4 },
        { transform: 'scaleX(0.85)' },
      ], { duration: 1400, easing: 'cubic-bezier(0.22, 1, 0.36, 1)', fill: 'forwards' });
    }
  }

  // Back/forward cache restores the page mid-handoff; put it back the way it was.
  window.addEventListener('pageshow', function (e) {
    if (!e.persisted) return;
    document.body.classList.remove('cluster-leaving');
    document.querySelectorAll('.cluster-crumb-face-next, .cluster-horizon-sweep').forEach(function (el) { el.remove(); });
    if (face && face.getAnimations) face.getAnimations().forEach(function (a) { a.cancel(); });
  });

  function arrive() {
    if (!_clusterArrival) return;
    var from = clusters().filter(function (c) { return c.name === _clusterArrival && !isCurrent(c); })[0];
    if (!from) return;
    var label = document.querySelector('[data-cluster-arrival]');
    if (label) {
      label.textContent = '';
      label.appendChild(svg(ICON_FROM));
      label.appendChild(h('span', { text: 'from ' + from.name }));
      label.classList.add('is-on');
      setTimeout(function () { label.classList.remove('is-on'); }, 4200);
    }
    if (horizon && !reduceMotion.matches) {
      // Finish the line the previous cluster started.
      horizon.animate([{ transform: 'scaleX(0.85)' }, { transform: 'scaleX(1)' }], { duration: 420, easing: EASE_OUT });
    }
  }

  // ---- Wiring --------------------------------------------------------------
  document.addEventListener('keydown', function (e) {
    if ((isMac ? e.metaKey : e.ctrlKey) && !e.altKey && !e.shiftKey && (e.key === 'j' || e.key === 'J')) {
      e.preventDefault();
      if (state.open) close(); else open();
    }
  });
  // Intent to switch: open the connections now, so pings and the jump land warm.
  function warmAll() {
    clusters().forEach(function (c) { if (!isCurrent(c)) warm(c); });
  }
  document.querySelectorAll('[data-cluster-open]').forEach(function (b) {
    b.addEventListener('pointerenter', warmAll, { once: true });
    b.addEventListener('focus', warmAll, { once: true });
    b.addEventListener('click', function () {
      if (document.activeElement && document.activeElement.blur && b.closest('.dropdown')) document.activeElement.blur();
      open();
    });
  });

  syncCrumb();
  arrive();
}
