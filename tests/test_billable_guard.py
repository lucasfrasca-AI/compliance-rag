"""The billable guard (`.claude/hooks/billable_guard.py`), driven as the harness
drives it: a PreToolUse payload on stdin, a decision (or silence) on stdout.
Each test gets its own project directory, so the real approvals file is never
read or written.

#322 added AWS, default-deny, and #326 kept it when Bedrock was withdrawn
(ADR-0057): SERVICES.md allows nothing on AWS, so every `aws` command is gated at a shell-segment
boundary, like gcloud, unless it is a read (`get-`, `list-`, `describe-`,
`batch-get-`, `simulate-`, `lookup-`, `help`, `wait`, which includes `sts
get-caller-identity`). The segmenter reads through quoting, continuations,
wrappers and `bash -c`; what it cannot see is stated in the guard's docstring.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
from datetime import date

import pytest

GUARD = pathlib.Path(__file__).resolve().parents[1] / ".claude" / "hooks" / "billable_guard.py"


def run_guard(command: str, project: pathlib.Path) -> dict[str, object] | None:
    """The hook's decision for `command`, or None when it stays silent."""
    result = subprocess.run(
        [sys.executable, str(GUARD)],
        input=json.dumps({"tool_input": {"command": command}}),
        capture_output=True,
        text=True,
        env={"CLAUDE_PROJECT_DIR": str(project), "PATH": "/usr/bin:/bin"},
        check=True,
    )
    if not result.stdout.strip():
        return None
    decision: dict[str, object] = json.loads(result.stdout)["hookSpecificOutput"]
    return decision


def denied(command: str, project: pathlib.Path) -> str:
    decision = run_guard(command, project)
    assert decision is not None, f"{command!r} was let through"
    assert decision["permissionDecision"] == "deny"
    return str(decision["permissionDecisionReason"])


@pytest.fixture
def project(tmp_path: pathlib.Path) -> pathlib.Path:
    return tmp_path


GATED_AWS = [
    "aws iam create-role --role-name compliance-rag-bedrock --assume-role-policy-document file://t.json",
    "aws iam put-role-policy --role-name r --policy-name p --policy-document file://p.json",
    "aws iam attach-role-policy --role-name r --policy-arn arn:aws:iam::aws:policy/X",
    "aws iam update-assume-role-policy --role-name r --policy-document file://t.json",
    "aws iam create-open-id-connect-provider --url https://accounts.google.com",
    "aws iam delete-role --role-name r",
    "aws bedrock put-use-case-for-model-access --form-data file://form.json",
    "aws bedrock create-inference-profile --inference-profile-name x",
    "aws bedrock create-model-invocation-job --job-name j",
    "aws bedrock-runtime invoke-model --model-id au.anthropic.claude-sonnet-5 --body x out.json",
    "aws bedrock-runtime converse --model-id au.anthropic.claude-sonnet-5",
    "aws bedrock-agent create-agent --agent-name a",
    "aws marketplace-entitlement start-something",
    "aws marketplace-catalog start-change-set --catalog AWSMarketplace",
    "aws aws-marketplace subscribe",
    "aws meteringmarketplace meter-usage",
    "aws service-quotas request-service-quota-increase --service-code bedrock "
    "--quota-code L-1 --desired-value 100000",
    "aws service-quotas put-service-quota-increase-request-into-template --service-code bedrock",
]

