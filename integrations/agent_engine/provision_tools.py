"""
HyveOS Provisioning AutoGen Tools — Agent tools for network provisioning.

5 tools registered with GoalManager under 'provision' tag:
  - provision_network_machine
  - scan_network_for_machines
  - check_provisioned_node
  - update_provisioned_node
  - list_provisioned_nodes
"""

import json
import logging
from typing import Annotated

logger = logging.getLogger('hevolve_provision_tools')


def provision_network_machine(
    target_host: Annotated[str, "IP address or hostname of the target machine"],
    ssh_user: Annotated[str, "SSH username (default: root)"] = "root",
    ssh_key_path: Annotated[str, "Path to SSH private key (optional)"] = "",
    join_peer: Annotated[str, "URL of existing hive node to join (optional)"] = "",
) -> str:
    """Install HyveOS on a remote network machine via SSH.

    This tool SSHs into the target machine, runs system checks,
    transfers the HyveOS install bundle, and executes the installer.
    The new node is automatically registered with the hive.

    Returns a JSON string with the provisioning result including
    the new node's ID and capability tier.
    """
    from integrations.agent_engine.network_provisioner import NetworkProvisioner

    result = NetworkProvisioner.provision_remote(
        target_host=target_host,
        ssh_user=ssh_user,
        ssh_key_path=ssh_key_path if ssh_key_path else None,
        join_peer=join_peer if join_peer else None,
    )
    return json.dumps(result, indent=2)


def scan_network_for_machines(
    subnet: Annotated[str, "CIDR subnet to scan (e.g., '192.168.1.0/24'). Auto-detect if empty."] = "",
) -> str:
    """Scan the local network for machines available for HyveOS provisioning.

    Discovers machines with open SSH port (22) on the specified subnet.
    If no subnet is provided, auto-detects the local network.

    Returns a JSON list of discovered machines with their IP, hostname,
    and SSH accessibility status.
    """
    from integrations.agent_engine.network_provisioner import NetworkProvisioner

    targets = NetworkProvisioner.discover_network_targets(
        subnet=subnet if subnet else None)

    return json.dumps({
        'count': len(targets),
        'targets': targets,
        'subnet': subnet or 'auto-detected',
    }, indent=2)


def check_provisioned_node(
    target_host: Annotated[str, "IP address or hostname of the provisioned node"],
    ssh_user: Annotated[str, "SSH username (default: root)"] = "root",
) -> str:
    """Check the health and status of a provisioned HyveOS node.

    SSHs into the node and checks:
    - Service status (backend, discovery, agent daemon, vision, LLM)
    - Backend HTTP health
    - Node identity
    - System uptime

    Returns a JSON health report.
    """
    from integrations.agent_engine.network_provisioner import NetworkProvisioner

    health = NetworkProvisioner.check_remote_health(
        target_host=target_host,
        ssh_user=ssh_user,
    )
    return json.dumps(health, indent=2)


def update_provisioned_node(
    target_host: Annotated[str, "IP address or hostname of the node to update"],
    ssh_user: Annotated[str, "SSH username (default: root)"] = "root",
) -> str:
    """Update HyveOS on a remote provisioned node to the latest version.

    SSHs into the node, pulls latest code (via git or re-transfer),
    restarts services, and verifies the backend comes back up.

    Returns a JSON result with success status.
    """
    from integrations.agent_engine.network_provisioner import NetworkProvisioner

    result = NetworkProvisioner.update_remote(
        target_host=target_host,
        ssh_user=ssh_user,
    )
    return json.dumps(result, indent=2)


def list_provisioned_nodes() -> str:
    """List all HyveOS nodes provisioned by this hive.

    Returns a JSON list of all provisioned nodes with their status,
    capability tier, IP address, and last health check time.
    """
    from integrations.agent_engine.network_provisioner import NetworkProvisioner

    nodes = NetworkProvisioner.list_provisioned()
    return json.dumps({
        'count': len(nodes),
        'nodes': nodes,
    }, indent=2)


# Tool metadata for GoalManager registration
PROVISION_TOOLS = [
    provision_network_machine,
    scan_network_for_machines,
    check_provisioned_node,
    update_provisioned_node,
    list_provisioned_nodes,
]
