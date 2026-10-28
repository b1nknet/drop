const $ = (selector) => document.querySelector(selector);
const state = {
  files: [],
  jobs: [],
  csrf: '',
  limit: 0,
  running: false,
  expired: false,
  loaded: false,
  deleting: new Set(),
  settings: null,
  savingSettings: false,
};
const paths = {
  download: 'M12 3v12m-5-5 5 5 5-5M5 16v4h14v-4',
  delete: 'M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7',
  file: 'M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8l-6-6Zm0 0v6h6M8 13h8m-8 4h5',
};

function icon(kind) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('fill', 'none');
  svg.setAttribute('aria-hidden', 'true');
  const path = document.createElementNS(svg.namespaceURI, 'path');
  for (const [key, value] of Object.entries({
    d: paths[kind],
    stroke: 'currentColor',
    'stroke-width': '1.6',
    'stroke-linecap': 'round',
    'stroke-linejoin': 'round',
  }))
    path.setAttribute(key, value);
  svg.append(path);
  return svg;
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function bytes(size) {
  if (size === 0) return '0 B';
  const unit = Math.min(Math.floor(Math.log(size) / Math.log(1024)), 4);
  return `${new Intl.NumberFormat('ko-KR', { maximumFractionDigits: unit ? 1 : 0 }).format(size / 1024 ** unit)} ${['B', 'KB', 'MB', 'GB', 'TB'][unit]}`;
}

function expire() {
  state.expired = true;
  window.location.replace('/login');
}

async function api(url, options = {}) {
  const response = await fetch(url, options);
  if (response.status === 401) {
    expire();
    throw new Error('로그인이 만료되었어요. 다시 로그인해 주세요.');
  }
  const data = await response.json();
  if (!response.ok)
    throw new Error(typeof data.detail === 'string' ? data.detail : '요청을 처리하지 못했어요.');
  return data;
}

let toastTimer;
function toast(message) {
  $('#toast').textContent = message;
  $('#toast').hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    $('#toast').hidden = true;
  }, 4500);
}

function fileKind(name) {
  const extension = name.includes('.') ? name.split('.').pop().toLowerCase() : '';
  if (['png', 'jpg', 'jpeg', 'webp', 'gif', 'svg', 'heic', 'avif'].includes(extension))
    return ['image', extension.toUpperCase()];
  if (extension === 'pdf') return ['pdf', 'PDF'];
  if (['zip', 'rar', '7z', 'gz', 'tar'].includes(extension))
    return ['archive', extension.toUpperCase()];
  if (['xls', 'xlsx', 'csv'].includes(extension)) return ['sheet', extension.toUpperCase()];
  if (['mp4', 'mov', 'mkv', 'mp3', 'wav'].includes(extension))
    return ['media', extension.toUpperCase()];
  return ['document', extension ? extension.toUpperCase().slice(0, 5) : 'FILE'];
}

function renderFiles() {
  const query = $('#search').value.trim().toLocaleLowerCase();
  const files = state.files.filter((file) => file.name.toLocaleLowerCase().includes(query));
  const sort = $('#sort').value;
  files.sort(
    sort === 'name'
      ? (a, b) => a.name.localeCompare(b.name, 'ko')
      : sort === 'size'
        ? (a, b) => b.size - a.size
        : (a, b) => b.modified - a.modified,
  );
  const fragment = document.createDocumentFragment();
  for (const file of files) {
    const row = element('tr');
    const nameCell = element('td');
    const nameWrap = element('div', 'file-name-wrap');
    const [kind, extension] = fileKind(file.name);
    const badge = element('span', `file-icon ${kind}`, extension);
    badge.setAttribute('aria-hidden', 'true');
    const name = element('span', 'file-name', file.name);
    name.title = file.name;
    const info = element('div', 'file-info');
    info.append(name);
    if (file.expires_at != null) {
      const expires = new Date(file.expires_at * 1000);
      const expiry = element(
        'span',
        'file-expiry',
        expires.getTime() <= Date.now()
          ? '자동 삭제 대기 중'
          : `${new Intl.DateTimeFormat('ko-KR', {
              month: '2-digit',
              day: '2-digit',
              hour: '2-digit',
              minute: '2-digit',
              hour12: false,
            }).format(expires)} 삭제 예정`,
      );
      expiry.title = `${expires.toLocaleString('ko-KR')} 이후 자동 삭제`;
      info.append(expiry);
    }
    nameWrap.append(badge, info);
    nameCell.append(nameWrap);
    const size = element('td', 'file-size', bytes(file.size));
    const date = element(
      'td',
      'file-date',
      new Intl.DateTimeFormat('ko-KR', {
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
      }).format(new Date(file.modified * 1000)),
    );
    date.title = new Date(file.modified * 1000).toLocaleString('ko-KR');
    const action = element('td', 'file-action');
    const download = element('a', 'download-button');
    download.href = `/api/files/${encodeURIComponent(file.name)}`;
    download.download = file.name;
    download.title = `${file.name} 다운로드`;
    download.setAttribute('aria-label', `${file.name} 다운로드`);
    download.append(icon('download'), element('span', '', '다운로드'));
    const remove = element('button', 'delete-button');
    remove.type = 'button';
    remove.title = `${file.name} 삭제`;
    remove.setAttribute('aria-label', `${file.name} 삭제`);
    remove.disabled = state.deleting.has(file.name);
    remove.setAttribute('aria-busy', String(remove.disabled));
    remove.append(icon('delete'));
    remove.addEventListener('click', () => deleteFile(file));
    const actions = element('div', 'file-actions');
    actions.append(download, remove);
    action.append(actions);
    row.append(nameCell, size, date, action);
    fragment.append(row);
  }
  $('#file-list').replaceChildren(fragment);
  $('#file-count').textContent = state.files.length;
  $('#total-size').textContent =
    `총 ${bytes(state.files.reduce((sum, file) => sum + file.size, 0))}`;
  $('#empty-state').hidden = files.length !== 0 || !state.loaded;
  $('#empty-title').textContent = query ? '검색 결과가 없어요' : '아직 보관한 파일이 없어요';
  $('#empty-copy').textContent = query
    ? '다른 이름으로 검색해 보세요.'
    : '첫 번째 파일을 올리고 나만의 공간을 채워보세요.';
  $('#list-summary').textContent = query
    ? `${state.files.length}개 중 ${files.length}개 파일`
    : `${files.length}개 파일`;
}

