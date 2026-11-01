'use strict';

const $ = selector => document.querySelector(selector);
let csrf = '';
let selected = null;
let pendingUpload = null;
let pendingRestore = null;

function notice(message) {
  $('#notice').textContent = message;
}

async function request(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (!['GET', 'HEAD'].includes(options.method || 'GET')) headers.set('X-XSRF-TOKEN', csrf);
  const response = await fetch(path, { credentials: 'same-origin', ...options, headers });
  if (!response.ok) {
    let detail = '';
    try {
      const body = await response.json();
      detail = body.message || body.error || '';
    } catch {}
    const error = new Error(`HTTP ${response.status}${detail ? `：${detail}` : ''}`);
    error.status = response.status;
    throw error;
  }
  const body = await response.text();
  return body ? JSON.parse(body) : null;
}

async function csrfRefresh() {
  csrf = (await request('/api/csrf')).token;
}

const json = data => ({ method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) });
const key = () => crypto.randomUUID().replaceAll('-', '');

function actionButton(label, handler, className = 'row-action') {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = className;
  button.textContent = label;
  button.addEventListener('click', () => handler().catch(error => notice(error.message)));
  return button;
}

function renderRepositories(repositories) {
  const list = $('#repositories');
  list.replaceChildren();
  if (!repositories.length) {
    const empty = document.createElement('li');
    empty.className = 'list-empty';
    empty.textContent = '还没有配置库，在上方输入名称新建。';
    list.append(empty);
    return;
  }
  for (const repository of repositories) {
    const item = document.createElement('li');
    const button = actionButton('', () => openRepo(repository.id), 'repo-option');
    button.dataset.repoId = repository.id;
    const active = selected !== null && selected.id === repository.id;
    if (active) button.setAttribute('aria-current', 'page');
    button.setAttribute('aria-label', `查看配置库 ${repository.name}`);
    const name = document.createElement('span');
    name.className = 'repo-name';
    name.textContent = repository.name;
    const id = document.createElement('span');
    id.className = 'repo-id';
    id.textContent = `#${repository.id}`;
    const action = document.createElement('span');
    action.className = 'repo-action';
    action.textContent = active ? '当前' : '查看';
    button.append(name, id, action);
    item.append(button);
    list.append(item);
  }
}

function renderTokens(tokens) {
  const list = $('#tokens');
  list.replaceChildren();
  if (!tokens.length) {
    const empty = document.createElement('li');
    empty.className = 'list-empty';
    empty.textContent = '暂无令牌。';
    list.append(empty);
    return;
  }
  for (const token of tokens) {
    const item = document.createElement('li');
    const label = document.createElement('span');
    label.className = 'token-label';
    label.textContent = token.label;
    const state = document.createElement('small');
    state.textContent = token.revokedAt ? '已撤销' : `有效期至 ${String(token.expiresAt).replace('T', ' ').slice(0, 10)}`;
    label.append(state);
    item.append(label);
    if (!token.revokedAt) {
      item.append(actionButton('撤销', async () => {
        await request(`/api/tokens/${token.id}`, { method: 'DELETE' });
        await refresh();
        notice('令牌已撤销。');
      }));
    }
    list.append(item);
  }
}

async function refresh() {
  const repositories = await request('/api/config-repositories');
  $('#auth').hidden = true;
  $('#app').hidden = false;
  $('#logout').hidden = false;
  renderRepositories(repositories);
  renderTokens(await request('/api/tokens'));
}

