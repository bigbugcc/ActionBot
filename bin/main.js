// const { authtoken, crepos } = require('./auth');
const { Octokit } = require("octokit");
let octokit = null;
const core = require("@actions/core");
const cache = require('@actions/cache');
const request = require('request');
const fs = require('fs');
const yaml = require('js-yaml');
const path = require('path');
const { Console } = require("console");
const header = { 'X-GitHub-Api-Version': '2022-11-28' };
const workflowInfo = new Array();
const updatedWorkflows = new Array();
const updatedKey = new Set();
//current repo
let Owner = "";
let Repo = "";


function readDirAsync(path) {
    return new Promise((resolve, reject) => {
        fs.readdir(path, (err, files) => {
            if (err) {
                reject(err);
            } else {
                resolve(files);
            }
        });
    });
}

function getRepoUrlInfo(repo_url) {
    const splitRepository = repo_url.replace('.git', '').split('/');
    if (splitRepository.length < 4) {
        core.setFailed(`this repo: ${repo_url} Invalid repository.`);
    }
    return {
        owner: splitRepository[3],
        name: splitRepository[4]
    }
}

async function getCommitIds() {
    const promises = workflowInfo.map(async (element) => {
        if (element.repo_url) {
            const repo_info = getRepoUrlInfo(element.repo_url);
            const repo_owner = repo_info.owner;
            const repo_name = repo_info.name;

            try {
                if (element.repo_url.includes('github.com')) {
                    const response = await octokit.request('GET /repos/{owner}/{repo}/commits', {
                        owner: repo_owner,
                        repo: repo_name,
                        headers: header
                    });
                    const commitId = response.data[0].sha;
                    console.log(`🎯 Github RepoName:${element.name} Last_CommitID：${commitId}`);
                    const key = `${repo_owner}:${repo_name}@${commitId}`.replace(/\s/g, '');
                    element.update_key = key;
                } else if (element.repo_url.includes('gitee.com')) {
                    const options = {
                        url: `https://gitee.com/api/v5/repos/${repo_owner}/${repo_name}/commits`,
                        method: 'GET',
                        headers: {
                            'Content-Type': 'application/json'
                        },
                        json: true
                    };
                    const body = await new Promise((resolve, reject) => {
                        request(options, (error, response, body) => {
                            if (error) {
                                reject(error);
                            } else {
                                resolve(body);
                            }
                        });
                    });
                    const commitId = body[0].sha;
                    console.log(`🎯 Gitee RepoName:${element.name} Last_CommitID：${commitId}`);
                    const key = `${repo_owner}:${repo_name}@${commitId}`.replace(/\s/g, '');
                    element.update_key = key;
                } else {
                    //core.setFailed('❌ Invalid repository');
                    element.status = 0;
                    core.warning(`⚠️ ${element.repo_url} is Invalid repository url, please check the url`);
                }
            }
            catch (error) {
                element.status = 0;
                core.warning(`⚠️ ${element.repo_url} possible problems, log: ${error}`);
            }
        }
    });

    await Promise.all(promises);
}

function mkdirp(dir) {
    if (fs.existsSync(dir)) { return true }
    const dirname = path.dirname(dir)
    mkdirp(dirname);
    fs.mkdirSync(dir);
}

async function triggerWorkflow(element) {
    if(element.update_key){
        updatedKey.add(element.update_key);
    }

    await octokit.request('POST /repos/{owner}/{repo}/actions/workflows/{workflow_id}/dispatches', {
        owner: Owner,
        repo: Repo,
        workflow_id: element.id,
        ref: 'main',
        inputs: {},
        headers: header
    }).then((response) => {
        if (response.status == 204) {
            console.log(`🚀 The ${element.name} workflow was activated successfully and is running!`);
        } else {
            console.log(`⚠️ The ${element.name} workflow failed to activate, please check the workflow configuration!`);
        }
    }).catch((error) => {
        console.log(`❌ The ${element.name} workflow error: ${error}`);
    });
}

