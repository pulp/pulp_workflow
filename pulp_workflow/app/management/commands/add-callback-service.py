from gettext import gettext as _
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.management import BaseCommand, CommandError
from django.db.utils import IntegrityError

from pulp_workflow.app.models import CallbackService


class Command(BaseCommand):
    """
    Django management command for adding a CallbackService.
    """

    help = "Adds a new CallbackService."

    def add_arguments(self, parser):
        parser.add_argument(
            "name",
            help=_("Name the CallbackService should get in the database."),
        )
        parser.add_argument(
            "script",
            help=_(
                "Path to an executable on the Pulp worker host. Relative paths are resolved "
                "against the current working directory; the resolved path must exist and be "
                "executable."
            ),
        )

    def handle(self, *args, **options):
        name = options["name"]
        script = options["script"]

        if not name or not name.strip():
            raise CommandError(_("`name` must be a non-blank string."))

        try:
            script_path = Path(script).resolve(strict=True)
        except OSError as e:
            raise CommandError(str(e)) from e

        try:
            CallbackService.objects.create(name=name, script=str(script_path))
        except ValidationError as e:
            raise CommandError("; ".join(e.messages)) from e
        except IntegrityError as e:
            raise CommandError(
                _("A callback service named {name!r} already exists in this domain.").format(
                    name=name
                )
            ) from e

        self.stdout.write(
            self.style.SUCCESS(
                _("Successfully added callback service {name} -> {script}.").format(
                    name=name, script=script_path
                )
            )
        )
