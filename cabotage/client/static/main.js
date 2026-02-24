/* ===== Cabotage PaaS - Vanilla JS ===== */

/* ---------- Slugify ---------- */
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

/* ---------- Tab Navigation ---------- */
function initTabs(containerSelector) {
  var container = document.querySelector(containerSelector || '[data-tabs]');
  if (!container) return;

  var tabs = container.querySelectorAll('[data-tab]');
  var panels = document.querySelectorAll('[data-tab-panel]');

  function activateTab(tabId) {
    tabs.forEach(function (t) {
      t.classList.toggle('tab-active', t.getAttribute('data-tab') === tabId);
    });

    // Emit lifecycle events before toggling visibility
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

    // Update URL hash without scrolling
    if (history.replaceState) {
      history.replaceState(null, null, '#' + tabId);
    }
  }

  tabs.forEach(function (tab) {
    tab.addEventListener('click', function (e) {
      e.preventDefault();
      activateTab(tab.getAttribute('data-tab'));
    });
  });

  // Activate from URL hash or default to first tab
  var hash = window.location.hash.replace('#', '');
  var validTab = false;
  tabs.forEach(function (t) {
    if (t.getAttribute('data-tab') === hash) validTab = true;
  });

  if (validTab) {
    activateTab(hash);
  } else if (tabs.length > 0) {
    activateTab(tabs[0].getAttribute('data-tab'));
  }
}

/* ---------- Increment/Decrement (Process Scaling) ---------- */
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

      // Show the update button
      document.querySelectorAll('.update_process_settings').forEach(function (el) {
        el.classList.remove('hidden');
      });
    });
  });

  // Pod size change handler
  document.querySelectorAll('.pod-size').forEach(function (select) {
    select.addEventListener('change', function () {
      document.querySelectorAll('.update_process_settings').forEach(function (el) {
        el.classList.remove('hidden');
      });
    });
  });
}

/* ---------- Env Var Reveal ---------- */
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

/* ---------- Dropdown Close ---------- */
function initDropdowns() {
  document.addEventListener('click', function (e) {
    if (!e.target.closest('.dropdown')) {
      document.querySelectorAll('.dropdown [tabindex]').forEach(function (el) {
        el.blur();
      });
    }
  });
}

/* ---------- Mobile Nav Toggle ---------- */
function initMobileNav() {
  var toggle = document.getElementById('mobile-nav-toggle');
  var menu = document.getElementById('mobile-nav-menu');
  if (toggle && menu) {
    toggle.addEventListener('click', function () {
      menu.classList.toggle('hidden');
    });
  }

  /* Clone tab-bar links into mobile menu */
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

/* ---------- Theme Toggle (click cycles, long-hover reveals dropdown) ---------- */
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
    // When entering terminal, auto-switch accent to white
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

  // Click cycles light→dark→system; hover reveals dropdown
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

    // Click cycles theme
    btn.addEventListener('click', function (e) {
      e.stopPropagation();
      hide();
      cycleTheme();
    });

    // Hover opens dropdown (with small delay on leave to allow moving to it)
    wrap.addEventListener('mouseenter', show);
    wrap.addEventListener('mouseleave', hideDelayed);

    // Close on click outside
    document.addEventListener('click', function (e) {
      if (!wrap.contains(e.target)) {
        hide();
      }
    });

    // Theme option clicks
    dropdown.querySelectorAll('.theme-opt').forEach(function (opt) {
      opt.addEventListener('click', function (e) {
        e.stopPropagation();
        applyPref(opt.getAttribute('data-theme-val'));
        hide();
      });
    });
  });

  // Listen for system theme changes (update resolved theme when in system mode)
  window.matchMedia('(prefers-color-scheme: light)').addEventListener('change', function () {
    var pref = localStorage.getItem('theme-pref') || 'system';
    if (pref === 'system') {
      applyPref('system');
    }
  });

  // Ensure data-theme-pref attribute is set on load
  var pref = localStorage.getItem('theme-pref') || 'system';
  document.documentElement.setAttribute('data-theme-pref', pref);
}

/* ---------- Accent Color Picker (lives inside theme dropdown) ---------- */
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

  // Bind all accent swatch buttons (inside theme dropdowns)
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

/* ---------- Raw Editor Modal ---------- */
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

  // Escape key
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && modal.style.display !== 'none') {
      closeModal();
    }
  });

  // Tab switching
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

      // Update placeholder
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

  // Copy ENV button
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
        // ignore
      }
    });
  }
}

/* ---------- Add Variable Modal ---------- */
function initAddVarModal() {
  var modal = document.getElementById('add-var-modal');
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
  }
  function closeModal() {
    modal.style.display = 'none';
  }

  // Open buttons
  document.querySelectorAll('#add-var-open, [data-add-var-open]').forEach(function (btn) {
    btn.addEventListener('click', openModal);
  });

  // Close buttons/backdrop
  modal.querySelectorAll('[data-add-var-close]').forEach(function (el) {
    el.addEventListener('click', closeModal);
  });

  // Escape key
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && modal.style.display !== 'none') {
      closeModal();
    }
  });

  // Auto-uppercase name field
  var nameField = modal.querySelector('input[name="name"]');
  if (nameField) {
    nameField.addEventListener('input', function () {
      var pos = this.selectionStart;
      this.value = this.value.toUpperCase().replace(/[^A-Z0-9_]/g, '_');
      this.selectionStart = this.selectionEnd = pos;
    });
  }
}

/* ---------- Expand Modal ---------- */
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

  // Bind all expand buttons
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

/* ---------- Detail Log Height Sync ---------- */
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

  /* Measure right column natural height (sum of children) */
  var logHeight = getColumnNaturalHeight(logCol);

  /* Open left-column cards one by one while left is shorter than right */
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

/* ---------- Build Progress Tracker ---------- */
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

  // Define step pipelines
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
      { id: 'resolve', label: 'Resolve', patterns: [/load build definition/i, /resolve image config/i], progress: 5 },
      { id: 'build', label: 'Build', patterns: [/\[\d+\/\d+\]/], progress: 40, substep: true },
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
      (step.substep ? '<span class="step-substep" data-substep></span>' : '');
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
  // Finalize duration for the previous step
  if (prevIdx >= 0 && this.phaseStartTimes[prevIdx] && !this.phaseDurations[prevIdx]) {
    this.phaseDurations[prevIdx] = (Date.now() - this.phaseStartTimes[prevIdx]) / 1000;
    this.showStepDuration(prevIdx);
  }
  // Start timing the new step
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
  // Detect error/failure patterns in build logs
  if (/error|failed|failure|exception|traceback/i.test(line) && !/no error/i.test(line) && !/warning/i.test(line)) {
    this.errored = true;
    if (this.errorStepIdx < 0) this.errorStepIdx = Math.max(this.currentStepIdx, 0);
  }

  var stepMatch = line.match(/\[(\d+)\/(\d+)\]/);
  if (stepMatch) {
    this.activate();
    var current = parseInt(stepMatch[1], 10);
    var total = parseInt(stepMatch[2], 10);
    if (total > this.totalSteps) this.totalSteps = total;
    if (current > this.maxStep) this.maxStep = current;
    var pct = 5 + (this.maxStep / this.totalSteps) * 70;
    this.setProgress(pct);
    this.setPhase('Building step ' + this.maxStep + ' of ' + this.totalSteps);
    this.setStep(1);
    var sub = this.stepsContainer && this.stepsContainer.querySelector('[data-substep]');
    if (sub) sub.textContent = this.maxStep + '/' + this.totalSteps;
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

  if (/load build definition/i.test(line) || /resolve image config/i.test(line)) {
    this.activate();
    this.setProgress(2);
    this.setPhase('Resolving build definition');
    this.setStep(0);
    return;
  }
};

