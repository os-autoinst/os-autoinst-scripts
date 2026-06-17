#!/usr/bin/python3
# Copyright SUSE LLC
"""Automate the rotation of GitLab Project Access Tokens and update CI/CD variables.

This script checks the expiration date of a specified GitLab Project Access Token.
If the token is scheduled to expire in 1 day or less, the script rotates the token,
extends its validity by 364 days (with Maintainer access level 40), and automatically
updates the corresponding CI/CD variable with the newly generated token value.

It is designed to be executed within a GitLab CI/CD pipeline to ensure automated
processes do not break due to expired credentials.

Environment Variables:
    CI_SERVER_PROTOCOL : Protocol for the GitLab instance (default: 'https').
    CI_SERVER_HOST     : Hostname of the GitLab instance (default: 'gitlab.suse.de').
    CI_PROJECT_ID      : The target GitLab project ID (default: '13758').
    CI_PUSH_TOKEN      : The default CI variable containing the current active token.

Command-line Arguments:
    -t, --token-name      : (Required) The name of the Project Access Token to monitor.
    -c, --ci-var-name    : (Optional) Overrides the default CI variable name (CI_PUSH_TOKEN)
"""
import argparse
import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone

import gitlab
from gitlab.v4.objects import Project

GITLAB_URL = os.getenv("CI_SERVER_PROTOCOL", "https") + "://" + os.getenv("CI_SERVER_HOST", "gitlab.suse.de")
PROJECT_ID = os.getenv("CI_PROJECT_ID", "13758")
CI_VAR_KEY = "CI_PUSH_TOKEN"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class CustomFormatter(argparse.ArgumentDefaultsHelpFormatter, argparse.RawDescriptionHelpFormatter):
    """Preserve multi-line __doc__ and provide default arguments in help strings."""


def update_ci_var(ci_var_key: str, new_access_token: str) -> None:
    """Authenticate with the NEW token to update the CI/CD variable."""
    gl = gitlab.Gitlab(GITLAB_URL, private_token=new_access_token, ssl_verify=False)
    glproject = gl.projects.get(PROJECT_ID)

    try:
        ci_var = glproject.variables.get(ci_var_key)
    except gitlab.exceptions.GitlabGetError:
        logger.info("Variable %s does not exist in this project", ci_var_key)

    ci_var.value = new_access_token
    ci_var.save()
    logger.info("%s updated with new Token!", ci_var_key)


def fetch_tokenid_by_name(gl_proj: Project, tok_name: str) -> int:
    """Find the ID of an active token by its name."""
    token_id = -1
    access_tokens = gl_proj.access_tokens.list(get_all=True)
    for token in access_tokens:
        if token.name == tok_name and token.active:
            token_id = token.id
            break
    return token_id


def main() -> None:
    """Run the main execution of the script."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=CustomFormatter)
    parser.add_argument("-t", "--token-name", type=str, required=True, default=None,
                        help="Project Access Token Name (default: None)")
    parser.add_argument("-c", "--ci-var-name", type=str, default=CI_VAR_KEY,
                        help="CI Variable Name holding Project Access Token Value")
    args = parser.parse_args()

    proj_access_tok_key = args.token_name

    ci_push_tok = os.getenv(CI_VAR_KEY, None)
    if ci_push_tok is None and args.ci_var_name is None:
        logger.info("Cannot find CI VARIABLE: %s, please provide CI Variable name using --ci_var_name", CI_VAR_KEY)
        sys.exit(1)
    elif args.ci_var_name is not None:
        ci_push_tok = os.getenv(args.ci_var_name)
        ci_var_name = args.ci_var_name

    logger.info("GITLAB_URL: %s", GITLAB_URL)
    logger.info("PROJECT_ID: %s", PROJECT_ID)
    logger.info("proj_access_tok_key: %s", proj_access_tok_key)
    logger.info("CI_VAR_KEY: %s", ci_var_name)

    # Authenticate with current token
    gl = gitlab.Gitlab(GITLAB_URL, private_token=ci_push_tok, ssl_verify=False)
    glproject = gl.projects.get(PROJECT_ID)

    tok_id = fetch_tokenid_by_name(glproject, proj_access_tok_key)
    if tok_id == -1:
        logger.info("Error: Could not find an active token named %s", args.token_name)
        sys.exit(1)

    access_token = glproject.access_tokens.get(tok_id)

    # Calculate days left for token expiry
    expires_at_date = date.fromisoformat(access_token.expires_at)
    days_left = (expires_at_date - datetime.now(timezone.utc).date()).days
    if days_left <= 1:
        logger.info("Rotating existing token with id: %s", access_token.id)
        expiry_date = datetime.now(timezone.utc).date() + timedelta(364)
        # access_levels:
        # 10 (Guest), 15 (Planner), 20 (Reporter), 25 (Security Manager)
        # 30 (Developer), 40 (Maintainer), and 50 (Owner)
        new_access_tok = access_token.rotate(self_rotate=True, access_level=40,
                                             expires_at=expiry_date.strftime("%Y-%m-%d"))
        logger.info("New Token ID: %s valid upto: %s", new_access_tok.get("id"), new_access_tok.get("expires_at"))
        update_ci_var(ci_var_name, new_access_tok.get("token"))
    else:
        logger.info("Token ID: %s", tok_id)
        logger.info("Days left: %s", days_left)
        logger.info("Expires At: %s", access_token.expires_at)


if __name__ == "__main__":
    main()