async function main() {
    let token = core.getInput('token');
    let repository = core.getInput('repository');
    let workflow = core.getInput('workflow');
    let workspace = core.getInput('workspace');
    //Local test
    // if (token == '' || repository == '') {
    //     token = authtoken;
    //     repository = crepos;
    // }
    octokit = new Octokit({ auth: token });

    //repo owner and repo name
    const splitRepository = repository.split('/');
    if (splitRepository.length !== 2 || !splitRepository[0] || !splitRepository[1]) {
        throw new Error(`❌ Invalid repository '${repository}'. Expected format {owner}/{repo}.`);
    }
    Owner = splitRepository[0];
    Repo = splitRepository[1];

    //get all workflows
    const workflowslist = await octokit.request('GET /repos/{owner}/{repo}/actions/workflows', {
        owner: Owner,
        repo: Repo,
        headers: header
    })

    workflowslist.data.workflows.forEach(element => {
        if (element.name != workflow) {
            //force_active parameter: 0:default, 1:force execute, 2:ignore
            workflowInfo.push({ id: element.id, name: element.name, repo_url: '', update_key: '', force_active: 0, status: 1 });
        }
    });
    console.log(`🎯workflow count: ${workflowInfo.length}`);

    //get all workflows
    const workflowDirectory = path.join(workspace, '.github/workflows');
    const files = await readDirAsync(workflowDirectory);
    console.log(`🎯workflow file count: ${files.length}`);
    for (const file of files) {
        const filePath = path.join(workflowDirectory, file);
        try {
            const fileContents = fs.readFileSync(filePath, 'utf8');
            const wfInfo = yaml.load(fileContents);

            //exclude the original(ActionBot) repo workflow and trigger workflow
            if (wfInfo.name != workflow && workflowInfo.find(element => element.name == wfInfo.name)) {
                //default value
                const repo_url = wfInfo.env.repo_url || wfInfo.env.REPO_URL;
                const force_active = wfInfo.env.force_active || 0;
                console.log(`👀 repo_url: ${repo_url}, force_active: ${force_active}`);

                if (force_active === 1 || repo_url) {
                    //force execute workflow, ignore repo commit id
                    workflowInfo.find(element => element.name == wfInfo.name).force_active = force_active;
                    if (repo_url) {
                        //repo commit id execute workflow
                        workflowInfo.find(element => element.name == wfInfo.name).repo_url = repo_url;
                    }
                    continue;
                } else {
                    //exclude workflow
                    const index = workflowInfo.findIndex(element => element.name == wfInfo.name);
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
    })

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
                triggerWorkflow(element);
                continue;
            }

            //find cache key
            if (cacheKey = keys.find(e => e.key == element.update_key)) {
                console.log(`👀 repo ：${element.name} Source do not update!`);
            } else {
                console.log(`👀 repo ：${element.name} Source is updated!`);
                //trigger workflows
                triggerWorkflow(element);
            }
        }
    } else {
        //All Updates
        workflowInfo.forEach(element => {
            triggerWorkflow(element);
        });
        console.log('🦄 Not Found Cache! will trigger all workflows!')
    }

    //Clear invalid cache
    updatedKey.forEach(element => {
        if(element){
            const invalidKeys = caches.data.actions_caches.filter(e => e.key.includes(element.split('@')[0]));
            if (invalidKeys.length > 0) {
                invalidKeys.forEach(async e => {
                    await octokit.request('DELETE /repos/{owner}/{repo}/actions/caches/{cache_id}', {
                        owner: Owner,
                        repo: Repo,
                        headers: header,
                        cache_id: e.id
                    }).then((response) => {
                        if (response.status == 204) {
                            console.log(`🚀 Delete Key: ${e.key} completed!`);
                        } else {
                            console.log(`⚠️ Exception when deleting Key: ${response} `);
                        }
                    }).catch((error) => {
                        console.log(`❌ Delete Key: ${e.key} Failed！ workflow error: ${error}`);
                    });
                });
            }
        }
    });
    
    //Action write caches
    for(const key in updatedKey){
        if (key) {
            try {
                //write cache
                const key = element.update_key;
                console.log(`🦄 Cache key: ${key}`);
                const path = `repo_keys/`;
                const cachePath = path + key;
                // Create cache folder
                await mkdirp(path);
                //create cache file
                await fs.writeFileSync(cachePath, Buffer.from(key, 'utf-8'), 'binary');

                const files = await readDirAsync(path);
                console.log(`🦄 Directory files : ${files}`);

                const paths = [`${cachePath}`];
                const cacheId = await cache.saveCache(paths, key);
                console.log(`🦄 Cache key saved: ${cacheId}`);
            } catch (error) {
                core.setFailed(error);
            }
        }
    }

    //check error workflow
    const errorWork = updatedWorkflows.find(element => element.status === 0);
    if (errorWork) {
        errorWork.forEach(element => {
            console.log(`⚠️ [ ${element.name} ] workflow is possible problems, please check the log!`);
        });
        core.setFailed('❌ Some workflows failed, please check!');
    }
}
main();