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

async function invoke({ cached = false, failRelease = false, prerelease = false, releaseMode = true } = {}) {
    const calls = [];
    const failures = [];
    const releaseKey = `MHSanaei:3x-ui@release-v3.9.0-${sha}`;
    const plainKey = `MHSanaei:3x-ui@${sha}`;
    const config = yaml.load(workflow);
    if (!releaseMode) delete config.env.repo_release;
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
        if (route === 'GET /repos/{owner}/{repo}/actions/caches') return { data: { actions_caches:
            cached ? [{ id: 10, key: releaseMode ? releaseKey : plainKey }] : [] } };
        if (route.endsWith('/dispatches')) return { status: 204 };
        throw new Error(`Unexpected API route: ${route}`);
    } };
    const adapters = {
        octokit: { Octokit: function () { return octokit; } },
        '@actions/core': {
            getInput: name => ({ token: 'test', repository: 'bigbugcc/ActionBot', workflow: 'AutoTrigger',
                workspace: '/work', mode: 'trigger' }[name] || ''),
            warning() {}, setFailed: message => failures.push(message), setOutput() {}
        },
        '@actions/cache': { saveCache: async () => 1 },
        fs: { promises: { readdir: async () => ['3x-ui-Docker.yml'] }, readFileSync: () => files,
            mkdirSync() {}, writeFileSync() {} },
        'js-yaml': yaml, path
    };
    const context = { require: name => {
        assert.ok(adapters[name], `Unexpected require: ${name}`);
        return adapters[name];
    }, process: { argv: [], env: {} }, Buffer, console: { log() {}, warn() {} } };
    vm.runInNewContext(entry.replace(/main\(\);\s*$/, 'globalThis.completion = main();'), context);
    await context.completion;
    return { calls, failures, dispatches: calls.filter(call => call.route.endsWith('/dispatches')) };
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
