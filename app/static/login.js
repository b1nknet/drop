const form = document.querySelector('#login-form');
const password = document.querySelector('#password');
const toggle = document.querySelector('#toggle-password');
const error = document.querySelector('#login-error');
const submit = form.querySelector('[type="submit"]');

toggle.addEventListener('click', () => {
  const visible = password.type === 'password';
  password.type = visible ? 'text' : 'password';
  toggle.setAttribute('aria-pressed', String(visible));
  toggle.setAttribute('aria-label', visible ? '비밀번호 숨기기' : '비밀번호 표시');
});

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  error.textContent = '';
  submit.disabled = true;
  submit.querySelector('span').textContent = '확인하는 중…';
  try {
    const response = await fetch('/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: password.value }),
    });
    const body = await response.json();
    if (!response.ok)
      throw new Error(typeof body.detail === 'string' ? body.detail : '비밀번호를 확인해 주세요.');
    window.location.replace('/');
  } catch (failure) {
    error.textContent =
      failure instanceof TypeError
        ? '서버에 연결하지 못했어요. 잠시 후 다시 시도해 주세요.'
        : failure.message;
    password.focus();
  } finally {
    submit.disabled = false;
    submit.querySelector('span').textContent = '로그인';
  }
});
