/**
 * 首屏 loading 在 i18n 初始化前执行，按 localStorage 中的语言偏好显示应用名。
 * 文案需与 apps/web-ele/src/locales/langs/*/authentication.json 的 appName 保持一致。
 */
(function () {
  var APP_NAMES = {
    'zh-CN': '运营后台',
    'zh-TW': '運營後台',
    'en-US': 'Operations Backend',
    'th-TH': 'แบ็กเอนด์ปฏิบัติการ',
    'vi-VN': 'Hệ thống quản trị vận hành',
  };

  function parseStorageValue(raw) {
    if (!raw) return null;
    try {
      var item = JSON.parse(raw);
      return item && item.value !== undefined ? item.value : null;
    } catch (e) {
      return raw;
    }
  }

  function getLocale() {
    try {
      for (var i = 0; i < localStorage.length; i++) {
        var key = localStorage.key(i);
        if (!key) continue;
        if (key.indexOf('preferences-locale') !== -1) {
          var locale = parseStorageValue(localStorage.getItem(key));
          if (locale) return locale;
        }
      }
      for (var j = 0; j < localStorage.length; j++) {
        var prefKey = localStorage.key(j);
        if (!prefKey || !/-preferences$/.test(prefKey)) continue;
        var pref = parseStorageValue(localStorage.getItem(prefKey));
        if (pref && pref.app && pref.app.locale) return pref.app.locale;
      }
    } catch (e) {
      /* ignore */
    }
    return 'zh-CN';
  }

  var locale = getLocale();
  var title = APP_NAMES[locale] || APP_NAMES['zh-CN'];
  window.__VBEN_APP_LOADING_TITLE__ = title;
  document.title = title;
})();
