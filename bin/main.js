const { authtoken,crepos} = require('./auth');
const { Octokit } = require("octokit");
let octokit = null;
const core = require("@actions/core");
const request = require('request');
const fs = require('fs');
const yaml = require('js-yaml');
const path = require('path');
const header = { 'X-GitHub-Api-Version': '2022-11-28' };
const workflowInfo = new Array();

function checkRepoCommitId() {
    workflowInfo.forEach(element => {
        const splitRepository = element.repo_url.split('/');
        if (splitRepository.length < 3) {
            throw new Error(`Invalid repository '${element.repo_url}'. Expected format {owner}/{repo}.`);
        }
        const repo_owner = splitRepository[3];
        const repo_name = splitRepository[4].split('.')[0];
        if(element.repo_url.includes('github.com')){
            octokit.request('GET /repos/{owner}/{repo}/commits', {
                owner: repo_owner,
                repo: repo_name,
                headers: header
            }).then((response) => {
                const commitId = response.data[0].sha;
    
            }).catch((error) => {
                console.log(error);
            });

        }else if(element.repo_url.includes('gitee.com'))
        {
            const options = {
                url: `https://gitee.com/api/v5/repos/${repo_owner}/${repo_name}/commits`,
                method: 'GET',
                headers: {
                    'Content-Type': 'application/json'
                },
                json: true
            };
            request(options, (error, response, body) => {
                if (error) {
                    console.log(error);
                } else {
                    const commitId = body[0].sha;
                }
            });
        }else{
            core.setFailed('Invalid repository');
        }

        
    })
}

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

async function main() {
    let token = core.getInput('token');
    let repository = core.getInput('repository');
    //Local test
    if (token == '' || repository == '') {
        token = authtoken;
        repository = crepos;
    }
    octokit = new Octokit({ auth: token });

    //repo owner and repo name
    const splitRepository = repository.split('/');
    if (splitRepository.length !== 2 || !splitRepository[0] || !splitRepository[1]) {
        throw new Error(`Invalid repository '${repository}'. Expected format {owner}/{repo}.`);
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
        if (element.name != "AutoTrigger") {
            workflowInfo.push({ id: element.id, name: element.name, repo_url: '' });
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
            if (data.name != "AutoTrigger") {
                console.log(data.name);
                workflowInfo.find(element => element.name == data.name).repo_url = data.env.repo;
                continue;
            }
        } catch (e) {
            console.log(e);
        }
    }
    if (workflowInfo.length < 1) { console.log('Not Workflow'); return; }

    checkRepoCommitId();

    // octokit.request('POST /repos/{owner}/{repo}/actions/workflows/{workflow_id}/dispatches', {
    //     owner: repo_owner,
    //     repo: repo_name,
    //     workflow_id: element.id,
    //     ref: 'main',
    //     inputs: {},
    //     headers: header
    // }).then((response) => {
    //     console.log(response);
    // }).catch((error) => {
    //     console.log(error);
    // });
}

main();