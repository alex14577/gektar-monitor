/**
 * auth.js — ЕСИА login button handler (Variant B, bd gektar_monitor-oem).
 *
 * Delegates click events from [data-action="login-start"] buttons.
 * Calls POST /auth/start, then polls GET /auth/status every 2 s.
 * Toast/announce helpers are provided by app.js via window.Monitor.
 *
 * No ES modules — loaded with <script defer>.
 * Compatible with ES2015 environments (no top-level await, no optional chaining).
 */
(function () {
  'use strict';

  var POLL_INTERVAL_MS = 2000;
  var HARD_TIMEOUT_MS = 300000; // 5 minutes
  var RATE_LIMIT_DISABLE_MS = 60000; // 60 s after 429

  var _pollTimer = null;
  var _pollDeadline = null;

  // ---------------------------------------------------------------------------
  // Helpers
  // ---------------------------------------------------------------------------

  function _toast(msg) {
    if (window.Monitor && typeof window.Monitor.toast === 'function') {
      window.Monitor.toast(msg);
    }
  }

  function _announce(msg, assertive) {
    if (window.Monitor && typeof window.Monitor.announce === 'function') {
      window.Monitor.announce(msg, !!assertive);
    }
  }

  function _setButtonState(btn, disabled, text) {
    if (!btn) return;
    btn.disabled = disabled;
    if (text !== undefined) btn.textContent = text;
  }

  function _errorMessage(error) {
    if (!error) return 'Ошибка входа';
    if (error === 'timeout') return 'Время вышло';
    if (error === 'cancelled') return 'Отменено';
    if (error === 'playwright_missing_binary') return 'Браузер не установлен. Выполните: playwright install chromium';
    if (error === 'playwright_missing_deps') return 'Не хватает системных библиотек для браузера. Выполните: sudo playwright install-deps chromium';
    if (String(error).indexOf('playwright') === 0) return 'Ошибка браузера';
    return 'Ошибка входа: ' + error;
  }

  // ---------------------------------------------------------------------------
  // Polling
  // ---------------------------------------------------------------------------

  function _stopPolling() {
    if (_pollTimer !== null) {
      clearInterval(_pollTimer);
      _pollTimer = null;
    }
  }

  function _startPolling(btn, originalText) {
    _stopPolling(); // Ensure any previous polling is stopped (guard against double-click)

    _pollDeadline = Date.now() + HARD_TIMEOUT_MS;

    _pollTimer = setInterval(function () {
      if (Date.now() > _pollDeadline) {
        _stopPolling();
        _setButtonState(btn, false, originalText);
        _toast('Время вышло — попробуйте снова');
        _announce('Время ожидания входа вышло.', true);
        return;
      }

      fetch('/auth/status', {
        method: 'GET',
        headers: { 'Accept': 'application/json' },
      })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (data.running) return; // still in progress

          _stopPolling();
          var outcome = data.last_outcome;
          if (outcome && outcome.success === true) {
            _toast('Вход выполнен');
            _announce('Вход через Госуслуги выполнен.', false);
            window.location.reload();
          } else {
            var errMsg = _errorMessage(outcome && outcome.error);
            _toast(errMsg);
            _announce(errMsg, true);
            _setButtonState(btn, false, originalText);
          }
        })
        .catch(function () {
          // Network error during poll — keep retrying until deadline.
        });
    }, POLL_INTERVAL_MS);
  }

  // ---------------------------------------------------------------------------
  // startLogin — called on button click
  // ---------------------------------------------------------------------------

  function startLogin(btn) {
    var originalText = btn.textContent.trim();
    _setButtonState(btn, true, 'Идёт вход…');

    fetch('/auth/start', {
      method: 'POST',
      headers: {
        'Accept': 'application/json',
        'X-Requested-With': 'fetch',
      },
    })
      .then(function (r) {
        if (r.status === 202) {
          _startPolling(btn, originalText);
        } else if (r.status === 409) {
          // Job already running — join existing polling.
          _startPolling(btn, originalText);
        } else if (r.status === 429) {
          _setButtonState(btn, true, originalText);
          _toast('Попробуйте через минуту');
          setTimeout(function () {
            _setButtonState(btn, false, originalText);
          }, RATE_LIMIT_DISABLE_MS);
        } else if (r.status === 503) {
          _setButtonState(btn, false, originalText);
          _toast('Сервис запускается — попробуйте через несколько секунд');
        } else {
          _setButtonState(btn, false, originalText);
          _toast('Ошибка запуска входа (' + r.status + ')');
        }
      })
      .catch(function () {
        _setButtonState(btn, false, originalText);
        _toast('Нет соединения — попробуйте снова');
      });
  }

  // ---------------------------------------------------------------------------
  // Event delegation — DOMContentLoaded
  // ---------------------------------------------------------------------------

  document.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-action="login-start"]');
    if (!btn) return;
    e.preventDefault();
    startLogin(btn);
  });

  // Cleanup polling on page navigation (htmx, manual navigation, etc.)
  window.addEventListener('pagehide', _stopPolling);

})();
