/* feed.js — backfill progress widget for the feed page.
   hiq3: simplified — show/hide #backfill-progress with spinner, no region/page/lots detail.
*/
(function () {
  'use strict';

  var _timer = null;
  var _widget = document.getElementById('backfill-progress');

  if (!_widget) return;

  function _update(data) {
    if (data.status === 'running') {
      _widget.style.display = '';
    } else {
      _widget.style.display = 'none';
      if (_timer !== null) {
        clearInterval(_timer);
        _timer = null;
      }
    }
  }

  function _poll() {
    fetch('/backfill/status', { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) { if (data) _update(data); })
      .catch(function () { /* silently ignore network errors */ });
  }

  // Initial fetch on page load; start polling only if running.
  fetch('/backfill/status', { credentials: 'same-origin' })
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (data) {
      if (!data) return;
      _update(data);
      if (data.status === 'running') {
        _timer = setInterval(_poll, 4000);
      }
    })
    .catch(function () { /* ignore */ });
}());
