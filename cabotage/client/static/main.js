/* ===== Cabotage PaaS - Vanilla JS ===== */

/**
 * Slugify a string for URL-safe slugs.
 */
function slugify(text) {
  return text.toString().toLowerCase()
    .replace(/\s+/g, '-')
    .replace(/[^\w\-]+/g, '')
    .replace(/\-\-+/g, '-')
    .replace(/^-+/, '')
    .replace(/-+$/, '')
    .replace(/[\s_-]+/g, '-');
}

/**
 * Auto-slugify: as user types in source field, destination field gets slugified value.
 * Stops auto-slugifying once user manually edits the destination field.
 */
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

/**
 * Increment / Decrement buttons for process count inputs.
 */
document.addEventListener('DOMContentLoaded', function () {
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
        if (oldValue > 0) {
          input.value = oldValue - 1;
        } else {
          input.value = 0;
          button.classList.add('inactive');
        }
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

  // Close dropdown on click outside (DaisyUI dropdowns)
  document.addEventListener('click', function (e) {
    if (!e.target.closest('.dropdown')) {
      document.querySelectorAll('.dropdown [tabindex]').forEach(function (el) {
        el.blur();
      });
    }
  });
});
