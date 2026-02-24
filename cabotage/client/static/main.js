/* ===== Cabotage PaaS - Vanilla JS ===== */

/* ---------- Slugify ---------- */
function slugify(text) {
  return text.toString().toLowerCase()
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
    tabs.forEach(function(t) {
      t.classList.toggle('tab-active', t.getAttribute('data-tab') === tabId);
    });

    // Emit lifecycle events before toggling visibility
    panels.forEach(function(p) {
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

  tabs.forEach(function(tab) {
    tab.addEventListener('click', function(e) {
      e.preventDefault();
      activateTab(tab.getAttribute('data-tab'));
    });
  });

  // Activate from URL hash or default to first tab
  var hash = window.location.hash.replace('#', '');
  var validTab = false;
  tabs.forEach(function(t) {
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
  document.querySelectorAll('.incr-btn').forEach(function(button) {
    button.addEventListener('click', function(e) {
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
      document.querySelectorAll('.update_process_settings').forEach(function(el) {
        el.classList.remove('hidden');
      });
    });
  });

  // Pod size change handler
  document.querySelectorAll('.pod-size').forEach(function(select) {
    select.addEventListener('change', function() {
      document.querySelectorAll('.update_process_settings').forEach(function(el) {
        el.classList.remove('hidden');
      });
    });
  });
}

/* ---------- Env Var Reveal ---------- */
function initEnvReveal() {
  document.querySelectorAll('[data-reveal]').forEach(function(btn) {
    btn.addEventListener('click', function() {
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
  document.addEventListener('click', function(e) {
    if (!e.target.closest('.dropdown')) {
      document.querySelectorAll('.dropdown [tabindex]').forEach(function(el) {
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
    toggle.addEventListener('click', function() {
      menu.classList.toggle('hidden');
    });
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
      meta.content = resolved === 'light' ? '#fafafe' : resolved === 'terminal' ? '#0a0a0a' : '#0f0f17';
    }
    // When entering terminal, auto-switch accent to white
    var accent = localStorage.getItem('accent-color') || 'purple';
    if (resolved === 'terminal' && accent !== 'white' && accent !== 'dark') {
      accent = 'white';
      localStorage.setItem('accent-color', accent);
      document.documentElement.setAttribute('data-accent', accent);
      // Update swatch highlight if accent picker is initialized
      document.querySelectorAll('.accent-opt').forEach(function(b) {
        b.style.borderColor = b.getAttribute('data-accent') === accent ? 'var(--color-base-content)' : 'transparent';
      });
    }
    if (window.__applyAccent) window.__applyAccent(accent, resolved);
  }

  // Click cycles light→dark→system; hover reveals dropdown
  var cycleThemes = ['light', 'dark', 'system'];

  document.querySelectorAll('.theme-toggle-wrap').forEach(function(wrap) {
    var btn = wrap.querySelector('button');
    var dropdown = wrap.querySelector('.theme-dropdown');
    var hideTimer = null;

    function show() {
      clearTimeout(hideTimer);
      dropdown.classList.remove('hidden');
    }
    function hide() { dropdown.classList.add('hidden'); }
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
    btn.addEventListener('click', function(e) {
      e.stopPropagation();
      hide();
      cycleTheme();
    });

    // Hover opens dropdown (with small delay on leave to allow moving to it)
    wrap.addEventListener('mouseenter', show);
    wrap.addEventListener('mouseleave', hideDelayed);

    // Close on click outside
    document.addEventListener('click', function(e) {
      if (!wrap.contains(e.target)) {
        hide();
      }
    });

    // Theme option clicks
    dropdown.querySelectorAll('.theme-opt').forEach(function(opt) {
      opt.addEventListener('click', function(e) {
        e.stopPropagation();
        applyPref(opt.getAttribute('data-theme-val'));
        hide();
      });
    });
  });

  // Listen for system theme changes (update resolved theme when in system mode)
  window.matchMedia('(prefers-color-scheme: light)').addEventListener('change', function() {
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
    document.querySelectorAll('.accent-opt').forEach(function(btn) {
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
  document.querySelectorAll('.accent-opt').forEach(function(opt) {
    opt.addEventListener('click', function(e) {
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
  document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape' && modal.style.display !== 'none') {
      closeModal();
    }
  });

  // Tab switching
  tabs.forEach(function(tab) {
    tab.addEventListener('click', function() {
      var tabId = tab.getAttribute('data-editor-tab');
      tabs.forEach(function(t) {
        t.classList.toggle('raw-editor-tab-active', t.getAttribute('data-editor-tab') === tabId);
      });
      panels.forEach(function(p) {
        p.style.display = p.getAttribute('data-editor-panel') === tabId ? '' : 'none';
      });
      if (formatInput) formatInput.value = tabId;

      // Update placeholder
      if (textarea) {
        if (tabId === 'json') {
          textarea.placeholder = '{\n  "DATABASE_URL": "postgres://...",\n  "REDIS_URL": "redis://..."\n}';
        } else {
          textarea.placeholder = '# Paste your environment variables here\nDATABASE_URL=postgres://...\nREDIS_URL=redis://...';
        }
      }
    });
  });

  // Copy ENV button
  if (copyBtn) {
    copyBtn.addEventListener('click', function() {
      var dataEl = document.getElementById('env-export-data');
      if (!dataEl) return;
      try {
        var configs = JSON.parse(dataEl.textContent);
        var lines = configs.map(function(c) {
          if (c.secret) return c.name + '=**secure**';
          return c.name + '=' + c.value;
        });
        var text = lines.join('\n');
        navigator.clipboard.writeText(text).then(function() {
          var orig = copyBtn.innerHTML;
          copyBtn.textContent = 'Copied!';
          setTimeout(function() { copyBtn.innerHTML = orig; }, 1500);
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
    if (nameInput) { nameInput.value = ''; nameInput.focus(); }
    var valueInput = modal.querySelector('input[name="value"]');
    if (valueInput) valueInput.value = '';
  }
  function closeModal() {
    modal.style.display = 'none';
  }

  // Open buttons
  document.querySelectorAll('#add-var-open, [data-add-var-open]').forEach(function(btn) {
    btn.addEventListener('click', openModal);
  });

  // Close buttons/backdrop
  modal.querySelectorAll('[data-add-var-close]').forEach(function(el) {
    el.addEventListener('click', closeModal);
  });

  // Escape key
  document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape' && modal.style.display !== 'none') {
      closeModal();
    }
  });

  // Auto-uppercase name field
  var nameField = modal.querySelector('input[name="name"]');
  if (nameField) {
    nameField.addEventListener('input', function() {
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

  document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape' && !modal.classList.contains('hidden')) {
      closeModal();
    }
  });

  if (copyBtn) {
    copyBtn.addEventListener('click', function() {
      var text = bodyEl.textContent;
      navigator.clipboard.writeText(text).then(function() {
        var orig = copyBtn.innerHTML;
        copyBtn.textContent = 'Copied!';
        setTimeout(function() { copyBtn.innerHTML = orig; }, 1500);
      });
    });
  }

  // Bind all expand buttons
  document.querySelectorAll('[data-expand]').forEach(function(btn) {
    btn.addEventListener('click', function() {
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
function BuildProgressTracker(barFill, phaseLabel, type, stepsContainer, elapsedEl) {
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
  this.startTime = Date.now();
  this.timerInterval = null;
  this.errored = false;
  this.errorStepIdx = -1;

  // Define step pipelines
  if (this.type === 'deploy') {
    this.steps = [
      { id: 'setup', label: 'Setup', patterns: [/Constructing API Clients/i], progress: 5 },
      { id: 'namespace', label: 'Namespace', patterns: [/Fetching Namespace/i], progress: 10 },
      { id: 'account', label: 'Account', patterns: [/Fetching ServiceAccount/i, /Patching ServiceAccount/i], progress: 20 },
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

BuildProgressTracker.prototype.renderSteps = function() {
  if (!this.stepsContainer) return;
  this.stepsContainer.innerHTML = '';
  var checkSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';
  for (var i = 0; i < this.steps.length; i++) {
    var step = this.steps[i];
    var el = document.createElement('div');
    el.className = 'progress-step';
    el.setAttribute('data-step', step.id);
    el.innerHTML =
      '<div class="step-dot">' + checkSvg + '<div class="step-dot-spinner"></div></div>' +
      '<span class="step-label">' + step.label + '</span>' +
      (step.substep ? '<span class="step-substep" data-substep></span>' : '');
    this.stepsContainer.appendChild(el);
  }
  this.stepEls = this.stepsContainer.querySelectorAll('.progress-step');
};

BuildProgressTracker.prototype.startTimer = function() {
  if (!this.elapsedEl) return;
  var self = this;
  this.timerInterval = setInterval(function() {
    var elapsed = Math.floor((Date.now() - self.startTime) / 1000);
    var min = Math.floor(elapsed / 60);
    var sec = elapsed % 60;
    self.elapsedEl.textContent = (min < 10 ? '0' : '') + min + ':' + (sec < 10 ? '0' : '') + sec;
  }, 1000);
};

BuildProgressTracker.prototype.stopTimer = function() {
  if (this.timerInterval) {
    clearInterval(this.timerInterval);
    this.timerInterval = null;
  }
};

BuildProgressTracker.prototype.setStep = function(idx) {
  if (idx <= this.currentStepIdx) return;
  this.currentStepIdx = idx;
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

BuildProgressTracker.prototype.activate = function() {
  if (this.activated) return;
  this.activated = true;
  this.barFill.classList.add('build-progress-bar-determinate');
  this.barFill.style.width = '0%';
};

BuildProgressTracker.prototype.setProgress = function(pct) {
  if (pct <= this.progress) return;
  this.progress = pct;
  this.barFill.style.width = Math.min(pct, 100) + '%';
};

BuildProgressTracker.prototype.setPhase = function(text) {
  if (this.phaseLabel) {
    this.phaseLabel.textContent = text;
  }
};

BuildProgressTracker.prototype.processLine = function(line) {
  if (this.type === 'deploy') {
    this.processDeployLine(line);
  } else {
    this.processBuildLine(line);
  }
};

BuildProgressTracker.prototype.processBuildLine = function(line) {
  // Detect error/failure patterns in build logs
  if (/error|failed|failure|exception|traceback/i.test(line) &&
      !/no error/i.test(line) && !/warning/i.test(line)) {
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

BuildProgressTracker.prototype.processDeployLine = function(line) {
  // Detect error/failure patterns in deploy logs
  if (/error|failed|failure|exception|traceback|timed?\s*out/i.test(line) &&
      !/no error/i.test(line)) {
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

BuildProgressTracker.prototype.complete = function() {
  this.activate();
  this.stopTimer();

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
        // Steps after failedAt remain in default (pending) state
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
    }
  }
};

/* ---------- Auto-deploy next-step polling ---------- */
/* Reload the current page after a short delay; the server will render
   with next_step_url populated once Celery creates the next object.
   On reload, the banner JS picks up next_step_url and shows the link. */
function pollForNextStep(currentUrl) {
  var attempts = 0;
  var maxAttempts = 12; // ~30s total
  (function poll() {
    attempts++;
    setTimeout(function() {
      /* Reload the page; if next_step_url is set, the banner will appear.
         We use a fetch to check without navigating, then redirect. */
      fetch(currentUrl, { headers: { 'Accept': 'text/html' } })
        .then(function() {
          window.location.reload();
        })
        .catch(function() {
          if (attempts < maxAttempts) poll();
          else window.location.reload();
        });
    }, 2500);
  })();
}

/* ---------- Pipeline Tracker (Overview Page) ---------- */
function PipelineTracker(container) {
  this.container = container;
  this.appId = container.getAttribute('data-application-id');
  this.statusUrl = '/applications/' + this.appId + '/pipeline_status';
  this.pollInterval = null;
  this.bannersEl = container.querySelector('[data-pipeline-banners]');
  this.progressEl = container.querySelector('[data-pipeline-progress]');
  this.segments = {
    build: container.querySelector('[data-segment="build"]'),
    release: container.querySelector('[data-segment="release"]'),
    deploy: container.querySelector('[data-segment="deploy"]'),
  };
  this.settled = false;

  // Live commit indicator state
  this.commitEl = document.getElementById('liveCommitStatus');
  this.currentCommit = this.commitEl ? this.commitEl.getAttribute('data-commit-sha') : null;
  this.githubRepo = this.commitEl ? this.commitEl.getAttribute('data-github-repo') : null;
  this.commitLastChanged = Date.now();

  // Check initial state and start polling if active
  this.poll();
}

PipelineTracker.prototype.poll = function() {
  var self = this;
  fetch(this.statusUrl, { credentials: 'same-origin' })
    .then(function(r) {
      if (!r.ok) throw new Error('pipeline_status ' + r.status);
      return r.json();
    })
    .then(function(data) { self.update(data); })
    .catch(function(err) { console.warn('[PipelineTracker]', err); });
};

PipelineTracker.prototype.startPolling = function() {
  if (this.pollInterval) return;
  var self = this;
  this.pollInterval = setInterval(function() { self.poll(); }, 3000);
};

PipelineTracker.prototype.stopPolling = function() {
  if (this.pollInterval) {
    clearInterval(this.pollInterval);
    this.pollInterval = null;
  }
};

PipelineTracker.prototype.update = function(data) {
  if (!data) return;

  if (data.pipeline_active) {
    this.showProgress();
    this.startPolling();
  }

  this.updateSegment('build', data.build);
  this.updateSegment('release', data.release);
  this.updateSegment('deploy', data.deploy);

  // Update live commit indicator
  this.updateCommitIndicator(data);

  // Pipeline just finished — redirect to the relevant stage
  if (!data.pipeline_active && !this.settled && this.pollInterval) {
    this.settled = true;
    this.stopPolling();
    var target = null;
    // If any stage errored, redirect to the earliest errored stage
    // (later stages may be stale data from a previous successful run)
    if (data.build && data.build.status === 'error' && data.build.id) {
      target = '/image/' + data.build.id;
    } else if (data.release && data.release.status === 'error' && data.release.id) {
      target = '/release/' + data.release.id;
    } else if (data.deploy && data.deploy.status === 'error' && data.deploy.id) {
      target = '/deployment/' + data.deploy.id;
    }
    // No errors — redirect to the furthest completed stage
    if (!target) {
      if (data.deploy && data.deploy.status === 'complete' && data.deploy.id) {
        target = '/deployment/' + data.deploy.id;
      } else if (data.release && data.release.status === 'complete' && data.release.id) {
        target = '/release/' + data.release.id;
      } else if (data.build && data.build.status === 'complete' && data.build.id) {
        target = '/image/' + data.build.id;
      }
    }
    setTimeout(function() {
      if (target) {
        window.location.href = target;
      } else {
        window.location.reload();
      }
    }, 2000);
  }
};

PipelineTracker.prototype.showProgress = function() {
  if (this.bannersEl) this.bannersEl.style.display = 'none';
  if (this.progressEl) this.progressEl.style.display = '';
};

PipelineTracker.prototype.updateSegment = function(name, info) {
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
    if (fill) { fill.style.width = '100%'; fill.className = 'pipe-seg-fill pipe-seg-fill-success'; }
  } else if (info.status === 'error') {
    seg.className = 'pipe-segment pipe-seg-error';
    if (dot) dot.className = 'pipe-seg-dot pipe-seg-dot-error';
    if (label) label.textContent = info.error_detail ? 'Failed' : 'Error';
    if (fill) { fill.style.width = '100%'; fill.className = 'pipe-seg-fill pipe-seg-fill-error'; }
  } else if (info.status === 'in_progress') {
    seg.className = 'pipe-segment pipe-seg-active';
    if (dot) dot.className = 'pipe-seg-dot pipe-seg-dot-active';
    if (label) label.textContent = name === 'build' ? 'Building' : name === 'release' ? 'Packaging' : 'Deploying';
    if (fill) { fill.className = 'pipe-seg-fill pipe-seg-fill-active'; }
  }

  // Update detail link
  if (link && info.id) {
    var base = name === 'build' ? '/image/' : name === 'release' ? '/release/' : '/deployment/';
    link.href = base + info.id;
  }
};

PipelineTracker.prototype.updateCommitIndicator = function(data) {
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
      shaHtml = '<a href="https://github.com/' + repo + '/commit/' + sha + '"'
        + ' target="_blank" rel="noopener"'
        + ' class="live-commit-sha" title="' + sha + '">' + shortSha + '</a>';
    } else {
      shaHtml = '<code class="live-commit-sha" title="' + sha + '">' + shortSha + '</code>';
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

  this.commitEl.className = 'live-commit' + freshClass;
  this.commitEl.innerHTML = '<span class="' + dotClass + '"></span>' + shaHtml;

  // Remove flash class after animation
  if (freshClass) {
    var el = this.commitEl;
    setTimeout(function() { el.classList.remove('live-commit-fresh'); }, 1500);
  }
};

function initPipelineTracker() {
  var container = document.querySelector('[data-pipeline-tracker]');
  if (container) {
    window.pipelineTracker = new PipelineTracker(container);
  }

  // Intercept the Deploy button — submit via fetch, stay on page, start tracking
  var deployForm = document.querySelector('[data-full-deploy-form]');
  if (deployForm && container) {
    deployForm.addEventListener('submit', function(e) {
      e.preventDefault();
      var btn = deployForm.querySelector('button[type="submit"]');
      if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<span class="loading loading-spinner loading-xs"></span> Deploying\u2026';
      }
      // Fire the POST in the background
      fetch(deployForm.action, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: new URLSearchParams(new FormData(deployForm)),
        redirect: 'follow',
      })
        .then(function() {
          // POST succeeded (server redirected, we don't follow) — start polling
          if (window.pipelineTracker) {
            window.pipelineTracker.showProgress();
            window.pipelineTracker.startPolling();
          }
        })
        .catch(function() {
          // On error, fall back to normal form submission
          deployForm.submit();
        });
    });
  }
}

/* ---------- Pipeline Toast Notifications ---------- */

function showPipelineToast(pipeline) {
  var container = document.getElementById('pipeline-toasts');
  if (!container) return;

  var stageLabel = '';
  if (pipeline.stages.deploy) stageLabel = 'Deploying';
  else if (pipeline.stages.release) stageLabel = 'Packaging';
  else if (pipeline.stages.build) stageLabel = 'Building';
  else stageLabel = 'Pipeline running';

  var href = '/projects/' + pipeline.org_slug + '/' + pipeline.project_slug
    + '/applications/' + pipeline.app_slug;

  var toast = document.createElement('a');
  toast.href = href;
  toast.className = 'pipeline-toast';
  toast.setAttribute('data-toast-app', pipeline.app_id);
  toast.innerHTML = '<span class="pipeline-toast-dot"></span>'
    + '<span><strong>' + pipeline.app_name + '</strong> '
    + '<span class="text-base-content/50">' + stageLabel + '</span></span>'
    + '<span class="pipeline-toast-dismiss" title="Dismiss">'
    + '<svg class="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">'
    + '<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg></span>';

  // Dismiss on X click (don't navigate)
  toast.querySelector('.pipeline-toast-dismiss').addEventListener('click', function(e) {
    e.preventDefault();
    e.stopPropagation();
    dismissToast(toast);
  });

  container.appendChild(toast);

  // Auto-dismiss after 15 seconds
  setTimeout(function() { dismissToast(toast); }, 15000);
}

function dismissToast(toast) {
  if (!toast || !toast.parentNode) return;
  toast.classList.add('toast-out');
  toast.addEventListener('animationend', function() { toast.remove(); });
}

function DashboardPipelinePoller() {
  this.knownActive = {};
  this.pollInterval = null;
  this.poll();
  this.startPolling();
}

DashboardPipelinePoller.prototype.startPolling = function() {
  var self = this;
  this.pollInterval = setInterval(function() { self.poll(); }, 5000);
};

DashboardPipelinePoller.prototype.poll = function() {
  var self = this;
  fetch('/active_pipelines', { credentials: 'same-origin' })
    .then(function(r) {
      if (!r.ok) throw new Error('active_pipelines ' + r.status);
      return r.json();
    })
    .then(function(data) { self.update(data.pipelines); })
    .catch(function(err) { console.warn('[DashboardPoller]', err); });
};

DashboardPipelinePoller.prototype.update = function(pipelines) {
  var nowActive = {};
  for (var i = 0; i < pipelines.length; i++) {
    var p = pipelines[i];
    nowActive[p.app_id] = p;
    if (!this.knownActive[p.app_id]) {
      showPipelineToast(p);
    }
  }
  this.knownActive = nowActive;
};

function initDashboardPoller() {
  // Only run on the dashboard (home) page when authenticated
  var toastContainer = document.getElementById('pipeline-toasts');
  var isDashboard = document.querySelector('[data-page="dashboard"]');
  if (toastContainer && isDashboard) {
    window.dashboardPoller = new DashboardPipelinePoller();
  }
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
  sourceTabs.forEach(function(tab) {
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
  inlineTabs.addEventListener('click', function(e) {
    var btn = e.target.closest('[data-inline-tab]');
    if (!btn) return;
    var tabId = btn.getAttribute('data-inline-tab');
    var realTab = tabBar.querySelector('[data-tab="' + tabId + '"]');
    if (realTab) realTab.click();
  });

  // Keep inline tabs in sync when real tabs change
  var observer = new MutationObserver(function() {
    sourceTabs.forEach(function(tab) {
      var id = tab.getAttribute('data-tab');
      var inline = inlineTabs.querySelector('[data-inline-tab="' + id + '"]');
      if (inline) {
        inline.classList.toggle('tab-active', tab.classList.contains('tab-active'));
      }
    });
  });
  sourceTabs.forEach(function(tab) {
    observer.observe(tab, { attributes: true, attributeFilter: ['class'] });
  });

  // Compact mode: "always" (user pref) or scroll-based
  var THRESHOLD = 20;
  var isCompact = false;
  var compactPref = localStorage.getItem('compact-mode') === 'true';

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
  toggles.forEach(function(toggle) {
    toggle.checked = compactPref;
    toggle.addEventListener('change', function() {
      compactPref = toggle.checked;
      localStorage.setItem('compact-mode', compactPref);
      // Sync all toggles
      toggles.forEach(function(t) { t.checked = compactPref; });
      checkScroll();
    });
  });
}

document.addEventListener('DOMContentLoaded', function() {
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
  initPipelineTracker();
  initDashboardPoller();
  autoExpandCollapsibleCards();
  syncDetailLogHeight();
  window.addEventListener('resize', function() {
    autoExpandCollapsibleCards();
    syncDetailLogHeight();
  });
});
