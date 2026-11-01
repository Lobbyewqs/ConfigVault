"""ConfigVault CLI: byte-preserving manual sync for one explicitly selected CFG."""
import argparse
import base64
import getpass
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

MAX_BYTES = 256 * 1024
STATE_NAME = '.configvault.json'

class VaultError(Exception):
    pass


class HttpError(VaultError):
    def __init__(self, status, detail):
        self.status = status
        super().__init__('服务器返回 HTTP %s：%s' % (status, detail))


def sha(data):
    return hashlib.sha256(data).hexdigest()


def atomic_json(path, obj):
    path = Path(path)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.configvault-', delete=False) as f:
            name = f.name
            json.dump(obj, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def read_state(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError) as e:
        raise VaultError('无法读取客户端状态：' + str(e)) from e


def check_url(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.hostname:
        raise VaultError('服务器地址无效。')
    if parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in ('localhost', '127.0.0.1', '::1')):
        raise VaultError('只允许 HTTPS，或回环地址上的 HTTP。')
    return url.rstrip('/')


def check_file(value):
    p = Path(value).expanduser().resolve(strict=True)
    if p.suffix.lower() != '.cfg' or not p.is_file():
        raise VaultError('请选择已有的 .cfg 普通文件。')
    return p


def read_cfg(path):
    data = Path(path).read_bytes()
    if len(data) > MAX_BYTES:
        raise VaultError('文件超过 256 KiB。')
    return data


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Api:
    def __init__(self, server, token):
        self.server, self.token = check_url(server), token
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, method, path, data=None, headers=None):
        h = {'Authorization': 'Bearer ' + self.token, **(headers or {})}
        request = urllib.request.Request(self.server + path, data=data, headers=h, method=method)
        try:
            with self.opener.open(request, timeout=30) as response:
                return response.read(), response.headers
        except urllib.error.HTTPError as e:
            detail = e.read(4096).decode('utf-8', errors='replace')
            e.close()
            raise HttpError(e.code, detail) from e
        except urllib.error.URLError as e:
            raise VaultError('服务器连接失败：' + str(e.reason)) from e

    def json(self, method, path, obj=None, headers=None):
        data = None if obj is None else json.dumps(obj, ensure_ascii=False).encode('utf-8')
        body, _ = self.request(method, path, data, {'Content-Type': 'application/json', **(headers or {})})
        return json.loads(body)

    def repo(self, state):
        return self.json('GET', '/api/config-repositories/' + str(state['repoId']))

    def content(self, state, version):
        url = '/api/config-repositories/%s/versions/%s/content' % (state['repoId'], version)
        body, headers = self.request('GET', url)
        expected = headers.get('X-Content-SHA256')
        size = headers.get('X-Content-Size')
        if not expected or not size or not size.isdigit() or int(size) != len(body) or sha(body) != expected:
            raise VaultError('下载长度或 SHA-256 不匹配，已取消应用。')
        return body

    def push(self, state, pending):
        boundary = 'cv' + secrets.token_hex(16)
        parts = []
        def field(name, value, filename=None):
            disp = 'Content-Disposition: form-data; name="' + name + '"'
            if filename:
                disp += '; filename="config.cfg"'
            parts.append(('--' + boundary + '\r\n' + disp + '\r\n' + ('Content-Type: application/octet-stream\r\n' if filename else '') + '\r\n').encode() + value + b'\r\n')
        field('baseVersionId', str(pending['baseVersionId'] or '').encode())
        field('message', pending['message'].encode('utf-8'))
        field('encodingLabel', pending['encodingLabel'].encode())
        field('file', base64.b64decode(pending['content']), True)
        parts.append(('--' + boundary + '--\r\n').encode())
        body, _ = self.request('POST', '/api/config-repositories/%s/versions' % state['repoId'], b''.join(parts),
                               {'Content-Type': 'multipart/form-data; boundary=' + boundary, 'Idempotency-Key': pending['key']})
        return json.loads(body)


def recover(state_path, state):
    pending = state.get('applyJournal')
    if not pending:
        return state
    current = sha(read_cfg(state['file']))
    if current == pending['newHash']:
        state['baseVersionId'] = pending['versionId']
        state['baseContentHash'] = current
        state.pop('preview', None)
        state.pop('applyJournal')
        atomic_json(state_path, state)
    elif current == pending['oldHash']:
        state.pop('applyJournal')
        atomic_json(state_path, state)
    else:
        raise VaultError('上次应用中断，目标文件与操作记录均不一致；请检查备份和状态文件。')
    return state


