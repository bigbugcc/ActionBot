// Exercise the actual ActionBot entry point with GitHub and cache adapters.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const yaml = require('js-yaml');

const root = path.resolve(__dirname, '..');
const entry = fs.readFileSync(path.join(root, 'bin/main.js'), 'utf8');
const workflow = fs.readFileSync(path.join(root, '.github/workflows/3x-ui-Docker.yml'), 'utf8');
const sha = 'a'.repeat(40);

async function invoke({ cached = false, failRelease = false, prerelease = false, releaseMode = true,
    failDispatch = false, dispatchStatus = 204, saveResult = 1, failSave = false, cacheEntries,
    forceActive = 0, mode = 'trigger', repositoryUrl = 'https://github.com/MHSanaei/3x-ui.git',
    branch = '', failCacheRead = false } = {}) {
    const calls = [];
    const failures = [];
    const outputs = {};
    const url = new URL(repositoryUrl);
    const [owner, repo] = url.pathname.slice(1).replace(/\.git$/, '').split('/');
    const plainKey = `${owner}:${repo}@${sha}`;
    const releaseKey = `${owner}:${repo}@release-v3.9.0-${sha}`;
    const entries = cacheEntries ?? (cached ? [{ id: 10, key: releaseMode ? releaseKey : plainKey }] : []);
    const config = yaml.load(workflow);
    if (!releaseMode) delete config.env.repo_release;
    config.env.repo_url = repositoryUrl;
    config.env.force_active = forceActive;
    const files = yaml.dump(config);
    const octokit = { request: async (route, params) => {
        calls.push({ route, params });
        if (route.endsWith('/actions/workflows')) return { data: { workflows: [
            { id: 1, name: 'AutoTrigger' }, { id: 2, name: config.name }
        ] } };
        if (route.endsWith('/releases/latest')) {
            if (failRelease) throw new Error('GitHub unavailable');
            return { data: { tag_name: 'v3.9.0', draft: false, prerelease } };
        }
        if (route.endsWith('/commits')) return { data: [{ sha }] };
        if (route === 'GET /repos/{owner}/{repo}/actions/caches') {
            if (failCacheRead) throw new Error('Cache listing failed');
            return { data: { actions_caches: entries.slice((params.page - 1) * 100, params.page * 100) } };
        }
        if (route.endsWith('/dispatches')) {
            if (failDispatch) throw new Error('Dispatch denied');
            return { status: dispatchStatus };
        }
        if (route.startsWith('DELETE ') || route.endsWith('/cancel')) return { status: 204 };
        throw new Error(`Unexpected API route: ${route}`);
    } };
    const adapters = {
        octokit: { Octokit: function () { return octokit; } },
        '@actions/core': {
            getInput: name => ({ token: 'test', repository: 'bigbugcc/ActionBot', workflow: 'AutoTrigger',
                workspace: '/work', mode, repo_url: repositoryUrl, branch }[name] || ''),
            warning() {}, setFailed: message => failures.push(message),
            setOutput: (name, value) => { outputs[name] = value; }
        },
        '@actions/cache': { saveCache: async (_, key) => {
            calls.push({ route: 'SAVE_CACHE', params: { key } });
            if (failSave) throw new Error('Cache storage unavailable');
            return saveResult;
        } },
        fs: { promises: { readdir: async () => ['3x-ui-Docker.yml'] }, readFileSync: () => files,
            mkdirSync() {}, writeFileSync() {} },
        'js-yaml': yaml, path
    };
    const context = { require: name => {
        assert.ok(adapters[name], `Unexpected require: ${name}`);
        return adapters[name];
    }, module: { exports: {} }, process: { argv: [], env: { GITHUB_RUN_ID: '42' } },
    fetch: async url => {
        calls.push({ route: 'GITEE_COMMITS', params: { url } });
        return { json: async () => [{ sha }] };
    }, console: { log() {}, warn() {} } };
    vm.runInNewContext(entry, context);
    await context.module.exports.main().catch(error => failures.push(error.message));
    return { calls, failures, outputs, dispatches: calls.filter(call => call.route.endsWith('/dispatches')) };
}

test('new official release dispatches its tag and resolves tag source', async () => {
    const result = await invoke();
    assert.equal(result.dispatches.length, 1);
    assert.equal(result.dispatches[0].params.inputs.version_tag, 'v3.9.0');
    assert.equal(result.calls.find(call => call.route.endsWith('/commits')).params.sha, 'refs/tags/v3.9.0');
    assert.equal(result.failures.length, 0);
});

test('unchanged official release does not rebuild for main activity', async () => {
    const result = await invoke({ cached: true });
    assert.equal(result.dispatches.length, 0);
    assert.equal(result.calls.filter(call => call.route.endsWith('/commits') && !call.params.sha).length, 0);
});

test('API failure with empty cache never dispatches an unresolved version', async () => {
    const result = await invoke({ failRelease: true });
    assert.equal(result.dispatches.length, 0);
    assert.equal(result.failures.length, 1);
});

test('prerelease cannot become automatic latest', async () => {
    const result = await invoke({ prerelease: true });
    assert.equal(result.dispatches.length, 0);
    assert.equal(result.failures.length, 1);
});

