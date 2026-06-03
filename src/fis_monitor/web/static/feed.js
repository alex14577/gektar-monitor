/* feed.js — 3-state backfill machine for the feed page (6jg).
   States: idle | running | done
   coldStart = registry was empty (data-registry-count="0") on page load.
   Drives: #registry-count live update + .feed-scope__notice visibility + toast once.
*/
(function () {
  'use strict';

  var _scope = document.querySelector('.feed-scope');
  if (!_scope) return;

  var _notice = _scope.querySelector('.feed-scope__notice');

  // coldStart: registry was empty when page was rendered
  var coldStart = (_scope.dataset.registryCount === '0');

  var _prevStatus = null;
  var _toastFired = false;
  var _timer = null;

  function _updateRegistry(count) {
    var el = document.getElementById('registry-count');
    if (!el) return;
    el.dataset.count = String(count);
    el.textContent = String(count);
  }

  function _update(data) {
    var status = data.status;
    var count = data.active_lot_count;

    if (typeof count === 'number') {
      _updateRegistry(count);
    }

    if (coldStart) {
      if (status === 'running') {
        if (_notice) _notice.removeAttribute('hidden');
      } else if (_prevStatus === 'running' && (status === 'done' || status === 'idle')) {
        if (!_toastFired && window.Monitor && window.Monitor.toast) {
          window.Monitor.toast('Каталог обновлён');
          _toastFired = true;
        }
        if (_notice) _notice.setAttribute('hidden', '');
        coldStart = false;
      }
    }

    _prevStatus = status;

    // Poll while cold-start is unresolved (to catch idle->running->done) OR while a routine
    // backfill is running (to keep X live). Stop otherwise.
    var shouldPoll = coldStart || status === 'running';
    if (shouldPoll && _timer === null) {
      _timer = setInterval(_poll, 4000);
    } else if (!shouldPoll && _timer !== null) {
      clearInterval(_timer);
      _timer = null;
    }
  }

  function _poll() {
    fetch('/backfill/status', { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) { if (data) _update(data); })
      .catch(function () { /* silently ignore network errors */ });
  }

  // Initial fetch: set registry count + decide notice visibility + start timer via _update
  fetch('/backfill/status', { credentials: 'same-origin' })
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (data) {
      if (!data) return;
      _update(data);
    })
    .catch(function () { /* ignore */ });
}());