BuildProgressTracker.prototype.processDeployLine = function (line) {
  // Detect error/failure patterns in deploy logs
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

  // No log content was received — don't show false success
  if (this.linesReceived === 0) {
    this.setPhase('No logs available');
    return;
  }

  this.activate();

  // Finalize duration for the last active step
  if (
    this.currentStepIdx >= 0 &&
    this.phaseStartTimes[this.currentStepIdx] &&
    !this.phaseDurations[this.currentStepIdx]
  ) {
    this.phaseDurations[this.currentStepIdx] = (Date.now() - this.phaseStartTimes[this.currentStepIdx]) / 1000;
    this.showStepDuration(this.currentStepIdx);
  }

  if (this.errored) {
    // Show error state — don't advance progress to 100%
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

  // Success path
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

/* ---------- Auto-deploy next-step redirect ---------- */
/* After an auto-deploy build/package finishes, poll the current page
   until the server populates next_step_url, then redirect there
   so the user follows the pipeline: image → release → deployment. */
/* Poll pipeline_status until the next stage appears, then redirect.
   stage = 'build' → wait for release, 'release' → wait for deploy. */
function pollForNextStep(appId, stage) {
  var url = '/applications/' + appId + '/pipeline_status';
  var attempts = 0;
  var maxAttempts = 40; // 40 × 3s = 2 min max wait
  var banner = document.getElementById('nextStepBanner');

  // Show the "building next" banner immediately
  if (banner) banner.hidden = false;

  (function poll() {
    attempts++;
    setTimeout(function () {
      fetch(url, { credentials: 'same-origin' })
        .then(function (r) {
          if (!r.ok) throw new Error('pipeline_status ' + r.status);
          return r.json();
        })
        .then(function (data) {
          var target = null;
          if (stage === 'build' && data.release && data.release.id) {
            target = '/release/' + data.release.id;
          } else if (stage === 'release' && data.deploy && data.deploy.id) {
            target = '/deployment/' + data.deploy.id;
          }
          if (target) {
            // Update banner link so user can click it even before auto-redirect
            if (banner) {
              var link = banner.querySelector('a');
              if (link) link.href = target;
            }
            // Replace (not push) so back button skips this page
            // instead of bouncing back into the auto-redirect
            window.location.replace(target);
          } else if (attempts < maxAttempts) {
            poll();
          }
          // After max attempts, just stay on the page (already shows complete state)
        })
        .catch(function () {
          if (attempts < maxAttempts) poll();
        });
    }, 3000);
  })();
}

/* ---------- Pipeline Tracker (Overview Page) ---------- */
function PipelineTracker(container) {
  this.container = container;
  this.appId = container.getAttribute('data-application-id');
  this.statusUrl = '/applications/' + this.appId + '/pipeline_status';
  this.pollInterval = null;
  this.pollRate = 0; // 0 = not polling yet
  this.bannersEl = container.querySelector('[data-pipeline-banners]');
  this.progressEl = container.querySelector('[data-pipeline-progress]');
  this.segments = {
    build: container.querySelector('[data-segment="build"]'),
    release: container.querySelector('[data-segment="release"]'),
    deploy: container.querySelector('[data-segment="deploy"]'),
  };
  this.settled = false;
  this._lastFingerprint = ''; // track state changes for live section refresh
  this._refreshing = false;

  // Live commit indicator state
  this.commitEl = document.getElementById('liveCommitStatus');
  this.currentCommit = this.commitEl ? this.commitEl.getAttribute('data-commit-sha') : null;
  this.githubRepo = this.commitEl ? this.commitEl.getAttribute('data-github-repo') : null;
  this.commitLastChanged = Date.now();

  // Check initial state and start polling (idle rate until pipeline detected)
  this.poll();
  this.startPolling(10000); // idle: check every 10s for new pipelines
}

PipelineTracker.prototype.poll = function () {
  var self = this;
  fetch(this.statusUrl, { credentials: 'same-origin' })
    .then(function (r) {
      if (!r.ok) throw new Error('pipeline_status ' + r.status);
      return r.json();
    })
    .then(function (data) {
      self.update(data);
    })
    .catch(function (err) {
      console.warn('[PipelineTracker]', err);
    });
};

PipelineTracker.prototype.startPolling = function (rate) {
  rate = rate || 3000;
  // Already polling at this rate — no-op
  if (this.pollInterval && this.pollRate === rate) return;
  // Switch rate: clear old interval, set new one
  if (this.pollInterval) clearInterval(this.pollInterval);
  var self = this;
  this.pollRate = rate;
  this.pollInterval = setInterval(function () {
    self.poll();
  }, rate);
};

PipelineTracker.prototype.stopPolling = function () {
  if (this.pollInterval) {
    clearInterval(this.pollInterval);
    this.pollInterval = null;
    this.pollRate = 0;
  }
};

PipelineTracker.prototype.update = function (data) {
  if (!data) return;

  if (data.pipeline_active) {
    this.showProgress();
    this.startPolling(3000); // fast polling while pipeline is running
    this.settled = false;
  }

  this.updateSegment('build', data.build);
  this.updateSegment('release', data.release);
  this.updateSegment('deploy', data.deploy);

  // Update live commit indicator
  this.updateCommitIndicator(data);

  // Build a fingerprint of the current state to detect changes
  var fp = [
    data.build ? data.build.status + ':' + data.build.id + ':' + data.build.version : '-',
    data.release ? data.release.status + ':' + data.release.id + ':' + data.release.version : '-',
    data.deploy ? data.deploy.status + ':' + data.deploy.id + ':' + data.deploy.version : '-',
  ].join('|');

  if (this._lastFingerprint && fp !== this._lastFingerprint) {
    this.refreshLiveSections();
    this.flashPipelineCards();
  }
  this._lastFingerprint = fp;

  // Pipeline just finished — hide progress bar, refresh page sections, drop to idle
  if (!data.pipeline_active && !this.settled && this.pollRate === 3000) {
    this.settled = true;
    this.startPolling(10000); // back to idle polling
    this.hideProgress();
    this.refreshLiveSections();
  }
};

PipelineTracker.prototype.showProgress = function () {
  if (this.bannersEl) this.bannersEl.style.display = 'none';
  if (this.progressEl) this.progressEl.style.display = '';
};

PipelineTracker.prototype.hideProgress = function () {
  if (this.progressEl) this.progressEl.style.display = 'none';
  if (this.bannersEl) this.bannersEl.style.display = '';
};

/* Flash pipeline card borders to alert on state changes. */
PipelineTracker.prototype.flashPipelineCards = function () {
  var cards = document.querySelectorAll('.pipeline-stage');
  var segments = document.querySelectorAll('.pipe-segment');
  function flash(el, cls) {
    el.classList.remove(cls);
    // Force reflow so removing+adding the class restarts the animation
    void el.offsetWidth;
    el.classList.add(cls);
    el.addEventListener(
      'animationend',
      function () {
        el.classList.remove(cls);
      },
      { once: true },
    );
  }
  for (var i = 0; i < cards.length; i++) flash(cards[i], 'pipeline-stage-flash');
  for (var j = 0; j < segments.length; j++) flash(segments[j], 'pipe-segment-flash');
};

/* Fetch the full page HTML and swap in server-rendered sections so
   pipeline cards, recent activity, processes, and banners stay current. */
PipelineTracker.prototype.refreshLiveSections = function () {
  if (this._refreshing) return;
  this._refreshing = true;
  var self = this;
  fetch(window.location.pathname, { credentials: 'same-origin', headers: { Accept: 'text/html' } })
    .then(function (r) {
      return r.text();
    })
    .then(function (html) {
      var doc = new DOMParser().parseFromString(html, 'text/html');
      var pairs = [
        ['[data-pipeline-banners]', '[data-pipeline-banners]'],
        ['[data-live-processes]', '[data-live-processes]'],
        ['[data-live-activity]', '[data-live-activity]'],
        ['[data-live-pipeline]', '[data-live-pipeline]'],
      ];
      for (var i = 0; i < pairs.length; i++) {
        var fresh = doc.querySelector(pairs[i][0]);
        var stale = document.querySelector(pairs[i][1]);
        if (fresh && stale) stale.innerHTML = fresh.innerHTML;
      }
      self.bannersEl = self.container.querySelector('[data-pipeline-banners]');
    })
    .catch(function (err) {
      console.warn('[PipelineTracker] refreshLiveSections', err);
    })
    .then(function () {
      self._refreshing = false;
    });
};

PipelineTracker.prototype.updateSegment = function (name, info) {
  var seg = this.segments[name];
  if (!seg) return;

  var dot = seg.querySelector('.pipe-seg-dot');
  var label = seg.querySelector('.pipe-seg-status');
  var version = seg.querySelector('.pipe-seg-version');
  var fill = seg.querySelector('.pipe-seg-fill');
  var link = seg.querySelector('a[data-seg-link]');

  if (!info) {
    seg.className = 'pipe-segment pipe-seg-waiting';
    if (dot) dot.className = 'pipe-seg-dot';
    if (label) label.textContent = 'Waiting';
    if (version) version.textContent = '';
    if (fill) fill.style.width = '0%';
    return;
  }

  if (version) {
    if (info.version != null) {
      version.textContent = name === 'build' ? '#' + info.version : 'v' + info.version;
    } else {
      version.textContent = '';
    }
  }

  if (info.status === 'complete') {
    seg.className = 'pipe-segment pipe-seg-complete';
    if (dot) dot.className = 'pipe-seg-dot pipe-seg-dot-success';
    if (label) label.textContent = 'Complete';
    if (fill) {
      fill.style.width = '100%';
      fill.className = 'pipe-seg-fill pipe-seg-fill-success';
    }
  } else if (info.status === 'error') {
    seg.className = 'pipe-segment pipe-seg-error';
    if (dot) dot.className = 'pipe-seg-dot pipe-seg-dot-error';
    if (label) label.textContent = info.error_detail ? 'Failed' : 'Error';
    if (fill) {
      fill.style.width = '100%';
      fill.className = 'pipe-seg-fill pipe-seg-fill-error';
    }
  } else if (info.status === 'in_progress') {
    seg.className = 'pipe-segment pipe-seg-active';
    if (dot) dot.className = 'pipe-seg-dot pipe-seg-dot-active';
    if (label) label.textContent = name === 'build' ? 'Building' : name === 'release' ? 'Packaging' : 'Deploying';
    if (fill) {
      fill.className = 'pipe-seg-fill pipe-seg-fill-active';
    }
  }

  // Render sub-step indicators when progress data is available
  var stepsEl = seg.querySelector('.pipe-seg-steps');
  if (info.progress && info.progress.steps && info.progress.current >= 0) {
    var p = info.progress;
    if (!stepsEl) {
      stepsEl = document.createElement('div');
      stepsEl.className = 'pipe-seg-steps';
      // Insert after the track bar
      var track = seg.querySelector('.pipe-seg-track');
      if (track) track.parentNode.insertBefore(stepsEl, track.nextSibling);
    }
    var checkSvg =
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" class="pipe-step-check"><polyline points="20 6 9 17 4 12"/></svg>';
    var isComplete = info.status === 'complete';
    var html = '';
    for (var s = 0; s < p.steps.length; s++) {
      var cls = 'pipe-step';
      if (s < p.current || (isComplete && s <= p.current)) cls += ' pipe-step-done';
      else if (s === p.current) cls += ' pipe-step-active';
      html += '<div class="' + cls + '">';
      html += '<div class="pipe-step-dot">' + checkSvg + '</div>';
      html += '<span class="pipe-step-label">' + p.steps[s] + '</span>';
      if (p.substep && s === p.current) {
        html += '<span class="pipe-step-sub">' + p.substep + '</span>';
      }
      html += '</div>';
    }
    stepsEl.innerHTML = html;

    // Update fill bar to reflect step progress (only for in-progress)
    if (fill && p.steps.length > 1 && !isComplete) {
      var pct = Math.round((p.current / (p.steps.length - 1)) * 100);
      fill.style.width = Math.max(pct, 5) + '%';
    }
  } else if (stepsEl) {
    stepsEl.innerHTML = '';
  }

  // Update detail link
  if (link && info.id) {
    var base = name === 'build' ? '/image/' : name === 'release' ? '/release/' : '/deployment/';
    link.href = base + info.id;
  }
};

PipelineTracker.prototype.updateCommitIndicator = function (data) {
  if (!this.commitEl) return;

  var sha = data.commit_sha;
  var repo = data.github_repository || this.githubRepo;
  var pipelineActive = data.pipeline_active;

  // Detect commit change
  if (sha && sha !== this.currentCommit) {
    this.currentCommit = sha;
    this.commitLastChanged = Date.now();
    this.commitEl.setAttribute('data-commit-sha', sha);
  }
  if (repo) {
    this.githubRepo = repo;
    this.commitEl.setAttribute('data-github-repo', repo);
  }

  // Sync commit_info data attrs for the popup
  var ci = data.commit_info;
  if (ci) {
    if (ci.deploy_time) this.commitEl.setAttribute('data-deploy-time', ci.deploy_time);
    if (ci.release_version) this.commitEl.setAttribute('data-release-version', ci.release_version);
    if (ci.image_version) this.commitEl.setAttribute('data-image-version', ci.image_version);
    if (ci.ref) this.commitEl.setAttribute('data-commit-ref', ci.ref);
    if (ci.author) this.commitEl.setAttribute('data-commit-author', ci.author);
    if (ci.release_id) this.commitEl.setAttribute('data-release-id', ci.release_id);
    if (ci.image_id) this.commitEl.setAttribute('data-image-id', ci.image_id);
  }

  // Build the indicator content
  var dotClass = 'live-commit-dot';
  var shaHtml = '';
  var staleSeconds = (Date.now() - this.commitLastChanged) / 1000;

  if (pipelineActive) {
    dotClass += ' live-commit-dot-active';
  } else if (sha && staleSeconds > 60 && this._pipelineWasActive) {
    dotClass += ' live-commit-dot-stale';
  } else if (sha) {
    dotClass += ' live-commit-dot-ok';
  }

  // Track whether pipeline was recently active (for stale detection)
  if (pipelineActive) this._pipelineWasActive = true;
  if (!pipelineActive && sha && sha !== this._lastRenderedCommit) {
    this._pipelineWasActive = false;
  }

  if (sha) {
    var shortSha = sha.substring(0, 8);
    if (repo) {
      shaHtml =
        '<a href="https://github.com/' +
        repo +
        '/commit/' +
        sha +
        '"' +
        ' target="_blank" rel="noopener"' +
        ' class="live-commit-sha">' +
        shortSha +
        '</a>';
    } else {
      shaHtml = '<code class="live-commit-sha">' + shortSha + '</code>';
    }
  } else {
    shaHtml = '<span class="text-[0.625rem] text-success/40">Up to date</span>';
    dotClass += ' live-commit-dot-ok';
  }

  // Flash animation on commit change
  var freshClass = '';
  if (sha && sha !== this._lastRenderedCommit && this._lastRenderedCommit) {
    freshClass = ' live-commit-fresh';
  }
  this._lastRenderedCommit = sha || this._lastRenderedCommit;

  this.commitEl.className = 'live-commit commit-popup-anchor' + freshClass;
  this.commitEl.innerHTML = '<span class="' + dotClass + '"></span>' + shaHtml;

  // Remove flash class after animation
  if (freshClass) {
    var el = this.commitEl;
    setTimeout(function () {
      el.classList.remove('live-commit-fresh');
    }, 1500);
  }
};

/* ---------- Commit Popup ---------- */
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

  // Hover on the commit area opens popup
  commitEl.addEventListener('mouseenter', showPopup);
  commitEl.addEventListener('mouseleave', hidePopup);

  // Keep popup open while hovering over it
  commitEl.addEventListener('mouseover', function (e) {
    if (e.target.closest && e.target.closest('.commit-popup')) {
      clearTimeout(leaveTimeout);
    }
  });

  // Delegated click: intercept any .live-commit-sha link (survives innerHTML rebuilds)
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

  // Close on click outside or Escape
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

  // Build popup HTML
  // Header — "Deployed via GitHub" like Railway
  var html = '<div class="commit-popup-header">';
  html +=
    '<svg class="commit-popup-github-icon" viewBox="0 0 16 16" fill="currentColor"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"/></svg>';
  html += '<span class="commit-popup-header-text">Deployed via GitHub</span>';
  html += '</div>';

  // Commit message area (filled by API fetch)
  html += '<div class="commit-popup-message commit-popup-loading">';
  html += '<span class="commit-popup-author-area" data-commit-author-area></span>';
  html += '<span data-commit-msg-text>Loading...</span>';
  html += '</div>';

  // Meta rows
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

  // Full SHA + copy
  html += '<div class="commit-popup-sha">';
  html += '<code title="' + sha + '">' + sha + '</code>';
  html += '<button class="commit-popup-copy" title="Copy SHA" data-copy-sha="' + sha + '">';
  html +=
    '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>';
  html += '</button>';
  html += '</div>';

  // Links
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

  // Copy button handler
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

  // Animate in
  requestAnimationFrame(function () {
    popup.classList.add('commit-popup-open');
  });

  // Fetch commit message from GitHub API
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

  // Author avatar + name in the author area
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

  // Commit message text
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

/* ---------- Live Timestamp Ticker ---------- */
/* Keeps all <time data-timestamp="..."> elements up-to-date every second */

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

function stopAllPollers() {
  if (window.pipelineTracker && window.pipelineTracker.pollInterval) {
    clearInterval(window.pipelineTracker.pollInterval);
  }
  if (window.observabilityPanel && window.observabilityPanel.timer) {
    clearInterval(window.observabilityPanel.timer);
  }
  if (window.pipelineMetricsPanel && window.pipelineMetricsPanel.timer) {
    clearInterval(window.pipelineMetricsPanel.timer);
  }
  if (window.dashboardPoller && window.dashboardPoller.pollInterval) {
    clearInterval(window.dashboardPoller.pollInterval);
  }
}

function initPipelineTracker() {
  var container = document.querySelector('[data-pipeline-tracker]');
  if (container && !isLowDataMode()) {
    window.pipelineTracker = new PipelineTracker(container);
  }
  // Always start the timestamp ticker on pages with timestamps
  if (document.querySelector('time[data-timestamp]')) {
    startTimestampTicker();
  }

  // Auto-deploy form: show spinner, let browser submit normally so
  // it follows the server redirect to the image build page.
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

/* ---------- Pipeline Toast Notifications ---------- */

function showPipelineToast(pipeline) {
  if (localStorage.getItem('pipeline-toasts') === 'false') return;
  var container = document.getElementById('pipeline-toasts');
  if (!container) return;

  var stageLabel = '';
  if (pipeline.stages.deploy) stageLabel = 'Deploying';
  else if (pipeline.stages.release) stageLabel = 'Packaging';
  else if (pipeline.stages.build) stageLabel = 'Building';
  else stageLabel = 'Pipeline running';

  var href = '/projects/' + pipeline.org_slug + '/' + pipeline.project_slug + '/applications/' + pipeline.app_slug;

  var toast = document.createElement('a');
  toast.href = href;
  toast.className = 'pipeline-toast';
  toast.setAttribute('data-toast-app', pipeline.app_id);
  toast.innerHTML =
    '<span class="pipeline-toast-dot"></span>' +
    '<span><strong>' +
    pipeline.app_name +
    '</strong> ' +
    '<span class="text-base-content/50">' +
    stageLabel +
    '</span></span>' +
    '<span class="pipeline-toast-dismiss" title="Dismiss">' +
    '<svg class="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">' +
    '<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg></span>';

  // Dismiss on X click (don't navigate)
  toast.querySelector('.pipeline-toast-dismiss').addEventListener('click', function (e) {
    e.preventDefault();
    e.stopPropagation();
    dismissToast(toast);
  });

  container.appendChild(toast);

  // Auto-dismiss after 15 seconds
  setTimeout(function () {
    dismissToast(toast);
  }, 15000);
}

function dismissToast(toast) {
  if (!toast || !toast.parentNode) return;
  toast.classList.add('toast-out');
  toast.addEventListener('animationend', function () {
    toast.remove();
  });
}

function DashboardPipelinePoller(excludeAppId) {
  this.knownActive = {};
  this.excludeAppId = excludeAppId || null;
  this.pollInterval = null;
  this.poll();
  this.startPolling();
}

DashboardPipelinePoller.prototype.startPolling = function () {
  var self = this;
  this.pollInterval = setInterval(function () {
    self.poll();
  }, 5000);
};

DashboardPipelinePoller.prototype.poll = function () {
  var self = this;
  fetch('/active_pipelines', { credentials: 'same-origin' })
    .then(function (r) {
      if (!r.ok) throw new Error('active_pipelines ' + r.status);
      return r.json();
    })
    .then(function (data) {
      self.update(data.pipelines);
    })
    .catch(function (err) {
      console.warn('[DashboardPoller]', err);
    });
};

DashboardPipelinePoller.prototype.update = function (pipelines) {
  var nowActive = {};
  for (var i = 0; i < pipelines.length; i++) {
    var p = pipelines[i];
    nowActive[p.app_id] = p;
    // Skip toast for the app already tracked inline on this page
    if (p.app_id === this.excludeAppId) continue;
    if (!this.knownActive[p.app_id]) {
      showPipelineToast(p);
    }
  }
  this.knownActive = nowActive;
};

function initDashboardPoller() {
  if (isLowDataMode()) return;
  // Run on any authenticated page that has the toast container
  var toastContainer = document.getElementById('pipeline-toasts');
  if (!toastContainer) return;
  // Skip pages that already have a PipelineTracker (app overview) —
  // those apps are already tracked inline, avoid duplicate toasts
  var trackedAppId = null;
  var trackerEl = document.querySelector('[data-pipeline-tracker]');
  if (trackerEl) trackedAppId = trackerEl.getAttribute('data-application-id');
  window.dashboardPoller = new DashboardPipelinePoller(trackedAppId);
}

/* ---------- Observability Panel ---------- */
function ObservabilityPanel(container) {
  this.container = container;
  this.appId = container.getAttribute('data-application-id');
  this.logsUrl = container.getAttribute('data-logs-url') || '';
  this.active = false;
  this.timer = null;
  this.range = '1h';

  // DOM refs
  this.cpuValue = container.querySelector('[data-obs-cpu-value]');
  this.cpuLimit = container.querySelector('[data-obs-cpu-limit]');
  this.cpuGauge = container.querySelector('[data-obs-cpu-gauge]');
  this.memValue = container.querySelector('[data-obs-mem-value]');
  this.memLimit = container.querySelector('[data-obs-mem-limit]');
  this.memGauge = container.querySelector('[data-obs-mem-gauge]');
  this.podValue = container.querySelector('[data-obs-pod-count]');
  this.podLabel = container.querySelector('[data-obs-pod-status]');
  this.restartValue = container.querySelector('[data-obs-restart-count]');
  this.cpuChart = container.querySelector('[data-obs-cpu-chart] svg');
  this.memChart = container.querySelector('[data-obs-mem-chart] svg');
  this.podsGrid = container.querySelector('[data-obs-pods-grid]');
  this.eventsEl = container.querySelector('[data-obs-events]');

  // Range buttons
  var self = this;
  var rangeButtons = container.querySelectorAll('[data-obs-range]');
  rangeButtons.forEach(function (btn) {
    btn.addEventListener('click', function () {
      rangeButtons.forEach(function (b) {
        b.classList.remove('obs-range-active');
      });
      btn.classList.add('obs-range-active');
      self.range = btn.getAttribute('data-obs-range');
      self.fetch();
    });
  });

  // Listen for tab lifecycle
  var panel = container.closest('[data-tab-panel]');
  if (panel) {
    panel.addEventListener('tab-activated', function () {
      self.activate();
    });
    panel.addEventListener('tab-deactivated', function () {
      self.deactivate();
    });
    // If tab is already active (e.g. loaded via URL hash), activate now
    if (panel.classList.contains('tab-panel-active')) {
      self.activate();
    }
  }
}

ObservabilityPanel.prototype.activate = function () {
  if (this.active) return;
  this.active = true;
  this.fetch();
  var self = this;
  this.timer = setInterval(function () {
    self.fetch();
  }, 15000);
};

ObservabilityPanel.prototype.deactivate = function () {
  this.active = false;
  if (this.timer) {
    clearInterval(this.timer);
    this.timer = null;
  }
};

ObservabilityPanel.prototype.setLoading = function (loading) {
  if (!this.container) return;
  var cards = this.container.querySelectorAll('.obs-metric-card, .obs-chart-card, .obs-section-card');
  cards.forEach(function (card) {
    if (loading) card.classList.add('obs-loading');
    else card.classList.remove('obs-loading');
  });
};

ObservabilityPanel.prototype.fetch = function () {
  var self = this;
  if (!this._loaded) this.setLoading(true);
  var url = '/applications/' + this.appId + '/observability?range=' + this.range;
  fetch(url, { credentials: 'same-origin' })
    .then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    })
    .then(function (data) {
      self._loaded = true;
      self.setLoading(false);
      self.render(data);
    })
    .catch(function (err) {
      self.setLoading(false);
      console.warn('Observability fetch error:', err);
    });
};

ObservabilityPanel.prototype.render = function (data) {
  this.updateGauges(data.current, data.limits);
  this.renderChart(
    this.cpuChart,
    data.history,
    'cpu_usage_m',
    data.limits ? data.limits.total_cpu_limit_m : null,
    '#22d3ee',
  );
  this.renderChart(
    this.memChart,
    data.history,
    'memory_usage_bytes',
    data.limits ? data.limits.total_memory_limit_bytes : null,
    '#a78bfa',
  );
  this.updatePodsGrid(data.pods);
  this.updateEvents(data.events);
};

ObservabilityPanel.prototype.updateGauges = function (current, limits) {
  if (!current) return;

  // CPU
  var cpuVal = current.cpu_usage_m;
  if (cpuVal != null && this.cpuValue) {
    this.cpuValue.textContent = Math.round(cpuVal) + 'm';
    if (limits && limits.total_cpu_limit_m && this.cpuLimit) {
      this.cpuLimit.textContent = '/ ' + limits.total_cpu_limit_m + 'm';
      var cpuPct = Math.min((cpuVal / limits.total_cpu_limit_m) * 100, 100);
      if (this.cpuGauge) {
        this.cpuGauge.style.width = cpuPct + '%';
        this.cpuGauge.className =
          'obs-gauge-fill' + (cpuPct > 90 ? ' obs-gauge-fill-danger' : cpuPct > 70 ? ' obs-gauge-fill-warning' : '');
      }
    }
  } else if (this.cpuValue) {
    this.cpuValue.textContent = '—';
  }

  // Memory
  var memVal = current.memory_usage_bytes;
  if (memVal != null && this.memValue) {
    this.memValue.textContent = this.formatBytes(memVal);
    if (limits && limits.total_memory_limit_bytes && this.memLimit) {
      this.memLimit.textContent = '/ ' + this.formatBytes(limits.total_memory_limit_bytes);
      var memPct = Math.min((memVal / limits.total_memory_limit_bytes) * 100, 100);
      if (this.memGauge) {
        this.memGauge.style.width = memPct + '%';
        this.memGauge.className =
          'obs-gauge-fill' + (memPct > 90 ? ' obs-gauge-fill-danger' : memPct > 70 ? ' obs-gauge-fill-warning' : '');
      }
    }
  } else if (this.memValue) {
    this.memValue.textContent = '—';
  }

  // Pods & Restarts
  if (this.podValue) this.podValue.textContent = current.pod_count || 0;
  if (this.podLabel) {
    var running = current.pod_count || 0;
    this.podLabel.textContent = running === 1 ? 'running' : running + ' running';
  }
  if (this.restartValue) this.restartValue.textContent = current.restart_count || 0;
};

ObservabilityPanel.prototype.formatBytes = function (bytes) {
  if (bytes == null) return '—';
  if (bytes < 1024) return bytes + 'B';
  if (bytes < 1048576) return (bytes / 1024).toFixed(0) + 'Ki';
  if (bytes < 1073741824) return (bytes / 1048576).toFixed(0) + 'Mi';
  return (bytes / 1073741824).toFixed(1) + 'Gi';
};

ObservabilityPanel.prototype.renderChart = function (svgEl, history, key, limit, color) {
  if (!svgEl || !history || !history.length) {
    if (svgEl)
      svgEl.innerHTML =
        '<text x="300" y="100" text-anchor="middle" fill="var(--text-muted)" font-size="13">No data yet</text>';
    return;
  }

  var isMemory = key === 'memory_usage_bytes';
  var self = this;
  var w = 600,
    h = 200,
    padL = 52,
    padR = 10,
    padT = 30,
    padB = 30;
  var values = history.map(function (p) {
    return p[key] || 0;
  });
  var dataMax = Math.max.apply(null, values);
  var dataMin = Math.min.apply(null, values);

  // Auto-scale to data range with 20% headroom; show limit line as reference
  var maxVal = dataMax * 1.2 || 1;
  // If limit is close to the data (within 2x), include it in the range
  if (limit && limit <= dataMax * 2) maxVal = Math.max(maxVal, limit * 1.05);
  if (maxVal === 0) maxVal = 1;

  // Scale values
  var chartW = w - padL - padR;
  var chartH = h - padT - padB;
  var xStep = chartW / Math.max(values.length - 1, 1);
  var points = values.map(function (v, i) {
    return { x: padL + i * xStep, y: padT + chartH - (v / maxVal) * chartH };
  });

  var svg = '';

  // Grid lines + Y-axis labels
  for (var g = 0; g < 4; g++) {
    var gy = padT + (chartH / 3) * g;
    var gridVal = maxVal * (1 - g / 3);
    var label = isMemory ? self.formatBytes(gridVal) : Math.round(gridVal) + 'm';
    svg += '<line x1="' + padL + '" y1="' + gy + '" x2="' + (w - padR) + '" y2="' + gy + '" class="obs-grid-line" />';
    svg +=
      '<text x="' +
      (padL - 4) +
      '" y="' +
      (gy + 4) +
      '" text-anchor="end" class="obs-chart-label">' +
      label +
      '</text>';
  }

  // Limit line (only draw if it falls within the visible range)
  if (limit && limit <= maxVal) {
    var ly = padT + chartH - (limit / maxVal) * chartH;
    svg += '<line x1="' + padL + '" y1="' + ly + '" x2="' + (w - padR) + '" y2="' + ly + '" class="obs-limit-line" />';
    var limitLabel = isMemory ? self.formatBytes(limit) : Math.round(limit) + 'm';
    svg +=
      '<text x="' +
      (w - padR) +
      '" y="' +
      (ly - 4) +
      '" text-anchor="end" class="obs-chart-label obs-chart-limit-label">' +
      limitLabel +
      ' limit</text>';
  }

  // Area fill
  var pathD = 'M' + points[0].x + ',' + points[0].y;
  for (var i = 1; i < points.length; i++) {
    pathD += ' L' + points[i].x + ',' + points[i].y;
  }
  var areaD =
    pathD + ' L' + points[points.length - 1].x + ',' + (h - padB) + ' L' + points[0].x + ',' + (h - padB) + ' Z';
  svg += '<path d="' + areaD + '" class="obs-area-fill" fill="' + color + '" />';
  svg += '<path d="' + pathD + '" class="obs-line" stroke="' + color + '" />';

  // Current value label at the last data point
  var lastPt = points[points.length - 1];
  var lastVal = values[values.length - 1];
  var currentLabel = isMemory ? self.formatBytes(lastVal) : Math.round(lastVal) + 'm';
  svg +=
    '<text x="' +
    lastPt.x +
    '" y="' +
    (lastPt.y - 8) +
    '" text-anchor="end" class="obs-chart-current-label" fill="' +
    color +
    '">' +
    currentLabel +
    '</text>';

  svgEl.innerHTML = svg;
};

ObservabilityPanel.prototype.updatePodsGrid = function (pods) {
  if (!this.podsGrid) return;
  if (!pods || !pods.length) {
    this.podsGrid.innerHTML = '<div class="obs-empty-state">No pods running</div>';
    return;
  }
  var self = this;
  var html = '';
  pods.forEach(function (pod) {
    var phase = (pod.phase || 'Unknown').toLowerCase();
    var dotClass =
      phase === 'running'
        ? 'obs-pod-dot-running'
        : phase === 'pending'
          ? 'obs-pod-dot-pending'
          : phase === 'failed'
            ? 'obs-pod-dot-failed'
            : 'obs-pod-dot-unknown';
    var name = (pod.name || '').replace(/[<>&"]/g, '');
    var podLogUrl = self.logsUrl ? self.logsUrl + '?pod=' + encodeURIComponent(name) : '';
    html +=
      '<div class="obs-pod-card" data-pod-name="' + name + '">' +
      '<span class="obs-pod-dot ' +
      dotClass +
      '"></span>' +
      (podLogUrl
        ? '<a href="' + podLogUrl + '" class="obs-pod-name obs-pod-link" data-pod-link>' + name + '</a>'
        : '<span class="obs-pod-name">' + name + '</span>') +
      '<div class="obs-pod-metrics">' +
      '<span class="obs-pod-metric"><span class="obs-pod-metric-label">CPU</span> ' +
      (pod.cpu_display || '—') +
      '</span>' +
      '<span class="obs-pod-metric"><span class="obs-pod-metric-label">MEM</span> ' +
      (pod.mem_display || '—') +
      '</span>' +
      (pod.restart_count > 0
        ? '<span class="obs-pod-metric obs-pod-restarts">' +
          pod.restart_count +
          ' restart' +
          (pod.restart_count > 1 ? 's' : '') +
          '</span>'
        : '') +
      '</div></div>';
  });
  this.podsGrid.innerHTML = html;

  // Set up multi-select listener once (delegated, so safe across re-renders)
  if (self.logsUrl && !this._podClickBound) {
    this._podClickBound = true;
    this.podsGrid.addEventListener('click', function (e) {
      var link = e.target.closest('[data-pod-link]');
      if (!link) return;
      if (e.ctrlKey || e.metaKey || e.shiftKey) {
        e.preventDefault();
        var card = link.closest('.obs-pod-card');
        card.classList.toggle('obs-pod-selected');
        self.updateMultiPodLink();
      }
      // Normal click: default <a> navigation (single pod)
    });
  }
};

ObservabilityPanel.prototype.updateMultiPodLink = function () {
  var selected = this.podsGrid.querySelectorAll('.obs-pod-selected');
  var allCards = this.podsGrid.querySelectorAll('.obs-pod-card');

  if (selected.length > 1) {
    // Build multi-pod URL and update all selected links
    var params = new URLSearchParams();
    selected.forEach(function (card) {
      params.append('pod', card.getAttribute('data-pod-name'));
    });
    var multiUrl = this.logsUrl + '?' + params.toString();
    // Show a "View logs for N pods" action
    var existing = this.podsGrid.querySelector('.obs-pod-multi-action');
    if (!existing) {
      existing = document.createElement('a');
      existing.className = 'obs-pod-multi-action';
      this.podsGrid.appendChild(existing);
    }
    existing.href = multiUrl;
    existing.textContent = 'View logs for ' + selected.length + ' pods →';
    existing.style.display = '';
  } else {
    // Remove multi-action if only 0-1 selected
    var existing = this.podsGrid.querySelector('.obs-pod-multi-action');
    if (existing) existing.style.display = 'none';
    // Clear selection if 0
    if (selected.length === 0) {
      allCards.forEach(function (c) { c.classList.remove('obs-pod-selected'); });
    }
  }
};

ObservabilityPanel.prototype.updateEvents = function (events) {
  if (!this.eventsEl) return;
  if (!events || !events.length) {
    this.eventsEl.innerHTML = '<div class="obs-empty-state">No recent events</div>';
    return;
  }
  var html = '';
  events.forEach(function (ev) {
    var dotClass = ev.type === 'Warning' ? 'obs-event-dot-warn' : 'obs-event-dot-ok';
    var reason = (ev.reason || '').replace(/[<>&"]/g, '');
    var msg = (ev.message || '').replace(/[<>&"]/g, '');
    var time = (ev.time_ago || '').replace(/[<>&"]/g, '');
    html +=
      '<div class="obs-event-item">' +
      '<span class="obs-event-dot ' +
      dotClass +
      '"></span>' +
      '<div class="obs-event-content">' +
      '<span class="obs-event-reason">' +
      reason +
      '</span>' +
      '<span class="obs-event-msg">' +
      msg +
      '</span>' +
      '</div>' +
      '<span class="obs-event-time">' +
      time +
      '</span>' +
      '</div>';
  });
  this.eventsEl.innerHTML = html;
};

function initObservabilityPanel() {
  if (isLowDataMode()) return;
  var container = document.querySelector('[data-observability-panel]');
  if (!container) return;
  window.observabilityPanel = new ObservabilityPanel(container);
}

/* ---------- Pipeline Metrics Panel ---------- */
function PipelineMetricsPanel(container) {
  this.container = container;
  this.appId = container.getAttribute('data-application-id');
  this.active = false;
  this.range = 50;

  // DOM refs — summary cards
  this.buildRate = container.querySelector('[data-pm-build-rate]');
  this.buildCounts = container.querySelector('[data-pm-build-counts]');
  this.buildAvg = container.querySelector('[data-pm-build-avg]');
  this.deployAvg = container.querySelector('[data-pm-deploy-avg]');

  // DOM refs — stat chips
  this.buildChips = container.querySelector('[data-pm-build-chips]');
  this.releaseChips = container.querySelector('[data-pm-release-chips]');
  this.deployChips = container.querySelector('[data-pm-deploy-chips]');

  // DOM refs — charts
  this.buildChart = container.querySelector('[data-pm-build-chart]');
  this.releaseChart = container.querySelector('[data-pm-release-chart]');
  this.deployChart = container.querySelector('[data-pm-deploy-chart]');

  // Range buttons
  var self = this;
  var rangeButtons = container.querySelectorAll('[data-pm-range]');
  rangeButtons.forEach(function (btn) {
    btn.addEventListener('click', function () {
      rangeButtons.forEach(function (b) {
        b.classList.remove('obs-range-active');
      });
      btn.classList.add('obs-range-active');
      self.range = parseInt(btn.getAttribute('data-pm-range'), 10);
      self.fetch();
    });
  });

  // Tab lifecycle
  var panel = container.closest('[data-tab-panel]');
  if (panel) {
    panel.addEventListener('tab-activated', function () {
      self.activate();
    });
    panel.addEventListener('tab-deactivated', function () {
      self.deactivate();
    });
    if (panel.classList.contains('tab-panel-active')) {
      self.activate();
    }
  }
}

PipelineMetricsPanel.prototype.activate = function () {
  if (this.active) return;
  this.active = true;
  this.fetch();
};

PipelineMetricsPanel.prototype.deactivate = function () {
  this.active = false;
};

PipelineMetricsPanel.prototype.fetch = function () {
  var self = this;
  var url = '/applications/' + this.appId + '/pipeline-metrics?range=' + this.range;
  fetch(url, { credentials: 'same-origin' })
    .then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    })
    .then(function (data) {
      self.render(data);
    })
    .catch(function (err) {
      console.warn('Pipeline metrics fetch error:', err);
    });
};

PipelineMetricsPanel.prototype.formatDuration = function (secs) {
  if (secs == null) return '—';
  if (secs < 60) return secs + 's';
  var m = Math.floor(secs / 60);
  var s = secs % 60;
  return m + 'm ' + s + 's';
};

PipelineMetricsPanel.prototype.render = function (data) {
  // Summary cards
  if (this.buildRate) {
    this.buildRate.textContent =
      data.images.success_rate != null ? data.images.success_rate + '%' : '—';
  }
  if (this.buildCounts) {
    this.buildCounts.textContent =
      data.images.total > 0
        ? data.images.success + ' ok / ' + data.images.error + ' err of ' + data.images.total
        : '';
  }
  if (this.buildAvg) {
    this.buildAvg.textContent = this.formatDuration(data.images.avg_secs);
  }
  if (this.deployAvg) {
    this.deployAvg.textContent = this.formatDuration(data.deployments.avg_secs);
  }

  // Stage sections
  this.renderStage(this.buildChips, this.buildChart, data.images, '/image/');
  this.renderStage(this.releaseChips, this.releaseChart, data.releases, '/release/');
  this.renderStage(this.deployChips, this.deployChart, data.deployments, '/deployment/');
};

PipelineMetricsPanel.prototype.renderStage = function (chipsEl, svgEl, stage, urlPrefix) {
  // Stat chips
  if (chipsEl) {
    var chips = '';
    chips += '<span class="pm-stat-chip">' + stage.total + ' total</span>';
    chips += '<span class="pm-stat-chip pm-stat-chip-ok">' + stage.success + ' ok</span>';
    if (stage.error > 0) {
      chips += '<span class="pm-stat-chip pm-stat-chip-err">' + stage.error + ' err</span>';
    }
    if (stage.p50_secs != null) {
      chips += '<span class="pm-stat-chip">p50 ' + this.formatDuration(stage.p50_secs) + '</span>';
    }
    if (stage.p95_secs != null) {
      chips += '<span class="pm-stat-chip">p95 ' + this.formatDuration(stage.p95_secs) + '</span>';
    }
    chipsEl.innerHTML = chips;
  }

  // Bar chart
  this.renderBarChart(svgEl, stage.history, stage.avg_secs, urlPrefix);
};

PipelineMetricsPanel.prototype.renderBarChart = function (svgEl, history, avgSecs, urlPrefix) {
  if (!svgEl) return;
  if (!history || !history.length) {
    svgEl.innerHTML =
      '<text x="300" y="60" text-anchor="middle" fill="var(--text-muted)" font-size="13">No data yet</text>';
    return;
  }

  var w = 600,
    h = 120,
    padL = 4,
    padR = 30, // room for avg label
    padTB = 4;
  var n = history.length;
  var gap = 2;
  var barW = Math.max(1, (w - padL - padR - (n - 1) * gap) / n);

  // Compute ceiling from durations
  var maxSecs = 1;
  history.forEach(function (item) {
    if (item.secs != null && item.secs > maxSecs) maxSecs = item.secs;
  });
  var ceiling = maxSecs * 1.15;

  var svg = '';

  // Avg line
  if (avgSecs != null && avgSecs > 0) {
    var avgY = h - padTB - (avgSecs / ceiling) * (h - 2 * padTB);
    svg +=
      '<line x1="' + padL + '" y1="' + avgY + '" x2="' + (w - 4) + '" y2="' + avgY + '" ' +
      'stroke="var(--text-faintest)" stroke-width="1" stroke-dasharray="4 3" />';
    svg +=
      '<text x="' + (w - 2) + '" y="' + (avgY - 4) + '" text-anchor="end" ' +
      'fill="var(--text-faintest)" font-size="10">avg</text>';
  }

  // Bars
  var self = this;
  history.forEach(function (item, i) {
    var x = padL + i * (barW + gap);
    var secs = item.secs;
    var href = item.id && urlPrefix ? urlPrefix + item.id : null;
    if (secs == null || secs <= 0) {
      // In-progress or no duration — show minimal gray bar
      var stub =
        '<rect x="' + x + '" y="' + (h - padTB - 3) + '" width="' + barW + '" height="3" ' +
        'rx="1" fill="var(--text-faintest)" opacity="0.3" class="pm-bar">' +
        '<title>#' + (item.version || '?') + ' — in progress</title></rect>';
      svg += href ? '<a href="' + href + '">' + stub + '</a>' : stub;
      return;
    }
    var barH = Math.max(2, (secs / ceiling) * (h - 2 * padTB));
    var barY = h - padTB - barH;
    var color = item.error ? '#ef4444' : item.ok ? '#22c55e' : 'var(--text-faintest)';
    var tooltip =
      '#' + (item.version || '?') + ' — ' + self.formatDuration(secs) + (item.error ? ' (error)' : '');
    var bar =
      '<rect x="' + x + '" y="' + barY + '" width="' + barW + '" height="' + barH + '" ' +
      'rx="1" fill="' + color + '" opacity="0.85" class="pm-bar">' +
      '<title>' + tooltip + '</title></rect>';
    svg += href ? '<a href="' + href + '">' + bar + '</a>' : bar;
  });

  svgEl.innerHTML = svg;
};

function initPipelineMetricsPanel() {
  if (isLowDataMode()) return;
  var container = document.querySelector('[data-pipeline-metrics-panel]');
  if (!container) return;
  window.pipelineMetricsPanel = new PipelineMetricsPanel(container);
}

/* ---------- Live Status Mini (Overview tab) ---------- */
function ObservabilityMini(container) {
  this.container = container;
  this.appId = container.getAttribute('data-application-id');
  this.timer = null;
  this._loaded = false;

  // DOM refs
  this.cpuValue = container.querySelector('[data-ls-cpu-value]');
  this.cpuSpark = container.querySelector('[data-ls-cpu-spark]');
  this.memValue = container.querySelector('[data-ls-mem-value]');
  this.memSpark = container.querySelector('[data-ls-mem-spark]');
  this.podCount = container.querySelector('[data-ls-pod-count]');
  this.podDots = container.querySelector('[data-ls-pod-dots]');
}

ObservabilityMini.prototype.activate = function () {
  if (this.timer) return;
  this.container.classList.add('ls-loading');
  this.fetch();
  var self = this;
  this.timer = setInterval(function () {
    self.fetch();
  }, 30000);
};

ObservabilityMini.prototype.deactivate = function () {
  if (this.timer) {
    clearInterval(this.timer);
    this.timer = null;
  }
};

ObservabilityMini.prototype.fetch = function () {
  var self = this;
  var url = '/applications/' + this.appId + '/observability?range=1h';
  fetch(url, { credentials: 'same-origin' })
    .then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    })
    .then(function (data) {
      self.container.classList.remove('ls-loading');
      self._loaded = true;
      self.render(data);
    })
    .catch(function () {
      self.container.classList.remove('ls-loading');
    });
};

ObservabilityMini.prototype.formatBytes = function (bytes) {
  if (bytes == null) return '—';
  if (bytes < 1024) return bytes + 'B';
  if (bytes < 1048576) return (bytes / 1024).toFixed(0) + 'Ki';
  if (bytes < 1073741824) return (bytes / 1048576).toFixed(0) + 'Mi';
  return (bytes / 1073741824).toFixed(1) + 'Gi';
};

ObservabilityMini.prototype.render = function (data) {
  var current = data.current;
  var limits = data.limits;
  if (!current) return;

  // CPU
  var cpuVal = current.cpu_usage_m;
  var cpuLim = limits ? limits.total_cpu_limit_m : 0;
  var cpuPct = cpuLim ? (cpuVal / cpuLim) * 100 : 0;
  if (this.cpuValue) {
    this.cpuValue.textContent = cpuVal != null ? Math.round(cpuVal) + 'm' : '—';
    this.cpuValue.className = 'ls-val' +
      (cpuPct > 90 ? ' ls-val-danger' : cpuPct > 70 ? ' ls-val-warn' : '');
  }

  // Memory
  var memVal = current.memory_usage_bytes;
  var memLim = limits ? limits.total_memory_limit_bytes : 0;
  var memPct = memLim ? (memVal / memLim) * 100 : 0;
  if (this.memValue) {
    this.memValue.textContent = memVal != null ? this.formatBytes(memVal) : '—';
    this.memValue.className = 'ls-val' +
      (memPct > 90 ? ' ls-val-danger' : memPct > 70 ? ' ls-val-warn' : '');
  }

  // Pods
  var podN = current.pod_count || 0;
  if (this.podCount) {
    this.podCount.textContent = podN;
  }
  if (this.podDots) {
    var dotsHtml = '';
    var pods = data.pods || [];
    if (pods.length > 0) {
      pods.forEach(function (pod) {
        var phase = (pod.phase || '').toLowerCase();
        var cls = 'ls-pod-dot';
        if (phase === 'pending') cls += ' ls-pod-dot-warn';
        else if (phase !== 'running') cls += ' ls-pod-dot-err';
        dotsHtml += '<span class="' + cls + '" title="' + (pod.name || '').replace(/"/g, '') + '"></span>';
      });
    } else if (podN > 0) {
      for (var i = 0; i < podN; i++) {
        dotsHtml += '<span class="ls-pod-dot"></span>';
      }
    }
    this.podDots.innerHTML = dotsHtml;
  }

  // Sparklines
  this.renderSparkline(this.cpuSpark, data.history, 'cpu_usage_m', cpuLim, 'oklch(0.7 0.15 230)');
  this.renderSparkline(this.memSpark, data.history, 'memory_usage_bytes', memLim, 'oklch(0.7 0.15 290)');
};

ObservabilityMini.prototype.renderSparkline = function (container, history, key, limit, color) {
  if (!container) return;
  var svg = container.querySelector('svg');
  if (!svg) return;
  if (!history || !history.length) {
    // Show flat baseline when no data
    svg.innerHTML = '<line x1="0" y1="27" x2="120" y2="27" stroke="' + color + '" stroke-opacity="0.15" stroke-width="1"/>';
    container.classList.add('ls-loaded');
    return;
  }

  var values = history.map(function (h) { return h[key] || 0; });
  var max = limit || Math.max.apply(null, values) || 1;
  var w = 120;
  var h = 28;
  var pad = 2;

  var points = [];
  for (var i = 0; i < values.length; i++) {
    var x = (i / Math.max(values.length - 1, 1)) * w;
    var y = pad + (h - pad * 2) - ((values[i] / max) * (h - pad * 2));
    points.push(x.toFixed(1) + ',' + y.toFixed(1));
  }
  var pathD = 'M' + points.join('L');

  // Gradient fill under the line
  var areaPath = pathD + 'L' + w + ',' + h + 'L0,' + h + 'Z';

  var gradId = 'ls-grad-' + key;
  // Baseline at bottom + limit dashed line if applicable
  var baseline = '<line x1="0" y1="' + (h - 1) + '" x2="' + w + '" y2="' + (h - 1) + '" stroke="' + color + '" stroke-opacity="0.15" stroke-width="1"/>';
  svg.innerHTML =
    '<defs><linearGradient id="' + gradId + '" x1="0" y1="0" x2="0" y2="1">' +
    '<stop offset="0%" stop-color="' + color + '" stop-opacity="0.15"/>' +
    '<stop offset="100%" stop-color="' + color + '" stop-opacity="0"/>' +
    '</linearGradient></defs>' +
    baseline +
    '<path d="' + areaPath + '" fill="url(#' + gradId + ')"/>' +
    '<path d="' + pathD + '" fill="none" stroke="' + color + '" stroke-width="1.5" ' +
    'stroke-linecap="round" stroke-linejoin="round" class="ls-spark-path"/>';

  // Animate the line drawing in
  var linePath = svg.querySelector('.ls-spark-path');
  if (linePath) {
    var len = linePath.getTotalLength();
    linePath.style.strokeDasharray = len;
    linePath.style.strokeDashoffset = len;
    linePath.style.setProperty('--ls-path-len', len);
    linePath.style.animation = 'ls-spark-draw 0.8s ease-out forwards';
  }

  container.classList.add('ls-loaded');
};

function initLiveStatus() {
  if (isLowDataMode()) return;
  var container = document.querySelector('[data-live-status]');
  if (!container) return;
  var mini = new ObservabilityMini(container);

  // Activate when overview tab is visible
  var panel = container.closest('[data-tab-panel]');
  if (panel) {
    panel.addEventListener('tab-activated', function () { mini.activate(); });
    panel.addEventListener('tab-deactivated', function () { mini.deactivate(); });
    if (panel.classList.contains('tab-panel-active')) {
      mini.activate();
    }
  }
}

/* ---------- Build/Release Detail Page ---------- */
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
  var progressBanner = document.querySelector('.build-progress-banner');
  var protocol = (window.location.protocol === 'https:') ? 'wss://' : 'ws://';
  var wsUrl = opts.wsUrl || (window.location.pathname + '/livelogs');
  var socket = new WebSocket(protocol + window.location.host + wsUrl);
  socket.addEventListener('message', function(ev) {
    if (ev.data === '=================END OF LOGS=================') {
      logsFinished = true;
      if (tracker) {
        tracker.complete();
        if (tracker.errored && progressBanner) progressBanner.classList.add('deploy-progress-banner-error');
      }
      socket.close();
      return;
    }
    logsPre.textContent += ev.data + '\n';
    logsPre.scrollTop = logsPre.scrollHeight;
    if (tracker) tracker.processLine(ev.data);
  });
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
        if (opts.isAutoDeploy) pollForNextStep(opts.appId, opts.stage);
      })
      .catch(function() {
        if (statusPollAttempts < maxStatusPolls) setTimeout(fetchBuildStatus, 2000);
      });
  }
  socket.addEventListener('close', function() {
    if (logsFinished) {
      fetchBuildStatus();
    } else {
      if (tracker) tracker.setPhase('Waiting for build logs\u2026');
      setTimeout(function() { window.location.reload(); }, 10000);
    }
  });
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

/* ---------- Deploy Detail Page ---------- */
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
  var protocol = (window.location.protocol === 'https:') ? 'wss://' : 'ws://';
  var wsUrl = protocol + window.location.host + window.location.pathname + '/livelogs';
  function connectWebSocket() {
    var socket = new WebSocket(wsUrl);
    socket.addEventListener('message', function(ev) {
      if (ev.data === '=================END OF LOGS=================') {
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
      logsPre.textContent += ev.data + '\n';
      logsPre.scrollTop = logsPre.scrollHeight;
      if (tracker) tracker.processLine(ev.data);
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

/* ---------- App Logs Page ---------- */
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

/* ---------- App Shell Page ---------- */
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

/* ---------- Init All ---------- */
/* ---------- Compact Topbar (scroll collapse) ---------- */
function initCompactTopbar() {
  var topbar = document.querySelector('[data-topbar]');
  var tabBarWrapper = document.querySelector('[data-tab-bar-wrapper]');
  var inlineTabs = document.querySelector('[data-inline-tabs]');
  var tabBar = document.querySelector('[data-tabs]');

  if (!topbar || !tabBarWrapper || !inlineTabs || !tabBar) return;

  // Clone tab items into the inline container
  var sourceTabs = tabBar.querySelectorAll('[data-tab]');
  sourceTabs.forEach(function (tab) {
    var clone = document.createElement('button');
    clone.className = 'topbar-inline-tab';
    clone.setAttribute('data-inline-tab', tab.getAttribute('data-tab'));
    if (tab.classList.contains('tab-active')) {
      clone.classList.add('tab-active');
    }
    // Copy icon SVG + text
    var svg = tab.querySelector('svg');
    if (svg) clone.appendChild(svg.cloneNode(true));
    // Get text content (label only, not badge)
    var label = tab.childNodes;
    for (var i = 0; i < label.length; i++) {
      if (label[i].nodeType === 3 && label[i].textContent.trim()) {
        clone.appendChild(document.createTextNode(label[i].textContent.trim()));
        break;
      }
    }
    // Copy badge if present
    var badge = tab.querySelector('.badge');
    if (badge) clone.appendChild(badge.cloneNode(true));
    inlineTabs.appendChild(clone);
  });

  // Click handler: sync with real tabs
  inlineTabs.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-inline-tab]');
    if (!btn) return;
    var tabId = btn.getAttribute('data-inline-tab');
    var realTab = tabBar.querySelector('[data-tab="' + tabId + '"]');
    if (realTab) realTab.click();
  });

  // Keep inline tabs in sync when real tabs change
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

  // Compact mode: "always" (user pref) or scroll-based
  var THRESHOLD = 20;
  var isCompact = false;
  var compactPref = localStorage.getItem('compact-mode') === 'true';

  // Remove pre-paint class so transitions work after hydration
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

  // Toggle handlers
  var toggles = document.querySelectorAll('.compact-mode-toggle');
  toggles.forEach(function (toggle) {
    toggle.checked = compactPref;
    toggle.addEventListener('change', function () {
      compactPref = toggle.checked;
      localStorage.setItem('compact-mode', compactPref);
      // Sync all toggles
      toggles.forEach(function (t) {
        t.checked = compactPref;
      });
      checkScroll();
    });
  });
}

/* ---------- Preference Toggles (reduce motion, pipeline toasts, low data) ---------- */
function initPreferenceToggles() {
  // Reduce motion
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

  // Pipeline toasts (default on)
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

  // Low data mode (disables WebSocket polling)
  var dataToggles = document.querySelectorAll('.low-data-toggle');
  var dataPref = localStorage.getItem('low-data-mode') === 'true';
  dataToggles.forEach(function (toggle) {
    toggle.checked = dataPref;
    toggle.addEventListener('change', function () {
      dataPref = toggle.checked;
      localStorage.setItem('low-data-mode', dataPref);
      dataToggles.forEach(function (t) { t.checked = dataPref; });
      // Stop or restart pollers on toggle
      if (dataPref) {
        stopAllPollers();
      } else {
        window.location.reload();
      }
    });
  });
}

/* Delegated handler: [data-href] spans open links in new tabs (avoids nested <a>) */
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
  initPipelineTracker();
  initDashboardPoller();
  initObservabilityPanel();
  initPipelineMetricsPanel();
  initLiveStatus();
  autoExpandCollapsibleCards();
  syncDetailLogHeight();
  window.addEventListener('resize', function () {
    autoExpandCollapsibleCards();
    syncDetailLogHeight();
  });
});