async function deleteFile(file) {
  if (state.deleting.has(file.name)) return;
  if (
    !window.confirm(
      `“${file.name}” 파일을 삭제할까요?\n\n저장소에서 영구 삭제되며 복구할 수 없습니다.`,
    )
  )
    return;
  state.deleting.add(file.name);
  renderFiles();
  try {
    await api(`/api/files/${encodeURIComponent(file.name)}`, {
      method: 'DELETE',
      headers: { 'X-CSRF-Token': state.csrf },
    });
    state.files = state.files.filter((item) => item.name !== file.name);
    renderFiles();
    toast(`“${file.name}” 파일을 삭제했어요.`);
    await loadFiles();
  } catch (failure) {
    toast(
      failure instanceof TypeError
        ? '삭제 결과를 확인하지 못했어요. 파일 목록을 새로고침해 주세요.'
        : failure.message,
    );
  } finally {
    state.deleting.delete(file.name);
    renderFiles();
  }
}

async function loadFiles() {
  $('#refresh').disabled = true;
  try {
    const data = await api('/api/files');
    state.files = data.files;
    applySettings(data.settings);
    state.loaded = true;
    $('#list-error').hidden = true;
    renderFiles();
  } catch (failure) {
    $('#list-error').textContent =
      failure instanceof TypeError
        ? '파일 목록을 불러오지 못했어요. 새로고침 버튼으로 다시 시도해 주세요.'
        : failure.message;
    $('#list-error').hidden = false;
    $('#list-summary').textContent = '목록을 불러오지 못했어요';
  } finally {
    $('#refresh').disabled = false;
  }
}

function retentionLabel(hours) {
  return hours < 24 ? `${hours}시간` : `${hours / 24}일`;
}

function applySettings(settings) {
  state.settings = settings;
  $('#retention-notice').hidden = !settings.enabled;
  $('#retention-notice').textContent = settings.enabled
    ? `모든 파일은 마지막 수정 후 ${retentionLabel(settings.retention_hours)}이 지나면 자동 삭제돼요. 삭제는 1분마다 확인하며, 삭제된 파일은 복구할 수 없어요.`
    : '';
  for (const file of state.files) {
    file.expires_at = settings.enabled ? file.modified + settings.retention_hours * 3600 : null;
  }
}

function updateSettingsControls() {
  $('#auto-delete-enabled').disabled = state.savingSettings;
  $('#retention-hours').disabled = state.savingSettings || !$('#auto-delete-enabled').checked;
  for (const selector of ['#save-settings', '#cancel-settings', '#close-settings']) {
    $(selector).disabled = state.savingSettings;
  }
  $('#save-settings').textContent = state.savingSettings ? '저장 중…' : '저장';
  $('#settings-form').setAttribute('aria-busy', String(state.savingSettings));
}

