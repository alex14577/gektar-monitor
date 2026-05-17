/* onboarding_step1.js — toggle macroregion cards on click.
 *
 * Extracted from inline onclick= because CspMiddleware.DEFAULT_POLICY ships
 * script-src 'self' (no 'unsafe-inline'), which blocks inline event handlers.
 */
(function () {
  'use strict';

  var cards = document.querySelectorAll('.region-card');
  for (var i = 0; i < cards.length; i++) {
    cards[i].addEventListener('click', function () {
      var pressed = this.getAttribute('aria-pressed') === 'true';
      var next = pressed ? 'false' : 'true';
      this.setAttribute('aria-pressed', next);
      var cb = this.querySelector('input[type="checkbox"]');
      if (cb) cb.checked = (next === 'true');
    });
  }
})();