READ_ONLY_AWS = [
    "aws bedrock get-inference-profile --inference-profile-identifier "
    "au.anthropic.claude-sonnet-5 --region ap-southeast-2",
    "aws bedrock list-inference-profiles --region ap-southeast-2",
    "aws bedrock list-foundation-models --region ap-southeast-2 --output json",
    "aws bedrock get-foundation-model-availability --model-id anthropic.claude-sonnet-5",
    "aws iam get-role --role-name compliance-rag-bedrock",
    "aws iam list-attached-role-policies --role-name r",
    "aws iam describe-something --x y",
    "aws iam simulate-principal-policy --policy-source-arn a --action-names bedrock:InvokeModel",
    "aws service-quotas get-service-quota --service-code bedrock --quota-code L-1",
    "aws service-quotas list-service-quotas --service-code bedrock --region ap-southeast-2",
    "aws marketplace-entitlement get-entitlements",
    "aws iam help",
    "aws bedrock",
    "aws sts get-caller-identity --profile compliance-rag",
    "aws sts get-caller-identity",
    "aws --version",
    "aws help",
    "aws",
    "aws iam wait role-exists --role-name r",
    "aws ec2 describe-instances --region ap-southeast-2",
    "aws s3api list-buckets",
    "aws lambda get-function --function-name f",
    "aws iam --profile x get-role --role-name r",
    "aws --query 'Roles[?x y]' iam list-roles",
]


@pytest.mark.parametrize("command", GATED_AWS)
def test_an_aws_command_that_creates_or_changes_iam_bedrock_marketplace_or_quotas_is_denied(
    command: str, project: pathlib.Path
) -> None:
    reason = denied(command, project)
    assert "BILLABLE" in reason and "AWS" in reason and "ADR-0056" in reason
    # the exact approval line the human would paste is offered, whole
    assert f"{date.today().isoformat()} | {command}" in reason.replace("  ", " ")


@pytest.mark.parametrize("command", READ_ONLY_AWS)
def test_the_read_only_get_list_and_describe_commands_stay_open(
    command: str, project: pathlib.Path
) -> None:
    assert run_guard(command, project) is None


@pytest.mark.parametrize(
    "command",
    [
        "aws --profile compliance-rag iam create-role --role-name r",
        "aws --profile=compliance-rag --region ap-southeast-2 iam create-role --role-name r",
        "aws --no-cli-pager --region ap-southeast-2 bedrock put-use-case-for-model-access",
        "aws --debug --output json service-quotas request-service-quota-increase",
        "AWS_PROFILE=compliance-rag aws iam create-role --role-name r",
        "AWS_PROFILE=x AWS_REGION=ap-southeast-2 aws bedrock create-inference-profile",
        "/opt/homebrew/bin/aws iam create-role --role-name r",
        "/usr/local/bin/aws --profile x iam attach-role-policy --role-name r",
        "sudo aws iam create-role --role-name r",
        "env AWS_PROFILE=x aws iam create-role --role-name r",
        "sudo AWS_PROFILE=x aws iam create-role --role-name r",
        "AWS_PROFILE=x env aws iam create-role --role-name r",
        "AWS Iam create-role --role-name r",
        "aws    iam    create-role",
    ],
)
def test_global_options_a_path_or_an_env_prefix_do_not_hide_a_gated_command(
    command: str, project: pathlib.Path
) -> None:
    denied(command, project)


@pytest.mark.parametrize(
    "command",
    [
        "aws iam get-role --role-name r && aws iam create-role --role-name r",
        "aws sts get-caller-identity; aws iam attach-role-policy --role-name r",
        "aws iam list-roles | head && aws bedrock put-use-case-for-model-access",
        "aws bedrock get-inference-profile --inference-profile-identifier x\n"
        "aws iam create-role --role-name r",
        "(aws iam create-role --role-name r)",
        "true || aws service-quotas request-service-quota-increase",
    ],
)
def test_a_gated_command_after_a_shell_boundary_is_found(
    command: str, project: pathlib.Path
) -> None:
    denied(command, project)


@pytest.mark.parametrize(
    "command",
    [
        'echo "aws iam create-role --role-name r"',
        "git commit -m 'docs: run aws iam create-role by hand'",
        "grep -n 'aws bedrock put-use-case-for-model-access' docs/setup/aws-bedrock.md",
        "cat docs/setup/aws-bedrock.md",
        "uv run pytest tests/test_billable_guard.py -k aws-iam",
        "ls /tmp/aws-iam-create-role",
        "paws iam create-role",
    ],
)
def test_prose_that_merely_mentions_a_gated_command_does_not_trip_it(
    command: str, project: pathlib.Path
) -> None:
    """Boundary anchoring: a rule matches only at the start of a shell segment."""
    assert run_guard(command, project) is None