$('#open-settings').addEventListener('click', async () => {
  $('#open-settings').disabled = true;
  try {
    const settings = await api('/api/settings');
    applySettings(settings);
    renderFiles();
    $('#auto-delete-enabled').checked = settings.enabled;
    $('#retention-hours').value = String(settings.retention_hours);
    $('#settings-error').hidden = true;
    updateSettingsControls();
    $('#settings-dialog').showModal();
  } catch (failure) {
    toast(failure.message);
  } finally {
    $('#open-settings').disabled = false;
  }
});
$('#auto-delete-enabled').addEventListener('change', updateSettingsControls);
for (const selector of ['#close-settings', '#cancel-settings']) {
  $(selector).addEventListener('click', () => $('#settings-dialog').close());
}
$('#settings-dialog').addEventListener('cancel', (event) => {
  if (state.savingSettings) event.preventDefault();
});
$('#settings-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (state.savingSettings) return;
  state.savingSettings = true;
  $('#settings-error').hidden = true;
  updateSettingsControls();
  try {
    const settings = await api('/api/settings', {
      method: 'PUT',
      headers: { 'X-CSRF-Token': state.csrf, 'Content-Type': 'application/json' },
      body: JSON.stringify({
        enabled: $('#auto-delete-enabled').checked,
        retention_hours: Number($('#retention-hours').value),
      }),
    });
    applySettings(settings);
    renderFiles();
    $('#settings-dialog').close();
    toast('설정을 저장했어요.');
    await loadFiles();
  } catch (failure) {
    $('#settings-error').textContent =
      failure instanceof TypeError
        ? '설정 저장 결과를 확인하지 못했어요. 잠시 후 설정창을 다시 열어 확인해 주세요.'
        : failure.message;
    $('#settings-error').hidden = false;
  } finally {
    state.savingSettings = false;
    updateSettingsControls();
  }
});

function updateJob(job) {
  job.label.textContent =
    job.status === 'queued'
      ? '대기 중'
      : job.status === 'uploading'
        ? job.percent === 100
          ? '저장 중…'
          : `${job.percent}%`
        : job.status === 'done'
          ? '완료'
          : job.status === 'cancelled'
            ? '취소됨'
            : '실패';
  job.row.dataset.status = job.status;
  job.progress.value = job.percent;
  job.progress.hidden = !['queued', 'uploading'].includes(job.status);
  job.cancel.hidden = !['queued', 'uploading'].includes(job.status);
  job.message.textContent =
    job.error ||
    (job.stored && job.stored !== job.file.name
      ? `${bytes(job.file.size)} · ${job.stored}(으)로 저장됨`
      : bytes(job.file.size));
}

function addFiles(files) {
  for (const file of files) {
    const job = { file, percent: 0, status: 'queued', error: '', xhr: null };
    job.row = element('li', 'upload-job');
    const symbol = element('div', 'job-icon');
    symbol.append(icon('file'));
    const info = element('div', 'job-info');
    const name = element('strong', 'job-name', file.name);
    name.title = file.name;
    job.message = element('span', 'job-message');
    job.progress = element('progress');
    job.progress.max = 100;
    job.progress.setAttribute('aria-label', `${file.name} 업로드 진행률`);
    info.append(name, job.message, job.progress);
    job.label = element('span', 'job-status');
    job.cancel = element('button', 'icon-button cancel-upload', '×');
    job.cancel.type = 'button';
    job.cancel.setAttribute('aria-label', `${file.name} 업로드 취소`);
    job.cancel.addEventListener('click', () => {
      if (job.xhr) job.xhr.abort();
      else {
        job.status = 'cancelled';
        updateJob(job);
      }
    });
    if (file.size > state.limit) {
      job.status = 'error';
      job.error = `최대 ${bytes(state.limit)}까지 업로드할 수 있어요.`;
    }
    job.row.append(symbol, info, job.label, job.cancel);
    $('#upload-list').append(job.row);
    state.jobs.push(job);
    updateJob(job);
  }
  $('#upload-panel').hidden = state.jobs.length === 0;
  processQueue();
}

