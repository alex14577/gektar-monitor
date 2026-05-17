/* onboarding_step2.js — SMTP provider suggestion and app-password hint
 * for the onboarding step-2 form.
 */
(function () {
  'use strict';

  var loginEl    = document.getElementById('smtp-login');
  var hostEl     = document.getElementById('smtp-host');
  var portEl     = document.getElementById('smtp-port');
  var advancedEl = document.getElementById('smtp-advanced');
  var hintEl     = document.getElementById('smtp-app-password-hint');
  var hintLabel  = document.getElementById('smtp-provider-label');
  var hintLink   = document.getElementById('smtp-app-password-link');

  if (!loginEl || !hostEl || !portEl) return;

  // Track whether the user has manually edited host/port after the last
  // catalog suggestion. If so, we do NOT overwrite their values.
  var userEditedHost = Boolean(hostEl.value);
  var userEditedPort = false;

  hostEl.addEventListener('input', function () { userEditedHost = true; });
  portEl.addEventListener('input', function () { userEditedPort = true; });

  function applySuggestion(data) {
    if (data.smtp_host === null) {
      // Unknown domain — open advanced section so user can enter manually.
      advancedEl.open = true;
      hintEl.hidden = true;
      return;
    }

    // Prefill host/port only when user has not manually edited them.
    if (!userEditedHost) {
      hostEl.value = data.smtp_host;
    }
    if (!userEditedPort) {
      portEl.value = String(data.smtp_port);
    }

    // Collapse advanced section — host/port are auto-filled.
    advancedEl.open = false;

    // Show or hide app-password hint.
    if (data.app_password_url) {
      hintLabel.textContent = data.provider_label || 'Провайдер';
      hintLink.href = data.app_password_url;
      hintEl.hidden = false;
    } else {
      hintEl.hidden = true;
    }
  }

  var _debounceTimer = null;

  function fetchSuggestion(email) {
    if (!email) return;
    fetch('/settings/smtp/suggest?email=' + encodeURIComponent(email))
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) { if (data) applySuggestion(data); })
      .catch(function () { /* network error — ignore, keep current state */ });
  }

  loginEl.addEventListener('input', function () {
    clearTimeout(_debounceTimer);
    var val = loginEl.value.trim();
    _debounceTimer = setTimeout(function () { fetchSuggestion(val); }, 300);
  });

  loginEl.addEventListener('blur', function () {
    clearTimeout(_debounceTimer);
    fetchSuggestion(loginEl.value.trim());
  });
}());
