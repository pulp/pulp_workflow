from gettext import gettext as _

from django.core.management import BaseCommand, CommandError
from django.db.models.deletion import ProtectedError

from pulpcore.plugin.util import get_domain_pk

from pulp_workflow.app.models import CallbackService


class Command(BaseCommand):
    """
    Django management command for removing a CallbackService.
    """

    help = "Removes a CallbackService by name."

    def add_arguments(self, parser):
        parser.add_argument(
            "name",
            help=_("Name of the CallbackService to remove."),
        )

    def handle(self, *args, **options):
        name = options["name"]

        if not name or not name.strip():
            raise CommandError(_("`name` must be a non-blank string."))

        try:
            obj = CallbackService.objects.get(pulp_domain_id=get_domain_pk(), name=name)
        except CallbackService.DoesNotExist:
            raise CommandError(
                _("No callback service named {name!r} exists in this domain.").format(name=name)
            )

        try:
            obj.delete()
        except ProtectedError as e:
            raise CommandError(
                _(
                    "Cannot remove callback service {name!r}: it is still referenced "
                    "by one or more workflows."
                ).format(name=name)
            ) from e

        self.stdout.write(
            self.style.SUCCESS(_("Successfully removed callback service {name}.").format(name=name))
        )