test('workflows without repo_release retain default branch behavior', async () => {
    const result = await invoke({ releaseMode: false });
    assert.equal(result.dispatches.length, 1);
    assert.equal(Object.keys(result.dispatches[0].params.inputs).length, 0);
    assert.equal(result.calls.find(call => call.route.endsWith('/commits')).params.sha, undefined);
});

test('failed or unexpected dispatch responses never record an update key', async () => {
    for (const options of [{ failDispatch: true }, { dispatchStatus: 200 }]) {
        const result = await invoke(options);
        assert.equal(result.failures.length, 1);
        assert.equal(result.calls.filter(call => call.route === 'SAVE_CACHE').length, 0);
        assert.equal(result.calls.filter(call => call.route.startsWith('DELETE ')).length, 0);
    }
});

test('unconfirmed cache saves preserve previous update markers', async () => {
    for (const options of [{ saveResult: -1 }, { saveResult: null }, { failSave: true }]) {
        const result = await invoke({ ...options, cacheEntries: [{ id: 1, key: 'MHSanaei:3x-ui@old' }] });
        assert.equal(result.dispatches.length, 1);
        assert.equal(result.calls.filter(call => call.route.startsWith('DELETE ')).length, 0);
    }
});

test('old markers are deleted after saving and only in the exact repository namespace', async () => {
    const result = await invoke({ cacheEntries: [
        { id: 1, key: 'MHSanaei:3x-ui@old' },
        { id: 2, key: 'other-MHSanaei:3x-ui@old' },
        { id: 3, key: 'MHSanaei:3x-ui-extra@old' }
    ] });
    const deletion = result.calls.findIndex(call => call.route.startsWith('DELETE '));
    const saved = result.calls.findIndex(call => call.route === 'SAVE_CACHE');
    assert.ok(deletion > saved);
    assert.deepEqual(result.calls.filter(call => call.route.startsWith('DELETE ')).map(call => call.params.cache_id), [1]);
});

test('cached release on a later API page does not dispatch again', async () => {
    const entries = Array.from({ length: 100 }, (_, index) => ({ id: index, key: `other@${index}` }));
    entries.push({ id: 100, key: `MHSanaei:3x-ui@release-v3.9.0-${sha}` });
    const result = await invoke({ cacheEntries: entries });
    assert.equal(result.dispatches.length, 0);
    assert.equal(result.calls.filter(call => call.route === 'GET /repos/{owner}/{repo}/actions/caches').length, 2);
});

test('force_active one triggers cached workflows and two skips configured sources', async () => {
    const forced = await invoke({ cached: true, forceActive: '1' });
    assert.equal(forced.dispatches.length, 1);
    const ignored = await invoke({ forceActive: 2 });
    assert.equal(ignored.dispatches.length, 0);
    assert.equal(ignored.failures.length, 0);
    assert.equal(ignored.calls.filter(call => call.route.endsWith('/releases/latest')).length, 0);
});

test('cache listing failures stop dispatch and marker mutation', async () => {
    const result = await invoke({ failCacheRead: true });
    assert.deepEqual(result.failures, ['Cache listing failed']);
    assert.equal(result.dispatches.length, 0);
    assert.equal(result.calls.filter(call => call.route === 'SAVE_CACHE').length, 0);
});

test('check mode retains unchanged-source output and cancellation behavior', async () => {
    const result = await invoke({ mode: 'check', cached: true, releaseMode: false, branch: 'main' });
    assert.equal(result.outputs.updated, 'false');
    assert.equal(result.calls.filter(call => call.route.endsWith('/cancel')).length, 1);
    assert.equal(result.dispatches.length, 0);
    assert.equal(result.calls.filter(call => call.route === 'SAVE_CACHE').length, 0);
});

test('check mode records changed sources before removing their old marker', async () => {
    const result = await invoke({ mode: 'check', releaseMode: false,
        cacheEntries: [{ id: 1, key: 'MHSanaei:3x-ui@old' }] });
    assert.equal(result.outputs.updated, 'true');
    assert.equal(result.dispatches.length, 0);
    assert.ok(result.calls.findIndex(call => call.route.startsWith('DELETE ')) >
        result.calls.findIndex(call => call.route === 'SAVE_CACHE'));
});

test('Gitee sources remain supported in both trigger and check modes', async () => {
    for (const mode of ['trigger', 'check']) {
        const result = await invoke({ mode, releaseMode: false, branch: 'feature/test',
            repositoryUrl: 'https://gitee.com/zuohuaijun/Admin.NET' });
        const request = result.calls.find(call => call.route === 'GITEE_COMMITS');
        assert.ok(request.params.url.startsWith('https://gitee.com/api/v5/repos/zuohuaijun/Admin.NET/commits'));
        if (mode === 'check') assert.ok(request.params.url.endsWith('?sha=feature%2Ftest'));
        assert.equal(result.failures.length, 0);
        assert.equal(result.calls.find(call => call.route === 'SAVE_CACHE').params.key, `zuohuaijun:Admin.NET@${sha}`);
    }
});
