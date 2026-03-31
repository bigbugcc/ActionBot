const { Octokit } = require("octokit");
const core = require("@actions/core");
const cache = require('@actions/cache');
const fs = require('fs');
const yaml = require('js-yaml');
const path = require('path');
const header = { 'X-GitHub-Api-Version': '2022-11-28' };
const workflowInfo = [];
const updatedKey = new Set();
let octokit = null;
let Owner = "";
let Repo = "";

function getRepoUrlInfo(repo_url) {
    const splitRepository = repo_url.replace('.git', '').split('/');
    if (splitRepository.length < 5) {
        core.setFailed(`this repo: ${repo_url} Invalid repository.`);
    }
    return {
        owner: splitRepository[3],
        name: splitRepository[4]
    }
}

async function fetchLatestCommitId(repo_url, branch) {
    const repo_info = getRepoUrlInfo(repo_url);
    const repo_owner = repo_info.owner;
    const repo_name = repo_info.name;

    if (repo_url.includes('github.com')) {
        const params = {
            owner: repo_owner,
            repo: repo_name,
            headers: header
        };
        if (branch) params.sha = branch;
        const response = await octokit.request('GET /repos/{owner}/{repo}/commits', params);
        const commitId = response.data[0].sha;
        const branchLabel = branch || 'default';
        console.log(`🎯 Github Repo: ${repo_owner}/${repo_name} [${branchLabel}] Last_CommitID：${commitId}`);
        return { commitId, key: `${repo_owner}:${repo_name}@${commitId}`.replace(/\s/g, '') };
    } else if (repo_url.includes('gitee.com')) {
        let url = `https://gitee.com/api/v5/repos/${repo_owner}/${repo_name}/commits`;
        if (branch) url += `?sha=${encodeURIComponent(branch)}`;
        const response = await fetch(url, {
            method: 'GET',
            headers: { 'Content-Type': 'application/json' }
        });
        const body = await response.json();
        const commitId = body[0].sha;
        const branchLabel = branch || 'default';
        console.log(`🎯 Gitee Repo: ${repo_owner}/${repo_name} [${branchLabel}] Last_CommitID：${commitId}`);
        return { commitId, key: `${repo_owner}:${repo_name}@${commitId}`.replace(/\s/g, '') };
    } else {
        throw new Error(`⚠️ ${repo_url} is Invalid repository url, please check the url`);
    }
}

async function getCommitIds() {
    const promises = workflowInfo.map(async (element) => {
        if (!element.repo_url) return;
        try {
            const result = await fetchLatestCommitId(element.repo_url);
            element.update_key = result.key;
        } catch (error) {
            element.status = 0;
            core.warning(`⚠️ ${element.repo_url} possible problems, log: ${error}`);
        }
    });

    await Promise.all(promises);
}

async function triggerWorkflow(element) {
    if (element.update_key) {
        updatedKey.add(element.update_key);
    }

    try {
        const response = await octokit.request('POST /repos/{owner}/{repo}/actions/workflows/{workflow_id}/dispatches', {
            owner: Owner,
            repo: Repo,
            workflow_id: element.id,
            ref: 'main',
            inputs: {},
            headers: header
        });
        if (response.status === 204) {
            console.log(`🚀 The ${element.name} workflow was activated successfully and is running!`);
        } else {
            console.log(`⚠️ The ${element.name} workflow failed to activate, please check the workflow configuration!`);
        }
    } catch (error) {
        console.log(`❌ The ${element.name} workflow error: ${error}`);
    }
}