def test_an_approval_line_authorises_exactly_that_command_once(project: pathlib.Path) -> None:
    command = "aws iam create-role --role-name compliance-rag-bedrock"
    denied(command, project)  # no approval yet (and the file is created, empty of approvals)
    approvals = project / ".claude" / "billable-approvals.txt"
    approvals.write_text(approvals.read_text() + f"2026-11-10 | {command}\n")
    assert run_guard(command, project) is None  # the human's line passes it
    assert "# used" in approvals.read_text()
    denied(command, project)  # and only once


def test_an_approval_does_not_cover_a_longer_command_or_a_chain(project: pathlib.Path) -> None:
    command = "aws iam create-role --role-name r"
    denied(command, project)
    approvals = project / ".claude" / "billable-approvals.txt"
    approvals.write_text(approvals.read_text() + f"2026-11-10 | {command}\n")
    denied(f"{command} && aws iam attach-role-policy --role-name r", project)
    denied(f"{command} --description x", project)


def test_the_existing_gcloud_rules_still_work(project: pathlib.Path) -> None:
    assert "Cloud Run" in denied("gcloud run deploy reranker --image x", project)
    assert "IAM" in denied("gcloud projects add-iam-policy-binding p --member=m", project)
    assert "Deleting cloud resources" in denied("gcloud projects delete p", project)
    assert "Service-account keys" in denied(
        "gcloud iam service-accounts keys create k.json --iam-account a", project
    )
    # read-only gcloud stays open
    assert (
        run_guard("gcloud iam service-accounts describe a@p --format='value(uniqueId)'", project)
        is None
    )
    assert run_guard("gcloud run services list", project) is None


def test_env_assignments_still_come_off_before_the_match(project: pathlib.Path) -> None:
    denied("CLOUDSDK_CORE_PROJECT=p gcloud run deploy api --image x", project)
    denied("env CLOUDSDK_CORE_PROJECT=p gcloud run deploy api --image x", project)


DEFAULT_DENY_AWS = [
    "aws s3 cp x s3://bucket/x",
    "aws s3 ls",
    "aws s3 mb s3://bucket",
    "aws lambda invoke --function-name f out.json",
    "aws ec2 run-instances --image-id i",
    "aws secretsmanager create-secret --name n",
    "aws sts assume-role --role-arn r --role-session-name s",
    "aws cloudformation deploy --template-file t --stack-name s",
    "aws dynamodb put-item --table-name t",
    "aws ssm send-command --document-name d",
    "aws sso login --profile compliance-rag",
    "aws configure",
    "aws configure list",
    "aws configure set aws_session_token x",
    "aws configure sso --profile compliance-rag",
    "aws totally-new-service do-a-thing",
    "aws iam --profile x create-role --role-name r",
]


@pytest.mark.parametrize("command", DEFAULT_DENY_AWS)
def test_aws_is_default_deny_for_every_service_but_the_read_only_verbs(
    command: str, project: pathlib.Path
) -> None:
    """SERVICES.md allows nothing on AWS (Bedrock is not adopted, ADR-0057),
    so a service the guard has never heard of is gated too."""
    reason = denied(command, project)
    assert "BILLABLE" in reason and "AWS" in reason


@pytest.mark.parametrize(
    "command",
    [
        "aws --query 'x y' iam create-role --role-name r",
        'aws --query "a b" --profile "my profile" iam delete-role --role-name r',
        "aws --query 'Roles[?RoleName==`x`]' --region ap-southeast-2 iam attach-role-policy",
        "aws --profile='a b' bedrock put-use-case-for-model-access",
        "aws --output 'json' --endpoint-url 'https://x y' service-quotas "
        "request-service-quota-increase",
    ],
)
def test_quoted_option_values_with_spaces_do_not_hide_the_command(
    command: str, project: pathlib.Path
) -> None:
    denied(command, project)


