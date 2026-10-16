(function () {
  var root = document.documentElement;
  var T = {
    ru: {light: 'Светлая тема', dark: 'Тёмная тема', copied: 'Скопировано'},
    en: {light: 'Light theme', dark: 'Dark theme', copied: 'Copied'},
    zh: {light: '浅色主题', dark: '深色主题', copied: '已复制'},
    es: {light: 'Tema claro', dark: 'Tema oscuro', copied: 'Copiado'}
  }[(root.lang || 'ru').slice(0, 2)] || {};

  // Theme: dark by default, follows the system until the visitor picks one.
  var button = document.getElementById('theme');
  var light = window.matchMedia('(prefers-color-scheme: light)');
  function current() { return root.dataset.theme || (light.matches ? 'light' : 'dark'); }
  function label() { button.title = current() === 'dark' ? T.light : T.dark; }
  button.addEventListener('click', function () {
    var next = current() === 'dark' ? 'light' : 'dark';
    root.dataset.theme = next;
    try { localStorage.setItem('owa-mail-theme', next); } catch (e) {}
    label();
  });
  label();

  // Copy buttons.
  document.querySelectorAll('[data-copy]').forEach(function (b) {
    var html = b.innerHTML;
    b.addEventListener('click', function () {
      var el = document.querySelector(b.dataset.copy);
      if (!el || !navigator.clipboard) return;
      navigator.clipboard.writeText(el.textContent.trim()).then(function () {
        b.textContent = T.copied;
        b.classList.add('done');
        setTimeout(function () { b.innerHTML = html; b.classList.remove('done'); }, 1600);
      }, function () {});
    });
  });

  // Tabs (WAI-ARIA pattern). Panels stay in the HTML, so readers without JS and agents see all of them.
  document.querySelectorAll('[data-tabs]').forEach(function (box) {
    var tabs = Array.prototype.slice.call(box.querySelectorAll('[role="tab"]'));
    function select(tab, focus) {
      tabs.forEach(function (t) {
        var on = t === tab;
        t.setAttribute('aria-selected', String(on));
        t.tabIndex = on ? 0 : -1;
        document.getElementById(t.getAttribute('aria-controls')).hidden = !on;
      });
      if (focus) tab.focus();
    }
    // Every panel is visible in the HTML itself (for agents and readers without JS); hide the
    // inactive ones only now.
    select(tabs.filter(function (t) { return t.getAttribute('aria-selected') === 'true'; })[0] || tabs[0]);
    tabs.forEach(function (tab, i) {
      tab.addEventListener('click', function () { select(tab); });
      tab.addEventListener('keydown', function (e) {
        var d = e.key === 'ArrowRight' ? 1 : e.key === 'ArrowLeft' ? -1 : 0;
        if (d) { e.preventDefault(); select(tabs[(i + d + tabs.length) % tabs.length], true); }
      });
    });
  });

  // "Install through your agent" link opens the matching tab.
  var agentLink = document.querySelector('a[href="#agents"]');
  if (agentLink) agentLink.addEventListener('click', function () {
    var t = document.getElementById('t-agent');
    if (t) t.click();
  });

  // Language switcher: remember the choice so the root page stops guessing; close on outside click or Escape.
  document.querySelectorAll('[data-lang]').forEach(function (a) {
    a.addEventListener('click', function () {
      try { localStorage.setItem('owa-mail-lang', a.dataset.lang); } catch (e) {}
    });
  });
  var lang = document.querySelector('details.lang');
  if (lang) {
    document.addEventListener('click', function (e) { if (!lang.contains(e.target)) lang.open = false; });
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && lang.open) { lang.open = false; lang.querySelector('summary').focus(); } });
  }

  // Current version from the repository; the static label stays if the request fails.
  fetch('https://raw.githubusercontent.com/Khomiakov7706/owa-mail-mcp/main/src/owa_mail_mcp/__init__.py')
    .then(function (r) { return r.ok ? r.text() : ''; })
    .then(function (t) { var m = /__version__ = "([^"]+)"/.exec(t); if (m) document.getElementById('version').textContent = 'v' + m[1]; })
    .catch(function () {});
})();
