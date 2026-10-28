(() => {
  const storageKey = 'drop-theme';
  let theme = 'dark';
  try {
    if (window.localStorage.getItem(storageKey) === 'light') theme = 'light';
  } catch {
    /* 저장소를 사용할 수 없으면 기본 다크모드로 시작합니다. */
  }

  function applyTheme(value) {
    theme = value;
    document.documentElement.dataset.theme = theme;
    document.querySelector('meta[name="color-scheme"]').content = theme;
    document.querySelector('meta[name="theme-color"]').content =
      theme === 'dark' ? '#101622' : '#f7f9fc';
    const label = theme === 'dark' ? '라이트 모드' : '다크 모드';
    document.querySelectorAll('.theme-toggle').forEach((button) => {
      button.setAttribute('aria-label', `${label}로 전환`);
      button.title = `${label}로 전환`;
      button.querySelector('[data-theme-label]').textContent = label;
    });
  }

  // 스타일을 읽기 전에 저장된 테마를 적용해 화면 깜박임을 막습니다.
  applyTheme(theme);
  document.addEventListener('DOMContentLoaded', () => {
    applyTheme(theme);
    document.querySelectorAll('.theme-toggle').forEach((button) => {
      button.addEventListener('click', () => {
        applyTheme(theme === 'dark' ? 'light' : 'dark');
        try {
          window.localStorage.setItem(storageKey, theme);
        } catch {
          /* 저장에 실패해도 현재 화면의 테마 전환은 유지합니다. */
        }
      });
    });
  });

  window.addEventListener('storage', (event) => {
    if (event.key === storageKey || event.key === null) {
      applyTheme(event.newValue === 'light' ? 'light' : 'dark');
    }
  });
})();
