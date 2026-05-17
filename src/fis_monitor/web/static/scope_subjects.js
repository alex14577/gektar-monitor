/* scope_subjects.js — scope-and-subjects partial behaviour.
 *
 * 1. Toast on save: reads optional data island #scope-saved-data
 *    (type="application/json") to learn which group was saved.
 * 2. Disables "Сохранить" button for regions when no checkboxes are checked
 *    (at least one region required; ADR-035 I4: empty subjects = notify-all).
 */
(function () {
  'use strict';

  // --- 1. Toast on save ---
  var savedIsland = document.getElementById('scope-saved-data');
  if (savedIsland) {
    try {
      var saved = JSON.parse(savedIsland.textContent);
      var label = saved === 'subjects' ? 'Субъекты сохранены' : 'Округа сохранены';
      if (window.Monitor && window.Monitor.toast) {
        window.Monitor.toast(label, { timeout: 3000 });
      }
    } catch (e) { /* malformed JSON — ignore */ }
  }

  // --- 2. Disable Save button for regions when zero checkboxes checked ---
  function syncDisabled(group) {
    var boxes = document.querySelectorAll(
      'input[data-scope-checkbox="' + group + '"]'
    );
    var btn = document.querySelector(
      'button[data-scope-submit="' + group + '"]'
    );
    if (!btn) return;
    if (group === 'subjects') {
      // Empty notify-scope is valid — always allow save.
      btn.disabled = false;
      return;
    }
    var any = false;
    boxes.forEach(function (b) { if (b.checked) any = true; });
    btn.disabled = !any;
  }

  ['regions', 'subjects'].forEach(function (g) {
    syncDisabled(g);
    document
      .querySelectorAll('input[data-scope-checkbox="' + g + '"]')
      .forEach(function (b) {
        b.addEventListener('change', function () { syncDisabled(g); });
      });
  });
}());