def preview(state_path, state, api):
    repo = api.repo(state)
    head = repo['headVersionId']
    if head is None:
        raise VaultError('云端尚无版本。')
    data = api.content(state, head)
    local_hash = sha(read_cfg(state['file']))
    info = {'versionId': head, 'cloudHash': sha(data), 'localHash': local_hash,
            'localChanged': local_hash != state.get('baseContentHash'),
            'cloudChanged': head != state.get('baseVersionId')}
    state['preview'] = info
    atomic_json(state_path, state)
    return info


def apply(state_path, state, api, accept_cloud=False):
    if state.get('pendingPush'):
        raise VaultError('存在结果未确认的推送；请先重试 push 或检查云端历史后执行 abandon-push。')
    p = state.get('preview')
    if not p:
        raise VaultError('请先运行 pull --dry-run。')
    path = Path(state['file'])
    old = read_cfg(path)
    if sha(old) != p['localHash']:
        raise VaultError('预览后本地文件发生变化；请重新预览。')
    if api.repo(state)['headVersionId'] != p['versionId']:
        raise VaultError('预览后云端版本发生变化；请重新预览。')
    if p['localChanged'] and sha(old) != p['cloudHash'] and not accept_cloud:
        raise VaultError('本地存在改动；明确采用云端请使用 --accept-cloud。')
    data = api.content(state, p['versionId'])
    if sha(data) != p['cloudHash']:
        raise VaultError('下载内容与预览不一致。')
    if data == old:
        state['baseVersionId'], state['baseContentHash'] = p['versionId'], sha(data)
        state.pop('preview', None)
        atomic_json(state_path, state)
        return None
    directory = path.parent / '.configvault-backups'
    directory.mkdir(exist_ok=True)
    backup = directory / (path.name + '.' + secrets.token_hex(16) + '.bak')
    with backup.open('xb') as f:
        f.write(old)
        f.flush()
        os.fsync(f.fileno())
    if sha(backup.read_bytes()) != sha(old):
        raise VaultError('备份校验失败，已取消应用。')
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.configvault-', suffix='.tmp', delete=False) as f:
            tmp = Path(f.name)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if sha(read_cfg(path)) != p['localHash']:
            raise VaultError('备份期间本地文件发生变化，已取消应用。')
        state['applyJournal'] = {'oldHash': sha(old), 'newHash': sha(data), 'versionId': p['versionId'], 'backup': str(backup)}
        atomic_json(state_path, state)
        if sha(read_cfg(path)) != p['localHash']:
            raise VaultError('替换前本地文件发生变化，已取消应用。')
        os.replace(tmp, path)
        if sha(read_cfg(path)) != sha(data):
            raise VaultError('替换后校验失败；请检查备份和状态文件。')
        state['baseVersionId'], state['baseContentHash'] = p['versionId'], sha(data)
        state.pop('applyJournal')
        state.pop('preview', None)
        atomic_json(state_path, state)
        return str(backup)
    finally:
        if tmp and tmp.exists():
            tmp.unlink()


def do_push(state_path, state, api, message, encoding):
    pending = state.get('pendingPush')
    if pending is None:
        data = read_cfg(state['file'])
        pending = {'baseVersionId': state.get('baseVersionId'), 'content': base64.b64encode(data).decode(),
                   'message': message, 'encodingLabel': encoding, 'key': secrets.token_urlsafe(24)}
        state['pendingPush'] = pending
        atomic_json(state_path, state)
    try:
        result = api.push(state, pending)
    except HttpError as error:
        if 400 <= error.status < 500 and error.status not in (408, 429):
            state.pop('pendingPush')
            atomic_json(state_path, state)
        raise
    sent_hash = sha(base64.b64decode(pending['content']))
    if result['sha256'] != sent_hash:
        raise VaultError('服务器返回的内容哈希不匹配，保留待重试请求。')
    if state.get('baseVersionId') in (pending['baseVersionId'], result['id']):
        state['baseVersionId'], state['baseContentHash'] = result['id'], sent_hash
    else:
        result = dict(result)
        result['baseNotAdvanced'] = True
    state.pop('pendingPush')
    state.pop('preview', None)
    atomic_json(state_path, state)
    return result


