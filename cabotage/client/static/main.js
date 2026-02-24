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
    panels.forEach(function(p) {
      p.classList.toggle('tab-panel-active', p.getAttribute('data-tab-panel') === tabId);
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

/* ---------- Theme Toggle (3-state: light → dark → system) ---------- */
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
      meta.content = resolved === 'light' ? '#fafafe' : '#0f0f17';
    }
  }

  function cyclePref() {
    var current = localStorage.getItem('theme-pref') || 'system';
    var next = current === 'light' ? 'dark' : current === 'dark' ? 'system' : 'light';
    applyPref(next);
  }

  // Bind all toggle buttons
  document.querySelectorAll('#theme-toggle, #theme-toggle-unauth').forEach(function(btn) {
    btn.addEventListener('click', cyclePref);
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

/* ---------- Init All ---------- */
document.addEventListener('DOMContentLoaded', function() {
  initTabs();
  initCountInputs();
  initEnvReveal();
  initDropdowns();
  initMobileNav();
  initThemeToggle();
  initRawEditor();
  initAddVarModal();
});
