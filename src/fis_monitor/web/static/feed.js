/* feed.js — backfill progress widget for the feed page. */
(function () {
  'use strict';

  var _timer = null;
  var _widget = document.getElementById('backfill-progress');
  var _elRegion = document.getElementById('backfill-region');
  var _elPage   = document.getElementById('backfill-page');
  var _elLots   = document.getElementById('backfill-lots');

  if (!_widget || !_elRegion || !_elPage || !_elLots) return;

  function _update(data) {
    if (data.status === 'running') {
      _widget.style.display = '';
      _elRegion.textContent = data.current_region != null
        ? '· регион ' + data.current_region
        : '';
      _elPage.textContent = data.current_page != null
        ? '· стр. ' + data.current_page + (data.total_pages_seen > 0 ? ' (' + data.total_pages_seen + ' всего)' : '')
        : '';
      _elLots.textContent = data.lots_seen > 0
        ? '· ' + data.lots_seen.toLocaleString('ru') + ' лотов'
        : '';
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