def main():
    parser = argparse.ArgumentParser(description='ConfigVault：手动、安全地同步单个 CFG 文件')
    parser.add_argument('--state', default=STATE_NAME, help='客户端状态文件路径')
    subs = parser.add_subparsers(dest='command', required=True)
    init = subs.add_parser('init')
    init.add_argument('--server', required=True)
    init.add_argument('--repo', required=True, type=int)
    init.add_argument('--file', required=True)
    subs.add_parser('status')
    push = subs.add_parser('push')
    push.add_argument('--message', default='')
    push.add_argument('--encoding', choices=['utf-8', 'utf-8-bom', 'gb18030', 'unknown'], default='unknown')
    pull = subs.add_parser('pull')
    modes = pull.add_mutually_exclusive_group(required=True)
    modes.add_argument('--dry-run', action='store_true')
    modes.add_argument('--apply', action='store_true')
    pull.add_argument('--accept-cloud', action='store_true')
    subs.add_parser('history')
    subs.add_parser('abandon-push', help='明确放弃保留的待重试请求，先检查服务端历史')
    subs.add_parser('backup-list')
    restore = subs.add_parser('backup-restore')
    restore.add_argument('backup_id')
    args = parser.parse_args()
    try:
        state_path = Path(args.state).resolve()
        if args.command == 'init':
            if state_path.exists():
                raise VaultError('状态文件已存在，请选择其他 --state 路径。')
            state = {'server': check_url(args.server), 'repoId': args.repo, 'file': str(check_file(args.file)),
                     'baseVersionId': None, 'baseContentHash': None}
            atomic_json(state_path, state)
            print('初始化完成。首次推送须指向空库；已有库请先预览并应用云端。')
            return
        state = recover(state_path, read_state(state_path))
        if args.command == 'abandon-push':
            if state.pop('pendingPush', None) is None:
                raise VaultError('没有待重试的推送请求。')
            atomic_json(state_path, state)
            print('已放弃待重试请求；请先检查云端历史并重新处理冲突。')
            return
        if args.command == 'backup-list':
            directory = Path(state['file']).parent / '.configvault-backups'
            for p in sorted(directory.glob(Path(state['file']).name + '.*.bak')):
                print(p.name)
            return
        if args.command == 'backup-restore':
            name = args.backup_id
            if Path(name).name != name or not name.startswith(Path(state['file']).name + '.') or not name.endswith('.bak'):
                raise VaultError('备份 ID 无效。')
            source = Path(state['file']).parent / '.configvault-backups' / name
            old = read_cfg(state['file'])
            data = source.read_bytes()
            if len(data) > MAX_BYTES:
                raise VaultError('备份过大。')
            target = Path(state['file'])
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix='.configvault-', delete=False) as f:
                tmp = Path(f.name); f.write(data); f.flush(); os.fsync(f.fileno())
            try:
                safety = source.parent / (target.name + '.' + secrets.token_hex(16) + '.bak')
                with safety.open('xb') as f:
                    f.write(old); f.flush(); os.fsync(f.fileno())
                if sha(safety.read_bytes()) != sha(old) or sha(read_cfg(target)) != sha(old):
                    raise VaultError('备份或本地文件校验失败。')
                os.replace(tmp, target)
            finally:
                if tmp.exists(): tmp.unlink()
            print('备份已恢复；恢复前内容已备份。状态将显示本地变化。')
            return
        token = getpass.getpass('客户端令牌（输入不回显）：')
        if not token:
            raise VaultError('令牌不能为空。')
        api = Api(state['server'], token)
        if args.command == 'status':
            repo = api.repo(state)
            print(json.dumps({'baseVersionId': state.get('baseVersionId'), 'headVersionId': repo['headVersionId'],
                              'localChanged': sha(read_cfg(state['file'])) != state.get('baseContentHash'),
                              'cloudChanged': repo['headVersionId'] != state.get('baseVersionId'),
                              'pendingPush': bool(state.get('pendingPush'))}, ensure_ascii=False, indent=2))
        elif args.command == 'push':
            print(json.dumps(do_push(state_path, state, api, args.message, args.encoding), ensure_ascii=False, indent=2))
        elif args.command == 'pull':
            if args.dry_run:
                print(json.dumps(preview(state_path, state, api), ensure_ascii=False, indent=2))
            else:
                backup = apply(state_path, state, api, args.accept_cloud)
                print('应用完成。' + (' 备份：' + backup if backup else ' 内容一致，无需替换。'))
        elif args.command == 'history':
            print(json.dumps(api.json('GET', '/api/config-repositories/%s/versions' % state['repoId']), ensure_ascii=False, indent=2))
    except (VaultError, OSError, ValueError, KeyError) as e:
        print('错误：' + str(e), file=sys.stderr)
        sys.exit(1)

if __name__ == '__main__':
    main()
