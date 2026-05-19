/* ========================================================================
   Монитор гектара — minimal client JS
   - clipboard copy + toast
   - SSE-listener stub (with comment markers)
   - IntersectionObserver "seen" reporter for [NEW] badge
   - aria-live announcements
   - pulse-dot status indicator
   - star, expand/collapse toggles (UI only)
   - lot freshness flash, sticky "N new above" pill, scroll persistence
   - escalation progress, since-arrived counter
   - context menu, pin, copy-as-markdown
   ======================================================================== */
(() => {
  'use strict';

  // ---------- helpers ----------
  const $  = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const pad2 = (n) => String(n).padStart(2, '0');

  // ---------- toaster ----------
  function ensureToaster() {
    let el = $('.toaster');
    if (!el) {
      el = document.createElement('div');
      el.className = 'toaster';
      el.setAttribute('role', 'status');
      el.setAttribute('aria-live', 'polite');
      document.body.appendChild(el);
    }
    return el;
  }
  function toast(msg, { timeout = 2200 } = {}) {
    const wrap = ensureToaster();
    const t = document.createElement('div');
    t.className = 'toast';
    t.textContent = msg;
    wrap.appendChild(t);
    setTimeout(() => {
      t.style.transition = 'opacity 200ms';
      t.style.opacity = '0';
      setTimeout(() => t.remove(), 220);
    }, timeout);
  }

  // ---------- aria-live announcer (assertive vs polite) ----------
  function announce(msg, assertive = false) {
    const id = assertive ? '__live_assert' : '__live_polite';
    let el = document.getElementById(id);
    if (!el) {
      el = document.createElement('div');
      el.id = id;
      el.className = 'sr-only';
      el.setAttribute('aria-live', assertive ? 'assertive' : 'polite');
      el.setAttribute('aria-atomic', 'true');
      document.body.appendChild(el);
    }
    el.textContent = '';
    // tick the DOM so SR re-reads
    setTimeout(() => { el.textContent = msg; }, 30);
  }

  // ---------- "seen" reporter (NEW badge clears once scrolled into view ≥1s) ----------
  const seenTimers = new WeakMap();
  const seenIO = ('IntersectionObserver' in window)
    ? new IntersectionObserver(entries => {
        entries.forEach(e => {
          const el = e.target;
          if (e.isIntersecting && el.dataset.seen !== 'true') {
            // start a 1s timer; clear if scrolled away
            const t = setTimeout(() => {
              el.dataset.seen = 'true';
              const badge = el.querySelector('.chip--new');
              if (badge) badge.remove();
              // hx-post-like notification to server — replace with htmx in templates
              // fetch(`/lots/${el.dataset.lotId}/seen`, { method: 'POST' });
              seenIO.unobserve(el);
            }, 1000);
            seenTimers.set(el, t);
          } else {
            const t = seenTimers.get(el);
            if (t) { clearTimeout(t); seenTimers.delete(el); }
          }
        });
      }, { threshold: 0.6 })
    : null;
  if (seenIO) $$('.lot[data-seen="false"]').forEach(el => seenIO.observe(el));

  // ---------- clipboard ----------
  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      toast('Скопировано');
    } catch {
      // fallback
      const t = document.createElement('textarea');
      t.value = text; t.style.position = 'fixed'; t.style.opacity = '0';
      document.body.appendChild(t); t.select();
      try { document.execCommand('copy'); toast('Скопировано'); }
      finally { t.remove(); }
    }
  }
  document.addEventListener('click', (e) => {
    const c = e.target.closest('[data-copy]');
    if (!c) return;
    e.preventDefault();
    const val = c.dataset.copy || c.textContent.trim();
    copyText(val);
  });

  // ---------- expand details ----------
  // Toggle "open" state on a lot, and sync the ▼/▲ caret on any expand button inside it.
  function toggleLot(lot, forceOpen) {
    if (!lot) return;
    const willOpen = forceOpen !== undefined
      ? Boolean(forceOpen)
      : lot.dataset.open !== 'true';
    lot.dataset.open = String(willOpen);
    lot.setAttribute('aria-expanded', String(willOpen));
    const caret = lot.querySelector('[data-action="expand"]');
    if (caret) caret.textContent = willOpen ? '▲ Скрыть' : '▼ Детали';
  }

  // Explicit caret button — keeps working as before.
  document.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-action="expand"]');
    if (!btn) return;
    e.stopPropagation();
    toggleLot(btn.closest('.lot'));
  });

  // Whole-card click → open the lot in a new tab (same as the primary
  // «Открыть» button). Skip clicks on inner interactive controls so copy,
  // links, and buttons keep their own behaviour.
  const INTERACTIVE = 'a, button, input, textarea, select, label, [data-copy], [role="button"], summary, details';
  function openLotPrimary(lot) {
    const link = lot.querySelector('a.btn--primary[href]');
    if (!link) return;
    // Respect the disabled-on-session-expired state set in the template.
    if (link.getAttribute('aria-disabled') === 'true') return;
    // Use .click() so target="_blank" + rel="noopener" semantics are honoured.
    link.click();
  }
  document.addEventListener('click', (e) => {
    const lot = e.target.closest('.lot');
    if (!lot) return;
    if (window.getSelection && String(window.getSelection()).length > 0) return;
    if (e.target.closest(INTERACTIVE)) return;
    if (lot.dataset.event === 'gone') return;
    openLotPrimary(lot);
  });

  // Keyboard: Enter or Space on the focused card opens the lot.
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    const lot = e.target.closest('.lot');
    if (!lot) return;
    if (e.target !== lot) return;
    if (lot.dataset.event === 'gone') return;
    e.preventDefault();
    openLotPrimary(lot);
  });

  // ---------- note inline open ----------
  document.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-action="note"]');
    if (!btn) return;
    const lot = btn.closest('.lot');
    if (!lot) return;
    let area = lot.querySelector('.lot__note-input');
    if (!area) {
      area = document.createElement('textarea');
      area.className = 'lot__note-input';
      area.placeholder = 'Заметка (хранится локально)…';
      area.setAttribute('aria-label', 'Заметка к лоту');
      const cta = lot.querySelector('.lot__cta');
      (cta || lot).appendChild(area);
    }
    area.focus();
  });

  // ---------- SSE live-reconnect on view-filter change (m72b, ADR-052 resolved) ----------
  // POST /filters/view and POST /filters/clear respond with HX-Trigger: filter-changed.
  // htmx fires a `filter-changed` CustomEvent on the request element which bubbles to
  // document.body. We cycle the sse-connect attribute on #sse-root so htmx-sse tears
  // down the old EventSource and creates a new one that reads the updated view_filters
  // cookie in its GET /events handshake.
  // Debounce ~200 ms protects against rapid sort_dir clicks spawning many reconnects.
  let _reconnectTimer = null;
  document.body.addEventListener('filter-changed', function() {
    const root = document.getElementById('sse-root');
    if (!root) return;
    const url = root.getAttribute('sse-connect');
    if (!url) return;
    clearTimeout(_reconnectTimer);
    _reconnectTimer = setTimeout(function() {
      root.removeAttribute('sse-connect');
      setTimeout(function() {
        root.setAttribute('sse-connect', url);
        if (window.htmx && htmx.process) htmx.process(root);
      }, 0);
    }, 200);
  });

  document.addEventListener('click', (e) => {
    const trigger = e.target.closest('[data-toggle-menu]');
    if (trigger) {
      const id = trigger.dataset.toggleMenu;
      const menu = document.getElementById(id);
      if (menu) {
        menu.hidden = !menu.hidden;
        trigger.setAttribute('aria-expanded', String(!menu.hidden));
      }
      return;
    }
    // close all when clicking outside
    if (!e.target.closest('[data-menu]')) {
      $$('[data-menu]').forEach(m => {
        m.hidden = true;
        const btn = document.querySelector(`[data-toggle-menu="${m.id}"]`);
        if (btn) btn.setAttribute('aria-expanded', 'false');
      });
    }
  });

  // ---------- modal + menu close on Esc ----------
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      const m = $('.modal-backdrop:not([hidden])');
      if (m) m.hidden = true;
      $$('[data-menu]:not([hidden])').forEach(menu => {
        menu.hidden = true;
        const btn = document.querySelector(`[data-toggle-menu="${menu.id}"]`);
        if (btn) btn.setAttribute('aria-expanded', 'false');
      });
    }
  });

  // ---------- SSE stub ----------
  //
  // Real wiring (replace stub when integrating with FastAPI sse-starlette):
  //
  //   const es = new EventSource('/sse/lots');
  //   es.addEventListener('lot.new',       (e) => onLotNew(JSON.parse(e.data)));
  //   es.addEventListener('lot.status',    (e) => onLotStatusChange(JSON.parse(e.data)));
  //   es.addEventListener('cycle.tick',    (e) => onCycleTick(JSON.parse(e.data)));
  //   es.addEventListener('session.warn',  (e) => onSessionWarn(JSON.parse(e.data)));
  //   es.addEventListener('session.expired', () => onSessionExpired());
  //
  // For HTMX-driven UI, prefer hx-sse on the feed container, e.g.:
  //   <main hx-ext="sse" sse-connect="/sse/lots" sse-swap="lot.new">…</main>
  // and let the server send full <article class="lot">…</article> fragments.
  //
  // The handlers below are for vanilla-JS demos and for the "what to call where"
  // map. Keep them when you do hand-rolled SSE.

  // tier 1 — important (lot in subscribed subjects): "двойной pop"
  // tier 2 — background (in observed macro-region only): "одинарный pop"
  // tier 3 — diff (lot gone): "pluck"
  function playNotificationSound(tier) {
    // <PLACEHOLDER> wire actual audio assets
    // const url = ({1: '/static/sfx/double_pop.mp3', 2:'/static/sfx/single_pop.mp3', 3:'/static/sfx/pluck.mp3'})[tier];
    // (new Audio(url)).play().catch(()=>{});
    console.debug('[notify] tier', tier);
  }
  // _deniedToastShown prevents re-showing the denied-notification toast on every lot event.
  let _deniedToastShown = false;
  function maybeBrowserNotify(title, body) {
    if (!('Notification' in window)) return;
    if (Notification.permission === 'denied') {
      if (!_deniedToastShown && !localStorage.getItem('fis_notif_denied_toast')) {
        toast('Уведомления заблокированы — разрешите в настройках браузера', { timeout: 4000 });
        localStorage.setItem('fis_notif_denied_toast', '1');
        _deniedToastShown = true;
      }
      return;
    }
    if (Notification.permission !== 'granted') return;
    // Chrome suppresses Notification() on the active foreground tab when the call
    // has no user gesture (SSE handler). Sound + in-page UI suffice in that case.
    if (!document.hidden) return;
    try { new Notification(title, { body, icon: '/static/icons/icon-192.png', tag: 'fis-monitor-lot' }); }
    catch {}
  }

  // onLotNew accepts an HTMLElement (the <article> node inserted by htmx sse-swap,
  // carrying data-title and data-area attributes per ADR-049 stable contract),
  // or null/undefined for a sound-only fallback when the node is unavailable.
  // Plain-object signature is NOT supported — use data-* attributes.
  function onLotNew(lot) {
    const tier   = 1;
    const region = lot ? (lot.dataset?.title || '') : '';
    const area   = lot ? (lot.dataset?.area  || '') : '';
    // 1. play sound by tier
    playNotificationSound(tier);
    // 2. browser notification
    const body = [region, area ? `${area} га` : ''].filter(Boolean).join(', ');
    maybeBrowserNotify('Новый лот', body);
    // 3. aria-live polite
    announce(`Новый лот: ${region}${area ? ', ' + area + ' гектара' : ''}.`);
    // 4. DOM prepend (HTMX will normally do this server-side via sse-swap)
    // const html = renderLotCard(lot);
    // feed.insertAdjacentHTML('afterbegin', html);
    // 5. start escalation timer
    escalationStart();
  }
  // ---------- SSE wiring via htmx:sseMessage ----------
  // htmx-sse extension fires a synthetic "htmx:sseMessage" on document.body
  // for every incoming SSE event AFTER performing its own sse-swap.
  // We intercept "lot.new" to drive sound + browser notification + aria-live.
  // The htmx DOM insertion has already happened at this point, so we look up
  // the freshly prepended <article> in #feed to read its data-* attributes.
  //
  // Contract — e.detail.type invariant (vendored htmx-sse extension, line 154-155):
  //   swap(child, event.data); api.triggerEvent(elt, "htmx:sseMessage", event);
  //   `event` is the native MessageEvent; its `.type` is the SSE event name
  //   (e.g. "lot.new"). Do NOT rely on e.detail.data for structured data —
  //   parse data-* attributes from the already-swapped DOM element instead.
  document.body.addEventListener('htmx:sseMessage', (e) => {
    const type = e.detail && e.detail.type;
    if (type === 'lot.new') {
      // Locate the article that htmx just inserted (it is the first child of
      // #feed because sse-swap prepends to the feed container).
      const feed = document.getElementById('feed');
      const node = feed ? feed.firstElementChild : null;
      if (node && node.classList.contains('lot')) {
        onLotNew(node);
      } else {
        // Fallback: fire with null so sound still plays; node may have been
        // swapped before this listener ran or #feed is absent.
        if (!feed) console.warn('[sseMessage] #feed not found — DOM structure may have changed');
        onLotNew(null);
      }
    }
  });

  // ---------- Notification permission — persistent button in header ----------
  // Replaces the removed one-shot body-click listener.
  // The button (#notif-perm-btn) is rendered in base.html.jinja with
  // data-notif-perm="default". This function syncs state on load and after
  // requestPermission() resolves. High cohesion: all permission UX lives here.
  function initNotifPermButton() {
    const btn = document.getElementById('notif-perm-btn');
    if (!btn) return;

    if (!('Notification' in window)) {
      // API unavailable (old browser / insecure context).
      btn.hidden = true;
      return;
    }

    // Map permission value → label + aria-label + data-notif-perm.
    const STATE_META = {
      default:     { label: 'Включить браузерные уведомления о новых лотах',                    title: 'Нажмите, чтобы включить уведомления' },
      granted:     { label: 'Уведомления включены. Нажмите для тестовой проверки',              title: 'Уведомления включены. Нажмите для проверки' },
      denied:      { label: 'Уведомления заблокированы. Разрешите их в настройках браузера',    title: 'Браузер заблокировал уведомления — разрешите в настройках' },
      unavailable: { label: 'Уведомления недоступны в этом браузере',                           title: 'Уведомления недоступны в этом браузере' },
    };

    function applyState(perm) {
      const normalised = Object.prototype.hasOwnProperty.call(STATE_META, perm) ? perm : 'default';
      const meta = STATE_META[normalised];
      btn.dataset.notifPerm = normalised;
      btn.setAttribute('aria-label', meta.label);
      btn.setAttribute('title', meta.title);
      if (normalised === 'denied') {
        btn.setAttribute('aria-disabled', 'true');
      } else {
        btn.removeAttribute('aria-disabled');
      }
    }

    // Apply current permission on load.
    applyState(Notification.permission);

    btn.addEventListener('click', function handlePermClick() {
      const perm = Notification.permission;
      if (perm === 'denied') {
        // Already blocked; browser dialog won't appear. Inform via toast.
        toast('Уведомления заблокированы — разрешите в настройках браузера', { timeout: 4000 });
        return;
      }
      if (perm === 'granted') {
        // Already granted — fire a test notification so user can verify it works.
        try {
          new Notification('Монитор гектара', {
            body: 'Уведомления работают.',
            icon: '/static/icons/icon-192.png',
            tag: 'fis-monitor-test',
          });
        } catch (e) { console.warn('test notification failed', e); }
        return;
      }
      // perm === 'default' — request permission (requires user gesture; we are inside click).
      Notification.requestPermission().then((result) => {
        applyState(result);
        if (result === 'granted') {
          toast('Уведомления включены');
        } else if (result === 'denied') {
          toast('Уведомления заблокированы — разрешите в настройках браузера', { timeout: 4000 });
        }
      }).catch((e) => { console.warn('requestPermission failed', e); });
    });
  }
  initNotifPermButton();

  function onLotStatusChange(diff) {
    playNotificationSound(3);
    announce(`Лот ушёл: ${diff.region}.`, false);
  }
  function onCycleTick(info) {
    // refresh header countdown / health widget
  }
  function onSessionWarn(t) {
    // show yellow banner; assertive announce
    announce(`Сессия истекает в ${t.expires_at}. Продлите заранее.`, true);
  }
  function onSessionExpired() {
    const m = $('#session-expired-modal');
    if (m) m.hidden = false;
    announce('Сессия истекла. Войдите через Госуслуги, чтобы продолжить.', true);
  }

  // ---------- escalation timer ----------
  // first pop quiet → 60s later louder → 2min later title pulses
  let escState = null;
  function escalationStart() {
    if (escState) return;
    const baseTitle = document.title;
    escState = {
      t1: setTimeout(() => { playNotificationSound(1); }, 60_000),
      t2: setTimeout(() => {
        // pulse title every 1s with a red dot favicon
        escState.pulse = setInterval(() => {
          document.title = document.title.startsWith('⚠ ') ? baseTitle : '⚠ ' + baseTitle;
        }, 1000);
      }, 180_000),
      baseTitle,
    };
    // any user interaction stops escalation
    const stop = () => escalationStop();
    ['click', 'keydown', 'focus'].forEach(ev => window.addEventListener(ev, stop, { once: true }));
  }
  function escalationStop() {
    if (!escState) return;
    clearTimeout(escState.t1);
    clearTimeout(escState.t2);
    if (escState.pulse) clearInterval(escState.pulse);
    document.title = escState.baseTitle;
    escState = null;
  }

  // ========================================================================
  // ENHANCEMENTS (added in this iteration)
  // ========================================================================

  // ---------- 1. Double-click on cadastral copies it ----------
  // Augments the explicit copy button. Triggers on any element with
  // .lot__cad-inline (list cards) or .lot__cad text spans (poster cards).
  document.addEventListener('dblclick', (e) => {
    const target = e.target.closest('.lot__cad, .lot__cad-inline, [data-copy]');
    if (!target) return;
    // prefer a data-copy value if present, else trim text
    const val = target.dataset.copy || target.textContent.trim();
    if (!val) return;
    e.preventDefault();
    // small ephemeral selection so user sees the doubleclick "worked"
    const sel = window.getSelection();
    const range = document.createRange();
    range.selectNodeContents(target);
    sel.removeAllRanges();
    sel.addRange(range);
    setTimeout(() => sel.removeAllRanges(), 600);
    copyText(val);
  });

  // ---------- 2. Persist scroll position ----------
  // Survives page reload and HTMX swaps. Per-pathname key.
  (function () {
    const KEY = `monitor:scroll:${location.pathname}`;
    let restoring = true;
    // restore on load
    const saved = Number(sessionStorage.getItem(KEY) || 0);
    if (saved > 0) {
      // wait a tick for layout
      requestAnimationFrame(() => {
        window.scrollTo({ top: saved, behavior: 'auto' });
        // give layout one more frame, then start saving
        setTimeout(() => { restoring = false; }, 100);
      });
    } else {
      restoring = false;
    }
    let saveTimer = null;
    window.addEventListener('scroll', () => {
      if (restoring) return;
      clearTimeout(saveTimer);
      saveTimer = setTimeout(() => {
        sessionStorage.setItem(KEY, String(window.scrollY));
      }, 120);
    }, { passive: true });
    // Re-restore after HTMX swap if we got scrolled to top
    document.body.addEventListener('htmx:afterSwap', () => {
      const v = Number(sessionStorage.getItem(KEY) || 0);
      if (v > 0 && window.scrollY < 50) {
        requestAnimationFrame(() => window.scrollTo({ top: v }));
      }
    });
  })();

  // ---------- 3. Sticky "↑ N новых сверху" pill ----------
  // When a new lot is prepended above the user's current scroll position,
  // accumulate a counter and surface a pill that scrolls them up smoothly.
  (function () {
    const feed = document.getElementById('feed');
    if (!feed) return;

    let pill = document.querySelector('.jump-new');
    if (!pill) {
      pill = document.createElement('button');
      pill.className = 'jump-new';
      pill.type = 'button';
      pill.hidden = true;
      pill.setAttribute('aria-live', 'polite');
      pill.innerHTML = '<span class="jump-new__count">1</span> новый сверху <span aria-hidden="true">↑</span>';
      document.body.appendChild(pill);
    }
    let pending = 0;
    function show() {
      pill.hidden = false;
      pill.querySelector('.jump-new__count').textContent = String(pending);
    }
    function clearAndScroll() {
      pending = 0;
      pill.hidden = true;
      // scroll to the first not-yet-seen lot
      const target = feed.querySelector('.lot[data-seen="false"]') || feed.firstElementChild;
      if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
    pill.addEventListener('click', clearAndScroll);

    // Hook 1: htmx prepended a new fragment via SSE
    feed.addEventListener('htmx:afterSwap', (e) => {
      // only when actually added to top
      if (e.detail && e.detail.target === feed) {
        if (window.scrollY > 200) { pending += 1; show(); }
      }
    });

    // Hook 2: vanilla DOM prepend (Monitor.onLotNew path)
    const mo = new MutationObserver(muts => {
      muts.forEach(m => {
        m.addedNodes.forEach(n => {
          if (n.nodeType !== 1) return;
          if (n.classList && n.classList.contains('lot')) {
            // 4. Freshness flash (Tier 3, #4)
            n.dataset.fresh = 'true';
            setTimeout(() => { delete n.dataset.fresh; }, 2400);
            if (window.scrollY > 200) { pending += 1; show(); }
          }
        });
      });
    });
    mo.observe(feed, { childList: true, subtree: true });

    // Hide pill once scrolled to top manually
    window.addEventListener('scroll', () => {
      if (window.scrollY < 200 && !pill.hidden) {
        pending = 0;
        pill.hidden = true;
      }
    }, { passive: true });
  })();

  // ---------- 5. Escalation progress indicator ----------
  // bd-bi7i: текстовый чип «Громче через …» убран из шаблона
  // (_header_status.html.jinja) — пользователю формулировка непонятна. CSS-флаг
  // body.dataset.escalating оставлен: им можно стилизовать индикатор без слов
  // (например, пульсирующая точка), если решим вернуть визуал.
  const _origEscStart = escalationStart;
  const _origEscStop  = escalationStop;
  // eslint-disable-next-line no-func-assign
  escalationStart = function () {
    if (escState) return;
    _origEscStart.apply(this, arguments);
    document.body.dataset.escalating = 'true';
  };
  // eslint-disable-next-line no-func-assign
  escalationStop = function () {
    _origEscStop.apply(this, arguments);
    delete document.body.dataset.escalating;
  };

  // ---------- 6. Since-arrived counter ----------
  (function () {
    const el = document.querySelector('[data-since-arrived]');
    if (!el) return;
    const t0 = Date.now();
    function plural(n, forms) {
      // [1, 2-4, 5-20] — стандартные русские формы
      const n10 = n % 10, n100 = n % 100;
      if (n10 === 1 && n100 !== 11) return forms[0];
      if (n10 >= 2 && n10 <= 4 && (n100 < 10 || n100 >= 20)) return forms[1];
      return forms[2];
    }
    function fmt() {
      const secs = Math.floor((Date.now() - t0) / 1000);
      if (secs < 60) return `${secs} ${plural(secs, ['секунду', 'секунды', 'секунд'])}`;
      const mins = Math.floor(secs / 60);
      if (mins < 60) return `${mins} ${plural(mins, ['минуту', 'минуты', 'минут'])}`;
      const hrs = Math.floor(mins / 60);
      return `${hrs} ${plural(hrs, ['час', 'часа', 'часов'])}`;
    }
    function tick() { el.textContent = fmt(); }
    tick();
    setInterval(tick, 1000);
  })();

  // ---------- 7. Density toggle (compact / auto / poster) ----------
  // localStorage key: monitor:density = 'auto' | 'compact' | 'poster'
  (function () {
    const DKEY = 'monitor:density';
    const apply = (mode) => {
      document.body.dataset.density = mode;
      // visual sync of any density radio
      document.querySelectorAll('[data-density-option]').forEach(el => {
        el.setAttribute('aria-pressed', String(el.dataset.densityOption === mode));
      });
    };
    apply(localStorage.getItem(DKEY) || 'auto');
    document.addEventListener('click', (e) => {
      const btn = e.target.closest('[data-density-option]');
      if (!btn) return;
      const mode = btn.dataset.densityOption;
      localStorage.setItem(DKEY, mode);
      apply(mode);
      toast(`Плотность: ${({auto:'авто', compact:'плотно', poster:'постер'})[mode]}`);
    });
  })();

  // ---------- 8. Pin lot to top ----------
  // localStorage key: monitor:pinned = ['id1', 'id2']
  (function () {
    const PKEY = 'monitor:pinned';
    function loadPinned() {
      try { return JSON.parse(localStorage.getItem(PKEY) || '[]'); }
      catch { return []; }
    }
    function savePinned(ids) { localStorage.setItem(PKEY, JSON.stringify(ids)); }

    function applyPinned() {
      const ids = loadPinned();
      ids.forEach(id => {
        const lot = document.getElementById('lot-' + id) || document.querySelector(`.lot[data-lot-id="${CSS.escape(id)}"]`);
        if (lot) lot.dataset.pinned = 'true';
      });
    }
    applyPinned();

    document.addEventListener('click', (e) => {
      const btn = e.target.closest('[data-action="pin"]');
      if (!btn) return;
      const lot = btn.closest('.lot');
      if (!lot) return;
      const id = lot.dataset.lotId;
      let ids = loadPinned();
      if (ids.includes(id)) {
        ids = ids.filter(x => x !== id);
        delete lot.dataset.pinned;
        toast('Снято с закрепления');
      } else {
        ids.unshift(id);
        lot.dataset.pinned = 'true';
        // move to top of its zone
        const zone = lot.closest('.zone');
        if (zone) {
          const head = zone.querySelector('.zone__head');
          if (head && head.nextSibling !== lot) zone.insertBefore(lot, head.nextSibling);
        }
        toast('Закреплено сверху');
      }
      savePinned(ids);
    });
  })();

  // ---------- 9. Copy as Markdown ----------
  document.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-action="copy-md"]');
    if (!btn) return;
    const lot = btn.closest('.lot');
    if (!lot) return;
    const title = lot.querySelector('.lot__title')?.textContent.trim() || '';
    const cad   = lot.querySelector('.lot__cad, .lot__cad-inline')?.textContent.trim() || '';
    const meta  = lot.querySelector('.lot__meta')?.textContent.trim() || '';
    const url   = lot.querySelector('a.btn--primary')?.getAttribute('href') || '';
    const md = `**${title}**\n${meta}\n\`${cad}\`${url ? `\n[Открыть на сайте](${url})` : ''}`;
    copyText(md);
  });

  // ---------- Context menu for lots (right-click) ----------
  // Lightweight, no library. Spawns a <div class="ctx-menu"> at pointer position.
  document.addEventListener('contextmenu', (e) => {
    const lot = e.target.closest('.lot');
    if (!lot) return;
    if (lot.dataset.event === 'gone') return; // nothing actionable
    e.preventDefault();
    closeCtxMenu();
    const id = lot.dataset.lotId || '';
    const isPinned = lot.dataset.pinned === 'true';
    const menu = document.createElement('div');
    menu.className = 'ctx-menu';
    menu.setAttribute('role', 'menu');
    menu.style.left = `${Math.min(e.clientX, window.innerWidth - 240)}px`;
    menu.style.top  = `${Math.min(e.clientY, window.innerHeight - 280)}px`;
    menu.innerHTML = `
      <button role="menuitem" data-ctx="open">Открыть на сайте</button>
      <button role="menuitem" data-ctx="copy-cad">Скопировать кадастровый</button>
      <button role="menuitem" data-ctx="copy-md">Скопировать как Markdown</button>
      <hr/>
      <button role="menuitem" data-ctx="pin">${isPinned ? 'Открепить' : 'Закрепить сверху'}</button>
      <button role="menuitem" data-ctx="note">Заметка…</button>
    `;
    document.body.appendChild(menu);
    menu.addEventListener('click', (ev) => {
      const m = ev.target.closest('[data-ctx]');
      if (!m) return;
      const action = m.dataset.ctx;
      closeCtxMenu();
      if (action === 'open') {
        lot.querySelector('a.btn--primary')?.click();
      } else if (action === 'copy-cad') {
        const cad = lot.querySelector('.lot__cad, .lot__cad-inline')?.textContent.trim();
        if (cad) copyText(cad);
      } else if (action === 'copy-md') {
        lot.querySelector('[data-action="copy-md"]')?.click() ||
          // synthetic — copy-md handler reads from the lot regardless of source button
          (function(){ const fake = document.createElement('button'); fake.dataset.action='copy-md'; lot.appendChild(fake); fake.click(); fake.remove(); })();
      } else if (action === 'pin') {
        // dispatch a synthetic pin
        const fake = document.createElement('button'); fake.dataset.action='pin'; lot.appendChild(fake); fake.click(); fake.remove();
      } else if (action === 'note') {
        lot.querySelector('[data-action="note"]')?.click();
      }
    });
  });
  function closeCtxMenu() {
    document.querySelectorAll('.ctx-menu').forEach(m => m.remove());
  }
  document.addEventListener('click', (e) => {
    if (!e.target.closest('.ctx-menu')) closeCtxMenu();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeCtxMenu();
  });

  // expose for templates / debugging
  window.Monitor = {
    toast, announce, copyText,
    onLotNew, onLotStatusChange, onCycleTick, onSessionWarn, onSessionExpired,
  };
})();
