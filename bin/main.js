const { Octokit } = require("octokit");
const fs = require('fs');
const yaml = require('js-yaml');
const path = require('path');
const core = require("@actions/core");
const { simpleGit, CleanOptions } = require('simple-git');
const header = { 'X-GitHub-Api-Version': '2022-11-28' };
const workflowInfo = new Array();

function checkRepoUpdate() {
    const localPath = './repo';
    workflowInfo.forEach(element => {
        simpleGit.clone(element.repo_url, localPath)
        .then(repository => {
          console.log('Repository cloned:', repository.path());
        })
        .catch(error => {
          console.error('Repository cloning failed:', error);
        });
    })

}

async function main() {
    const token = core.getInput('token');
    const repository = core.getInput('repository');
    simpleGit().clean(simpleGit.CleanOptions.FORCE);

    const octokit = new Octokit({ auth: token });

    //repo owner and repo name
    const splitRepository = repository.split('/');
    if (splitRepository.length !== 2 || !splitRepository[0] || !splitRepository[1]) {
        throw new Error(`Invalid repository '${repository}'. Expected format {owner}/{repo}.`);
    }
    const repo_owner = splitRepository[0];
    const repo_name = splitRepository[1];

    //get all workflows
    const workflowslist = await octokit.request('GET /repos/' + repo_owner + '/' + repo_name + '/actions/workflows', {
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
    await fs.readdir(workflowDirectory, (err, files) => {
        if (err) {
            console.error('Error reading workflow directory:', err);
            return;
        }

        files.forEach((file) => {
            const filePath = path.join(workflowDirectory, file);
            try {
                const fileContents = fs.readFileSync(filePath, 'utf8');
                const data = yaml.load(fileContents);
                console.log(data.name);
                console.log(data.env.repo);
                if (data.name != "AutoTrigger") {
                    workflowInfo.forEach(element => {
                        if (element.name == data.name) {
                            element.repo_url = data.env.repo;
                        }
                    });
                }
            } catch (e) {
                console.log(e);
            }
        });
    });
    if (workflowInfo.length < 1) { console.log('Not Workflow'); return; }

    //checkRepoUpdate();


    // await octokit.request('POST /repos/' + repo_owner + '/' + repo_name + '/actions/workflows/{workflow_id}/dispatches', {
    //     owner: repo_owner,
    //     repo: repo_name,
    //     workflow_id: 'WORKFLOW_ID',
    //     ref: 'main',
    //     inputs: {},
    //     headers: header
    // })

}

main();