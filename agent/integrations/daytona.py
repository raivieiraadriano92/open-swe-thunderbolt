import os

from daytona import CreateSandboxFromSnapshotParams, Daytona, DaytonaConfig
from langchain_daytona import DaytonaSandbox

DEFAULT_DAYTONA_SANDBOX_SNAPSHOT = "daytonaio/sandbox:0.6.0"
DAYTONA_SANDBOX_SNAPSHOT_ENV = "DAYTONA_SANDBOX_SNAPSHOT"


def _get_daytona_sandbox_params() -> CreateSandboxFromSnapshotParams:
    snapshot = os.getenv(DAYTONA_SANDBOX_SNAPSHOT_ENV, DEFAULT_DAYTONA_SANDBOX_SNAPSHOT).strip()
    if not snapshot:
        raise ValueError(f"{DAYTONA_SANDBOX_SNAPSHOT_ENV} must not be empty")
    # THU-696: bound the sandbox lifecycle. Upstream Open SWE never calls
    # sandbox.delete() for Daytona (only the LangSmith proxy path does), and
    # Daytona's own defaults leave sandboxes alive for days. Auto-stop after
    # 15 min idle; ephemeral=True destroys on stop instead of the default
    # multi-day retention. Overridable via env for slow long-running tasks.
    auto_stop_min = int(os.getenv("DAYTONA_AUTO_STOP_MINUTES", "15"))
    return CreateSandboxFromSnapshotParams(
        snapshot=snapshot,
        auto_stop_interval=auto_stop_min,
        ephemeral=True,
    )


def create_daytona_sandbox(sandbox_id: str | None = None):
    api_key = os.getenv("DAYTONA_API_KEY")
    if not api_key:
        raise ValueError("DAYTONA_API_KEY environment variable is required")

    daytona = Daytona(config=DaytonaConfig(api_key=api_key))

    if sandbox_id:
        sandbox = daytona.get(sandbox_id)
    else:
        sandbox = daytona.create(params=_get_daytona_sandbox_params())

    return DaytonaSandbox(sandbox=sandbox)