async function checkSingleRepo(repo_url, branch) {
    if (!repo_url) {
        core.setFailed('❌ check mode requires repo_url input');
        return;
    }

    console.log(`🔍 Check mode: monitoring ${repo_url}${branch ? ' [' + branch + ']' : ''}`);

    //fetch latest commit id
    let result;
    try {
        result = await fetchLatestCommitId(repo_url, branch);
    } catch (error) {
        core.setFailed(`❌ Failed to fetch commit ID: ${error.message}`);
        return;
    }

    const updateKey = result.key;
    const repoPrefix = updateKey.split('@')[0];
    console.log(`🔑 Current key: ${updateKey}`);

    //get existing caches
    const caches = await octokit.request('GET /repos/{owner}/{repo}/actions/caches', {
        owner: Owner,
        repo: Repo,
        headers: header
    });

    //check if the key already exists in cache
    const existingCache = caches.data.actions_caches.find(e => e.key === updateKey);
    if (existingCache) {
        console.log(`☕ Source has not been updated, commit ID matches cache. Cancelling workflow.`);
        core.setOutput('updated', 'false');
        //cancel current workflow run
        const runId = process.env.GITHUB_RUN_ID;
        if (runId) {
            try {
                await octokit.request('POST /repos/{owner}/{repo}/actions/runs/{run_id}/cancel', {
                    owner: Owner,
                    repo: Repo,
                    run_id: runId,
                    headers: header
                });
                console.log(`🛑 Workflow run ${runId} cancelled.`);
            } catch (error) {
                core.warning(`⚠️ Failed to cancel workflow run: ${error}`);
            }
        }
        return;
    }

    console.log(`✅ Source is updated! New commit detected.`);
    core.setOutput('updated', 'true');

    //clear old caches for this repo
    const oldCaches = caches.data.actions_caches.filter(e => e.key.includes(repoPrefix));
    for (const e of oldCaches) {
        try {
            const response = await octokit.request('DELETE /repos/{owner}/{repo}/actions/caches/{cache_id}', {
                owner: Owner,
                repo: Repo,
                headers: header,
                cache_id: e.id
            });
            if (response.status === 204) {
                console.log(`🚀 Delete old cache: ${e.key} completed!`);
            }
        } catch (error) {
            console.log(`❌ Delete cache: ${e.key} failed: ${error}`);
        }
    }

    //save new cache key
    try {
        const dir = "repo_keys/";
        const cachePath = dir + updateKey;
        fs.mkdirSync(dir, { recursive: true });
        fs.writeFileSync(cachePath, Buffer.from(updateKey, 'utf-8'), 'binary');

        const cacheId = await cache.saveCache([cachePath], updateKey);
        if (cacheId <= 0) {
            core.warning(`⚠️ Warning: Cache not saved: ${cacheId} Cache key: ${updateKey}`);
        } else {
            console.log(`🦄 Cache saved: ${cacheId} Cache key: ${updateKey}`);
        }
    } catch (error) {
        core.warning(`⚠️ Failed to save cache: ${error}`);
    }
}

