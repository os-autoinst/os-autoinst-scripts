import os.path
import importlib.machinery
from argparse import Namespace
from unittest.mock import MagicMock, call, patch

rootpath = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

loader = importlib.machinery.SourceFileLoader(
    "openqa", rootpath + "/amqp-listen-gitea.py"
)
spec = importlib.util.spec_from_loader(loader.name, loader)
openqa = importlib.util.module_from_spec(spec)
loader.exec_module(openqa)

def args_factory():
    args = Namespace()
    args.myself = 'qam-openqa'
    args.verbose = 1
    args.openqa_host = 'https://openqa.example'
    return args


def mocked_create_openqa_job(args, job_params):
    return { 'foo': 'bar' }


def mocked_openqa_schedule(args, params):
    return 'https://openqa.opensuse.org/tests/123456'


def mocked_openqa_cli(host, subcommand, cmds, dry_run=False):
    output = """
    {"count":6,"failed":[],"ids":[5335402,5335403,5335404,5335405,5335406,5335407],"scheduled_product_id":515537}
    6 jobs have been created:
    - https://openqa.opensuse.org/tests/5335402
    - https://openqa.opensuse.org/tests/5335403
    - https://openqa.opensuse.org/tests/5335404
    - https://openqa.opensuse.org/tests/5335405
    - https://openqa.opensuse.org/tests/5335406
    - https://openqa.opensuse.org/tests/5335407
    """
    return output


def mocked_gitea_post_status(job_params, job_url):
    pass

def mocked_fetch_url(url, request_type="text"):
    return b''


def mocked_request_post(url, headers, payload):
    pass


data = {
    'requested_reviewer': { 'username': 'qam-openqa' },
    'pull_request': {
        'id': '23',
        'html_url': 'https://src.opensuse.org/owner/reponame/pulls/1234',
        'head': {
            'sha': 'c0ffee',
            'ref': 'branch',
            'label': 'pr_user:branch',
            'repo': {
                'clone_url': 'https://src.opensuse.org/owner/reponame.git',
                'name': 'reponame',
            }
        }
    },
    'repository': {
        'url': 'https://src.opensuse.org/api/v1/repos/owner/reponame',
        'html_url': 'https://src.opensuse.org/owner/reponame',
    },
}

job_params = {
    'id': '23',
    'sha': 'c0ffee',
    'label': 'pr_user:branch',
    'clone_url': 'https://src.opensuse.org/owner/reponame.git',
    'branch': 'branch',
    'pr_html_url': 'https://src.opensuse.org/owner/reponame/pulls/1234',
    'repo_name': 'reponame',
    'repo_api_url': 'https://src.opensuse.org/api/v1/repos/owner/reponame',
    'repo_html_url': 'https://src.opensuse.org/owner/reponame'
}


class TestAMQP:


    def test_nothing_todo(mock_amqp):
        args = args_factory()
        data = {
            'requested_reviewer': { 'username': 'someone-else' }
        }
        openqa.handle_review_request(data, args)
        #openqa.create_openqa_job_params.assert_not_called()


    def test_schedule_job(mock_amqp):
        args = args_factory()
        openqa.openqa_cli = MagicMock(side_effect=mocked_openqa_cli)
        openqa.fetch_url = MagicMock(side_effect=mocked_fetch_url)
        job_url = openqa.openqa_schedule(args, {'webhook_id': 'gitea:pr:42', 'foo': 'bar'})
        cmd_args = [
            '--param-file',
            'SCENARIO_DEFINITIONS_YAML=/tmp/distri-openqa-scenario.yaml',
            'VERSION=Tumbleweed',
            'DISTRI=openqa',
            'FLAVOR=dev',
            'ARCH=x86_64',
            'HDD_1=opensuse-Tumbleweed-x86_64-20250920-minimalx@uefi.qcow2',
            'webhook_id=gitea:pr:42',
            'foo=bar',
        ]
        openqa.openqa_cli.assert_called_once_with(args.openqa_host, 'schedule', cmd_args, False)
        print(job_url)
        assert(job_url == 'https://openqa.opensuse.org/tests/5335402')


    def test_gitea_post_status(mock_amqp):
        openqa.request_post = MagicMock(side_effect=mocked_request_post)
        os.environ["GITEA_TOKEN"] = "abcdef"
        openqa.gitea_post_status(job_params, 'https://openqa.example')
        openqa.request_post.assert_called_once_with(
            'https://src.opensuse.org/api/v1/repos/owner/reponame/statuses/c0ffee',
            {
                'Accept': 'application/json',
                'Authorization': 'token abcdef',
                'User-Agent': 'amqp-listen-gitea.py (https://github.com/os-autoinst/scripts)',
            },
            {
                'context': 'qam-openqa',
                'description': 'openQA check',
                'state': 'pending',
                'target_url': 'https://openqa.example',
            }
        )

    def test_create_openqa_job_params(mock_amqp):
        args = args_factory()
        openqa.openqa_schedule = MagicMock(side_effect=mocked_openqa_schedule)
        openqa.gitea_post_status = MagicMock(side_effect=mocked_gitea_post_status)
        openqa.handle_review_request(data, args)
        openqa.openqa_schedule.assert_called_once_with(args, {
            'BUILD': 'reponame#c0ffee',
            'CASEDIR': 'https://src.opensuse.org/owner/reponame.git#c0ffee',
            '_GROUP_ID': '0',
            'PRIO': '100',
            'NEEDLES_DIR': '%%CASEDIR%%/needles',
            'SCENARIO_DEFINITIONS_YAML_FILE': 'https://src.opensuse.org/owner/reponame/raw/branch/c0ffee/scenario-definitions.yaml',
            'CI_TARGET_URL': 'https://openqa.example',
            'GITEA_REPO': 'reponame',
            'GITEA_SHA': 'c0ffee',
            'GITEA_STATUSES_URL': 'https://src.opensuse.org/api/v1/repos/owner/reponame/statuses/c0ffee',
            'GITEA_PR_URL': 'https://src.opensuse.org/owner/reponame/pulls/1234',
            'webhook_id': 'gitea:pr:23',
        })
        openqa.gitea_post_status.assert_called_once_with(job_params, 'https://openqa.opensuse.org/tests/123456')


    def test_handle_review_request(mock_amqp):
        args = args_factory()
        openqa.create_openqa_job_params = MagicMock(side_effect=mocked_create_openqa_job)
        openqa.handle_review_request(data, args)
        openqa.create_openqa_job_params.assert_called_once_with(args, job_params)


