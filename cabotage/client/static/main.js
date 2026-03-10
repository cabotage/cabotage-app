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
    var accent = localStorage.getItem('accent-color') || 'purple';
    if (resolved === 'terminal' && accent !== 'white' && accent !== 'dark') {
      accent = 'white';
      localStorage.setItem('accent-color', accent);
      document.documentElement.setAttribute('data-accent', accent);
      document.querySelectorAll('.accent-opt').forEach(function (b) {
        b.style.borderColor = b.getAttribute('data-accent') === accent ? 'var(--color-base-content)' : 'transparent';
      });
    }
    if (window.__applyAccent) window.__applyAccent(accent, resolved);
  }

  var cycleThemes = ['light', 'dark', 'system'];

  document.querySelectorAll('.theme-toggle-wrap').forEach(function (wrap) {
    var btn = wrap.querySelector('button');
    var dropdown = wrap.querySelector('.theme-dropdown');
    var hideTimer = null;

    function show() {
      clearTimeout(hideTimer);
      dropdown.classList.remove('hidden');
    }
    function hide() {
      dropdown.classList.add('hidden');
    }
    function hideDelayed() {
      hideTimer = setTimeout(hide, 200);
    }

    function cycleTheme() {
      var current = localStorage.getItem('theme-pref') || 'system';
      var idx = cycleThemes.indexOf(current);
      var next = cycleThemes[(idx + 1) % cycleThemes.length];
      applyPref(next);
    }

    btn.addEventListener('click', function (e) {
      e.stopPropagation();
      hide();
      cycleTheme();
    });

    wrap.addEventListener('mouseenter', show);
    wrap.addEventListener('mouseleave', hideDelayed);

    document.addEventListener('click', function (e) {
      if (!wrap.contains(e.target)) {
        hide();
      }
    });

    dropdown.querySelectorAll('.theme-opt').forEach(function (opt) {
      opt.addEventListener('click', function (e) {
        e.stopPropagation();
        applyPref(opt.getAttribute('data-theme-val'));
        hide();
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

  var current = localStorage.getItem('accent-color') || 'purple';
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
  var previewEl = document.getElementById('add-var-preview');
  var previewFadeUpdate = previewEl ? initFadeScroll(previewEl) : null;
  var siblingDataEl = document.getElementById('sibling-ref-data');
  var siblingRefs = null;

  if (siblingDataEl) {
    try { siblingRefs = JSON.parse(siblingDataEl.textContent); } catch (e) { /* ignore */ }
  }

  function resolvePreview(val) {
    if (!siblingRefs || !val || val.indexOf('${') === -1) return null;
    var pattern = /\$\{([a-zA-Z0-9_-]+)(?:\.([a-zA-Z0-9_-]+))?\.(url|host)\}/g;
    var hasMatch = false;
    var resolved = val.replace(pattern, function (match, appSlug, middle, prop) {
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
  var templatePattern = /\$\{[a-zA-Z0-9_-]+(?:\.[a-zA-Z0-9_-]+)?\.(url|host)\}/;

  function hasTemplateVars(val) {
    return val && templatePattern.test(val);
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
  var refCheck = document.getElementById('add-var-ref-check');
  var refPicker = document.getElementById('add-var-ref-picker');

  if (refCheck && refPicker) {
    refCheck.addEventListener('change', function () {
      refPicker.style.display = this.checked ? '' : 'none';
      if (this.checked && secureCheckbox) {
        secureCheckbox.checked = false;
      }
    });

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

    _addVarResetHooks.push(function () {
      refCheck.checked = false;
      refPicker.style.display = 'none';
      if (previewEl) previewEl.style.display = 'none';
    });
  }
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

/* Build/Release Detail Page */
function initBuildDetailPage(opts) {
  var nextStepUrl = opts.nextStepUrl;
  var nextStepBanner = document.getElementById('nextStepBanner');
  if (nextStepUrl && nextStepBanner) {
    nextStepBanner.querySelector('a').href = nextStepUrl;
    nextStepBanner.hidden = false;
  }
  var logsPre = document.getElementById(opts.logElementId);
  var placeholder = document.getElementById('logPlaceholder');
  if (!logsPre || !placeholder) return;
  logsPre.innerHTML = '';
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
    logsPre.appendChild(document.createTextNode(pendingLines.join('\n') + '\n'));
    logsPre.scrollTop = logsPre.scrollHeight;
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
  var text = document.getElementById('buildLog').textContent;
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
  var placeholder = document.getElementById('logPlaceholder');
  if (!logsPre || !placeholder) return;
  logsPre.innerHTML = '';
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
    logsPre.appendChild(document.createTextNode(pendingLines.join('\n') + '\n'));
    logsPre.scrollTop = logsPre.scrollHeight;
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
  var text = document.getElementById('deployLog').textContent;
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
    var scrollAnchor = root.querySelector('.js-scroll-anchor');
    var logNav = root.querySelector('.js-log-nav');
    var scrollTopBtn = root.querySelector('.js-scroll-top');
    var scrollBottomBtn = root.querySelector('.js-scroll-bottom');
    var noPoll = root.getAttribute('data-no-poll') === 'true';

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

    // Build a document fragment from entries
    function buildFragment(entries) {
      var frag = document.createDocumentFragment();
      for (var i = 0; i < entries.length; i++) {
        var e = entries[i];
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

      if (mode === 'newer' && newestTs) {
        params.set('start', tsIncrement(newestTs));
        params.set('direction', 'forward');
      } else if (mode === 'older' && oldestTs) {
        params.set('end', tsDecrement(oldestTs));
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
      if (noPoll) return;
      stopPolling();
      pollTimer = setTimeout(function() { fetchLogs('newer'); }, pollInterval);
    }
    function stopPolling() { if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; } }
    function resetAndPoll() { stopPolling(); cancelFetch(); fetchLogs('reset'); }

    processFilter.addEventListener('change', function() { resetAndPoll(); });
    searchInput.addEventListener('input', function() {
      clearTimeout(debounceTimer);
      debounceTimer = setTimeout(function() { resetAndPoll(); }, 300);
    });
    showProbes.addEventListener('change', function() { resetAndPoll(); });
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

  var sourceTabs = tabBar.querySelectorAll('[data-tab]');
  sourceTabs.forEach(function (tab) {
    var isDisabled = tab.classList.contains('tab-disabled');
    var href = tab.tagName === 'A' ? tab.getAttribute('href') : null;
    var clone = document.createElement(href && !isDisabled ? 'a' : 'button');
    clone.className = 'topbar-inline-tab';
    if (href && !isDisabled) clone.setAttribute('href', href);
    if (isDisabled) clone.classList.add('tab-disabled');
    clone.setAttribute('data-inline-tab', tab.getAttribute('data-tab'));
    if (tab.classList.contains('tab-active')) {
      clone.classList.add('tab-active');
    }
    var svg = tab.querySelector('svg');
    if (svg) clone.appendChild(svg.cloneNode(true));
    var label = tab.childNodes;
    for (var i = 0; i < label.length; i++) {
      if (label[i].nodeType === 3 && label[i].textContent.trim()) {
        var text = label[i].textContent.trim();
        var soonMatch = text.match(/^(.+?)\s*\(([^)]+)\)$/);
        if (soonMatch) {
          var labelWrap = document.createElement('span');
          labelWrap.className = 'topbar-inline-tab-label';
          labelWrap.appendChild(document.createTextNode(soonMatch[1]));
          var sub = document.createElement('span');
          sub.className = 'topbar-inline-tab-sub';
          sub.textContent = soonMatch[2];
          labelWrap.appendChild(sub);
          clone.appendChild(labelWrap);
        } else {
          clone.appendChild(document.createTextNode(text));
        }
        break;
      }
    }
    var badge = tab.querySelector('.badge');
    if (badge) clone.appendChild(badge.cloneNode(true));
    inlineTabs.appendChild(clone);
  });

  inlineTabs.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-inline-tab]');
    if (!btn) return;
    if (btn.classList.contains('tab-disabled')) {
      e.preventDefault();
      return;
    }
    var tabId = btn.getAttribute('data-inline-tab');
    var realTab = tabBar.querySelector('[data-tab="' + tabId + '"]');
    if (realTab) realTab.click();
  });

  var observer = new MutationObserver(function () {
    sourceTabs.forEach(function (tab) {
      var id = tab.getAttribute('data-tab');
      var inline = inlineTabs.querySelector('[data-inline-tab="' + id + '"]');
      if (inline) {
        inline.classList.toggle('tab-active', tab.classList.contains('tab-active'));
      }
    });
  });
  sourceTabs.forEach(function (tab) {
    observer.observe(tab, { attributes: true, attributeFilter: ['class'] });
  });

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
  initEnvReveal();
  initDropdowns();
  initMobileNav();
  initThemeToggle();
  initRawEditor();
  initAddVarModal();
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
  window.addEventListener('resize', function () {
    autoExpandCollapsibleCards();
    syncDetailLogHeight();
  });
});

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
