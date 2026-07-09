import json

from review_orchestrator.openhands import OpenHandsClient
from review_orchestrator.review_results import ReviewSkillInput


def test_start_payload_uses_openhands_workspace_path() -> None:
    client = OpenHandsClient(base_url="http://openhands:3000")
    payload = client._start_payload(
        ReviewSkillInput(
            provider="github",
            repo_full_name="tolatolatop/review-orchestrator",
            pr_number=12,
            base_sha="a" * 40,
            head_sha="b" * 40,
            workspace_path="/var/lib/review-orchestrator/workspaces/repo",
        )
    )

    message = payload["initial_message"]["content"][0]["text"]
    review_input = json.loads(message.split("\n\n", 1)[1])

    assert review_input["workspace_path"] == "/workspace/project/review-orchestrator"