async function openRepo(id) {
  const repository = await request(`/api/config-repositories/${id}`);
  const versions = await request(`/api/config-repositories/${id}/versions?size=100`);
  selected = repository;
  $('#emptyRepo').hidden = true;
  $('#repo').hidden = false;
  $('#repoTitle').textContent = repository.name;
  $('#repoHead').textContent = `配置库 #${id}　/　${repository.headVersionId === null ? '尚无版本' : `当前为 v${repository.headRevision}`}`;
  for (const button of document.querySelectorAll('.repo-option')) {
    const active = Number(button.dataset.repoId) === id;
    if (active) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
    button.querySelector('.repo-action').textContent = active ? '当前' : '查看';
  }

  const list = $('#versions');
  list.replaceChildren();
  $('#versionCount').textContent = versions.length ? `${versions.length} 个版本` : '';
  if (!versions.length) {
    const empty = document.createElement('div');
    empty.className = 'version-empty';
    empty.textContent = '还没有版本，上传一个 CFG 文件开始记录。';
    list.append(empty);
  }
  for (const version of versions) {
    const row = document.createElement('div');
    row.className = 'version';
    const number = document.createElement('span');
    number.className = 'version-number';
    number.textContent = `v${version.revision}`;
    const body = document.createElement('div');
    body.className = 'version-body';
    const message = document.createElement('div');
    message.className = 'version-message';
    message.textContent = version.message || '无备注';
    const meta = document.createElement('div');
    meta.className = 'version-meta';
    meta.textContent = `#${version.id}　/　${version.byteSize} 字节　/　${version.encodingLabel}${version.restoredFromId ? `　/　恢复自 #${version.restoredFromId}` : ''}`;
    body.append(message, meta);
    const link = document.createElement('a');
    link.href = `/api/config-repositories/${id}/versions/${version.id}/content`;
    link.textContent = '下载';
    link.setAttribute('aria-label', `下载版本 v${version.revision}`);
    row.append(number, body, link);
    list.append(row);
  }
  for (const select of document.querySelectorAll('#diffForm select, #restoreForm select')) {
    select.replaceChildren();
    select.disabled = !versions.length;
    for (const version of versions) {
      const option = document.createElement('option');
      option.value = version.id;
      option.textContent = `v${version.revision}（#${version.id}）`;
      select.append(option);
    }
  }
  $('#diffForm button').disabled = !versions.length;
  $('#restoreForm button').disabled = !versions.length;
  $('#diff').textContent = '';
}

function hasPendingWrite() {
  return (pendingUpload && pendingUpload.id === selected.id) || (pendingRestore && pendingRestore.id === selected.id);
}

for (const button of document.querySelectorAll('[data-close]')) {
  button.addEventListener('click', () => document.getElementById(button.dataset.close).close());
}

$('#renameRepo').addEventListener('click', () => {
  $('#renameInput').value = selected.name;
  $('#renameError').textContent = '';
  $('#renameDialog').showModal();
  $('#renameInput').select();
});

$('#renameForm').addEventListener('submit', async event => {
  event.preventDefault();
  const button = event.target.querySelector('[type=submit]');
  button.disabled = true;
  let confirmed = false;
  try {
    const repository = await request(`/api/config-repositories/${selected.id}`, {
      ...json({name: $('#renameInput').value}), method: 'PATCH'
    });
    confirmed = true;
    selected = repository;
    $('#repoTitle').textContent = repository.name;
    $('#renameDialog').close();
    await refresh();
    notice('配置库名称已更新。');
  } catch (error) {
    if (confirmed) notice(`名称已更新，但列表刷新失败：${error.message}`);
    else $('#renameError').textContent = `重命名失败：${error.message}`;
  }
  finally { button.disabled = false; }
});

$('#deleteRepo').addEventListener('click', () => {
  if (hasPendingWrite()) {
    notice('该配置库有结果未确认的操作，请先重试或检查历史后刷新页面。');
    return;
  }
  $('#deleteDescription').textContent = `确定删除“${selected.name}”吗？当前有 ${selected.headRevision} 个版本。`;
  $('#deleteDialog').showModal();
});

$('#deleteForm').addEventListener('submit', async event => {
  event.preventDefault();
  const button = event.target.querySelector('[type=submit]');
  button.disabled = true;
  const id = selected.id;
  let confirmed = false;
  try {
    await request(`/api/config-repositories/${id}`, {
      ...json({baseVersionId: selected.headVersionId}), method: 'DELETE'
    });
    confirmed = true;
    selected = null;
    $('#deleteDialog').close();
    $('#repo').hidden = true;
    $('#emptyRepo').hidden = false;
    await refresh();
    notice('配置库及其版本已删除。');
  } catch (error) {
    if ($('#deleteDialog').open) $('#deleteDialog').close();
    notice(confirmed ? `配置库已删除，但列表刷新失败：${error.message}` : `删除失败：${error.message}`);
  } finally { button.disabled = false; }
});

$('#authForm').addEventListener('submit', async event => {
  event.preventDefault();
  try {
    await request('/api/session', { method: 'POST', body: new URLSearchParams(new FormData(event.target)) });
    await csrfRefresh();
    event.target.reset();
    await refresh();
    notice('登录成功。');
  } catch (error) { notice(error.message); }
});