function uploadFile(job) {
  return new Promise((resolve) => {
    const xhr = new XMLHttpRequest();
    job.xhr = xhr;
    job.status = 'uploading';
    updateJob(job);
    xhr.open('PUT', `/api/files/${encodeURIComponent(job.file.name)}`);
    xhr.setRequestHeader('X-CSRF-Token', state.csrf);
    xhr.setRequestHeader('Content-Type', 'application/octet-stream');
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) {
        job.percent = Math.round((event.loaded / event.total) * 100);
        updateJob(job);
      }
    };
    function finish(status, error = '') {
      job.status = status;
      job.error = error;
      job.xhr = null;
      updateJob(job);
      resolve();
    }
    xhr.onload = () => {
      if (xhr.status === 401) {
        expire();
        finish('error', '로그인이 만료되었어요.');
        return;
      }
      let body = {};
      try {
        body = JSON.parse(xhr.responseText);
      } catch {
        /* 프록시의 HTML 오류 응답도 처리합니다. */
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        job.percent = 100;
        job.stored = body.name;
        finish('done');
      } else {
        finish(
          'error',
          typeof body.detail === 'string'
            ? body.detail
            : `업로드하지 못했어요. (HTTP ${xhr.status})`,
        );
      }
    };
    xhr.onerror = () =>
      finish(
        'error',
        '연결이 끊겼어요. 파일 목록을 새로고침해 저장 여부를 확인한 뒤 다시 선택해 주세요.',
      );
    xhr.onabort = () =>
      finish('cancelled', '전송을 취소했어요. 이미 저장된 파일은 목록에서 확인할 수 있어요.');
    xhr.send(job.file);
  });
}

async function processQueue() {
  if (state.running || state.expired) return;
  state.running = true;
  let completed = 0;
  try {
    let job;
    while (!state.expired && (job = state.jobs.find((item) => item.status === 'queued'))) {
      await uploadFile(job);
      if (job.status === 'done') completed++;
      await loadFiles();
    }
    if (completed) toast(`${completed}개 파일을 보관했어요.`);
  } finally {
    state.running = false;
  }
}

$('#choose-files').disabled = true;
$('#choose-files').addEventListener('click', () => $('#file-input').click());
$('#file-input').addEventListener('change', (event) => {
  addFiles([...event.target.files]);
  event.target.value = '';
});
$('#search').addEventListener('input', renderFiles);
$('#sort').addEventListener('change', renderFiles);
$('#refresh').addEventListener('click', loadFiles);
$('#clear-uploads').addEventListener('click', () => {
  state.jobs = state.jobs.filter((job) => {
    if (['queued', 'uploading'].includes(job.status)) return true;
    job.row.remove();
    return false;
  });
  $('#upload-panel').hidden = state.jobs.length === 0;
});
$('#logout').addEventListener('click', async () => {
  if (state.running) {
    toast('업로드가 끝난 후 로그아웃해 주세요.');
    return;
  }
  try {
    await api('/api/logout', { method: 'POST', headers: { 'X-CSRF-Token': state.csrf } });
    window.location.replace('/login');
  } catch (failure) {
    toast(failure.message);
  }
});

// 파일을 영역 바깥에 놓아도 브라우저가 해당 파일로 이동하지 않게 합니다.
let dragDepth = 0;
document.addEventListener('dragenter', (event) => {
  if (!event.dataTransfer?.types.includes('Files')) return;
  event.preventDefault();
  dragDepth++;
  $('#dropzone').classList.add('dragging');
});
document.addEventListener('dragover', (event) => {
  if (!event.dataTransfer?.types.includes('Files')) return;
  event.preventDefault();
  event.dataTransfer.dropEffect = 'copy';
});
document.addEventListener('dragleave', () => {
  if (--dragDepth <= 0) {
    dragDepth = 0;
    $('#dropzone').classList.remove('dragging');
  }
});
document.addEventListener('drop', (event) => {
  event.preventDefault();
  dragDepth = 0;
  $('#dropzone').classList.remove('dragging');
  if (!state.csrf) return;
  const items = [...(event.dataTransfer?.items || [])];
  if (items.some((item) => item.webkitGetAsEntry?.()?.isDirectory)) {
    toast('폴더 대신 폴더 안의 파일을 선택해 주세요.');
  }
  const files = items.length
    ? items
        .filter((item) => !item.webkitGetAsEntry?.()?.isDirectory)
        .map((item) => item.getAsFile())
        .filter(Boolean)
    : [...(event.dataTransfer?.files || [])];
  addFiles(files);
});

window.addEventListener('beforeunload', (event) => {
  if (state.running && !state.expired) {
    event.preventDefault();
    event.returnValue = '';
  }
});

async function initialize() {
  try {
    const session = await api('/api/session');
    state.csrf = session.csrf_token;
    state.limit = session.max_upload_bytes;
    $('#upload-limit').textContent = `파일당 최대 ${bytes(state.limit)} · 여러 파일 선택 가능`;
    $('#choose-files').disabled = false;
    $('#open-settings').disabled = false;
    await loadFiles();
    setInterval(() => {
      if (!document.hidden && !state.expired && !$('#refresh').disabled) loadFiles();
    }, 60_000);
  } catch {
    if (!state.expired) {
      $('#list-error').textContent = '서버에 연결하지 못했어요. 페이지를 새로고침해 주세요.';
      $('#list-error').hidden = false;
    }
  }
}
initialize();
