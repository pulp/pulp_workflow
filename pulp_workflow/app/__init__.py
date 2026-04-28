from pulpcore.plugin import PulpPluginAppConfig


class PulpWorkflowPluginAppConfig(PulpPluginAppConfig):
    """Entry point for the pulp_workflow plugin."""

    name = "pulp_workflow.app"
    label = "workflow"
    version = "0.1.0.dev"
    python_package_name = "pulp-workflow"
    domain_compatible = True