$('#register').addEventListener('click', async () => {
  const form = $('#authForm');
  if (!form.reportValidity()) return;
  try {
    await request('/api/register', json(Object.fromEntries(new FormData(form))));
    notice('注册成功，请登录。');
  } catch (error) { notice(error.message); }
});

$('#logout').addEventListener('click', async () => {
  try {
    await request('/api/logout', { method: 'POST' });
    location.reload();
  } catch (error) { notice(error.message); }
});

$('#newRepo').addEventListener('submit', async event => {
  event.preventDefault();
  try {
    const repository = await request('/api/config-repositories', json(Object.fromEntries(new FormData(event.target))));
    event.target.reset();
    await refresh();
    await openRepo(repository.id);
    notice('配置库已创建。');
  } catch (error) { notice(error.message); }
});

$('#newToken').addEventListener('submit', async event => {
  event.preventDefault();
  try {
    const token = await request('/api/tokens', json(Object.fromEntries(new FormData(event.target))));
    $('#tokenOutput').textContent = `请立即复制，令牌不会再次显示：\n${token.token}`;
    event.target.reset();
    await refresh();
  } catch (error) { notice(error.message); }
});

$('#upload').addEventListener('submit', async event => {
  event.preventDefault();
  let confirmed = false;
  try {
    if (pendingUpload && pendingUpload.id !== selected.id) throw new Error('上一请求结果未知，请先回到原配置库重试。');
    if (!pendingUpload) {
      const form = new FormData(event.target);
      form.set('baseVersionId', selected.headVersionId ?? '');
      pendingUpload = { id: selected.id, key: key(), form };
    }
    const result = await request(`/api/config-repositories/${pendingUpload.id}/versions`, {
      method: 'POST', headers: { 'Idempotency-Key': pendingUpload.key }, body: pendingUpload.form
    });
    confirmed = true;
    pendingUpload = null;
    event.target.reset();
    await openRepo(selected.id);
    notice(result.noChange ? '文件与当前版本相同，没有创建新版本。' : '版本已上传。');
  } catch (error) {
    if (confirmed) {
      notice(`版本已上传，但刷新失败：${error.message}`);
    } else if (error.status >= 400 && error.status < 500 && ![408, 429].includes(error.status)) {
      pendingUpload = null;
      notice(`上传失败：${error.message}`);
    } else notice(`上传结果未确认，保持页面打开并再次点击可重试：${error.message}`);
  }
});

$('#diffForm').addEventListener('submit', async event => {
  event.preventDefault();
  try {
    const params = new URLSearchParams(new FormData(event.target));
    const diff = await request(`/api/config-repositories/${selected.id}/diff?${params}`);
    $('#diff').textContent = diff.lines.map(line => ({ same: '  ', add: '+ ', remove: '- ' }[line.type] + line.text)).join('\n');
  } catch (error) { notice(error.message); }
});

$('#restoreForm').addEventListener('submit', async event => {
  event.preventDefault();
  let confirmed = false;
  try {
    if (pendingRestore && pendingRestore.id !== selected.id) throw new Error('上一请求结果未知，请先回到原配置库重试。');
    if (!pendingRestore) {
      const form = new FormData(event.target);
      pendingRestore = { id: selected.id, key: key(), data: {
        sourceVersionId: Number(form.get('sourceVersionId')), baseVersionId: selected.headVersionId, message: form.get('message')
      } };
    }
    await request(`/api/config-repositories/${pendingRestore.id}/restore`, {
      ...json(pendingRestore.data), headers: { 'Content-Type': 'application/json', 'Idempotency-Key': pendingRestore.key }
    });
    confirmed = true;
    pendingRestore = null;
    await openRepo(selected.id);
    notice('已恢复为新版本。');
  } catch (error) {
    if (confirmed) {
      notice(`已恢复为新版本，但刷新失败：${error.message}`);
    } else if (error.status >= 400 && error.status < 500 && ![408, 429].includes(error.status)) {
      pendingRestore = null;
      notice(`恢复失败：${error.message}`);
    } else notice(`恢复结果未确认，保持页面打开并再次点击可重试：${error.message}`);
  }
});

(async () => {
  try {
    await csrfRefresh();
    await refresh();
  } catch (error) {
    if (error.status !== 401) notice(error.message);
  }
})();
