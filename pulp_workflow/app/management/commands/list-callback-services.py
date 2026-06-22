from django.core.management import BaseCommand

from pulpcore.plugin.util import get_domain_pk

from pulp_workflow.app.models import CallbackService


class Command(BaseCommand):
    """
    Django management command for listing callback services.
    """

    help = "List all CallbackServices."

    def add_arguments(self, parser):
        pass

    def handle(self, *args, **options):
        results = list(
            CallbackService.objects.filter(pulp_domain_id=get_domain_pk())
            .order_by("name")
            .values_list("name", flat=True)
        )
        output = "\n".join(results)
        if output:
            self.stdout.write(output)
