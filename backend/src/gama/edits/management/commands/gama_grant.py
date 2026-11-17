"""Grant (or revoke) a user the permission to apply and restore genre edits."""
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Allow USER to apply and restore genre edits (edits.apply_edits)."

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument("--revoke", action="store_true")

    def handle(self, *args, username, revoke=False, **opts):
        try:
            user = get_user_model().objects.get(username=username)
        except get_user_model().DoesNotExist:
            raise CommandError(f"no user {username!r}") from None
        perm = Permission.objects.get(content_type__app_label="edits", codename="apply_edits")
        if revoke:
            user.user_permissions.remove(perm)
            self.stdout.write(f"{username} may no longer apply edits")
        else:
            user.user_permissions.add(perm)
            self.stdout.write(f"{username} may now apply and restore genre edits")
