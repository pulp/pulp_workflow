from pulpcore.plugin import PulpPluginAppConfig


class PulpWorkflowPluginAppConfig(PulpPluginAppConfig):
    """Entry point for the pulp_workflow plugin."""

    name = "pulp_workflow.app"
    label = "workflow"
    version = "0.2.0.dev"
    python_package_name = "pulp-workflow"
    domain_compatible = True

    def ready(self):
        super().ready()
        from pulp_workflow.app import signals  # noqa: F401