async function main() {
    const isDebugMode = process.argv.includes('--debug');
    if (isDebugMode) {
        try {
            require('dotenv').config();
            console.log('🔧 Debug mode active: Loading environment variables from .env file');
        } catch (error) {
            console.warn('⚠️ Failed to load .env file:', error.message);
        }
    }
    const token = core.getInput('token') || process.env.GITHUB_TOKEN;
    const repository = core.getInput('repository') || process.env.GITHUB_REPOSITORY;
    const workflow = core.getInput('workflow') || process.env.GITHUB_WORKFLOW;
    const workspace = core.getInput('workspace') || process.env.GITHUB_WORKSPACE;
    const mode = core.getInput('mode') || process.env.ACTION_MODE || 'trigger';
    const repo_url = core.getInput('repo_url') || process.env.REPO_URL || '';
    const branch = core.getInput('branch') || process.env.REPO_BRANCH || '';
    octokit = new Octokit({ auth: token });

    const splitRepository = repository.split('/');
    if (splitRepository.length !== 2 || !splitRepository[0] || !splitRepository[1]) {
        throw new Error(`❌ Invalid repository '${repository}'. Expected format {owner}/{repo}.`);
    }
    Owner = splitRepository[0];
    Repo = splitRepository[1];

    //check mode: single repo update check
    if (mode === 'check') {
        await checkSingleRepo(repo_url, branch);
        return;
    }

    //trigger mode: original behavior - get all workflows
    const workflowslist = await octokit.request('GET /repos/{owner}/{repo}/actions/workflows', {
        owner: Owner,
        repo: Repo,
        headers: header
    });

    for (const element of workflowslist.data.workflows) {
        if (element.name !== workflow) {
            //force_active parameter: 0:default, 1:force execute, 2:ignore
            workflowInfo.push({ id: element.id, name: element.name, repo_url: '', update_key: '', force_active: 0, status: 1 });
        }
    }
    console.log(`🎯workflow count: ${workflowInfo.length}`);

    //read workflow files
    const workflowDirectory = path.join(workspace, '.github/workflows');
    const files = await fs.promises.readdir(workflowDirectory);
    for (const file of files) {
        const filePath = path.join(workflowDirectory, file);
        try {
            const fileContents = fs.readFileSync(filePath, 'utf8');
            const wfInfo = yaml.load(fileContents);

            //exclude the original(ActionBot) repo workflow and trigger workflow
            const matched = workflowInfo.find(element => element.name === wfInfo.name);
            if (wfInfo.name !== workflow && matched) {
                const repo_url = wfInfo.env.repo_url || wfInfo.env.REPO_URL;
                const force_active = wfInfo.env.force_active || 0;
                console.log(`👀 repo_url: ${repo_url}, force_active: ${force_active}`);

                if (force_active === 1 || repo_url) {
                    matched.force_active = force_active;
                    if (repo_url) {
                        matched.repo_url = repo_url;
                    }
                    continue;
                } else {
                    //exclude workflow
                    const index = workflowInfo.indexOf(matched);
                    if (index !== -1) {
                        workflowInfo.splice(index, 1);
                    }
                }
            }
        } catch (e) {
            console.log(e);
        }
    }

    if (workflowInfo.length < 1) { core.setFailed('❌ Not Workflow'); return; }
    await getCommitIds();

    //get cache key
    const caches = await octokit.request('GET /repos/{owner}/{repo}/actions/caches', {
        owner: Owner,
        repo: Repo,
        headers: header
    });

    if (caches.data.actions_caches.length > 0) {
        const keys = caches.data.actions_caches;
        //check repo updated
        for (const element of workflowInfo) {
            //exclude exception repo workflow
            if (element.status === 0) {
                console.log(`⚠️ repo ：${element.repo_url} Source is invalid, Already skipped!`);
                continue;
            }
            //force active workflow
            if (element.force_active === 1) {
                await triggerWorkflow(element);
                continue;
            }

            //find cache key
            const cacheKey = keys.find(e => e.key === element.update_key);
            if (cacheKey) {
                console.log(`☕ repo ：${element.name} Source do not update!`);
            } else {
                console.log(`✅ repo ：${element.name} Source is updated!`);
                await triggerWorkflow(element);
            }
        }
    } else {
        //all Updates
        for (const element of workflowInfo) {
            await triggerWorkflow(element);
        }
        console.log('🦄 Not Found Cache! will trigger all workflows!');
    }

    //clear cache
    for (const element of updatedKey) {
        if (!element) continue;
        const invalidKeys = caches.data.actions_caches.filter(e => e.key.includes(element.split('@')[0]));
        for (const e of invalidKeys) {
            try {
                const response = await octokit.request('DELETE /repos/{owner}/{repo}/actions/caches/{cache_id}', {
                    owner: Owner,
                    repo: Repo,
                    headers: header,
                    cache_id: e.id
                });
                if (response.status === 204) {
                    console.log(`🚀 Delete Cache: ${e.key} completed!`);
                } else {
                    console.log(`⚠️ Exception when deleting Key: ${response} `);
                }
            } catch (error) {
                console.log(`❌ Delete Key: ${e.key} Failed！ workflow error: ${error}`);
            }
        }
    }

    //action write caches
    for (const key of updatedKey) {
        if (!key) continue;
        try {
            const dir = "repo_keys/";
            const cachePath = dir + key;
            fs.mkdirSync(dir, { recursive: true });
            fs.writeFileSync(cachePath, Buffer.from(key, 'utf-8'), 'binary');

            const cacheId = await cache.saveCache([cachePath], key);
            if (cacheId <= 0) {
                core.warning(`⚠️⚠️⚠️ Warning: Cache not saved: ${cacheId} Cache key: ${key}`);
            } else {
                console.log(`🦄 Cache saved: ${cacheId} Cache key: ${key}`);
            }
        } catch (error) {
            core.setFailed(error);
        }
    }

    //check error workflow
    const errorWorks = workflowInfo.filter(element => element.status === 0);
    if (errorWorks.length > 0) {
        errorWorks.forEach(element => {
            console.log(`⚠️ [ ${element.name} ] workflow is possible problems, please check the log!`);
        });
        core.setFailed('❌ Some workflows failed, please check!');
    }
}
main();