@pytest.mark.parametrize(
    "command",
    [
        "aws iam \\\ncreate-role --role-name r",
        "aws \\\n  --region ap-southeast-2 \\\n  iam create-role \\\n  --role-name r",
        "aws bedrock put-use-case-for-model-access \\\n --form-data file://f.json",
        "true && \\\naws iam create-role --role-name r",
    ],
)
def test_backslash_line_continuations_are_joined_before_segmenting(
    command: str, project: pathlib.Path
) -> None:
    assert "\\\n" in command
    denied(command, project)


def test_a_gcloud_command_split_over_lines_is_found_too(project: pathlib.Path) -> None:
    assert "Cloud Run" in denied("gcloud run \\\n  deploy api --image x", project)


@pytest.mark.parametrize(
    "command",
    [
        "env -i aws iam create-role --role-name r",
        "env -i AWS_PROFILE=x aws iam create-role --role-name r",
        "env -u AWS_PROFILE aws iam create-role --role-name r",
        "env -u A -u B AWS_REGION=r aws iam create-role --role-name r",
        "sudo -E aws iam create-role --role-name r",
        "sudo -u root -E aws iam create-role --role-name r",
        "sudo -n -- aws iam create-role --role-name r",
        "nice aws iam create-role --role-name r",
        "nice -n 10 aws iam create-role --role-name r",
        "nice -5 aws iam create-role --role-name r",
        "time aws iam create-role --role-name r",
        "time -p aws iam create-role --role-name r",
        "nohup aws iam create-role --role-name r",
        "timeout 30 aws iam create-role --role-name r",
        "timeout -s KILL 30 aws iam create-role --role-name r",
        "echo r | xargs aws iam delete-role --role-name",
        "echo r | xargs -n1 aws iam delete-role --role-name",
        "echo r | xargs -I{} aws iam delete-role --role-name {}",
        "echo r | xargs -n 1 -I {} aws iam delete-role --role-name {}",
        "command aws iam create-role --role-name r",
        "exec aws iam create-role --role-name r",
        "sudo nice -n 5 env -i time aws iam create-role --role-name r",
        "env -i gcloud run deploy api --image x",
    ],
)
def test_wrappers_and_their_options_do_not_hide_a_gated_command(
    command: str, project: pathlib.Path
) -> None:
    denied(command, project)


@pytest.mark.parametrize(
    "command",
    [
        "bash -c 'aws iam create-role --role-name r'",
        'bash -c "aws iam create-role --role-name r"',
        "sh -c 'echo hi && aws iam create-role --role-name r'",
        "zsh -lc 'aws iam create-role --role-name r'",
        "bash -ec 'aws bedrock put-use-case-for-model-access'",
        "eval 'aws iam create-role --role-name r'",
        'eval "aws" iam create-role --role-name r',
        "bash -c 'bash -c \"aws iam create-role --role-name r\"'",
        "sudo bash -c 'aws iam create-role --role-name r'",
        "bash -c 'gcloud run deploy api --image x'",
        '"aws" iam create-role --role-name r',
        "'aws' iam create-role --role-name r",
        '"/opt/homebrew/bin/aws" iam create-role --role-name r',
        "echo $(aws iam create-role --role-name r)",
        'echo "$(aws iam create-role --role-name r)"',
    ],
)
def test_a_shell_payload_a_quoted_program_name_or_a_substitution_is_read_through(
    command: str, project: pathlib.Path
) -> None:
    denied(command, project)


@pytest.mark.parametrize(
    "command",
    [
        "bash -c 'aws iam get-role --role-name r'",
        "echo $(aws sts get-caller-identity)",
        'echo "$(aws iam list-roles)"',
        "timeout 30 aws iam list-roles",
        "sudo -E aws bedrock get-inference-profile --inference-profile-identifier x",
        "env -i AWS_PROFILE=x aws iam get-role --role-name r",
        "xargs -n1 aws iam get-role --role-name",
        "git commit -m 'run `aws iam create-role` by hand'",
    ],
)
def test_reads_and_prose_stay_open_through_the_same_wrappers(
    command: str, project: pathlib.Path
) -> None:
    assert run_guard(command, project) is None
