(function () {
  // Theme: follows the system until the visitor picks one.
  var themeButton = document.getElementById('theme');
  var media = window.matchMedia('(prefers-color-scheme: dark)');
  function current() { return document.documentElement.dataset.theme || (media.matches ? 'dark' : 'light'); }
  function label() {
    var text = current() === 'dark' ? 'Светлая тема' : 'Тёмная тема';
    themeButton.setAttribute('aria-label', text);
    themeButton.title = text;
  }
  themeButton.addEventListener('click', function () {
    var next = current() === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem('owa-mail-theme', next); } catch (e) {}
    label();
  });
  if (!document.documentElement.dataset.theme) document.documentElement.dataset.theme = current();
  media.addEventListener('change', function () {
    var saved = null;
    try { saved = localStorage.getItem('owa-mail-theme'); } catch (e) {}
    if (!saved) { document.documentElement.dataset.theme = media.matches ? 'dark' : 'light'; label(); }
  });
  label();

  // The section for the visitor's current time of day gets the dot on the rail.
  function tick() {
    var d = new Date(), mins = d.getHours() * 60 + d.getMinutes(), now = null;
    document.querySelectorAll('.slot[data-t]').forEach(function (s) {
      var p = s.dataset.t.split(':');
      s.classList.remove('is-now');
      if (+p[0] * 60 + +p[1] <= mins) now = s;
    });
    if (now) now.classList.add('is-now');
  }
  tick();
  setInterval(tick, 30000);

  // Copy buttons.
  document.querySelectorAll('[data-copy]').forEach(function (button) {
    var original = button.textContent;
    button.addEventListener('click', function () {
      var source = document.querySelector(button.dataset.copy);
      var text = source ? source.textContent : '';
      var done = function () {
        button.textContent = 'Скопировано';
        button.classList.add('done');
        setTimeout(function () { button.textContent = original; button.classList.remove('done'); }, 1600);
      };
      if (navigator.clipboard) navigator.clipboard.writeText(text).then(done, function () {});
    });
  });

  // Client tabs.
  document.querySelectorAll('.tabs [data-tab]').forEach(function (tab) {
    tab.addEventListener('click', function () {
      var box = tab.closest('.clients');
      box.querySelectorAll('[data-tab]').forEach(function (t) { t.setAttribute('aria-selected', String(t === tab)); });
      box.querySelectorAll('[data-panel]').forEach(function (p) { p.classList.toggle('on', p.dataset.panel === tab.dataset.tab); });
    });
  });

  // Current version from the repository; the static label stays if the request fails.
  fetch('https://raw.githubusercontent.com/Khomiakov7706/owa-mail-mcp/main/src/owa_mail_mcp/__init__.py')
    .then(function (r) { return r.ok ? r.text() : ''; })
    .then(function (t) { var m = /__version__ = "([^"]+)"/.exec(t); if (m) document.getElementById('version').textContent = 'v' + m[1]; })
    .catch(function () {});
})();
