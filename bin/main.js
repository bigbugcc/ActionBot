// const { authtoken, crepos } = require('./auth');
const { Octokit } = require("octokit");
let octokit = null;
const core = require("@actions/core");
const cache = require('@actions/cache');
const request = require('request');
const fs = require('fs');
const yaml = require('js-yaml');
const path = require('path');
const header = { 'X-GitHub-Api-Version': '2022-11-28' };
const workflowInfo = new Array();
const updatedWorkflows = new Array();

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

async function getCommitIds() {
    const promises = workflowInfo.map(async (element) => {
        if (element.repo_url) {
            const splitRepository = element.repo_url.replace('.git', '').split('/');
            if (splitRepository.length < 4) {
                core.setFailed('Invalid repository');
                throw new Error(`Invalid repository ${element.repo_url}.`);
            }
            const repo_owner = splitRepository[3];
            const repo_name = splitRepository[4];
    
            if (element.repo_url.includes('github.com')) {
                const response = await octokit.request('GET /repos/{owner}/{repo}/commits', {
                    owner: repo_owner,
                    repo: repo_name,
                    headers: header
                });
                const commitId = response.data[0].sha;
                console.log(`🎯 Github RepoName:${element.name} Last_CommitID：${commitId}`);
                element.commitId = commitId;
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
                element.commitId = commitId;
            } else {
                core.setFailed('❌ Invalid repository');
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

async function main() {
    let token = core.getInput('token');
    let repository = core.getInput('repository');
    let workflow = core.getInput('workflow');
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
    const repo_owner = splitRepository[0];
    const repo_name = splitRepository[1];

    //get all workflows
    const workflowslist = await octokit.request('GET /repos/{owner}/{repo}/actions/workflows', {
        owner: repo_owner,
        repo: repo_name,
        headers: header
    })

    workflowslist.data.workflows.forEach(element => {
        if (element.name != workflow) {
            workflowInfo.push({ id: element.id, name: element.name, repo_url: '', commitId: '', force_active: false});
        }
    });

    //get all workflows
    const workflowDirectory = path.join(__dirname, '../.github', 'workflows');
    const files = await readDirAsync(workflowDirectory);
    for (const file of files) {
        const filePath = path.join(workflowDirectory, file);
        try {
            const fileContents = fs.readFileSync(filePath, 'utf8');
            const data = yaml.load(fileContents);
            if (data.name != workflow) {
                if(data.env.force_active){
                    workflowInfo.find(element => element.name == data.name).force_active = true;
                    continue;
                }else if (data.env.repo) {
                    workflowInfo.find(element => element.name == data.name).repo_url = data.env.repo;
                    continue;
                } else {
                    const index = workflowInfo.findIndex(element => element.name == data.name);
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

    //获取cache key
    const caches = await octokit.request('GET /repos/{owner}/{repo}/actions/caches', {
        owner: repo_owner,
        repo: repo_name,
        headers: header
    })

    if (caches.data.actions_caches.length > 0) {
        const keys = caches.data.actions_caches;
        //check repo updated
        for (const element of workflowInfo) {
            //force active workflow
            if(element.force_active)
            {
                updatedWorkflows.push(element);
                continue;
            }
            //make repo cache key
            let key = `${element.id}-${element.name}-${element.commitId}`;
            key = key.replace(/\s/g, '');
            //find cache key
            if (cacheKey = keys.find(e => e.key == key)) {
                console.log(`👀 repo ：${element.name} Source do not update!`);
            } else {
                console.log(`👀 repo ：${element.name} Source is updated!`);
                //trigger workflow
                updatedWorkflows.push(element);
            }
        }
    } else {
        workflowInfo.forEach(element => {
            updatedWorkflows.push(element);
        });
        console.log('🦄 Not Found Cache! will trigger all workflows!')
    }

    //Clear invalid cache
    updatedWorkflows.forEach(element => {
        const invalidKeys = caches.data.actions_caches.filter(e => e.key.includes(element.id));
        if (invalidKeys.length > 0) {
            invalidKeys.forEach(async e => {
                await octokit.request('DELETE /repos/{owner}/{repo}/actions/caches/{cache_id}', {
                    owner: repo_owner,
                    repo: repo_name,
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
    });

    //trigger workflow
    for (const element of updatedWorkflows) {
        await octokit.request('POST /repos/{owner}/{repo}/actions/workflows/{workflow_id}/dispatches', {
            owner: repo_owner,
            repo: repo_name,
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

        if(element.commitId)
        {
            try {
                //write cache
                const key = `${element.id}-${element.name}-${element.commitId}`.replace(/\s/g, '');
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
}
main();